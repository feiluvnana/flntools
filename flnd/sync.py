"""Each target's saved settings, `flnd download`'s use of them, and `flnd sync`: bring downloaded targets up to date.

`--image`, `--video` and `--audio` are saved per target in `.flnd/targets.db`, each on its own.
`download` reads a target's saved settings, puts the ones given on the command line on top,
compresses the target's files already saved with a setting that just changed (`compress.apply`),
saves the result (`record`), then runs the targets, grouped by their settings. `--from` and
`--time` describe one run, so they are never saved.

`run` is `flnd sync`: for each target it removes leftover `.part` files and forgets saved files
that are missing, empty or broken (repair), downloads what is new or was lost with the saved
settings and each source's default categories (fetch), then writes one line per target and the
totals. It changes no setting and compresses nothing already saved: that is `download`'s job. It
reads the whole history once per run (fixes #12), indexed by target folder.

    total = sync.download(engine, engine.resolve(["ins:example_user"]), Options(), {"image": Choice("webp", "mid")})
    lines = sync.run(engine, sync.plan(engine, []))     # every target downloaded so far

A folder downloaded before Phase 5 recorded settings has no row; it is synced without any, and
recorded then. A Phase 5 row (`compress_image: "balanced"`, `format_image: "webp"`) is read as
today's `image: "webp,mid"`, and its `categories`, `unpack`, `zip`, `since` and `until` are dropped.
"""

from __future__ import annotations

import datetime
import os
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Iterable, Iterator, Mapping, MutableMapping, Sequence

from flnd.common import compress, files
from flnd.common.compress import KINDS, Choice, Compressor
from flnd.engine import HISTORY_FILE, HISTORY_TABLE, Engine
from flnd.session import Options
from flnd.source import Target, TargetError
from shared import fmt, kv, log

TARGETS_FILE, TARGETS_TABLE = "targets.db", "targets"
# What is saved per target: --image, --video and --audio, each an `Options` field and a `targets.db` field.
SAVED = tuple(kind.option for kind in KINDS.values())

Choices = Mapping[str, "Choice | None"]


@dataclass
class Line:
    """One target's summary: what its folder holds, and what this sync repaired or couldn't get back."""

    target: str
    files: int = 0
    size: int = 0
    repaired: int = 0
    missing: int = 0
    # History keys whose file was gone, or broken and deleted, before the fetch, with their file names.
    lost: dict[str, str] = field(default_factory=dict, repr=False)
    broken: set[str] = field(default_factory=set, repr=False)


def download(engine: Engine, targets: Sequence[Target], options: Options, given: Choices) -> int:
    """`flnd download`'s work: each target with its saved settings and `given` on top, saved, applied, then run.

        total = sync.download(engine, targets, Options(since=date(2026, 1, 1)), {"image": Choice("webp", "mid")})

    Args:
        engine: the engine whose home holds `targets.db` and the history.
        targets: the targets, already resolved.
        options: the run's `--from` and `--time`; its `image`, `video` and `audio` are replaced
            by each target's settings.
        given: the settings given on the command line, by `SAVED` name; None is `off`. A name
            left out keeps the target's saved setting.

    Returns:
        How many files were written, over every group of targets.

    Raises:
        OSError: `targets.db` or the history can't be read or written.

    A target's saved settings that this run uses are shown first: `ins:example_user: --image
    webp,mid (saved)`. A setting `given` changes is applied to the target's files already saved
    before anything is downloaded, so its folder is never half one setting and half another;
    that is slow, and replaces originals with their smaller copies. A target's settings are saved
    only once that is done, so an apply cut short (Ctrl-C, a crash, a full disk) is finished by
    the next download with the same option.
    """
    rows = _recorded(engine.home)
    chosen: list[tuple[Target, Options]] = []
    changes: list[tuple[Target, str, Choice]] = []
    for target in targets:
        saved = _saved_choices(rows.get(_key(target), {}))
        _announce(target, {name: choice for name, choice in saved.items() if name not in given})
        chosen.append((target, _with(options, {**saved, **given})))
        changes += [(target, name, choice) for name, choice in given.items()
                    if choice is not None and choice != saved.get(name)]
    changed = {target for target, _, _ in changes}
    record(engine.home, [(target, options) for target, options in chosen if target not in changed], given=given)
    if changes:
        _apply(engine.home, changes, dict(chosen), given)
    return _fetch(engine, chosen)


