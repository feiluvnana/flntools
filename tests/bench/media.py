"""Benchmark group `media`: encoding speed of images (Pillow) and video (ffmpeg).

The inputs are synthetic, so the output sizes say nothing about real media: compare speeds
between runs, never sizes. Real samples are measured by hand, outside this suite.

1. Pillow, always: one 12 MP image (a gradient and noise) encoded in memory to AVIF at speed 6
   and 8, to WebP, and to JPEG, each the best of 3.
2. ffmpeg, when it is on PATH: a 10 s 1080p30 test clip (`testsrc2`) encoded with x264 medium
   and veryfast, and with SVT-AV1 and VideoToolbox when this ffmpeg has them, each once; and
   the cost of starting one `ffprobe`. Without ffmpeg this part is skipped.
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


def main() -> None:
    """Run both parts and emit the report."""
    report = kit.Report("media")
    images(report)
    videos(report)
    report.emit()


if __name__ == "__main__":
    main()
