"""`flnd sync`'s repair and tally: what a folder lost before fetch, and holds after."""

from __future__ import annotations

import datetime
import math
import os
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, MutableMapping, Sequence

from flnd.common import paths
from shared import archive, fs, media, text, ui, worker

CHECKS_AT_ONCE = 8
ROLLING = 30
CHECKED = "checked"
ARCHIVE_SUFFIXES = frozenset({".zip", ".7z", ".rar", ".tar", ".gz", ".bz2", ".xz", ".tgz"})


@dataclass
class Tally:
    """One target's sync: folder contents, repaired files, and missing files."""

    target: str
    folder: Path
    rows: Sequence[tuple[str, dict[str, Any]]] = field(default=(), repr=False)
    files: int = 0
    size: int = 0
    repaired: int = 0
    missing: int = 0
    lost: dict[str, str] = field(default_factory=dict, repr=False)
    broken: set[str] = field(default_factory=set, repr=False)


def index(
    history: MutableMapping[str, Any], folders: Iterable[Path]
) -> dict[Path, list[tuple[str, dict[str, Any]]]]:
    """Return the history rows whose file is in each folder, from one history read."""
    roots = {Path(os.path.abspath(folder)): folder for folder in folders}
    found: dict[Path, list[tuple[str, dict[str, Any]]]] = {folder: [] for folder in roots.values()}
    for key, row in history.items():
        if not (isinstance(row, dict) and row.get("path")):
            continue
        for parent in Path(os.path.abspath(row["path"])).parents:
            if parent in roots:
                found[roots[parent]].append((key, row))
                break
    return found


def repair(history: MutableMapping[str, Any], tally: Tally) -> None:
    """Remove leftover parts and forget missing or broken files for re-download."""
    _remove_leftovers(tally.folder)
    changed, trusted = _partition_rows(history, tally)
    # Checked longest ago first; a row never checked has no day, and sorts first.
    trusted.sort(key=lambda item: str(item[1].get(CHECKED, "")))
    due = changed + trusted[:math.ceil(len(trusted) / ROLLING)]
    today = datetime.date.today().isoformat()
    with ui.progress(f"repair {tally.target}", total=len(due)) as bar:
        with worker.Pool(concurrency=CHECKS_AT_ONCE, progress=bar.part()) as pool:
            for item in due:
                pool.submit(_broken, item)
            results = list(pool.as_completed())
        for result in results:
            if result.error is not None:
                raise result.error
            _apply_check(history, tally, result.item, bool(result.value), today)


def _partition_rows(
    history: MutableMapping[str, Any],
    tally: Tally,
) -> tuple[list[tuple[str, dict[str, Any]]], list[tuple[str, dict[str, Any]]]]:
    """Split target rows into changed/untrusted and unchanged/trusted, noting missing files."""
    changed: list[tuple[str, dict[str, Any]]] = []
    trusted: list[tuple[str, dict[str, Any]]] = []
    for key, row in tally.rows:
        path = Path(row["path"])
        try:
            is_unchanged = path.is_file() and paths.unchanged(path, row)
        except FileNotFoundError:
            del history[key]
            tally.lost[key] = path.name
            continue
        if not path.is_file():
            del history[key]
            tally.lost[key] = path.name
        else:
            (trusted if is_unchanged else changed).append((key, row))
    return changed, trusted


def _apply_check(
    history: MutableMapping[str, Any],
    tally: Tally,
    item: tuple[str, dict[str, Any]],
    is_broken: bool,
    today: str,
) -> None:
    """Update `history` and `tally` for one checked file."""
    key, row = item
    path = Path(row["path"])
    if is_broken:
        path.unlink(missing_ok=True)
        del history[key]
        tally.lost[key] = path.name
        tally.broken.add(key)
        return
    try:
        stamped = paths.stamp(path)
    except FileNotFoundError:
        del history[key]
        tally.lost[key] = path.name
        return
    history[key] = {**row, **stamped, CHECKED: today}


