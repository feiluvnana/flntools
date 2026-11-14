"""Each target's options, and the settings every source of a run shares.

`Options` is what one target's run downloads and how it compresses it; `RunShared` is what the run
shares across its sources (flnd's folder, the history, `targets.db` and the compressor). The engine
reads both, the job and the sources' sites through them, and `Engine.download` checks `--from` with
the helpers here.
"""

from __future__ import annotations

import datetime
import re
from dataclasses import dataclass, field
from pathlib import Path
from string import Formatter
from typing import Any, Mapping, MutableMapping, Sequence

from flnd.common.compress import Compressor
from flnd.source import Source, Target

# `--from all` takes every category of every source.
ALL = "all"
COMMON_FIELDS = frozenset({
    "source", "query", "target", "id", "category", "path", "name",
    "date", "extension", "num", "count", "index",
})
FOLDER_FIELDS = COMMON_FIELDS | frozenset({
    "title", "order", "author", "genres", "chapters", "album", "part", "chat", "n",
})
_FORMATTER = Formatter()
_DRIVE_RE = re.compile(r"^[A-Za-z]:")


@dataclass(frozen=True)
class Options:
    """What one target's run downloads and how: `Options(categories=("reels",), shrink=True)`."""

    # `--from`: None (for single-post targets) or "all" means every category of each source.
    categories: tuple[str, ...] | None = None
    # `--time`'s two days, both included; None is no limit on that side.
    since: datetime.date | None = None
    until: datetime.date | None = None
    # `--shrink`: compress each new photo, video and audio file at balanced level in its format.
    shrink: bool = False
    # `sync` of a target with nothing lost: a `newest_first` category whose last listing reached
    # its end stops after 5 saved posts in a row.
    stop_at_saved: bool = False
    # `--limit`: max discovered targets on a target-yielding route, or max posts on a normal target.
    limit: int | None = None
    # `--again`: ignore the new-post-only check and saved cursors, running from start to finish.
    again: bool = False
    # `--folder`: `(category, template)` pairs relative to `.flnd/media/`.
    folders: tuple[tuple[str, str], ...] = ()
    # Parent query string when this target was discovered from a `?query` route.
    query: str | None = None


def selected(options: Options, source: type[Source]) -> tuple[str, ...]:
    """The categories `--from` takes from one source, in the source's order.

        selected(Options(categories=("reels", "media")), Instagram)     # ("reels",)

    Without `--from` (for single-post targets), or with `all`, it is every category.
    """
    names = options.categories
    if names is None or ALL in names:
        return source.categories
    return tuple(name for name in source.categories if name in names)


def folder_for(options: Options, category: str) -> str | None:
    """The `--folder` template for `category`, falling back to `all`, or None."""
    rules = dict(options.folders)
    return rules.get(category) or rules.get(ALL)


def unknown_categories(names: Sequence[str], sources: Sequence[type[Source]]) -> str | None:
    """Why `--from names` cannot be used with these sources; None when it can."""
    known = {category for source in sources for category in source.categories}
    unknown = [name for name in names if name != ALL and name not in known]
    if not unknown:
        return None
    lists = "; ".join(f"{source.code}: {', '.join(source.categories)}" for source in sources)
    return f"no source here has {', '.join(map(repr, unknown))}: use all, or {lists}"


def single_targets(targets: Sequence[Target], sources: Mapping[str, type[Source]]) -> str | None:
    """Why `--from` cannot be used with these targets; None when it can."""
    for target in targets:
        if target.kind in sources[target.source].single_kinds:
            return (
                f"--from applies to profiles and chats; {target} is one post, downloaded whole: "
                "give it without --from"
            )
    return None


def parse_categories(
    categories: str | Sequence[str] | None,
    targets: Sequence[Target],
    sources: Mapping[str, type[Source]],
) -> tuple[str, ...] | None:
    """Validate and normalize `--from` against `targets`, requiring it for non-single targets."""
    by_source = categories_by_source(categories, targets, sources)
    merged = tuple(dict.fromkeys(cat for cats in by_source.values() if cats for cat in cats))
    return merged or None if categories is None else merged


