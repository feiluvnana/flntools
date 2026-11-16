"""Benchmark group `disk`: what writing one downloaded file costs, per file.

5,000 files of 100 KiB each, best of 3, each run in a fresh scratch folder. The plain
`open` + `write` is the floor; a temp file plus `os.replace` is what an atomic write needs at
least; `fs.write` is what flnd does today (a stage file, a lock file, then a replace). The gap
between the floor and `fs.write` is the bookkeeping the rewrite of the disk layer is gated on.
"""

from __future__ import annotations

import os
import shutil
import tempfile
from collections.abc import Callable
from pathlib import Path

import kit
from flnd import _files as fs

COUNT = 5000
DATA = os.urandom(100 * 1024)


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


def fs_write(folder: Path) -> None:
    """Write every file with `flnd._files.write`, as flnd does today."""
    for n in range(COUNT):
        fs.write(folder / str(n), DATA)


def fs_write_no_replace(folder: Path) -> None:
    """`fs.write(replace=False)`: a stage, a lock and a hard link, which never overwrites."""
    for n in range(COUNT):
        fs.write(folder / str(n), DATA, replace=False)


def per_file(write: Callable[[Path], None]) -> float:
    """The best of 3 runs of `write` into a fresh folder, in microseconds per file.

    Args:
        write: one of the functions above, given the folder to write into.
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
                        ("fs_write", fs_write), ("fs_write_no_replace", fs_write_no_replace)):
        report.add(name, per_file(write), "us")
    report.emit()


if __name__ == "__main__":
    main()
