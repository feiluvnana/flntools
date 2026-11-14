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
FOLDER_FIELDS = frozenset({
    "source", "query", "target", "id", "category", "path", "name",
    "title", "order", "date", "extension", "num", "count", "index",
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
    target_sources = (
        list(dict.fromkeys(sources[target.source] for target in targets))
        or list(sources.values())
    )
    if categories is None:
        if any(t.kind not in sources[t.source].single_kinds for t in targets):
            lists = "; ".join(f"{s.code}: {', '.join(s.categories)}" for s in target_sources)
            raise ValueError(f"--from is required (use all, or {lists})")
        return None
    items = [categories] if isinstance(categories, str) else list(categories)
    names = tuple(
        dict.fromkeys(name.strip() for item in items for name in item.split(",") if name.strip())
    )
    if not names:
        raise ValueError("--from must name at least one category, or all")
    if problem := unknown_categories(names, target_sources) or single_targets(targets, sources):
        raise ValueError(problem)
    if ALL not in names:
        for source in target_sources:
            if not any(cat in names for cat in source.categories):
                raise ValueError(
                    f"{source.code} has none of {', '.join(map(repr, names))}: "
                    f"use all, or {source.code}: {', '.join(source.categories)}"
                )
    return names


def parse_folders(
    folders: str | Sequence[str] | None,
    targets: Sequence[Target],
    sources: Mapping[str, type[Source]],
    selected_categories: tuple[str, ...] | None = None,
) -> tuple[tuple[str, str], ...]:
    """Validate `--folder <category>:<template>` entries before any run starts."""
    if not folders:
        return ()
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
    known = {category for source in target_sources for category in source.categories}
    items = [folders] if isinstance(folders, str) else list(folders)
    parsed: dict[str, str] = {}
    for raw in items:
        category, sep, template = raw.partition(":")
        category, template = category.strip(), template.strip()
        if not sep or not category or not template:
            raise ValueError(f"--folder {raw!r} must use <category>:<template> syntax")
        if category != ALL and category not in known:
            lists = "; ".join(f"{s.code}: {', '.join(s.categories)}" for s in target_sources)
            raise ValueError(f"--folder: no source here has {category!r}: use all, or {lists}")
        if (
            selected_categories is not None
            and ALL not in selected_categories
            and category != ALL
            and category not in selected_categories
        ):
            raise ValueError(f"--folder {category!r} is excluded by --from")
        _validate_template(template)
        parsed[category] = template
    return tuple(parsed.items())


def _validate_template(template: str) -> None:
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
    for _, field_name, format_spec, conversion in fields_iter:
        if field_name is None:
            continue
        if not field_name or conversion is not None or format_spec:
            raise ValueError(
                f"--folder template {template!r} has invalid placeholder {{{field_name}}}"
            )
        if field_name not in FOLDER_FIELDS:
            allowed = ", ".join(sorted(FOLDER_FIELDS))
            raise ValueError(
                f"--folder template {template!r} has unknown field {{{field_name}}}; "
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
