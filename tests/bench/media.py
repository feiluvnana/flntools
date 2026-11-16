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
   - `sync_compress_100k`: what `flnd sync --compress` costs over 100,000 rows of `flnd.db`
     whose files are all done: open the home, read the unfinished rows once, queue nothing.

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

# The 12 MP image of step 1, the test clip of step 2 (seconds, size), and the files of step 3.
IMAGE = (kit.size(4000, 400), kit.size(3000, 300))
CLIP = (kit.size(10, 1), "1920x1080" if not kit.SMOKE else "320x240")
JPEGS, DONE = kit.size(1000, 10), kit.size(100_000, 1000)
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

    gradient = Image.radial_gradient("L").resize(IMAGE)
    noise = Image.effect_noise(IMAGE, 40)
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
                        f"testsrc2=size={CLIP[1]}:rate=30:duration={CLIP[0]}", "-c:v", "libx264",
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
    """Time step 3's first number: the compress step over 1,000 JPEGs on the photo lane.

    The files have rows in a real home's `flnd.db`, as a download leaves them, and the chain
    writes each result to its row; the run fails unless every file was compressed, so the
    number always times 1,000 encodes, never 1,000 skips.
    """
    import random
    import shutil as files

    from PIL import Image, ImageDraw, ImageFilter

    from flnd import _home, _steps

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
        home = Path(scratch, ".flnd")
        folder = Path(scratch, "media", "x", "example_user")
        folder.mkdir(parents=True)
        pictures = min(20, JPEGS)
        for seed in range(pictures):
            photo(seed).save(folder / f"src{seed}.jpg", quality=90)
        rows = []
        for number in range(JPEGS):
            path = folder / f"{number:04}.jpg"
            files.copyfile(folder / f"src{number % pictures}.jpg", path)
            rows.append(_home.FileRow(f"x:{number}", "x:example_user", str(path),
                                      path.stat().st_size, post_id=str(number), kind="image"))
        for seed in range(pictures):
            (folder / f"src{seed}.jpg").unlink()
        with _home.open(home) as db:
            db.save(rows)
            start = time.perf_counter()
            with _steps.Chain(db) as chain:
                for row in rows:
                    chain.landed(_steps.Landed(row.key, Path(row.path), compress=True))
            report.add("jpegs_1000", time.perf_counter() - start, "s")
            done = [row for row in db.records() if row.shrink == "compressed"]
        before = sum(row.before or 0 for row in done)
        print(f"  media: {len(done)} of {JPEGS} compressed, "
              f"{1 - sum(row.size for row in done) / max(before, 1):.0%} smaller",
              file=sys.stderr)
        if len(done) != len(rows):
            raise SystemExit(f"media: only {len(done)} of {len(rows)} JPEGs were compressed")


def sync_compress(report: kit.Report) -> None:
    """Time step 3's second number: `sync --compress` over 100,000 done files.

    It does what the engine's `sync --compress` does once the targets are known: open the
    home, read the targets' unfinished rows (`Home.unfinished`) and hand them to the chain's
    `backlog`. Three of four rows are compressed, which SQL leaves out; the rest were kept
    with today's tools, which `backlog` passes by without a `stat`.
    """
    from flnd import _home, _shrink, _steps

    tools = _shrink.tools(_shrink.Kind.JPEG)
    with tempfile.TemporaryDirectory(prefix="flnd-bench-") as scratch:
        home = Path(scratch, ".flnd")
        folder = Path(scratch, "media", "x", "example_user")
        rows = [_home.FileRow(f"x:{number}", "x:example_user", f"{folder}/{number}.jpg", 120_000,
                              mtime=0, post_id=str(number), kind="image")
                for number in range(DONE)]
        with _home.open(home) as db:
            for start in range(0, len(rows), 1000):
                db.save(rows[start:start + 1000])
            # The steps' results, as `Home.update` writes them, in one transaction instead of
            # 100,000: only the time of the sync is measured.
            with db._store.transaction() as sql:
                sql.executemany("UPDATE files SET shrink = ?, tools = ?, before = ? WHERE key = ?",
                                [("compressed", None, 200_000, row.key) if number % 4
                                 else ("kept", tools, None, row.key)
                                 for number, row in enumerate(rows)])
        start = time.perf_counter()
        with _home.open(home) as db, _steps.Chain(db) as chain:
            queued = chain.backlog(db.unfinished(["x:example_user"]))
        report.add("sync_compress_100k", time.perf_counter() - start, "s")
        if queued:
            raise SystemExit(f"media: {queued} done files were queued")


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
