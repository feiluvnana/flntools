"""Each target's saved settings in `.flnd/metadata/targets.db`: `--shrink`, and its complete categories.

A row is keyed `<code>:<value>` and holds the target's kind (so `flnd sync` can rebuild it without
resolving it again), `shrink` (true or false), the time it was recorded, and `complete`: each
category whose listing last reached its end by itself, with the day it did
(plan/phase-6/redesign.md, A9 rule 3; `sync` stops at saved posts only with that record). `--from`
and `--time` describe one run, so they are never saved. `Engine.download` and `Engine.sync` decide
what to save; this module only reads and writes.

    saved = settings.shrink(settings.rows(home).get(settings.key(target), {}))
    settings.record(home, [(target, Options(shrink=True))])
    shown = settings.announce(target, saved)    # "--shrink (saved)"
"""

from __future__ import annotations

import datetime
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from flnd.common import files
from flnd.session import Options
from flnd.source import Target
from shared import kv, log

TARGETS_TABLE = "targets"
# The row field of `--shrink`: true or false.
SHRINK = "shrink"
# The row field of the categories that last listed to their end: {category: "YYYY-MM-DD"}.
COMPLETE = "complete"


def rows(home: Path) -> dict[str, Any]:
    """Every `targets.db` row by its `<code>:<value>` key; empty, without creating the file, when there is none.

        row = settings.rows(Path(".flnd")).get("ins:example_user", {})
    """
    if not (home / files.TARGETS).is_file():
        return {}
    with kv.open(home / files.TARGETS, table=TARGETS_TABLE) as saved:
        return dict(saved.items())


def key(target: Target) -> str:
    """A target's `targets.db` key: `ins:example_user`, the code and the value without the kind."""
    return f"{target.source}:{target.value}"


def shrink(row: Mapping[str, Any]) -> bool:
    """Whether a `targets.db` row saved `--shrink` on; False for a row without it, or {} for a target with none.

        settings.shrink({"shrink": True})      # True
    """
    return row.get(SHRINK) is True


def record(home: Path, chosen: Sequence[tuple[Target, Options]]) -> None:
    """Save each target's `--shrink` in `targets.db`, replacing its row in today's form.

        settings.record(Path(".flnd"), [(target, Options(shrink=True))])

    Args:
        home: flnd's folder, `.flnd`, which holds `targets.db`; the file is created when missing.
        chosen: (target, options) for each target, the options already holding its saved
            setting or the one given; `--from` and `--time` are never saved.

    Raises:
        OSError: `targets.db` can't be created or written.

    The row's `complete` record is kept: a setting changing doesn't change what was listed.
    """
    if not chosen:
        return
    now = datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")
    with kv.open(home / files.TARGETS, table=TARGETS_TABLE) as saved:
        for target, options in chosen:
            old = saved.get(key(target))
            complete = old.get(COMPLETE) if isinstance(old, dict) else None
            saved[key(target)] = {"kind": target.kind, SHRINK: options.shrink, "at": now,
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
    with kv.open(home / files.TARGETS, table=TARGETS_TABLE) as saved:
        old = saved.get(key(target))
        row: dict[str, Any] = dict(old) if isinstance(old, dict) else {"kind": target.kind}
        done = row.get(COMPLETE)
        row[COMPLETE] = {**(done if isinstance(done, dict) else {}), **{name: today for name in names}}
        saved[key(target)] = row


def announce(target: Target, shrink: bool) -> str:
    """Say when a run of this target shrinks by its saved setting, so it never applies by surprise, and return what was said.

        settings.announce(target, True)   # logs "ins:example_user: --shrink (saved)" and returns "--shrink (saved)"

    Args:
        target: the target, named at the start of the line.
        shrink: whether the saved `--shrink` this run uses is on.

    Returns:
        The setting as the line shows it, "" (and nothing logged) when it is off.
    """
    if not shrink:
        return ""
    log.info("{target}: --shrink (saved)", target=str(target))
    return "--shrink (saved)"
