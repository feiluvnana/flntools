"""`flnd sync`: bring every target already downloaded up to date, and the saved target settings.

    chosen = sync.plan(engine, [], {"compress_video": "small"})   # every recorded target, and these settings
    lines = sync.run(engine, chosen, sync.STEPS)                   # repair, fetch, compress, info

Every `flnd download` records its target and settings in `.flnd/targets.db` (`record`), so a sync
runs each target as it was downloaded. A folder downloaded before Phase 5 has no record; it is
synced with the default settings, and recorded then.
"""

from __future__ import annotations

import datetime
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterator, Mapping, MutableMapping, Sequence

from flnd import files, net
from flnd.compress import compress_file
from flnd.engine import HISTORY_FILE, HISTORY_TABLE, Engine
from flnd.session import PHOTO, VIDEO, Options, media_kind
from flnd.target import Target, TargetError
from shared import fmt, kv, log

TARGETS_FILE, TARGETS_TABLE = "targets.db", "targets"
STEPS = ("repair", "fetch", "compress", "info")
# What a target is downloaded with. --since and --until describe one run, not the target.
SETTINGS = ("categories", "unpack", "zip", "compress_image", "compress_video", "format_image", "format_video")
COLUMNS = ("target", "files", "photos", "videos", "size", "saved", "repaired", "missing")


@dataclass
class Line:
    """One target's summary: what its folder holds, and what this sync repaired or couldn't get back."""

    target: str
    files: int = 0
    photos: int = 0
    videos: int = 0
    size: int = 0
    saved: int = 0
    repaired: int = 0
    missing: int = 0
    # History keys whose file was gone, or broken and deleted, before the fetch, with their file names.
    lost: dict[str, str] = field(default_factory=dict, repr=False)
    broken: set[str] = field(default_factory=set, repr=False)


def record(home: Path, targets: Sequence[Target], options: Options) -> None:
    """Save each target's settings in `targets.db`, keyed `<code>:<value>`, replacing what was there.

        sync.record(Path(".flnd"), engine.resolve(["ins:example_user"]), Options(compress_video="small"))

    Args:
        home: flnd's folder, `.flnd`, which holds `targets.db`; the file is created when missing.
        targets: the targets just downloaded, as `Engine.resolve` gives them.
        options: the run's options; only the `SETTINGS` fields are kept, since `--since` and
            `--until` describe one run, not the target.

    Raises:
        OSError: `targets.db` can't be created or written.

    Each row also keeps the target's kind, so `plan` can rebuild it without resolving it again,
    and the time it was recorded, in UTC.
    """
    now = datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")
    with kv.open(home / TARGETS_FILE, table=TARGETS_TABLE) as saved:
        for target in targets:
            saved[f"{target.source}:{target.value}"] = {
                "kind": target.kind, **{name: getattr(options, name) for name in SETTINGS}, "at": now}


def plan(engine: Engine, targets: Sequence[Target], overrides: Mapping[str, Any]) -> list[tuple[Target, Options]]:
    """Each target to sync, with its recorded settings and `overrides` on top.

        chosen = sync.plan(engine, engine.resolve(["ins:example_user"]), {"zip": True})

    Args:
        engine: the engine whose home holds `targets.db` and `downloads/`, and whose sources count.
        targets: only these, already resolved, so a short link isn't followed a second time; when
            empty, every recorded target and every unrecorded folder under `downloads/<code>/`.
        overrides: settings, by `Options` field name, that replace the recorded ones.

    Returns:
        (target, options) for each target, in the order given or recorded.
    """
    rows: dict[str, Any] = {}
    if (engine.home / TARGETS_FILE).is_file():
        with kv.open(engine.home / TARGETS_FILE, table=TARGETS_TABLE) as saved:
            rows = dict(saved.items())
    if not targets:
        targets = [Target(code, row["kind"], value) for key, row in rows.items()
                   for code, value in [key.split(":", 1)] if code in engine.sources]
        targets += list(_unrecorded(engine, targets))
    return [(target, _options(rows.get(f"{target.source}:{target.value}", {}), overrides)) for target in targets]


