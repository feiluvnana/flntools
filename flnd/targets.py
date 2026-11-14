"""Each target's saved settings in `.flnd/metadata/targets.db`: `--shrink` and categories.

A row is keyed `<code>:<value>` and holds the target's kind (so `flnd sync` can rebuild it without
resolving it again), `shrink` (true or false), the time it was recorded, and `complete`: each
category whose listing last reached its end by itself, with the day it did (`sync` stops at saved
posts only with that record). `--from` and `--time` describe one run, so they are never saved.
`Engine.download` and `Engine.sync` decide what to save; this module only reads and writes.

    saved = targets.shrink(targets.rows(home).get(targets.key(target), {}))
    targets.record(home, [(target, Options(shrink=True))])
    shown = targets.announce(target, saved)    # "--shrink (saved)"
"""

from __future__ import annotations

import datetime
from pathlib import Path
from typing import Any, Iterable, Mapping, MutableMapping, Sequence

from flnd.common import paths
from flnd.options import Options
from flnd.source import Target
from shared import kv, log

TARGETS_TABLE = "targets"
# The row field of `--shrink`: true or false.
SHRINK = "shrink"
# The row field of `--sync`: true or false (rows without it count as true).
SYNC = "sync"
# The row field of a synced query's seen target keys: ["src:1", ...].
SEEN = "seen"
# The row field of `--folder` templates: {category: template}.
FOLDERS = "folders"
# The row field of a target's parent query string, when discovered from a query.
QUERY = "query"
# The row field of the categories that last listed to their end: {category: "YYYY-MM-DD"}.
COMPLETE = "complete"
# The row field of the cursors interrupted listings saved, for the next run: {category: cursor}.
CURSORS = "cursors"


def rows(home: Path) -> dict[str, Any]:
    """Every `targets.db` row by its `<code>:<value>` key; empty when file does not exist.

        row = targets.rows(Path(".flnd")).get("ins:example_user", {})
    """
    if not (home / paths.TARGETS).is_file():
        return {}
    with kv.open(home / paths.TARGETS, table=TARGETS_TABLE) as saved:
        return dict(saved.items())


def key(target: Target) -> str:
    """A target's `targets.db` key: `ins:example_user`, the code and the value without the kind."""
    return f"{target.source}:{target.value}"


def folders(row: Mapping[str, Any]) -> tuple[tuple[str, str], ...]:
    """Saved `--folder` `(category, template)` pairs from a `targets.db` row."""
    found = row.get(FOLDERS)
    if not isinstance(found, dict):
        return ()
    return tuple((str(k), str(v)) for k, v in found.items())


def query_name(row: Mapping[str, Any]) -> str | None:
    """Saved parent query string from a `targets.db` row, or None."""
    found = row.get(QUERY)
    return str(found) if isinstance(found, str) and found else None


def shrink(row: Mapping[str, Any]) -> bool:
    """Whether a `targets.db` row saved `--shrink` on; False when absent or disabled.

        targets.shrink({"shrink": True})      # True
    """
    return row.get(SHRINK) is True


def synced(saved: Mapping[str, Any]) -> dict[str, Any]:
    """Rows in `saved` whose `sync` is on; legacy rows without a `sync` field count as on.

        targets.synced({"ins:a": {"kind": "user"}, "ins:b": {"kind": "user", "sync": False}})
    """
    return {name: row for name, row in saved.items()
            if isinstance(row, dict) and row.get(SYNC, True) is True}


def sync_enabled(row: Mapping[str, Any]) -> bool:
    """Whether a `targets.db` row has `sync` on; rows without `sync` count as on."""
    return row.get(SYNC, True) is True


def record(
    home: Path, chosen: Sequence[tuple[Target, Options]], *, sync: bool | None = None
) -> None:
    """Save each target's `--shrink` and `--sync` in `targets.db`, replacing its row.

        targets.record(Path(".flnd"), [(target, Options(shrink=True))], sync=True)

    Args:
        home: flnd's folder, `.flnd`, which holds `targets.db`; the file is created when missing.
        chosen: (target, options) for each target, the options already holding its saved
            setting or the one given; `--from` and `--time` are never saved.
        sync: True to mark targets synced, False to keep existing sync state (or False for new
            targets), None to keep existing state (defaulting new/legacy rows to True).

    Raises:
        OSError: `targets.db` can't be created or written.

    The row's `complete`, `cursors` and `seen` records are kept.
    """
    if not chosen:
        return
    now = datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")
    with kv.open(home / paths.TARGETS, table=TARGETS_TABLE) as saved:
        for target, options in chosen:
            old = saved.get(key(target))
            kept: dict[str, Any] = {
                name: old[name] for name in (COMPLETE, CURSORS)
                if isinstance(old, dict) and isinstance(old.get(name), dict) and old[name]
            }
            if isinstance(old, dict) and isinstance(old.get(SEEN), list) and old[SEEN]:
                kept[SEEN] = old[SEEN]
            if options.folders:
                kept[FOLDERS] = dict(options.folders)
            elif isinstance(old, dict) and isinstance(old.get(FOLDERS), dict) and old[FOLDERS]:
                kept[FOLDERS] = old[FOLDERS]
            if options.query:
                kept[QUERY] = options.query
            elif isinstance(old, dict) and isinstance(old.get(QUERY), str) and old[QUERY]:
                kept[QUERY] = old[QUERY]
            if sync is True:
                is_sync = True
            elif isinstance(old, dict):
                is_sync = old.get(SYNC, True) is True
            else:
                is_sync = sync is None
            saved[key(target)] = {"kind": target.kind, SHRINK: options.shrink,
                                  SYNC: is_sync, "at": now, **kept}


