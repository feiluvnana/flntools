"""`flnd sync`'s repair and tally: what a target's folder lost before the fetch, and what it holds after.

`Engine.sync` reads the whole history once (`index`), then for each target, on its source's worker:
1. `repair` removes leftover `.part` files (keeping a recent resumable download's), and forgets the
   files its history names that are missing, empty or broken, deleting the broken ones, so the
   fetch that follows downloads them again;
2. the job fetches what is new or was lost;
3. `count` sees which lost files came back, names each that didn't, and counts the folder;
4. `summary` writes one line per target, then the totals.

A file whose size and modified time match its history row is trusted without a decode; a file that
changed is fully checked, and so is a rolling share of the trusted ones, so bit rot is still found.

    tally = repair.Tally("ins:example_user", folder, index[folder])
    repair.repair(history, tally)
    ...                                     # the fetch
    repair.count(history, tally)
    repair.summary([tally])                 # "ins:example_user: 120 files, 85.2 MB; 2 repaired, 1 missing"
"""

from __future__ import annotations

import datetime
import math
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, MutableMapping, Sequence

from flnd.common import files
from shared import fmt, log, worker

# Full checks at once: Pillow releases the GIL while it decodes, and ffmpeg runs in a process.
CHECKS_AT_ONCE = 8
# One in this many trusted files is fully checked each sync, those checked longest ago first, so
# every file is decoded again within a month of daily syncs.
ROLLING = 30
# The history field of the day a file was last fully checked.
CHECKED = "checked"


@dataclass
class Tally:
    """One target's sync: what its folder holds, and what this sync repaired or couldn't get back."""

    # The target as people write it, which starts its summary line.
    target: str
    # Its folder, and its (key, row) history rows from `index`.
    folder: Path
    rows: Sequence[tuple[str, dict]] = field(default=(), repr=False)
    # Files in its folder after the fetch, hidden ones left out, and their bytes.
    files: int = 0
    size: int = 0
    # Lost files the fetch saved again, and lost files it couldn't.
    repaired: int = 0
    missing: int = 0
    # History keys whose file was gone, or broken and deleted, before the fetch, with their file names.
    lost: dict[str, str] = field(default_factory=dict, repr=False)
    broken: set[str] = field(default_factory=set, repr=False)


def index(history: MutableMapping[str, Any], folders: Iterable[Path]) -> dict[Path, list[tuple[str, dict]]]:
    """The history rows whose file is in each folder, or was, from one read of the whole history.

        rows = repair.index(history, [folder])[folder]

    Args:
        history: the download history.
        folders: the target folders to index; each gets a list, empty when no row is in it.

    With 100 targets and 100,000 rows, a scan per target and step was 30 million row reads; this
    is 100,000. Paths are compared made absolute from the working directory, as they were saved,
    without resolving links, which would cost a system call per row.
    """
    roots = {Path(os.path.abspath(folder)): folder for folder in folders}
    found: dict[Path, list[tuple[str, dict]]] = {folder: [] for folder in roots.values()}
    for key, row in history.items():
        if not (isinstance(row, dict) and row.get("path")):
            continue
        for parent in Path(os.path.abspath(row["path"])).parents:
            if parent in roots:
                found[roots[parent]].append((key, row))
                break
    return found


def repair(history: MutableMapping[str, Any], tally: Tally) -> None:
    """Remove leftover parts, and forget the target's files that are gone, empty or broken, for the fetch to get again.

    Args:
        history: the download history; a broken file's row is deleted, a checked one's updated.
        tally: the target's tally, with its folder and rows; each lost file is noted in it.

    A broken file is deleted, which can't be undone. A file media refuses to open (`UnsafeError`)
    isn't known to be broken, so it is kept.
    """
    _remove_leftovers(tally.folder)
    changed: list[tuple[str, dict]] = []
    trusted: list[tuple[str, dict]] = []
    for key, row in tally.rows:
        path = Path(row["path"])
        if not path.is_file():
            tally.lost[key] = path.name
        else:
            (trusted if files.unchanged(path, row) else changed).append((key, row))
    # Checked longest ago first; a row never checked has no day, and sorts first.
    trusted.sort(key=lambda item: str(item[1].get(CHECKED, "")))
    due = changed + trusted[:math.ceil(len(trusted) / ROLLING)]
    today = datetime.date.today().isoformat()
    with log.progress(f"repair {tally.target}", total=len(due)) as bar:
        for result in worker.as_completed(_broken, due, concurrency=CHECKS_AT_ONCE):
            bar.advance()
            if result.error is not None:
                raise result.error
            key, row = result.item
            path = Path(row["path"])
            if result.value:
                path.unlink()
                del history[key]
                tally.lost[key] = path.name
                tally.broken.add(key)
            else:
                history[key] = {**row, **files.stamp(path), CHECKED: today}


