"""Benchmark group `media`: encoding speed of images (Pillow) and video (ffmpeg).

The inputs are synthetic, so the output sizes say nothing about real media: compare speeds
between runs, never sizes. Real samples are measured by hand, outside this suite.

1. Pillow, always: one 12 MP image (a gradient and noise) encoded in memory to AVIF at speed 6
   and 8, to WebP, and to JPEG, each the best of 3.
2. ffmpeg, when it is on PATH: a 10 s 1080p30 test clip (`testsrc2`) encoded with x264 medium
   and veryfast, and with SVT-AV1 and VideoToolbox when this ffmpeg has them, each once; and
   the cost of starting one `ffprobe`. Without ffmpeg this part is skipped.
3. The compress step, end to end through `flnd._steps.Chain` (plan, encode, verify with SSIM,
   place, record), once each:
   - `jpegs_1000`: 1,000 synthetic 1080×1350 JPEGs at quality 90 (an Instagram photo's size),
     made from 20 distinct pictures before the clock starts;
   - `sync_compress_100k`: what `flnd sync --compress` costs over 100,000 history rows whose
     files are all done: open the store, read every row once, queue nothing.

The real-sample check (savings and SSIM on downloads from the test accounts) is a hand check:
`make media-real ARGS=<folder>` (`media_real.py`).
"""

from __future__ import annotations

import io
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import kit

# (metric, format, options) for Pillow's `Image.save`.
IMAGES = (
    ("avif_speed_6", "AVIF", {"quality": 60, "speed": 6}),
    ("avif_speed_8", "AVIF", {"quality": 60, "speed": 8}),
    ("webp", "WEBP", {"quality": 80, "method": 4}),
    ("jpeg", "JPEG", {"quality": 80, "optimize": True}),
)
# (metric, encoder, its options): the encoder must be listed by `ffmpeg -encoders` to run.
VIDEOS = (
    ("x264_medium", "libx264", ["-preset", "medium", "-crf", "23"]),
    ("x264_veryfast", "libx264", ["-preset", "veryfast", "-crf", "23"]),
    ("svt_av1", "libsvtav1", ["-preset", "8", "-crf", "35"]),
    ("videotoolbox_h264", "h264_videotoolbox", ["-q:v", "65"]),
)


def images(report: kit.Report) -> None:
    """Time step 1: Pillow's encoders on one 12 MP image."""
    try:
        from PIL import Image
    except ImportError:
        report.skip("Pillow is not installed; the image part did not run")
        return

    gradient = Image.radial_gradient("L").resize((4000, 3000))
    noise = Image.effect_noise((4000, 3000), 40)
    image = Image.merge("RGB", (gradient, noise, Image.blend(gradient, noise, 0.5)))
    for name, kind, options in IMAGES:
        took = kit.best(lambda kind=kind, options=options: image.save(io.BytesIO(), kind,
                                                                      **options))
        report.add(f"image_{name}", took, "s")


def videos(report: kit.Report) -> None:
    """Time step 2: ffmpeg's encoders on a 10 s test clip, and one ffprobe start."""
    ffmpeg, ffprobe = shutil.which("ffmpeg"), shutil.which("ffprobe")
    if ffmpeg is None or ffprobe is None:
        report.skip("ffmpeg is not on PATH; the video part did not run")
        return
    listed = subprocess.run([ffmpeg, "-hide_banner", "-encoders"], capture_output=True,
                            text=True, check=True).stdout
    with tempfile.TemporaryDirectory(prefix="flnd-bench-") as scratch:
        clip = Path(scratch, "clip.mp4")
        # stderr is captured, not shown: SVT-AV1 prints its whole configuration there even
        # under `-v error`. A failed encode still raises, with that stderr attached.
        subprocess.run([ffmpeg, "-v", "error", "-f", "lavfi", "-i",
                        "testsrc2=size=1920x1080:rate=30:duration=10", "-c:v", "libx264",
                        "-preset", "ultrafast", "-qp", "0", str(clip)],
                       check=True, stderr=subprocess.PIPE)
        for name, encoder, options in VIDEOS:
            if f" {encoder} " not in listed:
                print(f"  media: {encoder} is not in this ffmpeg; {name} skipped",
                      file=sys.stderr)
                continue
            start = time.perf_counter()
            subprocess.run([ffmpeg, "-v", "error", "-y", "-i", str(clip), "-c:v", encoder,
                            *options, "-an", str(Path(scratch, f"{name}.mp4"))],
                           check=True, stderr=subprocess.PIPE)
            report.add(f"video_{name}", time.perf_counter() - start, "s")
        report.add("ffprobe_start", kit.median_wall([ffprobe, "-v", "error", str(clip)]) * 1e3,
                   "ms")


