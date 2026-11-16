"""Benchmark group `walk`: listing a media folder of 100,000 files, and `stat` on known paths.

The tree is 100 folders of 1,000 empty files each, made once in a scratch folder. `_files.walk` is
what flnd uses to list a folder today; `os.scandir` is the floor; `Path.rglob` with `is_file()`
is what folder statistics use today, and is the slow one. A walk that also reads each file's
size (`entry.stat()`) costs a `stat` per file, which is why the hot path avoids it.
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

import kit

from flnd import _files

FOLDERS, PER_FOLDER = 100, 1000
FILES = FOLDERS * PER_FOLDER
# How many known paths `stat` is called on.
STATS = 10_000


def scandir(root: Path) -> int:
    """Count the files under `root` with `os.scandir` and a stack: the floor."""
    count, stack = 0, [str(root)]
    while stack:
        with os.scandir(stack.pop()) as entries:
            for entry in entries:
                if entry.is_dir(follow_symlinks=False):
                    stack.append(entry.path)
                else:
                    count += 1
    return count


def main() -> None:
    """Build the tree, time each walk, and emit the report."""
    report = kit.Report("walk")
    with tempfile.TemporaryDirectory(prefix="flnd-bench-") as scratch:
        root = Path(scratch, "tree")
        for folder in range(FOLDERS):
            (root / str(folder)).mkdir(parents=True)
            for n in range(PER_FOLDER):
                (root / str(folder) / f"{n}.jpg").touch()
        report.add("fs_walk", kit.best(lambda: sum(1 for _ in _files.walk(root))) * 1e3, "ms")
        report.add("scandir", kit.best(lambda: scandir(root)) * 1e3, "ms")
        report.add("rglob_is_file",
                   kit.best(lambda: sum(1 for p in root.rglob("*") if p.is_file())) * 1e3, "ms")
        report.add("fs_walk_stat",
                   kit.best(lambda: sum(e.stat().st_size for e in _files.walk(root))) * 1e3, "ms")
        paths = [root / str(n % FOLDERS) / f"{n % PER_FOLDER}.jpg" for n in range(STATS)]
        report.add("stat", kit.best(lambda: [os.stat(p) for p in paths]) / STATS * 1e6, "us")
    report.emit()


if __name__ == "__main__":
    main()
