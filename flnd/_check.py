"""`flnd check`: the home's history in `flnd.db` against the files on disk, and the repair.

Who calls it: `Flnd.check` (and through it `flnd check [--fix]`). What it calls: `flnd._home`
for the rows, `flnd._files` to walk the home's `media/` folder and to hash, and nothing on the
network.

What it finds (`check`):
1. Every row that names a file on disk (saved files and their links) is looked up with one
   `stat`. A file that isn't there is **missing**; a saved file whose size isn't its row's is of
   the **wrong size** (cut short, or changed by another program). An archive removed once
   extracted is fine while its folder is there.
2. `media/` is walked once. A file no row names is **unknown**: a file a run renamed into place
   and then crashed before its row, or one put there by hand. These are never unknown: parts
   and what sits beside them, hidden files (`.DS_Store`), symlinks (the links flnd makes
   between category folders), a hard link of a known file (same inode), a sidecar of a known
   file, anything inside a folder an archive was extracted to, and a file renamed into place in
   the last minute (another run may be about to write its row).

What `fix=True` repairs, always toward the disk (it never deletes, moves or downloads a file):
- a missing file's row is removed, so a later download (`--again`) can fetch it again;
- a file of the wrong size gets its row's size, time and hash from the disk;
- an unknown file is adopted: it gets a row keyed by `(target, post, file)`. The key, target and
  post come from the failure that names its path, when a run recorded one; else the target is
  the folder it is in (`media/<code>/<target>/…`) and its key is its path there. A later run
  that lists the file finds it on disk and skips it, so nothing downloads twice.
Every repair is written in one transaction per kind.

Rules a change must keep:
- check reads the disk and writes only `flnd.db`;
- one `stat` per row and one walk of `media/`: no file is read except to hash one being fixed.
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

from flnd import _files, _home, _text
from flnd._events import Checked, Record

if TYPE_CHECKING:
    from flnd._home import FileRow, Home

__all__ = ["check"]

# A file renamed into place this recently may still be waiting for its row (another run's
# commit comes when its post's last file lands), so it is left for the next check. Seconds.
SETTLING = 60.0


@dataclass
class _Seen:
    """What the rows say is on disk, for the walk of `media/` (stage 2)."""

    # Every path a row names (a file or a sidecar), as an absolute, case-folded string.
    paths: set[str] = field(default_factory=set)
    # The (device, inode) of every file a row names, so a hard link of one is known.
    inodes: set[tuple[int, int]] = field(default_factory=set)
    # The folders archives were extracted to: what is inside is the archive's, not a row's.
    folders: set[str] = field(default_factory=set)


def check(home: Path, db: Home, *, fix: bool = False) -> Checked:
    """Compare the history with the disk, and with `fix`, make the history match it.

    Args:
        home: the home folder, whose `media/` is walked.
        db: its `flnd.db`, open.
        fix: also repair the history (see the module's docstring); False only reports.

    Returns:
        What was found, and whether it was fixed.

    Raises:
        DiskError: a folder can't be read, or `flnd.db` can't be written.
    """
    rows = db.on_disk()
    seen = _Seen()
    missing: list[FileRow] = []
    resized: list[tuple[FileRow, os.stat_result]] = []
    for row in rows:
        _look(row, seen, missing, resized)
    unknown = list(_unknown(home / _home.MEDIA, seen))
    if fix:
        _fix(db, home, missing, resized, unknown)
    return Checked(
        files=len(rows),
        missing=tuple(_record(row) for row in missing),
        unknown=tuple(unknown),
        resized=tuple(_record(row) for row, _ in resized),
        fixed=fix and bool(missing or resized or unknown),
    )


# ---------------------------------------------------------------------------------------------
# Finding


def _look(row: FileRow, seen: _Seen, missing: list[FileRow],
          resized: list[tuple[FileRow, os.stat_result]]) -> None:
    """Look up one row's file (stage 1), and note what it names for the walk.

    Args:
        row: a saved file's or a link's row.
        seen: what the rows name so far; this row's paths are added.
        missing: where a row whose file isn't there goes.
        resized: where a saved file of another size goes, with what the disk says.
    """
    if row.folder:
        seen.folders.add(_norm(row.folder))
    if row.sidecar:
        seen.paths.add(_norm(row.sidecar))
    path = Path(row.path)
    try:
        found = path.stat()
    except OSError:
        extracted = row.unpacked == "extracted" and row.folder and Path(row.folder).is_dir()
        if not extracted:
            missing.append(row)
        return
    seen.paths.add(_norm(path))
    seen.inodes.add((found.st_dev, found.st_ino))
    if row.state == _home.SAVED and found.st_size != row.size:
        resized.append((row, found))


def _unknown(media: Path, seen: _Seen) -> list[Path]:
    """The files under `media/` no row names (stage 2).

    Args:
        media: the home's `media/` folder; a missing one holds nothing.
        seen: what the rows name.

    Returns:
        The unknown files, in the order the walk met them.
    """
    if not media.is_dir():
        return []
    now = time.time()
    found = []
    for entry in _files.walk(media):
        if entry.name.startswith(".") or entry.is_symlink():
            continue
        name = _norm(entry.path)
        if name in seen.paths or _inside(name, seen.folders):
            continue
        if entry.name.endswith(".json") and name[: -len(".json")] in seen.paths:
            continue
        stat = entry.stat(follow_symlinks=False)
        if (stat.st_dev, stat.st_ino) in seen.inodes:
            continue
        # A rename sets the change time, not the modified time (which is the post's day).
        if now - stat.st_ctime < SETTLING:
            continue
        found.append(Path(entry.path))
    return found


def _inside(name: str, folders: set[str]) -> bool:
    """Whether a path is inside one of `folders`, at any depth.

    Args:
        name: the path, as `_norm` spells it.
        folders: the folders, spelled the same way.

    Returns:
        True when one of its parents is one of them.
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


# ---------------------------------------------------------------------------------------------
# Fixing


def _fix(db: Home, home: Path, missing: list[FileRow],
         resized: list[tuple[FileRow, os.stat_result]], unknown: list[Path]) -> None:
    """Make the history match the disk: drop, resize and adopt (see the module's docstring).

    Args:
        db: the home's `flnd.db`.
        home: the home folder.
        missing: rows whose file isn't there.
        resized: saved files of another size, with their `stat`.
        unknown: files no row names.
    """
    if missing:
        db.drop([row.key for row in missing])
    if resized:
        db.change([(row.key, {"size": found.st_size, "mtime": found.st_mtime_ns,
                              "hash": _files.digest(row.path)}) for row, found in resized])
    if unknown:
        db.save(_adopted(db, home, unknown))


def _adopted(db: Home, home: Path, unknown: list[Path]) -> list[FileRow]:
    """The rows that adopt unknown files, by `(target, post, file)`.

    Args:
        db: the home's `flnd.db`, for the failures that name a file and the targets it knows.
        home: the home folder.
        unknown: the files.

    Returns:
        One row per file, keyed so it takes no other file's key.
    """
    from flnd.common.paths import kind_of

    failed = {_norm(path): value for path, value in db.failed_at().items()}
    folders = {(row.source, _text.filename(row.value)): item
               for item, row in db.targets().items()}
    media = home / _home.MEDIA
    taken: set[str] = set()
    adopted = []
    for path in unknown:
        found = path.stat()
        named = failed.get(_norm(path))
        if named is not None:
            item, target, post = named
        else:
            # media/<code>/<target folder>/…: a file directly in a source's folder has none.
            parts = path.relative_to(media).parts
            code = parts[0]
            target = f"{code}:"
            if len(parts) > 2:
                target = folders.get((code, parts[1]), f"{code}:{parts[1]}")
            item, post = f"{code}:{'/'.join(parts[1:])}", ""
        item = _free(db, item, taken)
        adopted.append(_home.FileRow(
            item, target, str(path), found.st_size, found.st_mtime_ns, post_id=post,
            kind=kind_of(path.name), at=_home.now(), hash=_files.digest(path)))
    return adopted


def _free(db: Home, item: str, taken: set[str]) -> str:
    """`item`, or `item#2`, `item#3`…: the first key no file on disk and no other adopted
    file has. A row of a file recorded gone or failed may be replaced: the file is here.

    Args:
        db: the home's `flnd.db`.
        item: the key wanted.
        taken: the keys given to other adopted files so far; the one returned is added.

    Returns:
        A free key.
    """
    chosen, number = item, 1
    while chosen in taken or _on_disk(db.row(chosen)):
        number += 1
        chosen = f"{item}#{number}"
    taken.add(chosen)
    return chosen


def _on_disk(row: FileRow | None) -> bool:
    """Whether a row names a file on disk: a saved file or a link."""
    return row is not None and row.state in (_home.SAVED, _home.LINK)


def _record(row: FileRow) -> Record:
    """A row as the `Record` a `Checked` lists; its size is the row's."""
    return Record(row.key, Path(row.path), row.size, post=row.post_id, category=row.category,
                  date=_home._date(row.date), at=row.at)


def _norm(path: str | os.PathLike[str]) -> str:
    """A path as one spelling: absolute, and case-folded where the platform folds names."""
    return os.path.normcase(os.path.abspath(path))
