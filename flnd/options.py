"""Each target's options, the settings every source of a run shares, and their checks.

- `Options` is what one target's run downloads and how; `RunShared` what every source of a run
  shares (the home, its database, the stop signal, the browser, the event sink),
  and the home's config file.
- `categories_by_source` and `validate` check `--only` against the targets' sources before
  anything is fetched. A source's own templates are checked when its class is made
  (`flnd.source`), so a run never meets a broken one.
- `stop_after` reads, per source, how many saved posts in a row end a sync, from the home's
  config.toml.
"""

from __future__ import annotations

import datetime
import functools
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable, Mapping, Sequence

from flnd import _home
from flnd.errors import UsageError
from flnd.source import Source, Target

if TYPE_CHECKING:
    from flnd._run import Stop

# `--only all` takes every category of every source.
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
    """What all sources of a run share: the home, its database, the stop signal, the browser."""

    home: Path
    stop: Stop
    # The home's `flnd.db`, open for the run; None where nothing is read or saved (a login).
    db: _home.Home | None = None
    # The `runs` row the run's failures name; None outside a recorded run (a dry run).
    run: int | None = None
    # Whether a person can be asked a question, which a source's own login needs.
    interactive: bool = False
    # List what would be downloaded without writing or downloading anything.
    dry: bool = False
    # The run's browser and loop (`flnd._browser.RunBrowser`), set by `site.shared_browser`;
    # None outside a run.
    browser_pool: Any = field(default=None, repr=False)
    # Where the run's events go (`flnd._run.Run._emit`). It may wait while a script handles the
    # event before, so it is never called with a lock held. By default, events are dropped.
    emit: Callable[[Any], None] = field(default=lambda event: None, repr=False)
    # The steps after a file lands (`flnd._steps.Chain`); None outside a run, so nothing is
    # unpacked or compressed.
    chain: Any = field(default=None, repr=False)
    # Resolves the text of a target a listing yielded, on any source (the engine's `resolve`);
    # None outside the engine, where a source reads only its own.
    resolve: Callable[[str], Target] | None = field(default=None, repr=False)


# ---------------------------------------------------------------------------------------------
# The home's config file
# ---------------------------------------------------------------------------------------------

# The home's settings file (docs/storage).
CONFIG = "config.toml"
# What `[defaults] dedupe` takes: "link" (the default) hard-links a file whose bytes are already
# saved; "off" keeps both copies.
DEDUPE = ("link", "off")


@dataclass(frozen=True)
class Config:
    """The `[defaults]` of a home's `config.toml` that flnd reads, with their defaults.

    The library's calls keep to five options each, so a setting past those lives here.
    """

    # List every post again and ask again for files recorded gone (the CLI's `--again`).
    again: bool = False
    # Hard-link a saved file whose bytes another saved file already has (docs/storage).
    dedupe: bool = True
    # Write each saved file's post as `<file>.json` beside it (docs/storage).
    sidecars: bool = False


def config(home: Path) -> Config:
    """Read the `[defaults]` of a home's `config.toml`.

    Args:
        home: the home folder; a home without the file reads as every default.

    Returns:
        The settings.

    Raises:
        UsageError: the file isn't TOML, or a setting has a value it doesn't take.

    Example:
        >>> config(Path("no-such-home"))
        Config(again=False, dedupe=True, sidecars=False)
    """
    path = home / CONFIG
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return Config()
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError) as error:
        raise UsageError(f"{path}: {error}", hint="fix the file, or move it away") from None
    defaults = data.get("defaults", {})
    if not isinstance(defaults, dict):
        defaults = {}
    for name in ("again", "sidecars"):
        if not isinstance(defaults.get(name, False), bool):
            raise UsageError(f"{path}: {name} is true or false, not {defaults[name]!r}")
    dedupe = defaults.get("dedupe", "link")
    if dedupe not in DEDUPE:
        raise UsageError(f"{path}: dedupe is \"link\" or \"off\", not {dedupe!r}")
    return Config(again=defaults.get("again", False), dedupe=dedupe == "link",
                  sidecars=defaults.get("sidecars", False))