def jpegs(report: kit.Report) -> None:
    """Time step 3's first number: the compress step over 1,000 JPEGs on the photo lane."""
    import random
    import shutil as files

    from PIL import Image, ImageDraw, ImageFilter

    from flnd import _steps

    def photo(seed: int) -> Image.Image:
        width, height = 1080, 1350
        image = Image.linear_gradient("L").resize((width, height)).convert("RGB")
        draw = ImageDraw.Draw(image)
        rng = random.Random(seed)
        for _ in range(60):
            x, y = rng.randrange(width), rng.randrange(height)
            draw.ellipse((x, y, x + rng.randrange(20, 300), y + rng.randrange(20, 300)),
                         fill=(rng.randrange(256), rng.randrange(256), rng.randrange(256)))
        noise = Image.effect_noise((width, height), 18).convert("RGB")
        return Image.blend(image, noise, 0.15).filter(ImageFilter.GaussianBlur(1))

    with tempfile.TemporaryDirectory(prefix="flnd-bench-") as scratch:
        folder = Path(scratch)
        for seed in range(20):
            photo(seed).save(folder / f"src{seed}.jpg", quality=90)
        history = {}
        for number in range(1000):
            path = folder / f"{number:04}.jpg"
            files.copyfile(folder / f"src{number % 20}.jpg", path)
            history[f"x:{number}"] = {"path": str(path), "size": path.stat().st_size}
        for seed in range(20):
            (folder / f"src{seed}.jpg").unlink()
        start = time.perf_counter()
        with _steps.Chain(history) as chain:
            for key, row in history.items():
                chain.landed(_steps.Landed(key, Path(row["path"]), compress=True))
        report.add("jpegs_1000", time.perf_counter() - start, "s")
        done = [row for row in history.values() if row.get("shrink") == "compressed"]
        before = sum(row["before"] for row in done)
        print(f"  media: {len(done)} of 1000 compressed, "
              f"{1 - sum(row['size'] for row in done) / max(before, 1):.0%} smaller",
              file=sys.stderr)


def sync_compress(report: kit.Report) -> None:
    """Time step 3's second number: `sync --compress` over 100,000 done files."""
    from flnd import _db, _shrink, _steps

    tools = _shrink.tools(_shrink.Kind.JPEG)
    with tempfile.TemporaryDirectory(prefix="flnd-bench-") as scratch:
        folder = Path(scratch, "media", "x", "example_user")
        store = Path(scratch, "history.db")
        with _db.open(store) as db, db.batch():
            table = db["downloads"]
            for number in range(100_000):
                done = ({"shrink": "compressed", "before": 200_000} if number % 4
                        else {"shrink": "kept", "tools": tools})
                table[f"x:{number}"] = {"path": f"{folder}/{number}.jpg", "size": 120_000,
                                        "mtime": 0, **done}
        start = time.perf_counter()
        with _db.open(store) as db:
            history = db["downloads"]
            with _steps.Chain(history) as chain:
                queued = chain.backlog(history.items(), [folder])
        report.add("sync_compress_100k", time.perf_counter() - start, "s")
        assert queued == 0, f"{queued} done files were queued"


def main() -> None:
    """Run every part and emit the report."""
    report = kit.Report("media")
    images(report)
    jpegs(report)
    sync_compress(report)
    videos(report)
    report.emit()


if __name__ == "__main__":
    main()