def plan(engine: Engine, targets: Sequence[Target]) -> list[tuple[Target, Options]]:
    """Each target to sync, with its saved settings and each source's default categories.

        chosen = sync.plan(engine, engine.resolve(["ins:example_user"]))

    Args:
        engine: the engine whose home holds `targets.db` and `downloads/`, and whose sources count.
        targets: only these, already resolved, so a short link isn't followed a second time; when
            empty, every recorded target and every unrecorded folder under `downloads/<code>/`.

    Returns:
        (target, options) for each target, in the order given or recorded. Planning writes nothing.
    """
    rows = _recorded(engine.home)
    if not targets:
        targets = [Target(code, row["kind"], value) for key, row in rows.items()
                   for code, value in [key.split(":", 1)] if code in engine.sources]
        targets += list(_unrecorded(engine, targets))
    return [(target, _with(Options(), _saved_choices(rows.get(_key(target), {})))) for target in targets]


def record(home: Path, chosen: Sequence[tuple[Target, Options]], *, given: Iterable[str] = ()) -> None:
    """Save each target's `--image`, `--video` and `--audio` in `targets.db`, keyed `<code>:<value>`, replacing its row.

        sync.record(Path(".flnd"), [(target, Options(image=Choice("webp", "mid")))])

    Args:
        home: flnd's folder, `.flnd`, which holds `targets.db`; the file is created when missing.
        chosen: (target, options) for each target, the options already holding its saved
            settings and the ones given; `--from` and `--time` are never saved.
        given: the `SAVED` names given on the command line. Only these replace a saved value
            this flnd can't read (`compress.read_saved`); any other name keeps it as it is.

    Raises:
        OSError: `targets.db` can't be created or written.

    Each row also keeps the target's kind, so `plan` can rebuild it without resolving it again,
    and the time it was recorded, in UTC. A setting is saved as the command line writes it
    ("webp,mid"), or null when it is off.
    """
    given = set(given)
    if not chosen:
        return
    now = datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")
    with kv.open(home / TARGETS_FILE, table=TARGETS_TABLE) as saved:
        for target, options in chosen:
            old = saved.get(_key(target))
            old = old if isinstance(old, dict) else {}
            values = {}
            for name in SAVED:
                choice = getattr(options, name)
                if choice is None and name not in given and isinstance(old.get(name), str) and old[name]:
                    # Read as off only because this flnd can't read it (another version, or edited by
                    # hand): a readable value would be `choice`. Kept, so a run never erases it.
                    values[name] = old[name]
                else:
                    values[name] = choice and str(choice)
            saved[_key(target)] = {"kind": target.kind, **values, "at": now}