def selected(options: Options, source: type[Source]) -> tuple[str, ...]:
    """The categories `--only` takes from one source, in the source's order."""
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
    """Check `--only` (plain or `<source>:<cats>`) and give each target source its categories.

    Args:
        categories: what `--only` (or the library's `only`) names; None when it names nothing.
        targets: the run's targets, resolved.
        sources: the sources by code.
        every: whether naming nothing means every category of every target, as the library's
            `only=None` does; False makes `--only` required for a profile, as today's CLI does.

    Returns:
        Each source's categories by its code; None means every category.

    Raises:
        UsageError: `--only` is missing where it is required, names a category no source here
            has, or is given for a single post.
    """
    used = list(dict.fromkeys(sources[target.source] for target in targets))
    used = used or list(sources.values())
    singles = [t for t in targets if t.kind in sources[t.source].single_kinds]
    if categories is None:
        if not every and len(singles) < len(targets):
            raise UsageError(f"--only is required (use all, or {_lists(used)})")
        return {source.code: None for source in used}
    if singles:
        raise UsageError(
            f"--only applies to profiles and chats; {singles[0]} is one post, downloaded whole: "
            "give it without --only"
        )
    items = [categories] if isinstance(categories, str) else list(categories)
    plain, scoped = _split(items, {source.code for source in used}, sources)
    if not plain and not scoped:
        raise UsageError("--only must name at least one category, or all")
    if problem := unknown(plain, used):
        raise UsageError(problem)
    return {source.code: _names(source, plain, scoped) for source in used}


def unknown(names: Sequence[str], sources: Sequence[type[Source]]) -> str | None:
    """Why `--only names` fits none of these sources; None when it fits."""
    known = {category for source in sources for category in source.categories}
    missing = [name for name in names if name != ALL and name not in known]
    if not missing:
        return None
    return f"no source here has {', '.join(map(repr, missing))}: use all, or {_lists(sources)}"


def _split(
    items: Sequence[str], used: set[str], sources: Mapping[str, type[Source]],
) -> tuple[list[str], dict[str, list[str]]]:
    """Split `--only` tokens into plain names and `<source>:<cats>` buckets."""
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
            raise UsageError(f"--only {item!r}: no target from source {code!r}")
        if not names:
            raise UsageError(f"--only {item!r} must name at least one category, or all")
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
            f"--only is required for {source.code} (use all, or {', '.join(source.categories)})"
        )
    if ALL not in names and not any(name in names for name in source.categories):
        raise UsageError(
            f"{source.code} has none of {', '.join(map(repr, names))}: "
            f"use all, or {source.code}: {', '.join(source.categories)}"
        )
    return names


def _lists(sources: Sequence[type[Source]]) -> str:
    return "; ".join(f"{source.code}: {', '.join(source.categories)}" for source in sources)


# ---------------------------------------------------------------------------------------------
# --only, checked before a run
# ---------------------------------------------------------------------------------------------

def validate(
    sources: Mapping[str, type[Source]] | Sequence[type[Source]],
    from_selection: str | Sequence[str] | None = None,
    targets: Sequence[Target] = (),
) -> None:
    """Check the categories asked for against the targets' routes, before any request.

    A source's templates need no check here: they were checked when its class was made.

    Args:
        sources: the sources, by code or as a list.
        from_selection: the categories asked for (`--only`, or the library's `only`); None
            checks nothing.
        targets: the run's targets, whose routes say which categories they have.

    Raises:
        UsageError: a category is unknown, or given for a single post.
    """
    by_code = ({code: src for code, src in sources.items()} if isinstance(sources, Mapping)
               else {src.code: src for src in sources})
    if from_selection is not None:
        _validate_from(by_code, from_selection, targets)


def _validate_from(
    by_code: Mapping[str, type[Source]],
    from_selection: str | Sequence[str],
    targets: Sequence[Target],
) -> None:
    """Check the categories asked for against the categories the targets' routes have.

    Args:
        by_code: the sources, by code.
        from_selection: the categories asked for, one text or several.
        targets: the run's targets; none checks against every source's categories.

    Raises:
        UsageError: a category is unknown, or asked of a single post.
    """
    raw = [from_selection] if isinstance(from_selection, str) else list(from_selection)
    if not raw:
        return
    routes = _target_routes(by_code, targets)
    for target, route in routes:
        if not route.categories and not route.discovers:
            raise UsageError(
                f"--only applies to profiles and chats; {target} is one post, downloaded whole"
            )
    if routes:
        # A search's targets are its source's other routes', so it takes their categories.
        known = {cat for target, route in routes for cat in (
            by_code[target.source].categories if route.discovers else _names_of(route)
        )}
    else:
        known = {cat for src in by_code.values() for cat in src.categories}
    for entry in raw:
        _check_from_entry(entry, by_code, known)


