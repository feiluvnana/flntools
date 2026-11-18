"""Compare `flnd.db` file rows to files on disk; `fix` drops rows whose files are missing.

src/docs/storage ("Checking a home") explains it, and what a repair changes.
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

from flnd.engine import home
from flnd.engine.events import Checked, Record
from flnd.engine.home import MEDIA
from flnd.errors import DiskError
from flnkit import files

if TYPE_CHECKING:
    from flnd.engine.home import FileRow, Home

__all__ = ["check"]

SETTLING = 60.0


@dataclass
class _Seen:
    """What the rows say is on disk, for the walk of `media/`."""

    paths: set[str] = field(default_factory=set)
    inodes: set[tuple[int, int]] = field(default_factory=set)
    folders: set[str] = field(default_factory=set)


def check(home: Path, db: Home, *, fix: bool = False) -> Checked:
    """Compare the history with the disk, and with `fix`, drop missing rows and fix sizes.

    Args:
        home: the home folder, whose `media/` is walked.
        db: its `flnd.db`, open.
        fix: also repair missing and wrong-size rows; False only reports.

    Returns:
        What was found, and whether it was fixed.

    Raises:
        DiskError: `media/` is missing when `fix` is set, or the database can't be written.
    """
    rows = db.on_disk()
    seen = _Seen()
    missing: list[FileRow] = []
    resized: list[tuple[FileRow, os.stat_result]] = []
    for row in rows:
        _look(row, seen, missing, resized)
    unknown = list(_unknown(home / MEDIA, seen))
    if fix:
        if not (home / MEDIA).is_dir():
            raise DiskError(f"{home / MEDIA}: not found",
                            hint="create the media folder or pick another home")
        _fix(db, missing, resized)
    return Checked(
        files=len(rows),
        missing=tuple(_record(row) for row in missing),
        unknown=tuple(unknown),
        resized=tuple(_record(row) for row, _ in resized),
        fixed=fix and bool(missing or resized),
    )


def _look(row: FileRow, seen: _Seen, missing: list[FileRow],
          resized: list[tuple[FileRow, os.stat_result]]) -> None:
    """Look up one row's file and note what it names for the walk.

    Args:
        row: one saved file or link row.
        seen: paths and folders rows name so far; updated in place.
        missing: rows whose file is absent; appended to.
        resized: saved files of another size; appended to with their stat.
    """
    if row.folder:
        seen.folders.add(_norm(row.folder))
    if row.sidecar:
        seen.paths.add(_norm(row.sidecar))
    path = Path(row.path)
    try:
        found = path.stat()
    except FileNotFoundError:
        extracted = row.unpacked == "extracted" and row.folder and Path(row.folder).is_dir()
        if not extracted:
            missing.append(row)
        return
    except OSError:
        return
    seen.paths.add(_norm(path))
    seen.inodes.add((found.st_dev, found.st_ino))
    if row.state == home.SAVED and found.st_size != row.size:
        resized.append((row, found))


def _unknown(media: Path, seen: _Seen) -> list[Path]:
    """Files under `media/` no row names (report only).

    Args:
        media: the home's `media/` folder.
        seen: what rows already name.

    Returns:
        Unknown files, in walk order.
    """
    if not media.is_dir():
        return []
    now = time.time()
    found = []
    for entry in files.walk(media):
        if entry.name.startswith(".") or entry.is_symlink():
            continue
        name = _norm(entry.path)
        if name in seen.paths or _inside(name, seen.folders):
            continue
        if entry.name.endswith(".json") and name[: -len(".json")] in seen.paths:
            continue
        stat = entry.stat(follow_symlinks=False)
        if stat.st_ino == 0 and os.name == "nt":
            stat = os.stat(entry.path)
        if (stat.st_dev, stat.st_ino) in seen.inodes:
            continue
        if now - stat.st_ctime < SETTLING:
            continue
        found.append(Path(entry.path))
    return found


def _inside(name: str, folders: set[str]) -> bool:
    """Whether a path is inside one of `folders`, at any depth.

    Args:
        name: a normalised path string.
        folders: archive folders rows name.

    Returns:
        True when a parent of `name` is in `folders`.
    """
    if not folders:
        return False
    parent = os.path.dirname(name)
    while True:
        if parent in folders:
            return True
        up = os.path.dirname(parent)
        if up == parent:
            return False
        parent = up


def _fix(db: Home, missing: list[FileRow],
         resized: list[tuple[FileRow, os.stat_result]]) -> None:
    """Drop missing rows and fix sizes on disk.

    Args:
        db: the open home database.
        missing: rows whose files are gone.
        resized: rows whose sizes on disk differ from the history.
    """
    if missing:
        db.drop([row.key for row in missing])
    if resized:
        db.change([(row.key, {"size": found.st_size, "mtime": found.st_mtime_ns,
                              "hash": files.digest(row.path)}) for row, found in resized])


def _record(row: FileRow) -> Record:
    """A row as the `Record` a `Checked` lists."""
    return Record(row.key, Path(row.path), row.size, post=row.post_id, category=row.category,
                  date=home._date(row.date), at=row.at)


def _norm(path: str | os.PathLike[str]) -> str:
    """A path as one spelling: absolute, and case-folded where the platform folds names."""
    return os.path.normcase(os.path.abspath(path))