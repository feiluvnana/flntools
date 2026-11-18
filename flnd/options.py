"""Target options, run-wide settings, and config.toml structs.

This is flnd's own module (not a library): it knows sources, targets and the home. It holds:
- `Options`, what one target's run downloads and how, and `Retry`, what it does with the
  files earlier runs failed on;
- `RunShared`, what every source of a run shares: the home, its database, the stop signal, the
  browser, the event sink;
- `Config`, `SourceConfig` and `config`: every setting of the home's `config.toml`, read and
  checked in one place (flnd/docs/storage, "config.toml");
- `categories_by_source`, `validate` and `unknown`, which check `--only` against the targets'
  sources before anything is fetched, and `selected`, which applies it to one source.

Who calls it: the engine (`flnd.engine`) builds the `Options` and `RunShared` of a run and
checks `--only`; the job reads them; the library (`flnd._library`) and the steps after a download
(`flnd._steps`), a source's run (`flnd.site`) and the job read `config`. What it calls:
`flnd._home` (the database type), `flnd.source` (`Source`, `Target`), `flnd.ui.log` (a warning for
a key the config file has that flnd doesn't take) and `flnd.errors`.

How a setting resolves: a flag (or a library argument) given for one run, then the environment
(`FLND_HOME`, `FLND_SOURCES`, which only say where things are), then `config.toml`, then the
default. `config` reads and checks the whole file at once, so a bad value anywhere fails any
command that reads it, with the file, the key and the values it takes in the message.

How `--only` is checked, before any request:
1. Each entry is split at commas; an entry `<code>:<names>` whose code is a source of the run
   applies to that source only, any other entry to every source.
2. A name no source of the run has is refused, with every source's categories in the message.
3. Each source then gets the names that apply to it, and one that gets none of its own
   categories is refused, so a typo never downloads nothing quietly.
4. A single post (a route without categories) takes no `--only` at all.

A source's own templates are checked when its class is made (`flnd.source`), so a run never
meets a broken one. Every check here raises `UsageError`, which the CLI shows without a
traceback.
"""

from __future__ import annotations

import datetime
import functools
import tomllib
import urllib.parse
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from types import MappingProxyType
from typing import TYPE_CHECKING, Any

from flnd import _home
from flnd.errors import UsageError
from flnd.source import SCROLLS, Source, Target
from flnd.ui.log import ui

if TYPE_CHECKING:
    from flnd._run import Stop

# `--only all` takes every category of every source.
ALL = "all"


class Retry(Enum):
    """What a target's run does with the files earlier runs failed on (flnd/docs/errors)."""

    # List each of their posts again first, for those failed on at most `_home.RETRIES` runs;
    # then the target's listings: every download and sync.
    FIRST = "first"
    # List each of their posts again, every one of them, and nothing else: `flnd retry`.
    ONLY = "only"


@dataclass(frozen=True)
class Options:
    """What one target's run downloads and how: `Options(categories=("reels",))`."""

    # None (a single post) or "all" means every category of each source.
    categories: tuple[str, ...] | None = None
    # `--when`'s two days, both included; None is no limit on that side.
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
    # What to do with the files earlier runs failed on; `again` skips them, since it lists
    # every post anyway.
    retry: Retry = Retry.FIRST


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
    # The run's browser and loop (`flnd.kit.browser.RunBrowser`), set by `site.shared_browser`;
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
    # Each file's lifecycle for this target (`flnd._track.Tracks`); None outside a download.
    tracks: Any = field(default=None, repr=False)


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
) -> dict[str, tuple[str, ...] | None]:
    """Check `--only` (plain or `<source>:<cats>`) and give each target source its categories.

    Naming nothing takes every category of every target, as the library's `only=None` does.

    Args:
        categories: what `--only` (or the library's `only`) names; None when it names nothing.
        targets: the run's targets, resolved.
        sources: the sources by code.

    Returns:
        Each source's categories by its code; None means every category.

    Raises:
        UsageError: `--only` names a category no source here has, or is given for a single
            post.
    """
    used = list(dict.fromkeys(sources[target.source] for target in targets))
    used = used or list(sources.values())
    if categories is None:
        return {source.code: None for source in used}
    singles = [t for t in targets if t.kind in sources[t.source].single_kinds]
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
    """Why `--only names` fits none of these sources; None when it fits.

    `all` always fits. A name fits when any one of the sources has a category of that name.

    Args:
        names: the category names asked for, already split at commas.
        sources: the sources they must belong to.

    Returns:
        The message for a `UsageError`, naming each unknown name and every source's categories;
        None when every name is known.

    Example:
        >>> from flnd.source import Post, Source, target
        >>> class Example(Source):
        ...     code, domains, api = "exm", ("example.com",), 1
        ...     @target("exm:{user}")
        ...     def profile(self, site, user):
        ...         return user
        ...     @profile.category
        ...     def posts(self, site, profile):
        ...         yield Post("p1")
        >>> unknown(["posts", "all"], [Example]) is None
        True
        >>> unknown(["reels"], [Example])
        "no source here has 'reels': use all, or exm: posts"
    """
    known = {category for source in sources for category in source.categories}
    missing = [name for name in names if name != ALL and name not in known]
    if not missing:
        return None
    return f"no source here has {', '.join(map(repr, missing))}: use all, or {_lists(sources)}"