def categories_by_source(
    categories: str | Sequence[str] | None,
    targets: Sequence[Target],
    sources: Mapping[str, type[Source]],
) -> dict[str, tuple[str, ...] | None]:
    """Validate `--from` (plain or `<source>:<cats>`) and return categories per target source."""
    target_sources = (
        list(dict.fromkeys(sources[target.source] for target in targets))
        or list(sources.values())
    )
    by_code = {s.code: s for s in target_sources}
    if categories is None:
        if any(t.kind not in sources[t.source].single_kinds for t in targets):
            lists = "; ".join(f"{s.code}: {', '.join(s.categories)}" for s in target_sources)
            raise ValueError(f"--from is required (use all, or {lists})")
        return {s.code: None for s in target_sources}
    items = [categories] if isinstance(categories, str) else list(categories)
    plain, scoped = _split_category_items(items, by_code, sources)
    if not plain and not scoped:
        raise ValueError("--from must name at least one category, or all")
    if problem := single_targets(targets, sources):
        raise ValueError(problem)
    if plain and (problem := unknown_categories(plain, target_sources)):
        raise ValueError(problem)
    return _resolve_source_categories(target_sources, plain, scoped)


def _split_category_items(
    items: Sequence[str], by_code: Mapping[str, type[Source]], sources: Mapping[str, type[Source]],
) -> tuple[list[str], dict[str, list[str]]]:
    """Split `--from` tokens into plain names and `<source>:<cat>` buckets."""
    plain: list[str] = []
    scoped: dict[str, list[str]] = {}
    for item in items:
        code, sep, rest = item.partition(":")
        if sep and code.strip() in sources:
            src_code = code.strip()
            if src_code not in by_code:
                raise ValueError(f"--from {item!r}: no target from source {src_code!r}")
            cats = [c.strip() for c in rest.split(",") if c.strip()]
            if not cats:
                raise ValueError(f"--from {item!r} must name at least one category, or all")
            scoped.setdefault(src_code, []).extend(cats)
        else:
            plain.extend(c.strip() for c in item.split(",") if c.strip())
    return plain, scoped


def _resolve_source_categories(
    target_sources: Sequence[type[Source]],
    plain: Sequence[str],
    scoped: Mapping[str, Sequence[str]],
) -> dict[str, tuple[str, ...] | None]:
    """Assign validated categories to each target source."""
    result: dict[str, tuple[str, ...] | None] = {}
    for source in target_sources:
        own_scoped = tuple(dict.fromkeys(scoped.get(source.code, ())))
        if own_scoped and (problem := unknown_categories(own_scoped, [source])):
            raise ValueError(problem)
        names = tuple(dict.fromkeys([*plain, *own_scoped]))
        if not names:
            raise ValueError(
                f"--from is required for {source.code} (use all, or {', '.join(source.categories)})"
            )
        if ALL not in names and not any(cat in names for cat in source.categories):
            raise ValueError(
                f"{source.code} has none of {', '.join(map(repr, names))}: "
                f"use all, or {source.code}: {', '.join(source.categories)}"
            )
        result[source.code] = names
    return result


def parse_folders(
    folders: str | Sequence[str] | None,
    targets: Sequence[Target],
    sources: Mapping[str, type[Source]],
    selected_categories: tuple[str, ...] | None = None,
) -> tuple[tuple[str, str], ...]:
    """Validate `--folder <category>:<template>` entries before any run starts."""
    by_source = folders_by_source(
        folders, targets, sources,
        {sources[t.source].code: selected_categories for t in targets},
    )
    merged: dict[str, str] = {}
    for pairs in by_source.values():
        merged.update(pairs)
    return tuple(merged.items())


def folders_by_source(
    folders: str | Sequence[str] | None,
    targets: Sequence[Target],
    sources: Mapping[str, type[Source]],
    selected_by_source: Mapping[str, tuple[str, ...] | None] | None = None,
) -> dict[str, tuple[tuple[str, str], ...]]:
    """Validate `--folder` (`<cat>:<tpl>` or `<source>:<cat>:<tpl>`) per target source."""
    if not folders:
        return {}
    for target in targets:
        if target.kind in sources[target.source].single_kinds:
            raise ValueError(
                f"--folder applies to profiles, chats, and queries; {target} is one post, "
                "downloaded whole"
            )
    target_sources = (
        list(dict.fromkeys(sources[target.source] for target in targets))
        or list(sources.values())
    )
    by_code = {s.code: s for s in target_sources}
    items = [folders] if isinstance(folders, str) else list(folders)
    parsed: dict[str, dict[str, str]] = {s.code: {} for s in target_sources}
    selected_map = selected_by_source or {}
    for raw in items:
        _add_folder_rule(raw, target_sources, by_code, sources, selected_map, parsed)
    return {code: tuple(rules.items()) for code, rules in parsed.items() if rules}