def _remove_leftovers(folder: Path) -> None:
    """Remove the parts and dangling links an interrupted run left in a target's folder."""
    if not folder.is_dir():
        return
    # Every temporary file flnd writes matches PART_GLOB (`shared.atomic`'s parts), and nothing is
    # downloading into the folder yet. A resumable download's part (one with its `.part.json`) is
    # kept while it is recent, since the fetch that follows resumes it; its sidecar goes with it,
    # and a sidecar alone goes too.
    for stale in sorted(folder.rglob(files.PART_GLOB)):
        if not files.resumable(stale):
            stale.unlink(missing_ok=True)
            files.part_info(stale).unlink(missing_ok=True)
    for info in folder.rglob(files.PART_INFO_GLOB):
        if not info.with_name(info.name[: -len(files.PART_INFO_SUFFIX)]).is_file():
            info.unlink(missing_ok=True)
    # A link into another category's folder whose file is gone: made again by the next run that
    # lists the file, once the fetch has saved the file again.
    for link in folder.rglob("*"):
        if link.is_symlink() and not link.exists():
            link.unlink()


def _broken(item: tuple[str, dict]) -> bool:
    """Whether one saved file is empty, a web page, or a photo or video that won't decode."""
    from shared import media

    path = Path(item[1]["path"])
    kind = files.kind_of(path.name)
    # An empty file of any kind is a download that never got its bytes.
    if path.stat().st_size == 0:
        return True
    if kind is None:
        return False
    # The same check a download passes, so a file kept here would have been kept then. Audio is
    # only sniffed for a web page or JSON: a mostly playable file is worth more than a download
    # that may no longer exist.
    try:
        files.checked(path, kind, decode=kind != files.AUDIO)
    except (files.NotMediaError, media.BrokenError):
        return True
    except media.UnsafeError as error:
        # A file media refuses to open (a decompression bomb) isn't known to be broken, so it is
        # kept. Files that can't be checked here (no ffmpeg) are kept by `files.checked` itself.
        log.debug("{name}: not checked: {error}", name=path.name, error=error)
    return False


def count(history: MutableMapping[str, Any], tally: Tally) -> None:
    """Count what the target's folder holds after the fetch, and name each lost file not got back.

    Args:
        history: the download history, as the fetch left it.
        tally: the target's tally from `repair`; its counts are set here.
    """
    folder = tally.folder
    gone: dict[str, str] = {}
    for key, name in tally.lost.items():
        # One row each, not a scan: the fetch may have saved it again, under a new path.
        row = history.get(key)
        if isinstance(row, dict) and Path(row["path"]).is_file():
            tally.repaired += 1
        else:
            gone[key] = name
    for key, name in sorted(gone.items(), key=lambda item: item[1]):
        if key in tally.broken:
            log.warn("{target}: {name} was broken and couldn't be downloaded again", target=tally.target, name=name)
        else:
            log.warn("{target}: {name} is missing; its post may be gone", target=tally.target, name=name)
    tally.missing = len(gone)
    # A link is a view of a file counted already, not a file of its own.
    on_disk = [path for path in folder.rglob("*") if path.is_file() and not path.is_symlink()
               and not path.name.startswith(".")] if folder.is_dir() else []
    tally.files, tally.size = len(on_disk), sum(path.stat().st_size for path in on_disk)


def summary(tallies: Sequence[Tally]) -> None:
    """One line per target, then the totals: `ins:example_user: 120 files, 85.2 MB; 2 repaired, 1 missing`.

        repair.summary(tallies)
    """
    for tally in tallies:
        log.info("{target}: {body}", target=tally.target, body=_described(tally))
    total = Tally(fmt.count(len(tallies), "target"), Path(),
                  files=sum(tally.files for tally in tallies),
                  size=sum(tally.size for tally in tallies),
                  repaired=sum(tally.repaired for tally in tallies),
                  missing=sum(tally.missing for tally in tallies))
    log.info("total: {targets}, {body}", targets=total.target, body=_described(total))


def _described(tally: Tally) -> str:
    return (f"{fmt.count(tally.files, 'file')}, {fmt.size(tally.size)}; {tally.repaired:,} repaired, "
            f"{tally.missing:,} missing")
