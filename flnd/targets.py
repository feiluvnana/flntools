"""Target settings in `.flnd/metadata/targets.db` and up-front route/category validation."""

from __future__ import annotations

import datetime
import re
from pathlib import Path
from string import Formatter
from typing import Any, Iterable, Mapping, MutableMapping, Sequence

from flnd.common import paths
from flnd.options import Options
from flnd.source import Source, Target
from shared import cli, store

TARGETS_TABLE = "targets"
SYNC = "sync"
SEEN = "seen"
QUERY = "query"
COMPLETE = "complete"
CURSORS = "cursors"
ALL = "all"

COMMON_FIELDS = frozenset({
    "source", "site", "query", "target", "user", "id", "category", "path",
    "name", "date", "extension", "num", "count", "index",
})
FOLDER_FIELDS = COMMON_FIELDS | frozenset({
    "title", "order", "author", "genres", "chapters", "album", "part", "chat", "n",
})
_FORMATTER = Formatter()
_DRIVE_RE = re.compile(r"^[A-Za-z]:")


def rows(home: Path) -> dict[str, Any]:
    """Return every `targets.db` row by `<code>:<value>` key, or `{}` if missing."""
    if not (home / paths.TARGETS).is_file():
        return {}
    with store.open(home / paths.TARGETS, table=TARGETS_TABLE) as saved:
        return dict(saved.items())


def key(target: Target) -> str:
    """Return a target's `targets.db` key (`<code>:<value>`)."""
    return f"{target.source}:{target.value}"


def query_name(row: Mapping[str, Any]) -> str | None:
    """Return the saved parent query string from a `targets.db` row, or None."""
    found = row.get(QUERY)
    return str(found) if isinstance(found, str) and found else None


def synced(saved: Mapping[str, Any]) -> dict[str, Any]:
    """Return rows in `saved` whose `sync` is enabled (defaulting missing to True)."""
    return {name: row for name, row in saved.items()
            if isinstance(row, dict) and row.get(SYNC, True) is True}


def sync_enabled(row: Mapping[str, Any]) -> bool:
    """Return whether a `targets.db` row has `sync` enabled."""
    return row.get(SYNC, True) is True


def record(
    home: Path, chosen: Sequence[tuple[Target, Options]], *, sync: bool | None = None
) -> None:
    """Save each target's settings and `--sync` in `targets.db`, keeping progress state."""
    if not chosen:
        return
    now = datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")
    with store.open(home / paths.TARGETS, table=TARGETS_TABLE) as saved:
        for target, options in chosen:
            old = saved.get(key(target))
            kept = _kept_fields(old, options)
            is_sync = _next_sync(old, sync)
            saved[key(target)] = {
                "kind": target.kind, SYNC: is_sync, "at": now, **kept,
            }


def _kept_fields(old: Any, options: Options) -> dict[str, Any]:
    """Extract preserved `complete`, `cursors`, `seen`, and `query` fields for `record`."""
    kept: dict[str, Any] = {}
    if isinstance(old, dict):
        for name in (COMPLETE, CURSORS):
            if isinstance(old.get(name), dict) and old[name]:
                kept[name] = old[name]
        if isinstance(old.get(SEEN), list) and old[SEEN]:
            kept[SEEN] = old[SEEN]
    if options.query:
        kept[QUERY] = options.query
    elif isinstance(old, dict) and isinstance(old.get(QUERY), str) and old[QUERY]:
        kept[QUERY] = old[QUERY]
    return kept


def _next_sync(old: Any, sync: bool | None) -> bool:
    """Compute the updated `sync` flag for `record`."""
    if sync is True:
        return True
    if isinstance(old, dict):
        return old.get(SYNC, True) is True
    return sync is None


def toggle(home: Path, target: Target) -> bool:
    """Flip `target`'s `sync` state in `targets.db` and return its new state."""
    return _write_sync(home, target, enabled=None)