def _check_from_entry(
    entry: str, by_code: Mapping[str, type[Source]], known: set[str]
) -> None:
    """Check one entry of `--only`: `<cats>` or `<source>:<cats>`, comma-separated.

    Args:
        entry: the entry.
        by_code: the sources, by code.
        known: every category the run's targets have.

    Raises:
        UsageError: the entry names no category, or one no source here has.
    """
    code, sep, rest = entry.partition(":")
    if sep and code in by_code:
        src_cats = set(by_code[code].categories)
        names = [part.strip() for part in rest.split(",") if part.strip()]
        bad = [name for name in names if name != ALL and name not in src_cats]
    else:
        names = [part.strip() for part in entry.split(",") if part.strip()]
        bad = [name for name in names if name != ALL and name not in known]
    if not names:
        raise UsageError(f"--only {entry!r} has no category name")
    if bad:
        raise UsageError(f"unknown --only category {', '.join(map(repr, bad))}")


def _target_routes(
    by_code: Mapping[str, type[Source]], targets: Sequence[Target]
) -> list[tuple[Target, Any]]:
    """Pair each target with the route of its kind.

    Args:
        by_code: the sources, by code.
        targets: the targets; one whose source or route is unknown is left out.

    Returns:
        Each target with its route.
    """
    pairs: list[tuple[Target, Any]] = []
    for target in targets:
        source = by_code.get(target.source)
        if source is None:
            continue
        route = next((r for r in source.routes if r.kind == target.kind), None)
        if route is not None:
            pairs.append((target, route))
    return pairs


def _names_of(route: Any) -> frozenset[str]:
    """The names of a route's categories: what `--only` may name for one of its targets."""
    return frozenset(category.name for category in route.categories)


# ---------------------------------------------------------------------------------------------
# Settings from config.toml
# ---------------------------------------------------------------------------------------------

# The home's settings file, and how many saved posts in a row end a sync by default.
CONFIG = "config.toml"
STOP_AFTER = 5


def stop_after(home: Path, code: str) -> int:
    """How many saved posts in a row end a sync of one of a source's `newest_first` listings.

    Read from `[sources.<code>] stop_after = N` in the home's config.toml, like gallery-dl's
    `skip = "abort:N"`: a site that pins many posts above its newest needs more than 5.

    Args:
        home: the home folder.
        code: the source's code.

    Returns:
        The number; `STOP_AFTER` when the file or the setting isn't there.

    Raises:
        UsageError: the file isn't TOML, or the setting isn't a whole number of at least 1.
    """
    path = home / CONFIG
    try:
        stamp = path.stat().st_mtime_ns
    except OSError:
        return STOP_AFTER
    settings = _sources_settings(path, stamp).get(code, {})
    value = settings.get("stop_after", STOP_AFTER) if isinstance(settings, dict) else STOP_AFTER
    if not isinstance(value, int) or isinstance(value, bool) or value < 1:
        raise UsageError(f"{path}: [sources.{code}] stop_after is a whole number of at least 1, "
                         f"not {value!r}")
    return value


@functools.lru_cache(maxsize=4)
def _sources_settings(path: Path, stamp: int) -> dict[str, Any]:
    """The `[sources]` table of a config file, read once per change of the file (`stamp`).

    Args:
        path: the config file to read.
        stamp: a number that changes whenever the file does (its modification time); it only
            keys the cache, so a changed file is read again.

    Returns:
        The `[sources]` table, or an empty dict when the file has none or it is not a table.

    Raises:
        UsageError: the file can't be read or is not valid TOML.
    """
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError) as error:
        raise UsageError(f"{path}: {error}", hint="fix the file, or move it away") from None
    found = data.get("sources", {})
    return found if isinstance(found, dict) else {}
