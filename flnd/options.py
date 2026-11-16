"""Each target's options, and the settings every source of a run shares."""

from __future__ import annotations

import datetime
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Mapping, MutableMapping, Sequence

from flnd import _cli
from flnd.errors import UsageError
from flnd.source import Source, Target

# `--from all` takes every category of every source.
ALL = "all"


@dataclass(frozen=True)
class Options:
    """What one target's run downloads and how: `Options(categories=("reels",))`."""

    # None (a single post) or "all" means every category of each source.
    categories: tuple[str, ...] | None = None
    # `--time`'s two days, both included; None is no limit on that side.
    since: datetime.date | None = None
    until: datetime.date | None = None
    # A synced target with nothing lost stops a `newest_first` listing after 5 saved posts.
    stop_at_saved: bool = False
    # Max discovered targets on a discovering route, or max posts on a normal target.
    limit: int | None = None
    # Ignore the new-post-only check and saved cursors.
    again: bool = False
    # The query text when this target was discovered from a `?query` route.
    query: str | None = None
    # Compress each saved photo and video once it is recorded (`--compress`, docs/media),
    # and remove an archive once it is extracted.
    compress: bool = False


@dataclass
class RunShared:
    """What all sources of a run share: the folder, both stores, the stop signal, the browser."""

    home: Path
    history: MutableMapping[str, Any]
    stop: _cli.Stopping
    # `targets.db`'s rows: each target's complete categories and cursors.
    targets: MutableMapping[str, Any] = field(default_factory=dict)
    # Whether a person can be asked a question, which a source's own login needs.
    interactive: bool = False
    # List what would be downloaded without writing or downloading anything.
    dry: bool = False
    # The single-window browser pool `site.shared_browser` enters; None outside a run.
    browser_pool: Any = field(default=None, repr=False)
    # Where the run's events go (`flnd._run.Run._emit`). It may wait while a script handles the
    # event before, so it is never called with a lock held. By default, events are dropped.
    emit: Callable[[Any], None] = field(default=lambda event: None, repr=False)
    # The steps after a file lands (`flnd._steps.Chain`); None outside a run, so nothing is
    # unpacked or compressed.
    chain: Any = field(default=None, repr=False)


def selected(options: Options, source: type[Source]) -> tuple[str, ...]:
    """The categories `--from` takes from one source, in the source's order."""
    names = options.categories
    if names is None or ALL in names:
        return source.categories
    return tuple(name for name in source.categories if name in names)


def categories_by_source(
    categories: str | Sequence[str] | None,
    targets: Sequence[Target],
    sources: Mapping[str, type[Source]],
    *,
    every: bool = False,
) -> dict[str, tuple[str, ...] | None]:
    """Check `--from` (plain or `<source>:<cats>`) and give each target source its categories.

    Args:
        categories: what `--from` (or the library's `only`) names; None when it names nothing.
        targets: the run's targets, resolved.
        sources: the sources by code.
        every: whether naming nothing means every category of every target, as the library's
            `only=None` does; False makes `--from` required for a profile, as today's CLI does.

    Returns:
        Each source's categories by its code; None means every category.

    Raises:
        UsageError: `--from` is missing where it is required, names a category no source here
            has, or is given for a single post.
    """
    used = list(dict.fromkeys(sources[target.source] for target in targets))
    used = used or list(sources.values())
    singles = [t for t in targets if t.kind in sources[t.source].single_kinds]
    if categories is None:
        if not every and len(singles) < len(targets):
            raise UsageError(f"--from is required (use all, or {_lists(used)})")
        return {source.code: None for source in used}
    if singles:
        raise UsageError(
            f"--from applies to profiles and chats; {singles[0]} is one post, downloaded whole: "
            "give it without --from"
        )
    items = [categories] if isinstance(categories, str) else list(categories)
    plain, scoped = _split(items, {source.code for source in used}, sources)
    if not plain and not scoped:
        raise UsageError("--from must name at least one category, or all")
    if problem := unknown(plain, used):
        raise UsageError(problem)
    return {source.code: _names(source, plain, scoped) for source in used}


def unknown(names: Sequence[str], sources: Sequence[type[Source]]) -> str | None:
    """Why `--from names` fits none of these sources; None when it fits."""
    known = {category for source in sources for category in source.categories}
    missing = [name for name in names if name != ALL and name not in known]
    if not missing:
        return None
    return f"no source here has {', '.join(map(repr, missing))}: use all, or {_lists(sources)}"


def _split(
    items: Sequence[str], used: set[str], sources: Mapping[str, type[Source]],
) -> tuple[list[str], dict[str, list[str]]]:
    """Split `--from` tokens into plain names and `<source>:<cats>` buckets."""
    plain: list[str] = []
    scoped: dict[str, list[str]] = {}
    for item in items:
        code, colon, rest = item.partition(":")
        names = [name.strip() for name in (rest if colon else item).split(",") if name.strip()]
        if not colon or code.strip() not in sources:
            plain.extend(name.strip() for name in item.split(",") if name.strip())
            continue
        code = code.strip()
        if code not in used:
            raise UsageError(f"--from {item!r}: no target from source {code!r}")
        if not names:
            raise UsageError(f"--from {item!r} must name at least one category, or all")
        scoped.setdefault(code, []).extend(names)
    return plain, scoped


def _names(
    source: type[Source], plain: Sequence[str], scoped: Mapping[str, Sequence[str]],
) -> tuple[str, ...]:
    """The validated categories one source takes."""
    own = tuple(dict.fromkeys(scoped.get(source.code, ())))
    if own and (problem := unknown(own, [source])):
        raise UsageError(problem)
    names = tuple(dict.fromkeys([*plain, *own]))
    if not names:
        raise UsageError(
            f"--from is required for {source.code} (use all, or {', '.join(source.categories)})"
        )
    if ALL not in names and not any(name in names for name in source.categories):
        raise UsageError(
            f"{source.code} has none of {', '.join(map(repr, names))}: "
            f"use all, or {source.code}: {', '.join(source.categories)}"
        )
    return names


def _lists(sources: Sequence[type[Source]]) -> str:
    return "; ".join(f"{source.code}: {', '.join(source.categories)}" for source in sources)