def toggle(home: Path, target: Target) -> bool:
    """Flip `target`'s `sync` state in `targets.db` and return its new state.

        new_state = targets.toggle(Path(".flnd"), Target("ins", "user", "example_user"))
    """
    return _write_sync(home, target, enabled=None)


def set_sync(home: Path, target: Target, *, enabled: bool) -> bool:
    """Set `target`'s `sync` state in `targets.db` to `enabled`."""
    return _write_sync(home, target, enabled=enabled)


def _write_sync(home: Path, target: Target, *, enabled: bool | None) -> bool:
    """Update `target`'s `sync` state in `targets.db`, flipping it when `enabled` is None."""
    now = datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")
    with kv.open(home / paths.TARGETS, table=TARGETS_TABLE) as saved:
        old = saved.get(key(target))
        row = (
            dict(old) if isinstance(old, dict)
            else {"kind": target.kind, SHRINK: False, "at": now}
        )
        new_state = not (row.get(SYNC, True) is True) if enabled is None else enabled
        row[SYNC] = new_state
        saved[key(target)] = row
    return new_state


def delete(home: Path, target: Target) -> bool:
    """Remove `target` from `targets.db`, returning True if it was present."""
    with kv.open(home / paths.TARGETS, table=TARGETS_TABLE) as saved:
        target_key = key(target)
        if target_key not in saved:
            return False
        del saved[target_key]
        return True


def seen(saved: Mapping[str, Any], target: Target) -> tuple[str, ...]:
    """The target keys already seen by a synced query target, in order.

    Args:
        saved: `targets.db`'s rows, as the run opened them.
        target: the query target whose `seen` keys are read.
    """
    found = _row(saved, target).get(SEEN)
    return tuple(str(item) for item in found) if isinstance(found, list) else ()


def mark_seen(home: Path, target: Target, keys: Iterable[str]) -> None:
    """Append `keys` to `target`'s `seen` list in `targets.db`, preserving order and deduping.

    Args:
        home: flnd's folder, `.flnd`.
        target: the query target whose `seen` list is updated.
        keys: target keys (`<code>:<value>`) to mark seen.
    """
    items = list(dict.fromkeys(keys))
    if not items:
        return
    now = datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")
    with kv.open(home / paths.TARGETS, table=TARGETS_TABLE) as saved:
        old = saved.get(key(target))
        row = (
            dict(old) if isinstance(old, dict)
            else {"kind": target.kind, SHRINK: False, SYNC: True, "at": now}
        )
        existing = row.get(SEEN)
        current = [str(x) for x in existing] if isinstance(existing, list) else []
        merged = list(dict.fromkeys([*current, *items]))
        row[SEEN] = merged
        saved[key(target)] = row


def complete(saved: MutableMapping[str, Any], target: Target, categories: Iterable[str]) -> None:
    """Record that target categories listed to their end today, for `sync` stop."""
    names = list(categories)
    if not names:
        return
    today = datetime.date.today().isoformat()
    row = _row(saved, target)
    done = row.get(COMPLETE)
    row[COMPLETE] = {**(done if isinstance(done, dict) else {}), **{name: today for name in names}}
    saved[key(target)] = row


def listed(saved: Mapping[str, Any], target: Target) -> dict[str, str]:
    """The categories of a target whose last listing reached its end, each with the day it did.

    Args:
        saved: `targets.db`'s rows, as the run opened them.
        target: the target whose row is read.
    """
    found = _row(saved, target).get(COMPLETE)
    return dict(found) if isinstance(found, dict) else {}


def cursors(saved: Mapping[str, Any], target: Target) -> dict[str, str]:
    """The cursors a target's interrupted listings saved, by category; empty when there is none.

    Args:
        saved: `targets.db`'s rows, as the run opened them.
        target: the target whose row is read.
    """
    found = _row(saved, target).get(CURSORS)
    if not isinstance(found, dict):
        return {}
    return {name: value for name, value in found.items() if isinstance(value, str)}


def keep_cursors(saved: MutableMapping[str, Any], target: Target,
                 changed: Mapping[str, str | None]) -> None:
    """Save each category's cursor in `changed` into `targets.db`'s rows, or forget it for None.

    Args:
        saved: `targets.db`'s rows, as the run opened them.
        target: the target whose row is written.
        changed: each category's cursor to keep, or None to forget it.
    """
    if not changed:
        return
    row = _row(saved, target)
    found = row.get(CURSORS)
    merged = {**(found if isinstance(found, dict) else {}), **changed}
    kept = {name: value for name, value in merged.items() if value}
    if kept:
        row[CURSORS] = kept
    else:
        row.pop(CURSORS, None)
    saved[key(target)] = row


def announce(target: Target, shrink: bool) -> str:
    """Announce saved `--shrink` setting when active, returning the logged message."""
    if not shrink:
        return ""
    log.info("{target}: --shrink (saved)", target=str(target))
    return "--shrink (saved)"


def _row(saved: Mapping[str, Any], target: Target) -> dict[str, Any]:
    """A copy of the target's row, or a new one holding its kind."""
    old = saved.get(key(target))
    return dict(old) if isinstance(old, dict) else {"kind": target.kind}