def run(engine: Engine, chosen: Sequence[tuple[Target, Options]]) -> list[Line]:
    """`flnd sync`: repair every chosen target, fetch what is new or was lost, and write one line each.

        lines = sync.run(engine, sync.plan(engine, []))

    Args:
        engine: the engine whose home holds the downloads and their history; its `failed` lists
            the targets whose fetch failed.
        chosen: (target, options) for each target, as `plan` gives them.

    Returns:
        One `Line` per chosen target, in the same order, counting what its folder holds after the sync.

    Raises:
        OSError: the history or `targets.db` can't be opened or written.

    Repair deletes broken files, which can't be undone; the fetch makes network requests and can
    take hours. Targets with the same settings are fetched together, so different sources run at
    once. Each target's row is recorded again, in today's form, without changing a setting.
    """
    record(engine.home, chosen)
    for target, options in chosen:
        _announce(target, {name: getattr(options, name) for name in SAVED})
    lines = [Line(str(target)) for target, _ in chosen]
    folders = [files.target_folder(engine.home, target) for target, _ in chosen]
    with _history(engine.home) as history:
        index = _index(history, folders)
        for line, folder in zip(lines, folders):
            _repair(history, folder, index[folder], line)
    log.info("Storage: {path}", path=(engine.home / "downloads").resolve())
    _fetch(engine, chosen)
    with _history(engine.home) as history:
        for line, folder in zip(lines, folders):
            _tally(history, folder, line)
    _summary(lines)
    return lines


def _recorded(home: Path) -> dict[str, Any]:
    """Every `targets.db` row by its `<code>:<value>` key; empty, without creating the file, when there is none."""
    if not (home / TARGETS_FILE).is_file():
        return {}
    with kv.open(home / TARGETS_FILE, table=TARGETS_TABLE) as saved:
        return dict(saved.items())


def _key(target: Target) -> str:
    return f"{target.source}:{target.value}"


def _saved_choices(row: Mapping[str, Any]) -> dict[str, Choice | None]:
    """A `targets.db` row's settings, by `SAVED` name, from today's form or Phase 5's.

    Phase 5 saved a preset and a format apart (`compress_image: "balanced"`, `format_image:
    "webp"`), with the old preset names; it had no audio. Its other fields are dropped.
    """
    choices: dict[str, Choice | None] = {}
    for kind, spec in KINDS.items():
        if spec.option in row:
            choices[spec.option] = compress.read_saved(kind, row[spec.option])
        elif level := row.get(f"compress_{spec.option}"):
            choices[spec.option] = Choice(row.get(f"format_{spec.option}") or None,
                                          compress.OLD_LEVELS.get(str(level), str(level)))
        else:
            choices[spec.option] = None
    return choices


def _with(options: Options, choices: Choices) -> Options:
    """`options` with the `SAVED` settings in `choices`; one `choices` leaves out is None."""
    return replace(options, image=choices.get("image"), video=choices.get("video"), audio=choices.get("audio"))


def _announce(target: Target, saved: Mapping[str, Choice | None]) -> None:
    """Say which saved settings a run of this target uses, so they never apply by surprise."""
    used = " ".join(f"--{name} {choice}" for name, choice in saved.items() if choice is not None)
    if used:
        log.info("{target}: {options} (saved)", target=str(target), options=used)


def _apply(home: Path, changes: Sequence[tuple[Target, str, Choice]], chosen: Mapping[Target, Options],
           given: Iterable[str]) -> None:
    """Compress each changed target's files already saved with its new setting, then save its settings.

    Args:
        home: flnd's folder, which holds the history and `targets.db`.
        changes: (target, `SAVED` name, new choice) for each setting `download` changes.
        chosen: each target's options, saved settings and given ones together, as `record` takes them.
        given: the `SAVED` names given on the command line, for `record`.

    Raises:
        OSError: the history or `targets.db` can't be written.
    """
    folders = {target: files.target_folder(home, target) for target, _, _ in changes}
    compressor = Compressor()
    with _history(home) as history:
        index = _index(history, list(folders.values()))
        for target in folders:
            for changed, name, choice in changes:
                if changed == target:
                    kind = next(kind for kind, spec in KINDS.items() if spec.option == name)
                    compress.apply(history, index[folders[target]], kind, choice, compressor, label=str(target))
            # Saved only now that every file has it: saved before, an apply cut short would never be
            # finished, since the next download with the same option would see no change.
            record(home, [(target, chosen[target])], given=given)


