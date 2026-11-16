"""Benchmark group `disk`: what writing one downloaded file costs, per file.

5,000 files of 100 KiB each, best of 3, each run in a fresh scratch folder:
- `plain_write`: `open` + `write`, the floor;
- `temp_replace`: a temp file plus `os.replace`, the least an atomic write costs;
- `part_write`: what a download does: open and lock `<name>.part`, write it, set its modified
  time on the descriptor, `fstat` it for the history row, and rename it into place;
- `fs_write`: `flnd._files.write`, a small file of flnd's own, through the same part.
The gap between the floor and `part_write` is the disk bookkeeping per file.
"""

from __future__ import annotations

import os
import shutil
import tempfile
from collections.abc import Callable
from pathlib import Path

import kit

from flnd import _files

COUNT = kit.size(5000, 50)
DATA = os.urandom(100 * 1024)
# A post's date, as the downloader sets it: noon UTC.
MTIME = 1_767_355_200.0


def plain(folder: Path) -> None:
    """Write every file with `open` and `write`: the floor."""
    for n in range(COUNT):
        with open(folder / str(n), "wb") as handle:
            handle.write(DATA)


def temp_and_replace(folder: Path) -> None:
    """Write a hidden temp file, then rename it over the name: the least an atomic write costs."""
    for n in range(COUNT):
        temp = folder / f".{n}.tmp"
        with open(temp, "wb") as handle:
            handle.write(DATA)
        os.replace(temp, folder / str(n))


def part_write(folder: Path) -> None:
    """Write every file as a download does: a locked part, its time, one `fstat`, a rename."""
    for n in range(COUNT):
        with _files.part(folder / str(n), resume=True) as held:
            held.write(DATA)
            held.mtime = MTIME


def fs_write(folder: Path) -> None:
    """Write every file with `flnd._files.write`."""
    for n in range(COUNT):
        _files.write(folder / str(n), DATA)


def per_file(write: Callable[[Path], None]) -> float:
    """The best of 3 runs of `write` into a fresh folder, in microseconds per file.

    Args:
        write: one of the functions above, given the folder to write into.

    Returns:
        Microseconds per file.
    """
    with tempfile.TemporaryDirectory(prefix="flnd-bench-") as scratch:
        folder = Path(scratch, "out")

        def fresh() -> None:
            shutil.rmtree(folder, ignore_errors=True)
            folder.mkdir()

        return kit.best(lambda: write(folder), before=fresh) / COUNT * 1e6


def main() -> None:
    """Time each way of writing and emit the report."""
    report = kit.Report("disk")
    for name, write in (("plain_write", plain), ("temp_replace", temp_and_replace),
                        ("part_write", part_write), ("fs_write", fs_write)):
        report.add(name, per_file(write), "us")
    report.emit()


if __name__ == "__main__":
    main()