def run(engine: Engine, chosen: Sequence[tuple[Target, Options]], steps: Sequence[str]) -> list[Line]:
    """Run the `steps` over every chosen target, each step for every target before the next, and sum up.

        lines = sync.run(engine, sync.plan(engine, [], {}), ("repair", "fetch"))

    Args:
        engine: the engine whose home holds the downloads and their history; its `failed` lists
            the targets whose fetch failed.
        chosen: (target, options) for each target, as `plan` gives them.
        steps: which of `STEPS` to run, in `STEPS`' order whatever the order given: "repair"
            removes leftover parts and broken files, "fetch" downloads what is new or was lost,
            "compress" compresses what the options want, and "info" prints the summary table.

    Returns:
        One `Line` per chosen target, in the same order, counting what its folder holds after
        the sync, whichever steps ran.

    Raises:
        OSError: the history can't be opened or written.

    The fetch runs targets with the same settings together, so different sources run at once.
    It makes network requests and can take hours; repair deletes broken files, which can't be
    undone, and compress replaces originals with their smaller copies.
    """
    lines = [Line(str(target)) for target, _ in chosen]
    folders = [_folder(engine.home, target) for target, _ in chosen]
    if "repair" in steps:
        with _history(engine.home) as history:
            for line, folder in zip(lines, folders):
                _repair(history, folder, line)
    if "fetch" in steps:
        groups: dict[Options, list[Target]] = {}
        for target, options in chosen:
            groups.setdefault(options, []).append(target)
        for options, targets in groups.items():
            engine.run(targets, options)
    with _history(engine.home) as history:
        videos = {"on": True}
        # The kinds of compress error already warned of, for the whole compress step.
        warned: set[type[Exception]] = set()
        for (_, options), line, folder in zip(chosen, lines, folders):
            if "compress" in steps:
                _compress(history, folder, options, line, videos, warned)
            _tally(history, folder, line)
    if "info" in steps:
        _summary(lines)
    return lines


def _options(row: Mapping[str, Any], overrides: Mapping[str, Any]) -> Options:
    merged = {**{name: row[name] for name in SETTINGS if name in row}, **overrides}
    categories = merged.get("categories")
    return Options(categories=tuple(categories) if categories else None, unpack=bool(merged.get("unpack")),
                   zip=bool(merged.get("zip")), **{name: merged.get(name) for name in SETTINGS[3:]})


def _unrecorded(engine: Engine, known: Sequence[Target]) -> Iterator[Target]:
    seen = {(target.source, fmt.filename(target.value)) for target in known}
    downloads = engine.home / "downloads"
    for code in engine.sources:
        for folder in sorted((downloads / code).iterdir()) if (downloads / code).is_dir() else ():
            if folder.is_dir() and (code, folder.name) not in seen:
                try:
                    yield engine.resolve([f"{code}:{folder.name}"])[0]
                except TargetError as error:
                    log.warn("{folder}: not synced: {error}", folder=f"{code}/{folder.name}", error=error)


def _folder(home: Path, target: Target) -> Path:
    return home / "downloads" / target.source / fmt.filename(target.value)


def _history(home: Path) -> kv.KV:
    return kv.open(home / HISTORY_FILE, table=HISTORY_TABLE)


def _rows(history: MutableMapping[str, Any], folder: Path) -> list[tuple[str, dict]]:
    """The history rows whose file is in `folder`, or was."""
    root = folder.resolve()
    return [(key, row) for key, row in history.items()
            if isinstance(row, dict) and row.get("path") and Path(row["path"]).resolve().is_relative_to(root)]