def _scandir_walk(folder: Path) -> Iterator[os.DirEntry[str]]:
    """Yield every entry under `folder` recursively without extra stat calls."""
    stack = [str(folder)]
    while stack:
        try:
            with os.scandir(stack.pop()) as entries:
                for entry in entries:
                    yield entry
                    if entry.is_dir(follow_symlinks=False):
                        stack.append(entry.path)
        except OSError:
            pass


def _remove_leftovers(folder: Path) -> None:
    """Remove stale parts and dangling symlinks an interrupted run left in `folder`."""
    if not folder.is_dir():
        return
    fs.sweep(folder)
    for entry in _scandir_walk(folder):
        path = Path(entry.path)
        if entry.is_symlink() and not path.exists():
            path.unlink(missing_ok=True)


def _broken(item: tuple[str, dict[str, Any]]) -> bool:
    """Return whether one saved file is empty, a web page, or a corrupt media/archive file."""
    path = Path(item[1]["path"])
    try:
        if path.stat().st_size == 0:
            return True
    except FileNotFoundError:
        return True
    kind = paths.kind_of(path.name)
    if kind is None:
        return _broken_archive(path) if path.suffix.lower() in ARCHIVE_SUFFIXES else False
    try:
        if media.sniff(path) in paths.PAGES:
            return True
        media.info(path)
        if kind != paths.AUDIO:
            with media.open(path) as handle:
                _ = handle.raw
    except media.BrokenError:
        return True
    except (media.UnsafeError, media.MissingError, TimeoutError, OSError) as error:
        ui.debug("{name}: not checked: {error}", name=path.name, error=error)
    return False


def _broken_archive(path: Path) -> bool:
    """Return whether an archive fails `archive.ls` due to corruption."""
    try:
        archive.ls(path)
    except archive.BrokenError:
        return True
    except (archive.UnsafeError, archive.MissingError, archive.Error, OSError) as error:
        ui.debug("{name}: not checked: {error}", name=path.name, error=error)
    return False


def count(history: MutableMapping[str, Any], tally: Tally) -> None:
    """Count what the target's folder holds after fetch and log missing files."""
    gone: dict[str, str] = {}
    for key, name in tally.lost.items():
        row = history.get(key)
        if isinstance(row, dict) and Path(row["path"]).is_file():
            tally.repaired += 1
        else:
            gone[key] = name
    for key, name in sorted(gone.items(), key=lambda item: item[1]):
        if key in tally.broken:
            ui.warn("{target}: {name} was broken and couldn't be downloaded again",
                    target=tally.target, name=name)
        else:
            ui.warn("{target}: {name} is missing; its post may be gone",
                    target=tally.target, name=name)
    tally.missing = len(gone)
    files, size = 0, 0
    if tally.folder.is_dir():
        for entry in _scandir_walk(tally.folder):
            if (not entry.name.startswith(".") and not entry.is_symlink()
                    and entry.is_file(follow_symlinks=False)):
                files += 1
                size += entry.stat(follow_symlinks=False).st_size
    tally.files, tally.size = files, size


def summary(tallies: Sequence[Tally]) -> None:
    """Log one line per target, then the totals across all targets."""
    for tally in tallies:
        ui.info("{target}: {body}", target=tally.target, body=_described(tally))
    total = Tally(
        text.fmt.count(len(tallies), "target"),
        Path(),
        files=sum(tally.files for tally in tallies),
        size=sum(tally.size for tally in tallies),
        repaired=sum(tally.repaired for tally in tallies),
        missing=sum(tally.missing for tally in tallies),
    )
    ui.info("total: {targets}, {body}", targets=total.target, body=_described(total))


def _described(tally: Tally) -> str:
    """Format one tally's file count, size, repaired, and missing counts."""
    return (
        f"{text.fmt.count(tally.files, 'file')}, {text.fmt.size(tally.size)}; "
        f"{tally.repaired:,} repaired, {tally.missing:,} missing"
    )