def _add_folder_rule(
    raw: str,
    target_sources: Sequence[type[Source]],
    by_code: Mapping[str, type[Source]],
    sources: Mapping[str, type[Source]],
    selected_by_source: Mapping[str, tuple[str, ...] | None],
    parsed: dict[str, dict[str, str]],
) -> None:
    """Parse one `--folder` rule and validate it against its target source(s)."""
    first, sep, rest = raw.partition(":")
    first = first.strip()
    if sep and first in sources:
        if first not in by_code:
            raise ValueError(f"--folder {raw!r}: no target from source {first!r}")
        category, sep2, template = rest.partition(":")
        matching = [by_code[first]]
        syntax = "<source>:<category>:<template>"
    else:
        category, sep2, template = first, sep, rest
        matching = target_sources
        syntax = "<category>:<template>"
    category, template = category.strip(), template.strip()
    if not sep2 or not category or not template:
        raise ValueError(f"--folder {raw!r} must use {syntax} syntax")
    known = {cat for src in matching for cat in src.categories}
    if category != ALL and category not in known:
        lists = "; ".join(f"{s.code}: {', '.join(s.categories)}" for s in matching)
        raise ValueError(f"--folder: no source here has {category!r}: use all, or {lists}")
    for source in matching:
        if category != ALL and category not in source.categories:
            continue
        chosen = selected_by_source.get(source.code)
        if chosen is not None and ALL not in chosen and category != ALL and category not in chosen:
            raise ValueError(f"--folder {category!r} is excluded by --from")
        _validate_template(template, source)
        parsed[source.code][category] = template


def _validate_template(template: str, source: type[Source] | None = None) -> None:
    """Ensure `template` is a strict relative path under `media/` with valid placeholders."""
    if "\\" in template or template.startswith("/") or _DRIVE_RE.match(template):
        raise ValueError(f"--folder template {template!r} must be relative using '/' separators")
    segments = template.split("/")
    if any(seg in ("", ".", "..") for seg in segments):
        raise ValueError(f"--folder template {template!r} cannot contain empty, '.', or '..' parts")
    try:
        fields_iter = list(_FORMATTER.parse(template))
    except ValueError as error:
        raise ValueError(f"--folder template {template!r} is invalid: {error}") from None
    allowed_fields = (
        COMMON_FIELDS | frozenset(source.fields) if source is not None else FOLDER_FIELDS
    )
    for _, field_name, format_spec, conversion in fields_iter:
        if field_name is None:
            continue
        if not field_name or conversion is not None or format_spec:
            raise ValueError(
                f"--folder template {template!r} has invalid placeholder {{{field_name}}}"
            )
        if field_name not in allowed_fields:
            allowed = ", ".join(sorted(allowed_fields))
            who = f" for {source.code}" if source is not None else ""
            raise ValueError(
                f"--folder template {template!r} has unknown field {{{field_name}}}{who}; "
                f"use one of: {allowed}"
            )


@dataclass
class RunShared:
    """What all sources of a run share: flnd's folder, the history, `targets.db`, compressing."""

    # flnd's folder, where the downloads, history, logins and settings are kept.
    home: Path
    # The history of downloads, one row per file key, shared by every source of the run.
    history: MutableMapping[str, Any]
    # `targets.db`'s rows, opened once for the run: each target's complete categories and cursors.
    targets: MutableMapping[str, Any] = field(default_factory=dict)
    # Whether a person can be asked a question, which a source's own login needs.
    interactive: bool = False
    # `--dry`: list what would be downloaded without writing or downloading anything.
    dry: bool = False
    # Shared across the run so videos encode sequentially and each error warns once.
    compressor: Compressor = field(default_factory=Compressor)
    # Shared single-window browser pool entered by `site.shared_browser`, or None outside a run.
    _browser_pool: Any = field(default=None, repr=False)