def _split(
    items: Sequence[str], used: set[str], sources: Mapping[str, type[Source]],
) -> tuple[list[str], dict[str, list[str]]]:
    """Split `--only` tokens into plain names and `<source>:<cats>` buckets.

    An entry whose part before `:` isn't a source's code is plain, colon and all, so a category
    name is never mistaken for a code.

    Args:
        items: the `--only` entries as given, each one name or several joined by commas.
        used: the codes of the sources the run's targets belong to.
        sources: every known source, by code.

    Returns:
        The plain names, in order, and the scoped names by source code.

    Raises:
        UsageError: an entry is scoped to a source with no target in the run, or names no
            category after its `:`.
    """
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
    """The validated categories one source takes.

    Args:
        source: the source.
        plain: the names that apply to every source.
        scoped: the names given as `<code>:<names>`, by code; only `source`'s are read.

    Returns:
        The names, plain first, each once; it may hold `all`, or names of other sources' categories,
        which `selected` drops.

    Raises:
        UsageError: a scoped name isn't one of the source's, or none of the names is.
    """
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
    # The mapping itself, not a copy: a copy would import every source and plugin, where the
    # check needs only the targets' own (`flnd.sources.Registry` loads each on first lookup).
    by_code = (sources if isinstance(sources, Mapping)
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
# The home's config file
# ---------------------------------------------------------------------------------------------

# The home's settings file (flnd/docs/storage, "config.toml").
CONFIG = "config.toml"
# What `[defaults] dedupe` takes: "link" (the default) hard-links a file whose bytes are already
# saved; "off" keeps both copies.
DEDUPE = ("link", "off")
# What `[vault] key` takes: where the vault's key is kept, `secret.key` in the home (the
# default, which an unattended sync can always open) or the OS keychain (flnd/docs/storage).
FILE, KEYCHAIN = "file", "keychain"
VAULTS = (FILE, KEYCHAIN)
# The proxy schemes `[sources.<code>] proxy` takes: the ones curl speaks. `socks5h` also looks
# host names up through the proxy.
PROXIES = ("http", "https", "socks5", "socks5h")
# How many saved posts in a row end a sync by default (`stop_after`).
STOP_AFTER = 5
# Every key each table takes. A key not listed is warned about, never silently ignored, so a
# typo (`[defaults] compres = true`) can't pass for a setting that works.
KEYS = {
    "defaults": ("again", "dedupe", "sidecars", "when"),
    "vault": ("key",),
}
SOURCE_KEYS = ("pace", "scroll", "limits", "proxy", "stop_after")


@dataclass(frozen=True)
class SourceConfig:
    """One source's `[sources.<code>]` settings in config.toml; None leaves it to the source.

    Each setting overrides what the source's class says (`flnd.source.Source`), for this home.
    """

    # Seconds between the source's page requests; overrides `Source.pace`.
    pace: float | None = None
    # How its browser feeds scroll, "human" or "fast"; overrides `Source.scroll`.
    scroll: str | None = None
    # The most files at once from a media host, by host; merged over `Source.limits`, so a host
    # named in both takes this number.
    limits: Mapping[str, int] = field(default_factory=lambda: MappingProxyType({}))
    # The proxy every HTTP request of the source goes through (`socks5://127.0.0.1:1080`);
    # None for none. The browser isn't proxied: it is one window every source shares.
    proxy: str | None = None
    # Saved posts in a row that end a sync of one of its `newest_first` listings.
    stop_after: int = STOP_AFTER


@dataclass(frozen=True)
class Config:
    """The settings of a home's `config.toml`, with their defaults (flnd/docs/storage).

    The library's calls keep to five options each, so a setting past those lives here. A flag or
    a library argument given for one run wins over the file; the file wins over the default.
    """

    # List every post again and ask again for files recorded gone (the CLI's `--again`).
    again: bool = False
    # Hard-link a saved file whose bytes another saved file already has (flnd/docs/storage).
    dedupe: bool = True
    # Write each saved file's post as `<file>.json` beside it (flnd/docs/storage).
    sidecars: bool = False
    # The days a download, follow or sync takes when `--when` isn't given, as `--when` writes
    # them (`"1y"`); "" for every day.
    when: str = ""
    # Where the vault's key is kept: "file" (`secret.key` in the home) or "keychain".
    vault: str = FILE
    # Each source's own settings, by code; a source not named has `SourceConfig()`.
    sources: Mapping[str, SourceConfig] = field(default_factory=lambda: MappingProxyType({}))

    def source(self, code: str) -> SourceConfig:
        """One source's settings: its `[sources.<code>]` table, or every default.

        Args:
            code: the source's code.

        Returns:
            The settings.

        Example:
            >>> Config().source("ins").stop_after
            5
        """
        return self.sources.get(code) or SourceConfig()


def config(home: Path) -> Config:
    """Read and check the home's `config.toml`: every setting flnd takes, in one place.

    The file is read on each call (it is small), and parsed once per content, so a run that
    asks many times pays one parse, and a key it doesn't know is warned about once.

    Args:
        home: the home folder; a home without the file reads as every default.

    Returns:
        The settings.

    Raises:
        UsageError: the file isn't TOML, or a setting has a value it doesn't take; the message
            names the file, the table and key, and the values it takes.

    Example:
        >>> config(Path("no-such-home")) == Config()
        True
    """
    path = home / CONFIG
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return Config()
    except (OSError, UnicodeDecodeError) as error:
        raise UsageError(f"{path}: {error}", hint="fix the file, or move it away") from None
    return _parsed(path, text)


@functools.lru_cache(maxsize=8)
def _parsed(path: Path, text: str) -> Config:
    """Parse and check one content of a config file; cached by path and text.

    1. Parse the TOML; a syntax error names its line.
    2. Warn about each table and key flnd doesn't take.
    3. Check each setting's value, and build the `Config`.

    Args:
        path: the file, for messages.
        text: its content.

    Returns:
        The settings.

    Raises:
        UsageError: the text isn't TOML, or a value is one its key doesn't take.
    """
    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError as error:
        raise UsageError(f"{path}: {error}", hint="fix the file, or move it away") from None
    _warn_unknown(path, data)
    defaults = _table(path, data, "defaults")
    vault = _table(path, data, "vault")
    sources = _table(path, data, "sources")
    where = "defaults"
    return Config(
        again=_flag(path, where, "again", defaults.get("again", False)),
        dedupe=_choice(path, where, "dedupe", defaults.get("dedupe", "link"), DEDUPE) == "link",
        sidecars=_flag(path, where, "sidecars", defaults.get("sidecars", False)),
        when=_when_text(path, defaults.get("when", "")),
        vault=_choice(path, "vault", "key", vault.get("key", FILE), VAULTS),
        sources=MappingProxyType({
            code: _source(path, code, _table(path, sources, code, within="sources"))
            for code in sources
        }),
    )


def _source(path: Path, code: str, table: Mapping[str, Any]) -> SourceConfig:
    """Check one `[sources.<code>]` table.

    Args:
        path: the config file, for messages.
        code: the source's code, as the table names it.
        table: the table.

    Returns:
        The source's settings.

    Raises:
        UsageError: a value is one its key doesn't take.
    """
    where = f"sources.{code}"
    pace = table.get("pace")
    if pace is not None and (not isinstance(pace, (int, float)) or isinstance(pace, bool)
                             or pace < 0):
        raise _bad(path, where, "pace", "a number of seconds, 0 or more", pace)
    scroll = table.get("scroll")
    if scroll is not None:
        _choice(path, where, "scroll", scroll, SCROLLS)
    limits = table.get("limits", {})
    if not isinstance(limits, dict) or not all(
            isinstance(host, str) and host and isinstance(n, int) and not isinstance(n, bool)
            and n >= 1 for host, n in limits.items()):
        raise _bad(path, where, "limits", 'a table of "<host>" = <files at once, at least 1>',
                   limits)
    proxy = table.get("proxy")
    if proxy is not None:
        _proxy(path, where, proxy)
    stop = table.get("stop_after", STOP_AFTER)
    if not isinstance(stop, int) or isinstance(stop, bool) or stop < 1:
        raise _bad(path, where, "stop_after", "a whole number of at least 1", stop)
    return SourceConfig(pace=None if pace is None else float(pace), scroll=scroll,
                        limits=MappingProxyType(dict(limits)), proxy=proxy, stop_after=stop)


def _warn_unknown(path: Path, data: Mapping[str, Any]) -> None:
    """Warn about each table and key of the file that flnd doesn't take.

    A key is warned about rather than refused, so a file written for a newer flnd still opens
    in an older one.

    Args:
        path: the config file, for messages.
        data: the parsed file.
    """
    unknown: list[str] = []
    for name, value in data.items():
        if name == "sources":
            for code, table in value.items() if isinstance(value, dict) else ():
                keys = table if isinstance(table, dict) else {}
                unknown += [f"[sources.{code}] {key}" for key in keys if key not in SOURCE_KEYS]
        elif name in KEYS:
            keys = value if isinstance(value, dict) else {}
            unknown += [f"[{name}] {key}" for key in keys if key not in KEYS[name]]
        else:
            unknown.append(f"[{name}]")
    for setting in unknown:
        ui.warn("{path}: {setting} is not a setting flnd knows; it is ignored",
                path=str(path), setting=setting)


def _table(path: Path, data: Mapping[str, Any], name: str, *,
           within: str = "") -> Mapping[str, Any]:
    """A table of the file, or an empty one when it isn't there.

    Args:
        path: the config file, for messages.
        data: the table it is in.
        name: its name.
        within: the name of the table it is in, for messages; "" for the top.

    Returns:
        The table.

    Raises:
        UsageError: the name holds a value that isn't a table.
    """
    value = data.get(name, {})
    if not isinstance(value, dict):
        shown = f"{within}.{name}" if within else name
        raise UsageError(f"{path}: {shown} is a table ([{shown}]), not {value!r}",
                         hint="fix the file, or move it away")
    return value


def _flag(path: Path, where: str, name: str, value: Any) -> bool:
    """A setting that is true or false.

    Args:
        path: the config file, for messages.
        where: the table, as `[…]` writes it: `defaults`, `sources.ins`.
        name: the key.
        value: what the file has.

    Returns:
        The value.

    Raises:
        UsageError: it is anything else.
    """
    if not isinstance(value, bool):
        raise _bad(path, where, name, "true or false", value)
    return value


def _choice(path: Path, where: str, name: str, value: Any, allowed: Sequence[str]) -> str:
    """A setting that is one of a few words.

    Args:
        path: the config file, for messages.
        where: the table, as `[…]` writes it: `defaults`, `sources.ins`.
        name: the key.
        value: what the file has.
        allowed: the words it takes.

    Returns:
        The value.

    Raises:
        UsageError: it is any other value.
    """
    if value not in allowed:
        raise _bad(path, where, name, " or ".join(f'"{word}"' for word in allowed), value)
    return value


def _when_text(path: Path, value: Any) -> str:
    """`[defaults] when`, checked as `--when` reads it; "" for every day.

    Args:
        path: the config file, for messages.
        value: what the file has.

    Returns:
        The text, stripped.

    Raises:
        UsageError: it isn't text `--when` takes.
    """
    from flnd._library import WHEN_USAGE, _when

    allowed = 'text --when takes ("1y", "2026", "2026-01..2026-03"), or ""'
    if not isinstance(value, str):
        raise _bad(path, "defaults", "when", allowed, value)
    try:
        _when(value)
    except UsageError:
        raise _bad(path, "defaults", "when", allowed, value, hint=WHEN_USAGE) from None
    return value.strip()


def _proxy(path: Path, where: str, value: Any) -> None:
    """Check a proxy: `<scheme>://<host>[:<port>]`, the scheme one curl speaks.

    Args:
        path: the config file, for messages.
        where: the table, as `[…]` writes it: `defaults`, `sources.ins`.
        value: what the file has.

    Raises:
        UsageError: it is anything else.
    """
    allowed = " or ".join(f'"{scheme}://<host>:<port>"' for scheme in PROXIES)
    if not isinstance(value, str):
        raise _bad(path, where, "proxy", allowed, value)
    parts = urllib.parse.urlsplit(value)
    # A proxy may carry a user and password (`socks5://user:secret@host:1080`), which never
    # reach a message: the error shows the address with them cut out.
    shown = value
    if "@" in parts.netloc:
        shown = value.replace(parts.netloc, "…@" + parts.netloc.rpartition("@")[2], 1)
    try:
        port = parts.port
    except ValueError:
        port = -1
    if parts.scheme not in PROXIES or not parts.hostname or port == -1:
        raise _bad(path, where, "proxy", allowed, shown)


def _bad(path: Path, where: str, name: str, allowed: str, value: Any, *,
         hint: str | None = None) -> UsageError:
    """The error for a setting with a value it doesn't take.

    Args:
        path: the config file.
        where: the table, as `[…]` writes it: `defaults`, `sources.ins`.
        name: the key.
        allowed: what it takes, in words.
        value: what the file has.
        hint: what to do; by default, to fix the line.

    Returns:
        The `UsageError`, naming the file, the table, the key and what it takes.
    """
    return UsageError(f"{path}: [{where}] {name} is {allowed}, not {value!r}",
                      hint=hint or f"fix the line in {path}, or remove it for the default")
