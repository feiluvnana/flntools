"""The hand check behind the compress step's defaults: savings and SSIM on real downloads.

    make media-real ARGS="~/flnd-samples"                 # AV1 for videos (the default)
    make media-real ARGS="~/flnd-samples --video hevc"

Synthetic photos and `testsrc2` can't say what re-encoding what these sites already compressed
buys, so the defaults are fixed on real files: about 100 photos and 20 videos downloaded from
the authorized test accounts, kept in a folder outside the repo. This script is never part of
`make check` or `make bench`, and it never changes the samples.

How it goes:
1. Every file under the folder whose kind the compress step knows is copied into a scratch
   folder; the originals are only read.
2. Each copy goes through `flnd._shrink.shrink`, exactly as `--compress` would run it.
3. The result is compared with the original: a photo by `flnd._shrink.ssim` on its luma plane,
   a video by ffmpeg's `ssim` filter (the result scaled to the original's size).
4. One table is printed per kind: files, compressed / kept / failed, the median and total
   saving, the lowest and median SSIM, and the time per file. A kind whose median saving is
   under 15% is marked: the compress step leaves it alone by default.

It prints no file names, only counts per kind, so its output can be pasted into
`docs/performance.md` as it is.
"""

from __future__ import annotations

import argparse
import shutil
import statistics
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from flnd import _shrink


def video_ssim(original: Path, made: Path) -> float | None:
    """ffmpeg's mean SSIM ("All") of `made` against `original`, or None when ffmpeg fails."""
    try:
        said = subprocess.run(
            ["ffmpeg", "-hide_banner", "-nostdin", "-i", str(made), "-i", str(original),
             "-lavfi", "[0:v][1:v]scale2ref[a][b];[a][b]ssim", "-f", "null", "-"],
            capture_output=True, text=True, check=True).stderr
        return float(said.rsplit("All:", 1)[1].split()[0])
    except (subprocess.CalledProcessError, IndexError, ValueError, FileNotFoundError):
        return None


def photo_ssim(original: Path, made: Path) -> float:
    """The compress step's own SSIM of `made` against `original`."""
    from PIL import Image, ImageOps

    with Image.open(original) as a, Image.open(made) as b:
        return _shrink.ssim(ImageOps.exif_transpose(a).convert("L"),
                            ImageOps.exif_transpose(b).convert("L"))


def measure(folder: Path, video: str) -> dict[_shrink.Kind, list[dict]]:
    """Run stages 1 to 3 of the module docstring over every sample.

    Args:
        folder: the samples, searched recursively.
        video: "av1" or "hevc".

    Returns:
        Each kind's results, one dict per file.
    """
    results: dict[_shrink.Kind, list[dict]] = {}
    samples = sorted(p for p in folder.rglob("*") if p.is_file() and _shrink.kind(p))
    print(f"{len(samples)} samples", file=sys.stderr)
    with tempfile.TemporaryDirectory(prefix="flnd-media-real-") as scratch:
        for number, sample in enumerate(samples, 1):
            of = _shrink.kind(sample)
            assert of is not None
            work = Path(scratch, f"{number}{sample.suffix.lower()}")
            keep = Path(scratch, f"original-{number}{sample.suffix.lower()}")
            shutil.copyfile(sample, work)
            shutil.copyfile(sample, keep)
            start = time.perf_counter()
            outcome = _shrink.shrink(work, record=lambda outcome: None, video=video)
            took = time.perf_counter() - start
            score = None
            if outcome.state is _shrink.State.COMPRESSED:
                score = (photo_ssim(keep, outcome.path) if of in _shrink.PHOTOS
                         else video_ssim(keep, outcome.path))
            results.setdefault(of, []).append({
                "state": outcome.state, "saving": outcome.saving, "before": outcome.before,
                "after": outcome.after, "ssim": score, "seconds": took,
            })
            outcome.path.unlink(missing_ok=True)
            keep.unlink()
            print(f"  {number}/{len(samples)} {of.value}: {outcome.state.value}",
                  file=sys.stderr)
    return results


def table(results: dict[_shrink.Kind, list[dict]], video: str) -> str:
    """The Markdown table of stage 4."""
    lines = [f"Video codec: {video}. Tools: "
             + "; ".join(sorted({_shrink.tools(of, video=video) for of in results})), "",
             "| kind | files | compressed / kept / failed | median saving | total saving "
             "| lowest SSIM | median SSIM | s per file |",
             "| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |"]
    for of, rows in sorted(results.items(), key=lambda item: item[0].value):
        states = [sum(row["state"] is state for row in rows) for state in _shrink.State]
        savings = [row["saving"] for row in rows]
        before = sum(row["before"] for row in rows)
        after = sum(row["after"] for row in rows)
        scores = [row["ssim"] for row in rows if row["ssim"] is not None]
        median = statistics.median(savings)
        low = f"{min(scores):.4f}" if scores else "-"
        mid = f"{statistics.median(scores):.4f}" if scores else "-"
        mark = " (left alone: under 15%)" if median < _shrink.WORTH else ""
        lines.append(
            f"| {of.value}{mark} | {len(rows)} | {' / '.join(map(str, states))} "
            f"| {median:.0%} | {1 - after / before if before else 0:.0%} | {low} | {mid} "
            f"| {statistics.median(row['seconds'] for row in rows):.2f} |")
    return "\n".join(lines)


def main() -> None:
    """Read the command line, measure the samples and print the table."""
    parser = argparse.ArgumentParser(prog="make media-real",
                                     description="Savings and SSIM of --compress on real files.")
    parser.add_argument("folder", type=Path, help="the folder of samples (read only)")
    parser.add_argument("--video", choices=("av1", "hevc"), default="av1")
    args = parser.parse_args()
    folder = args.folder.expanduser()
    if not folder.is_dir():
        parser.error(f"no folder at {folder}")
    print(table(measure(folder, args.video), args.video))


if __name__ == "__main__":
    main()
