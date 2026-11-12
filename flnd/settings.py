"""Each target's saved settings in `.flnd/targets.db`: `--image`, `--video`, `--audio` and `--classify`, and its complete categories.

A row is keyed `<code>:<value>` and holds the target's kind (so `flnd sync` can rebuild it without
resolving it again), each saved compress setting as the command line writes it ("webp,mid") or
null when it is off, `classify` as true or false, the time it was recorded, and `complete`: each category whose listing last reached its end
by itself, with the day it did (plan/phase-6/redesign.md, A9 rule 3; `sync` stops at saved posts
only with that record). `--from` and `--time` describe one run, so they are never
saved. `Engine.download` and `Engine.sync` decide what to save; this module only reads and writes.

    saved = settings.choices(settings.rows(home).get(settings.key(target), {}))
    settings.record(home, [(target, Options(image=Choice("webp", "mid"), classify=True))], given={"image"})
    shown = settings.announce(target, saved, classify=True)   # "--image webp,mid --classify (saved)"

A Phase 5 row (`compress_image: "balanced"`, `format_image: "webp"`) is read as today's
`image: "webp,mid"`, and its `categories`, `unpack`, `zip`, `since` and `until` are dropped when
it is recorded again.
"""

from __future__ import annotations

import datetime
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from flnd.common import compress
from flnd.common.compress import KINDS, Choice
from flnd.session import Options
from flnd.source import Target
from shared import kv, log

TARGETS_FILE, TARGETS_TABLE = "targets.db", "targets"
# What is saved per target: --image, --video and --audio, each an `Options` field and a `targets.db` field.
SAVED = tuple(kind.option for kind in KINDS.values())
# --classify, saved beside them as true or false: the `Options` field and the `targets.db` field.
CLASSIFY = "classify"
# The row field of the categories that last listed to their end: {category: "YYYY-MM-DD"}.
COMPLETE = "complete"


def rows(home: Path) -> dict[str, Any]:
    """Every `targets.db` row by its `<code>:<value>` key; empty, without creating the file, when there is none.

        row = settings.rows(Path(".flnd")).get("ins:example_user", {})
    """
    if not (home / TARGETS_FILE).is_file():
        return {}
    with kv.open(home / TARGETS_FILE, table=TARGETS_TABLE) as saved:
        return dict(saved.items())


def key(target: Target) -> str:
    """A target's `targets.db` key: `ins:example_user`, the code and the value without the kind."""
    return f"{target.source}:{target.value}"


def choices(row: Mapping[str, Any]) -> dict[str, Choice | None]:
    """A `targets.db` row's settings, by `SAVED` name, from today's form or Phase 5's; None is off.

        settings.choices({"image": "webp,mid", "video": None})     # {"image": Choice("webp", "mid"), "video": None, "audio": None}

    Args:
        row: the row, or {} for a target with none.

    Phase 5 saved a preset and a format apart (`compress_image: "balanced"`, `format_image:
    "webp"`), with the old preset names; it had no audio. A value this flnd can't read is warned of
    and read as off (`compress.read_saved`); `record` keeps it.
    """
    found: dict[str, Choice | None] = {}
    for kind, spec in KINDS.items():
        if spec.option in row:
            found[spec.option] = compress.read_saved(kind, row[spec.option])
        elif level := row.get(f"compress_{spec.option}"):
            found[spec.option] = Choice(row.get(f"format_{spec.option}") or None,
                                        compress.OLD_LEVELS.get(str(level), str(level)))
        else:
            found[spec.option] = None
    return found


def classify(row: Mapping[str, Any]) -> bool:
    """Whether a `targets.db` row has `--classify` on; False for a row without it, as every one before it was.

        settings.classify({"image": "webp,mid", "classify": True})     # True
    """
    return row.get(CLASSIFY) is True


def record(home: Path, chosen: Sequence[tuple[Target, Options]], *, given: Iterable[str] = ()) -> None:
    """Save each target's `--image`, `--video`, `--audio` and `--classify` in `targets.db`, replacing its row in today's form.

        settings.record(Path(".flnd"), [(target, Options(image=Choice("webp", "mid")))])

    Args:
        home: flnd's folder, `.flnd`, which holds `targets.db`; the file is created when missing.
        chosen: (target, options) for each target, the options already holding its saved
            settings and the ones given; `--from` and `--time` are never saved.
        given: the `SAVED` names given on the command line. Only these replace a saved value
            this flnd can't read (`compress.read_saved`); any other name keeps it as it is.

    Raises:
        OSError: `targets.db` can't be created or written.

    The row's `complete` record is kept: a setting changing doesn't change what was listed.
    """
    given = set(given)
    if not chosen:
        return
    now = datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")
    with kv.open(home / TARGETS_FILE, table=TARGETS_TABLE) as saved:
        for target, options in chosen:
            old = saved.get(key(target))
            old = old if isinstance(old, dict) else {}
            values: dict[str, Any] = {}
            for name in SAVED:
                choice = getattr(options, name)
                if choice is None and name not in given and isinstance(old.get(name), str) and old[name]:
                    # Read as off only because this flnd can't read it (another version, or edited by
                    # hand): a readable value would be `choice`. Kept, so a run never erases it.
                    values[name] = old[name]
                else:
                    values[name] = choice and str(choice)
            values[CLASSIFY] = options.classify
            complete = old.get(COMPLETE)
            saved[key(target)] = {"kind": target.kind, **values, "at": now,
                                  **({COMPLETE: complete} if isinstance(complete, dict) and complete else {})}


def complete(home: Path, target: Target, categories: Iterable[str]) -> None:
    """Record that each of a target's categories listed to its end today, for `sync`'s saved-run stop.

        settings.complete(Path(".flnd"), target, ["posts", "reels"])

    Args:
        home: flnd's folder; `targets.db` is created when missing.
        target: the target whose categories ended by themselves, not by Ctrl-C, an error or an early stop.
        categories: their names; nothing is written when there is none.

    Raises:
        OSError: `targets.db` can't be written.
    """
    names = list(categories)
    if not names:
        return
    today = datetime.date.today().isoformat()
    with kv.open(home / TARGETS_FILE, table=TARGETS_TABLE) as saved:
        old = saved.get(key(target))
        row: dict[str, Any] = dict(old) if isinstance(old, dict) else {"kind": target.kind}
        done = row.get(COMPLETE)
        row[COMPLETE] = {**(done if isinstance(done, dict) else {}), **{name: today for name in names}}
        saved[key(target)] = row


def announce(target: Target, saved: Mapping[str, Choice | None], *, classify: bool = False) -> str:
    """Say which saved settings a run of this target uses, so they never apply by surprise, and return what was said.

        settings.announce(target, {"image": Choice("webp", "mid")})   # logs and returns "--image webp,mid (saved)"

    Args:
        target: the target, named at the start of the line.
        saved: the saved compress settings this run uses, by `SAVED` name; None ones are left out.
        classify: whether the run uses a saved `--classify`, shown last.

    Returns:
        The settings as the line shows them, "" (and nothing logged) when none is used.
    """
    used = " ".join([*(f"--{name} {choice}" for name, choice in saved.items() if choice is not None),
                     *(["--classify"] if classify else [])])
    if not used:
        return ""
    log.info("{target}: {options} (saved)", target=str(target), options=used)
    return f"{used} (saved)"