def _repair(history: MutableMapping[str, Any], folder: Path, line: Line) -> None:
    """Remove leftover parts, forget files that are gone or broken, and rename files to match their content."""
    from shared import media

    # Parts an interrupted run left behind; nothing else is downloading into the folder yet. Every
    # temporary file flnd writes matches PART_GLOB (`files.part_for`), so this finds them all.
    for stale in folder.rglob(files.PART_GLOB) if folder.is_dir() else ():
        stale.unlink(missing_ok=True)
    rows = _rows(history, folder)
    with log.progress(f"repair {line.target}", total=len(rows)) as bar:
        for key, row in rows:
            bar.advance()
            path = Path(row["path"])
            if not path.is_file():
                line.lost[key] = path.name
                continue
            kind = media_kind(path.name)
            if kind is None:
                continue
            # The same check a download passes, so a file kept here would have been kept then.
            try:
                extension, broken = files.checked(path, kind), False
            except (files.NotMediaError, media.BrokenError):
                extension, broken = None, True
            except media.UnsafeError as error:
                # A file media refuses to open isn't known to be broken, so it is kept, and still
                # named by its first bytes. Files that can't be checked here (no ffmpeg) are kept by
                # `files.checked` itself.
                log.debug("{name}: not checked: {error}", name=path.name, error=error)
                extension, broken = media.sniff(path), False
            if broken:
                path.unlink()
                del history[key]
                line.lost[key] = path.name
                line.broken.add(key)
            elif extension and not net.same_extension(extension, path.suffix):
                renamed = path.with_suffix(extension)
                if renamed.exists():
                    log.warn("{target}: {name} is a {extension}, but {other} is taken; left as it is",
                             target=line.target, name=path.name, extension=extension, other=renamed.name)
                    continue
                path.rename(renamed)
                history[key] = {**row, "path": str(renamed)}
                line.repaired += 1


def _compress(history: MutableMapping[str, Any], folder: Path, options: Options, line: Line,
              videos: dict[str, bool], warned: set[type[Exception]]) -> None:
    """Compress the target's files that the options want compressed and that aren't yet, recording each.

    Once one file can't be compressed on this machine at all, no more videos are tried this run.
    """
    wanted = {PHOTO: options.compress_image, VIDEO: options.compress_video}
    todo = [(key, row, kind) for key, row in _rows(history, folder)
            if "compressed" not in row and (kind := media_kind(row["path"])) and wanted[kind]
            and Path(row["path"]).is_file()]
    if not todo:
        return
    with log.progress(f"compress {line.target}", total=len(todo)) as bar:
        for key, row, kind in todo:
            bar.advance()
            if kind == VIDEO and not videos["on"]:
                continue
            fields = compress_file(Path(row["path"]), kind, options, warned)
            if fields is None:
                # No ffmpeg, encoder or extra here; compress_file has said which. Photos go on.
                if videos["on"]:
                    log.warn("video compression stops for this run")
                videos["on"] = False
                continue
            history[key] = {**row, **fields}


def _tally(history: MutableMapping[str, Any], folder: Path, line: Line) -> None:
    rows = _rows(history, folder)
    gone = {key: Path(row["path"]).name for key, row in rows if not Path(row["path"]).is_file()}
    for key, name in line.lost.items():
        row = history.get(key)
        if isinstance(row, dict) and Path(row["path"]).is_file():
            line.repaired += 1
        else:
            gone[key] = name
    for key, name in sorted(gone.items(), key=lambda item: item[1]):
        if key in line.broken:
            log.warn("{target}: {name} was broken and couldn't be downloaded again", target=line.target, name=name)
        else:
            log.warn("{target}: {name} is missing; its post may be gone", target=line.target, name=name)
    line.missing = len(gone)
    on_disk = [path for path in folder.rglob("*") if path.is_file() and not path.name.startswith(".")] \
        if folder.is_dir() else []
    kinds = [media_kind(path.name) for path in on_disk]
    line.files, line.photos, line.videos = len(on_disk), kinds.count(PHOTO), kinds.count(VIDEO)
    line.size = sum(path.stat().st_size for path in on_disk)
    line.saved = sum(row["original_size"] - row["size"] for _, row in rows
                     if row.get("compressed") not in (None, "none") and "original_size" in row)


def _summary(lines: Sequence[Line]) -> None:
    total = Line("total")
    for name in COLUMNS[1:]:
        setattr(total, name, sum(getattr(line, name) for line in lines))
    table = [COLUMNS] + [(line.target, f"{line.files:,}", f"{line.photos:,}", f"{line.videos:,}", fmt.size(line.size),
                          fmt.size(line.saved), f"{line.repaired:,}", f"{line.missing:,}")
                         for line in [*lines, total]]
    widths = [max(len(row[column]) for row in table) for column in range(len(COLUMNS))]
    for row in table:
        log.info("{row}", row="  ".join(cell.ljust(width) for cell, width in zip(row, widths)).rstrip())
