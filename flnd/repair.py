"""`flnd sync`'s repair and tally: what a target's folder lost before the fetch, and what it holds after.

`Engine.sync` reads the whole history once (`index`, fixes #12), then for each target:
1. `repair` removes leftover `.part` files (keeping a recent resumable download's), and forgets the files its history names that are
   missing, empty or broken, deleting the broken ones, so the fetch downloads them again;
2. the engine fetches what is new or was lost;
3. `count` sees which lost files came back, names each that didn't, and counts the folder;
4. `summary` writes one line per target, then the totals.

    tally = repair.Tally("ins:example_user")
    repair.repair(history, folder, index[folder], tally)
    ...                                     # the fetch
    repair.count(history, folder, tally)
    repair.summary([tally])                 # "ins:example_user: 120 files, 85.2 MB; 2 repaired, 1 missing"

Audio is only sniffed here, never decoded: Phase 5 kept every audio file, and a mostly playable
file is worth more than a download that may no longer exist. Modified times are set by the job's
`mtime` postprocessor as files are saved, so repair doesn't touch them.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, MutableMapping, Sequence

from flnd.common import files
from shared import fmt, log


@dataclass
class Tally:
    """One target's sync: what its folder holds, and what this sync repaired or couldn't get back."""

    # The target as people write it, which starts its summary line.
    target: str
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
    """The history rows whose file is in each folder, or was, from one read of the whole history (fixes #12).

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


def repair(history: MutableMapping[str, Any], folder: Path, rows: Sequence[tuple[str, dict]], tally: Tally) -> None:
    """Remove leftover parts, and forget the target's files that are gone, empty or broken, for the fetch to get again.

        repair.repair(history, folder, index[folder], tally)

    Args:
        history: the download history; a broken file's row is deleted.
        folder: the target's folder.
        rows: its (key, row) history rows, from `index`.
        tally: where each lost file is noted.

    A broken file is deleted, which can't be undone. A file media refuses to open (`UnsafeError`)
    isn't known to be broken, so it is kept.
    """
    from shared import media

    # Parts an interrupted run left behind; nothing else is downloading into the folder yet. Every
    # temporary file flnd writes matches PART_GLOB (`shared.atomic`'s parts), so this finds them all.
    # A resumable download's part (one with its `.part.json`) is kept while it is recent, since
    # the fetch that follows resumes it; its sidecar goes with it, and a sidecar alone goes too.
    leftovers = sorted(folder.rglob(files.PART_GLOB)) if folder.is_dir() else []
    for stale in leftovers:
        if not files.resumable(stale):
            stale.unlink(missing_ok=True)
            files.part_info(stale).unlink(missing_ok=True)
    for info in folder.rglob(files.PART_INFO_GLOB) if folder.is_dir() else ():
        if not info.with_name(info.name[: -len(files.PART_INFO_SUFFIX)]).is_file():
            info.unlink(missing_ok=True)
    with log.progress(f"repair {tally.target}", total=len(rows)) as bar:
        for key, row in rows:
            bar.advance()
            path = Path(row["path"])
            if not path.is_file():
                tally.lost[key] = path.name
                continue
            kind = files.kind_of(path.name)
            # An empty file of any kind is a download that never got its bytes.
            broken = path.stat().st_size == 0
            if not broken and kind is not None:
                # The same check a download passes, so a file kept here would have been kept then.
                # Audio is only sniffed for a web page or JSON: Phase 5 never decoded it and kept every
                # one, and a mostly playable file is worth more than a download that may no longer exist.
                try:
                    files.checked(path, kind, decode=kind != files.AUDIO)
                except (files.NotMediaError, media.BrokenError):
                    broken = True
                except media.UnsafeError as error:
                    # A file media refuses to open isn't known to be broken, so it is kept. Files that
                    # can't be checked here (no ffmpeg) are kept by `files.checked` itself.
                    log.debug("{name}: not checked: {error}", name=path.name, error=error)
            if broken:
                path.unlink()
                del history[key]
                tally.lost[key] = path.name
                tally.broken.add(key)


def count(history: MutableMapping[str, Any], folder: Path, tally: Tally) -> None:
    """Count what the folder holds after the fetch, and name each lost file the fetch couldn't get back.

        repair.count(history, folder, tally)

    Args:
        history: the download history, as the fetch left it.
        folder: the target's folder.
        tally: the target's tally from `repair`; its counts are set here.
    """
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
    on_disk = [path for path in folder.rglob("*") if path.is_file()
               and not path.name.startswith(".")] if folder.is_dir() else []
    tally.files, tally.size = len(on_disk), sum(path.stat().st_size for path in on_disk)


def summary(tallies: Sequence[Tally]) -> None:
    """One line per target, then the totals: `ins:example_user: 120 files, 85.2 MB; 2 repaired, 1 missing`.

        repair.summary(tallies)
    """
    for tally in tallies:
        log.info("{target}: {body}", target=tally.target, body=_described(tally))
    total = Tally(fmt.count(len(tallies), "target"), files=sum(tally.files for tally in tallies),
                  size=sum(tally.size for tally in tallies), repaired=sum(tally.repaired for tally in tallies),
                  missing=sum(tally.missing for tally in tallies))
    log.info("total: {targets}, {body}", targets=total.target, body=_described(total))


def _described(tally: Tally) -> str:
    return (f"{fmt.count(tally.files, 'file')}, {fmt.size(tally.size)}; {tally.repaired:,} repaired, "
            f"{tally.missing:,} missing")