def set_sync(home: Path, target: Target, *, enabled: bool) -> bool:
    """Set `target`'s `sync` state in `targets.db` to `enabled`."""
    return _write_sync(home, target, enabled=enabled)


def _write_sync(home: Path, target: Target, *, enabled: bool | None) -> bool:
    """Update `target`'s `sync` state in `targets.db`, flipping when `enabled` is None."""
    now = datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")
    with store.open(home / paths.TARGETS, table=TARGETS_TABLE) as saved:
        old = saved.get(key(target))
        row: dict[str, Any] = (dict(old) if isinstance(old, dict)
               else {"kind": target.kind, "at": now})
        new_state = not (row.get(SYNC, True) is True) if enabled is None else enabled
        row[SYNC] = new_state
        saved[key(target)] = row
    return new_state


def delete(home: Path, target: Target) -> bool:
    """Remove `target` from `targets.db`, returning True if it was present."""
    with store.open(home / paths.TARGETS, table=TARGETS_TABLE) as saved:
        target_key = key(target)
        if target_key not in saved:
            return False
        del saved[target_key]
        return True


def seen(saved: Mapping[str, Any], target: Target) -> tuple[str, ...]:
    """Return the target keys already seen by a synced query target, in order."""
    found = _row(saved, target).get(SEEN)
    return tuple(str(item) for item in found) if isinstance(found, list) else ()


def mark_seen(home: Path, target: Target, keys: Iterable[str]) -> None:
    """Append `keys` to `target`'s `seen` list in `targets.db`, preserving order."""
    items = list(dict.fromkeys(keys))
    if not items:
        return
    now = datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")
    with store.open(home / paths.TARGETS, table=TARGETS_TABLE) as saved:
        old = saved.get(key(target))
        row = (dict(old) if isinstance(old, dict)
               else {"kind": target.kind, SYNC: True, "at": now})
        existing = row.get(SEEN)
        current = [str(x) for x in existing] if isinstance(existing, list) else []
        row[SEEN] = list(dict.fromkeys([*current, *items]))
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
    """Return each category of `target` whose last listing reached its end and its date."""
    found = _row(saved, target).get(COMPLETE)
    return dict(found) if isinstance(found, dict) else {}


def cursors(saved: Mapping[str, Any], target: Target) -> dict[str, str]:
    """Return saved cursors for `target`'s interrupted listings, by category."""
    found = _row(saved, target).get(CURSORS)
    if not isinstance(found, dict):
        return {}
    return {name: value for name, value in found.items() if isinstance(value, str)}


def keep_cursors(
    saved: MutableMapping[str, Any], target: Target, changed: Mapping[str, str | None]
) -> None:
    """Save each category's cursor in `changed` into `targets.db`, or drop it for None."""
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


def validate(
    sources: Mapping[str, type[Source]] | Sequence[type[Source]],
    from_selection: str | Sequence[str] | None = None,
    targets: Sequence[Target] = (),
) -> None:
    """Validate `--from` categories and `@target` folder templates before any run work."""
    by_code = ({code: src for code, src in sources.items()} if isinstance(sources, Mapping)
               else {src.code: src for src in sources})
    _validate_folders(list(by_code.values()))
    if from_selection is not None:
        _validate_from(by_code, from_selection, targets)


def _validate_folders(sources: Sequence[type[Source]]) -> None:
    """Validate every source, route, and category `folder` template."""
    for source in sources:
        allowed = COMMON_FIELDS | FOLDER_FIELDS | frozenset(source.fields)
        for route in source.routes:
            route_fields = allowed | _route_fields(route)
            folder = getattr(route, "folder", None)
            _check_template(folder if folder is not None else source.folder, route_fields, source)
            for category in route.categories:
                if category.folder is not None:
                    _check_template(category.folder, route_fields, source)


def _route_fields(route: Any) -> frozenset[str]:
    """Collect extra placeholder names declared by a route's patterns or params."""
    names: set[str] = set()
    for pat in getattr(route, "_patterns", ()):
        names.update(getattr(pat, "names", ()))
    names.update(getattr(route, "params", {}).keys())
    return frozenset(names)