def _fetch(engine: Engine, chosen: Sequence[tuple[Target, Options]]) -> int:
    """Run the targets, those with the same options together, and return how many files were written."""
    groups: dict[Options, list[Target]] = {}
    for target, options in chosen:
        groups.setdefault(options, []).append(target)
    return sum(engine.run(targets, options) for options, targets in groups.items())


def _unrecorded(engine: Engine, known: Sequence[Target]) -> Iterator[Target]:
    """Folders under `downloads/<code>/` no row names: downloaded before Phase 5 recorded settings."""
    seen = {files.target_folder(engine.home, target) for target in known}
    downloads = engine.home / "downloads"
    for code in engine.sources:
        for folder in sorted((downloads / code).iterdir()) if (downloads / code).is_dir() else ():
            if folder.is_dir() and folder not in seen:
                try:
                    yield engine.resolve([f"{code}:{folder.name}"])[0]
                except TargetError as error:
                    log.warn("{folder}: not synced: {error}", folder=f"{code}/{folder.name}", error=error)


def _history(home: Path) -> kv.Store:
    return kv.open(home / HISTORY_FILE, table=HISTORY_TABLE)


def _index(history: MutableMapping[str, Any], folders: Iterable[Path]) -> dict[Path, list[tuple[str, dict]]]:
    """The history rows whose file is in each folder, or was, from one read of the whole history (fixes #12).

    With 100 targets and 100,000 rows, a scan per target and step was 30 million row reads; this
    is 100,000. Paths are compared made absolute from the working directory, as they were saved,
    without resolving links, which would cost a system call per row.
    """
    roots = {Path(os.path.abspath(folder)): folder for folder in folders}
    index: dict[Path, list[tuple[str, dict]]] = {folder: [] for folder in roots.values()}
    for key, row in history.items():
        if not (isinstance(row, dict) and row.get("path")):
            continue
        for parent in Path(os.path.abspath(row["path"])).parents:
            if parent in roots:
                index[roots[parent]].append((key, row))
                break
    return index


def _repair(history: MutableMapping[str, Any], folder: Path, rows: Sequence[tuple[str, dict]], line: Line) -> None:
    """Remove leftover parts, and forget the target's files that are gone, empty or broken, for the fetch to get again."""
    from shared import media

    # Parts an interrupted run left behind; nothing else is downloading into the folder yet. Every
    # temporary file flnd writes matches PART_GLOB (`shared.atomic`'s parts), so this finds them all.
    for stale in folder.rglob(files.PART_GLOB) if folder.is_dir() else ():
        stale.unlink(missing_ok=True)
    with log.progress(f"repair {line.target}", total=len(rows)) as bar:
        for key, row in rows:
            bar.advance()
            path = Path(row["path"])
            if not path.is_file():
                line.lost[key] = path.name
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
                line.lost[key] = path.name
                line.broken.add(key)


def _tally(history: MutableMapping[str, Any], folder: Path, line: Line) -> None:
    """Count what the folder holds after the fetch, and name each lost file the fetch couldn't get back."""
    gone: dict[str, str] = {}
    for key, name in line.lost.items():
        # One row each, not a scan: the fetch may have saved it again, under a new path.
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
    line.files, line.size = len(on_disk), sum(path.stat().st_size for path in on_disk)


def _summary(lines: Sequence[Line]) -> None:
    """One line per target, then the totals: `ins:example_user: 120 files, 85.2 MB; 2 repaired, 1 missing`."""
    for line in lines:
        log.info("{target}: {body}", target=line.target, body=_described(line))
    total = Line(fmt.count(len(lines), "target"), files=sum(line.files for line in lines),
                 size=sum(line.size for line in lines), repaired=sum(line.repaired for line in lines),
                 missing=sum(line.missing for line in lines))
    log.info("total: {targets}, {body}", targets=total.target, body=_described(total))


def _described(line: Line) -> str:
    return f"{fmt.count(line.files, 'file')}, {fmt.size(line.size)}; {line.repaired:,} repaired, {line.missing:,} missing"