def _check_template(template: str, allowed: frozenset[str], source: type[Source]) -> None:
    """Raise `cli.UsageError` if `template` escapes paths or uses invalid placeholders."""
    if not template:
        return
    if "\\" in template or template.startswith("/") or _DRIVE_RE.match(template):
        raise cli.UsageError(f"folder template {template!r} must be relative using '/' separators")
    if any(seg in ("", ".", "..") for seg in template.split("/")):
        raise cli.UsageError(f"folder template {template!r} cannot contain empty, '.', or '..'")
    try:
        parsed = list(_FORMATTER.parse(template))
    except ValueError as error:
        raise cli.UsageError(f"folder template {template!r} is invalid: {error}") from None
    for _, field_name, format_spec, conversion in parsed:
        if field_name is None:
            continue
        if not field_name or conversion is not None or format_spec:
            raise cli.UsageError(
                f"folder template {template!r} has invalid placeholder {{{field_name}}}"
            )
        if field_name not in allowed:
            raise cli.UsageError(
                f"folder template {template!r} has unknown field {{{field_name}}} for {source.code}"
            )


def _validate_from(
    by_code: Mapping[str, type[Source]],
    from_selection: str | Sequence[str],
    targets: Sequence[Target],
) -> None:
    """Validate `--from` category names against target routes' `from_` sets."""
    raw = [from_selection] if isinstance(from_selection, str) else list(from_selection)
    if not raw:
        return
    routes = _target_routes(by_code, targets)
    for target, route in routes:
        if not _route_from(route) and not route.discovers:
            raise cli.UsageError(
                f"--from applies to profiles and chats; {target} is one post, downloaded whole"
            )
    if routes:
        known = {cat for target, route in routes for cat in (
            {c for r in by_code[target.source].routes for c in _route_from(r)}
            if route.discovers else _route_from(route)
        )}
    else:
        known = {cat for src in by_code.values() for route in src.routes
                 for cat in _route_from(route)}
    for entry in raw:
        _check_from_entry(entry, by_code, known)


def _check_from_entry(
    entry: str, by_code: Mapping[str, type[Source]], known: set[str]
) -> None:
    """Validate one `--from` entry (`<cat>` or `<source>:<cats>`)."""
    code, sep, rest = entry.partition(":")
    if sep and code in by_code:
        src_cats = {cat for route in by_code[code].routes for cat in _route_from(route)}
        names = [part.strip() for part in rest.split(",") if part.strip()]
        bad = [name for name in names if name != ALL and name not in src_cats]
    else:
        names = [part.strip() for part in entry.split(",") if part.strip()]
        bad = [name for name in names if name != ALL and name not in known]
    if not names:
        raise cli.UsageError(f"--from {entry!r} has no category name")
    if bad:
        raise cli.UsageError(f"unknown --from category {', '.join(map(repr, bad))}")


def _target_routes(
    by_code: Mapping[str, type[Source]], targets: Sequence[Target]
) -> list[tuple[Target, Any]]:
    """Pair each target with its matching `Route`."""
    pairs: list[tuple[Target, Any]] = []
    for target in targets:
        source = by_code.get(target.source)
        if source is None:
            continue
        route = next((r for r in source.routes if r.kind == target.kind), None)
        if route is not None:
            pairs.append((target, route))
    return pairs


def _route_from(route: Any) -> frozenset[str]:
    """Return a route's `from_` category set, falling back to declared category names."""
    declared = getattr(route, "from_", None)
    if declared is not None:
        return frozenset(declared)
    return frozenset(category.name for category in route.categories)


def _row(saved: Mapping[str, Any], target: Target) -> dict[str, Any]:
    """Return a copy of the target's row, or a new one holding its kind."""
    old = saved.get(key(target))
    return dict(old) if isinstance(old, dict) else {"kind": target.kind}
