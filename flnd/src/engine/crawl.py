"""The crawl: one target's listings on the run's loop, its smart sync and early stops, the Ledger.

SPEC.md#sync explains it.
"""

from __future__ import annotations

import asyncio
import contextlib
import datetime
import filecmp
import functools
import inspect
import os
import re
import stat
import string
import threading
import time
from collections.abc import (
    AsyncIterator,
    Awaitable,
    Callable,
    Generator,
    Mapping,
    Sequence,
)
from dataclasses import dataclass, field, replace
from enum import Enum
from pathlib import Path, PurePosixPath
from types import MappingProxyType
from typing import TYPE_CHECKING, Any, TypeVar
from urllib.parse import urlsplit

from flnd.engine import config, jobs, save, store
from flnd.engine.ctx import Ctx, Listing, PluginRun, met_turn
from flnd.engine.events import NOTHING_NEW, RESUMING, Failed, Skipped, sink
from flnd.errors import (
    Blocked,
    Changed,
    Error,
    Hidden,
    LoginError,
    NetworkError,
    NotFound,
    PluginError,
    SiteError,
    Stopped,
    UsageError,
    classify,
)
from flnd.plugin import Category, File, Plugin, Post, Range, Route, Target
from flnd.ui.log import ui
from flnkit import files, text
from flnkit.clock import day, stamp
from flnkit.text import fill

if TYPE_CHECKING:
    from flnd.engine.events import Event
    from flnd.engine.run import Counts, Stop, Tally

__all__ = [
    "STOPPED", "Claims", "Downloads", "Ledger", "Options", "Placed", "Retry", "RunShared",
    "TargetReport", "Turn", "asked", "causes", "checked_dest",
    "discover", "download", "failure", "logged_out", "place", "plan", "posts", "target_root",
    "tell", "wanted",
]

class Retry(Enum):
    """What a target's run does with files earlier runs failed on
    (SPEC.md#when-it-goes-wrong)."""

    # List each of their posts again first, for those failed on at most `store.RETRIES` runs;
    # then the target's listings: every download and sync.
    FIRST = "first"
    # List each of their posts again, every one of them, and nothing else: `flnd target list`'s r.
    ONLY = "only"


@dataclass(frozen=True)
class Options:
    """What one target's run downloads and how: `Options(categories=("reels",))`."""

    # None (a single post) or "all" means every category of each source.
    categories: tuple[str, ...] | None = None
    # `--when`'s two days, both included; None is no limit on that side.
    since: datetime.date | None = None
    until: datetime.date | None = None
    # A sync (a followed target, or `flnd sync`): its `newest_first` listings stop at their head.
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
    # The value as typed, with a link's `user:pw@` the target's value leaves out: what its
    # route reads. None reads the target's value. A target it finds never takes it.
    link: str | None = None
    # The call's folder value under `media/<plugin>/` (`config.folders`): one template or a
    # list by category, overriding plugin, route, category and config folder templates for
    # the categories it names; None uses each target's own folder.
    folder: str | None = None


@dataclass
class RunShared:
    """What all sources of a run share: the home, its database, the stop signal, the browser."""

    home: Path
    stop: Stop
    # The home's `flnd.db`, open for the run; None where nothing is read or saved (a login).
    db: store.Store | None = None
    # The home's settings (`flnd.engine.config.Config`), read once as the run starts; None
    # reads them where they are needed (outside a run).
    config: Any = field(default=None, repr=False)
    # The `runs` row the run's failures name; None outside a recorded run (a dry run).
    run: int | None = None
    # Whether a person can be asked a question, which a source's own login needs.
    interactive: bool = False
    # List what would be downloaded without writing or downloading anything.
    dry: bool = False
    # The run's browser (`flnkit.browser.RunBrowser`), on the run's loop, set by
    # `site.shared_browser`; None outside a run.
    browser_pool: Any = field(default=None, repr=False)
    # Where the run's events go, awaited on the loop (`flnd.engine.run.sink`): it waits while
    # the channel to the reader is full. By default, events are dropped.
    emit: Callable[[Any], Awaitable[None]] = field(default=sink(None), repr=False)
    # The run's save (`flnd.engine.save.Saver`), set by `save.attached`; None outside a run.
    saver: Any = field(default=None, repr=False)
    # Resolves the text of a target a listing yielded, on any source (the engine's `resolve`);
    # None outside the engine, where a source reads only its own.
    resolve: Callable[[str], Target] | None = field(default=None, repr=False)
    # The run's live counts (`flnd.engine.run.Tally`), which `run.state` is made from; None
    # outside a run.
    tally: Any = field(default=None, repr=False)
    # Runs a target a listing discovered as a target of its own, on its own plugin's instance
    # (the engine's); None outside the engine.
    follow: Callable[..., Awaitable[Any]] | None = field(default=None, repr=False)
    # The files the run has queued, shared by every target (`flnd.engine.crawl.Claims`); None
    # outside the engine, where each target keeps its own.
    claims: Any = field(default=None, repr=False)


# The statuses that end a listing as a login problem when the site's own domain answers them.
LOGIN_WALLS = frozenset({401, 403})
# The statuses that stop asking a site for dates for the rest of the run.
BLOCKS = LOGIN_WALLS | {429}
# The day a `{date}_` filename template puts in front of a name, which `redate` replaces.
LEADING_DAY = re.compile(r"\d{4}-\d\d-\d\d_")
# The hour (UTC) a file's modified time is set to on its post's day: noon reads as the same day
# in every time zone from UTC-11 to UTC+11.
NOON = 12
# Posts in a row older than `--when` that end a `newest_first` listing: more than the posts a
# profile pins above its newest (Instagram pins up to 3).
OLDER_IN_A_ROW = 5
# Old posts in a row (the head itself, or older than it) that end a sync. A site pins up to 3
# posts above its newest (ins, tik), so 4 can't be pins, marked or not.
OLD_IN_A_ROW = 4
# Template fields known only once a post's files are listed: a key that names one can't be
# built from the post alone, so a lazy post's files must be listed to tell if it is saved.
LISTED_FIELDS = frozenset({"count", "index", "extension", "path", "dir", "file"})
# The extension a video joined with its audio track gets, unless both are WebM.
DASH_EXTENSION = ".mp4"
# An API 2 `File.name`'s own extension, which it keeps, of up to 7 characters: `.png` in
# `cover_12.png` and `.torrent` in `file.torrent`, not `.2` in `v1.2` (5.3).
EXTENSION = re.compile(r"\.[A-Za-z][A-Za-z0-9]{0,6}")
STOPPED = "stopped by Ctrl-C"
FINISHING = "stopping: {files} finishing (Ctrl-C again to stop now)"
# Seconds a wait on the loop looks again at the stop state: short enough that Ctrl-C feels at
# once, long enough to cost nothing.
WAKE = 0.1
# Seconds an async listing may run without awaiting before it is warned about (`_steps`).
HELD = 10.0
# A target's queue holds this many times its `jobs` files, queued or downloading, before its
# listing waits: enough that a burst of small files never starves the transfers.
QUEUE = 4
# Seconds a post the `date` hook had no day for isn't asked again (30 days), and the key of the
# source's saved state that keeps those posts.
ASKED_TTL = 30 * 24 * 3600
ASKED_KEY = "redate.asked"
# The `--only` value that takes every category.
ALL = "all"
# Posts a retry reads past the cursor of a failed post's page before it gives the post up for
# this run: the post was on that page, and a page holds at most a few dozen, so a hundred
# allows for posts the site inserted since.
RETRY_WINDOW = 100
_FORMATTER = string.Formatter()

T = TypeVar("T")
# What a lazy post's files may raise that fails the post alone, never its target.
# Errors a lazy post's files may raise that fail only that post (`Blocked` and `LoginError`
# still stop the plugin).
_POST_ERRORS: tuple[type[BaseException], ...] = (NotFound, Changed, Hidden, NetworkError, SiteError)


@dataclass(frozen=True)
class Placed:
    """Where one file of a post goes and what it is keyed by, before download."""

    post: Post
    file: File
    num: int
    path: str
    key: str
    kind: str | None
    values: Mapping[str, Any]


@dataclass(frozen=True)
class TargetReport:
    """Summary of one command's execution for a single target.

    `error` is what ended the target: a `flnd.Error` (`str(error)` is its message), `Stopped`
    when Ctrl-C ended it, or None when it finished. A file that failed alone doesn't set it.
    """

    target: Target
    written: tuple[Path, ...]
    skipped: int
    error: Error | Stopped | None = None
    # Targets its listings yielded, such as a post's link to another site, for the engine to
    # download after the run's own targets; each as the source wrote it, not yet resolved.
    found: tuple[Target, ...] = ()


def failure(error: BaseException, target: Target, source: Plugin | None = None) -> Error:
    """What a failure that ended `target` is, as a `flnd.Error` (see `flnd.errors.classify`).

    One more rule than `classify`'s: a 401 or 403 from the source's own domains that a listing
    let escape is a `LoginError` for that source, since that is what a site's login wall
    answers. The same status from another host (a CDN's expired link) stays a `SiteError`.

    Args:
        error: what ended the target; never a `KeyboardInterrupt` (the caller re-raises those).
        target: the target it ended.
        source: the target's source, whose `domains` tell a login wall; None to skip that rule.

    Returns:
        The `Error`, with `target` set.
    """
    made = classify(error, target=str(target))
    assert isinstance(made, Error), made
    if (source is not None and type(made) is SiteError and made.status in LOGIN_WALLS
            and _on_domains(made, source.domains)):
        login = logged_out(target, f"{source.code}: the site refused the login "
                                   f"(HTTP {made.status})")
        login.__cause__ = made
        return login
    return made


async def tell(target: Target, error: Error,
               emit: Callable[[Event], Awaitable[None]]) -> None:
    """Report the failure that ended `target`: a `Failed` event, and debug lines for `-v`.

    The event is what the CLI and a script show; the debug lines add the error's causes.

    Args:
        target: the target it ended.
        error: the failure, as `failure` made it.
        emit: the run's event sink (`RunShared.emit`), which gets one `Failed` event.
    """
    await emit(Failed(target, error))
    ui.debug("{target}: {error}", target=str(target), error=error)
    ui.debug("{target}: {chain}", target=str(target), chain=causes(error))


def logged_out(target: Target, message: str | None = None) -> LoginError:
    """The `LoginError` of a target whose source needs a login it doesn't have, or lost.

    Args:
        target: the target that can't go on.
        message: what happened; None for "not logged in". The target isn't in it: it is the
            error's `target`, and `tell` puts it in front.

    Returns:
        A `LoginError` whose hint is the command that logs in again.
    """
    return LoginError(message or "not logged in", hint=f"run flnd auth login {target.plugin}",
                      target=str(target))


def _on_domains(error: SiteError, domains: Sequence[str]) -> bool:
    """Whether the response `error` carries came from one of `domains`."""
    url = getattr(error.response, "url", None)
    if not isinstance(url, str):
        return True
    host = (urlsplit(url).hostname or "").lower()
    return any(host == domain or host.endswith("." + domain) for domain in domains)


def wanted(route: Route, options: Options) -> tuple[Category, ...]:
    """The categories of `route` that `--only` selects, in the order the source declares them.

    Args:
        route: the target's route.
        options: the target's options; `categories` None or naming `all` takes every one.

    Returns:
        The categories; a name `--only` gives that the route lacks is left out (`validate`
        refused it before the run).
    """
    names = options.categories
    if names is not None and ALL in names:
        return route.categories
    if names is None:
        # The default is the account's own content (`own`).
        return tuple(category for category in route.categories if category.own)
    selected = set(names)
    return tuple(category for category in route.categories if category.name in selected)


async def posts(source: Plugin, site: Ctx, target: Target,
                options: Options) -> AsyncIterator[Post]:
    """Every post of `target`'s route, or of each wanted category, that `--when` keeps.

    It is the plain listing, with none of the crawl's stops at saved posts:
    `tests/plugin_kit.py` and a plan use it. The route is called once, and each category
    gets what it returned.

    Args:
        source: the target's source.
        site: the `Ctx` the route and categories are given.
        target: the target.
        options: its options: `--when` and `--only`.

    Yields:
        Each post, filed under its category, in the order the source yields them.
    """
    span = Range(options.since, options.until)
    route = next(r for r in source.routes if r.kind == target.kind)
    categories = wanted(route, options)
    if route.categories and not categories:
        return
    context = await _context(source, route, site, target, options.link)
    if not route.categories:
        async with contextlib.aclosing(_Kept(context, "", newest_first=route.newest_first,
                                             span=span)) as kept:
            async for post in kept:
                yield post
        return
    for category in categories:
        method = getattr(source, category.name)
        async with contextlib.aclosing(_Kept(
                await _called(method, site, context), category.name,
                newest_first=category.newest_first, span=span)) as kept:
            async for post in kept:
                yield post


async def _context(source: Plugin, route: Route, site: Ctx, target: Target,
                   link: str | None = None) -> Any:
    """Call `target`'s route method on the loop."""
    return await _called(getattr(source, route.kind), site, link or target.value)


async def _called(method: Callable[..., Any], *args: Any) -> Any:
    """`method(*args)`: an async generator function's call only makes the generator, so
    nothing of the plugin runs until it is stepped; a coroutine function's is awaited."""
    if inspect.iscoroutinefunction(method):
        return await method(*args)
    return method(*args)


def _site(run: PluginRun, target: Target, since: datetime.date | None = None,
          until: datetime.date | None = None, *, again: bool = False) -> Ctx:
    """The `Ctx` a target's route and categories act through."""
    return Ctx(run, target, when=Range(since, until), again=again)


def _say(target: Target, level: str, message: str, **fields: Any) -> None:
    """A line about a target, its text in front as a field so a value can't break the format."""
    getattr(ui, level)("{target}: " + message, target=str(target), **fields)


async def plan(run: PluginRun, target: Target, options: Options) -> AsyncIterator[Placed]:
    """Every file a download of `target` would save, placed, with nothing written (`--dry`).

    A lazy post's files are listed, which may cost a request each. `--limit` counts posts.

    Args:
        run: the source's run.
        target: the target.
        options: its options: `--when`, `--only`, `--limit`.

    Yields:
        Each file's `Placed`: its path under the target's folder and its history key.
    """
    site = _site(run, target, options.since, options.until, again=options.again)
    route = next(r for r in run.source.routes if r.kind == target.kind)
    categories = {c.name: c for r in run.source.routes for c in r.categories}
    taken = 0
    try:
        async with contextlib.aclosing(posts(run.source, site, target, options)) as stream:
            async for post in stream:
                if options.limit is not None and taken >= options.limit:
                    break
                taken += 1
                if options.query and not route.categories:
                    post = _Filed.bare(post)
                for placed in place(run.source, post, await _files(run, site, post),
                                    category=categories.get(post.category), route=route,
                                    target=target, options=options):
                    yield placed
    finally:
        if not site._idle:
            await run.off(site.close)


async def _files(run: PluginRun, site: Ctx, post: Post) -> list[File]:
    """A post's files: a lazy post's listed with tabs of their own (`Ctx.aside`)."""
    if not callable(post.files):
        return list(post.files)
    async with site.aside():
        found = list(await post.files())
    stray = next((file for file in found if not isinstance(file, File)), None)
    if stray is not None:
        raise PluginError(f"{run.source.code}: post {post.id}'s files hold a "
                          f"{type(stray).__name__}; wrap each in File(...)")
    return found


def place(
    source: Plugin, post: Post, files: Sequence[File], *, category: Category | None = None,
    route: Route | None = None, target: Target | None = None, options: Options | None = None,
    typed: Callable[[File], str] | None = None,
) -> list[Placed]:
    """Give each file of `post` its path under the target's folder and its history key.

    The path is the folder template (category, else route, else source) filled with the
    file's fields, then its name (`_named`). Two files of one post with one name get
    the later one's number added, so neither is skipped as already queued. The key template's
    default is the path itself.

    Args:
        source: the post's source, whose templates apply when its category has none.
        post: the post.
        files: its files, in order; a file's number is its index.
        category: the post's category; None for a route's own posts.
        route: the target's route, whose `folder` comes before the source's.
        target: the target, for the `{source}` and `{target}` fields; None for the source's.
            A single post's `{target}` is its owner, else its id.
        options: the target's options, for the `{query}` field of a search's found target.
        typed: a file's `Content-Type` where known (the test kit); None for none.

    Returns:
        One `Placed` per file, in order.
    """
    folder = _folder_format(source, route, category, options)
    # A category the folder value leaves out keeps its default place, under `{target}`; its
    # history key still reads the path as the default place gives it.
    fallback = _falls_back(category, options)
    # A single post (a link) sits in its owner's folder: `{target}` is the owner, whatever
    # template its category has; with no owner it is the post's id, where it always went.
    owned = (_rooted(source, target, options) and not (route is not None and route.categories))
    if owned and not fallback:
        folder = f"{{target}}/{folder}" if folder else "{target}"
    filename, key = (_format(source, category, name) for name in ("filename", "key"))
    posted = day(post.date)
    placed: list[Placed] = []
    used: set[str] = set()
    for num, file in enumerate(files):
        extension, kind = planned(file, typed(file) if typed else "")
        values = _file_values(
            source, post, num, len(files), posted, extension, target, options,
        )
        if owned and post.owner:
            values["target"] = post.owner
        name = _named(post, file, filename, values, extension)
        where = fill(folder, values)
        path = "/".join(part for part in (where, name) if part)
        # Files of one post may share a name (`cover.jpg`): a later one takes its number, or it
        # would be skipped as already queued.
        if path in used:
            name = f"{PurePosixPath(name).stem}_{num}{PurePosixPath(name).suffix}"
            path = "/".join(part for part in (where, name) if part)
        used.add(path)
        kept = path.partition("/")[2] if fallback and values["target"] and "/" in path else path
        where_kept, _, file_kept = kept.rpartition("/")
        resolved_key = fill(key, {**values, "path": kept, "dir": where_kept, "file": file_kept},
                            safe=False) or kept
        placed.append(Placed(post, file, num, path, resolved_key, kind, values))
    return placed


def _file_values(
    source: Plugin, post: Post, num: int, count: int, posted: datetime.date | None,
    extension: str, target: Target | None, options: Options | None,
) -> dict[str, Any]:
    """The fields a folder, filename or key template of one file may name."""
    code = target.plugin if target is not None else source.code
    route = _route_of(source, target)
    target_val = (store.folder_name(target, route is not None and route.single)
                  if target is not None else "")
    query_val = (options.query or "") if options is not None else ""
    # `{source}` is the older name of `{plugin}`, kept for templates written with it.
    return {
        **post.fields, "post": post.id, "date": posted, "category": post.category,
        "num": num, "count": count, "index": "" if count == 1 else f"_{num}",
        "extension": extension, "source": code, "plugin": code, "target": target_val,
        "query": query_val,
    }


def _named(
    post: Post, file: File, filename: str, values: Mapping[str, Any], extension: str
) -> str:
    """A file's name (5.3): its `name` when it has its own extension (or holds text),
    else its `name` or the filename template as a stem, with `extension` after it."""
    if file.content is not None or own_extension(file.name):
        return text.filename(file.name or post.id)
    stem = file.name if file.name is not None else fill(filename, values)
    return text.filename(stem or post.id) + extension


def own_extension(name: str | None) -> str:
    """The extension a `File.name` keeps (`EXTENSION`); "" for a stem or no name."""
    if name is None:
        return ""
    suffix = PurePosixPath(name).suffix
    return suffix if EXTENSION.fullmatch(suffix) else ""


class Ledger:
    """One target's plan of what to download: which files are new, skipped, linked or held.

    The crawl hands it each post (`take`); it places the post's files, skips those the history
    or the disk already has, links a file saved in another category's folder, and returns the
    rest for `Downloads`. It also dates posts the site gave no day: a post is held until its
    category ends, then given the day its key's row has, the source's `date` hook's answer, or
    its nearest dated neighbour's in listing order (marked estimated).

    What reads or writes the disk or `flnd.db` runs off the loop, one hop a post
    (`PluginRun.off`): the crawl awaits it, so nothing else touches the ledger meanwhile. The
    `date` hook runs on the loop, with tabs of its own (`Ctx.aside`).
    The `Skipped` events the plan makes are sent on the loop after each hop (`_tell`).

    Args:
        run: the source's run: its source and settings (the home, the database, the sink).
        site: the target's site, for warnings and the source's saved state.
        target: the target.
        tally: the run's live counts, which its counts go to.
        options: the target's options, for `--again` and the templates' fields; None for none.
    """

    def __init__(
        self, run: PluginRun, site: Ctx, target: Target, tally: Tally, *,
        options: Options | None = None,
    ) -> None:
        """Initialize a target ledger with its stores, dates, and category state."""
        self.run, self.source, self.settings, self.site = run, run.source, run.settings, site
        self.target, self.tally, self._options = target, tally, options
        self._custom_folder = options is not None and options.folder is not None
        self.folder = target_root(self.settings.home, target, options, self.source)
        # The home's database; None for a plan with nothing to read (`flnd.dry`).
        self._db = self.settings.db
        # Told to ask again for everything: files recorded gone are asked for once more.
        self._again = options is not None and options.again
        self._route = next((r for r in self.source.routes if r.kind == target.kind), None)
        self._categories = {c.name: c for r in self.source.routes for c in r.categories}
        # A search's hit that is a single post goes to the search's folder, not its owner's.
        self._hit = bool(options and options.query and self._route
                         and not self._route.categories)
        # The files the run queued, by path and key: every target's (`Claims`).
        self._queued: Claims = self.settings.claims or Claims()
        self._queued_keys: set[str] = set()
        self._dates: dict[str, datetime.date] = {}
        self._waiting: dict[str, Post] = {}
        self._order: list[tuple[str, datetime.date | None]] = []
        self._last_dated: tuple[str, datetime.date] | None = None
        self._estimated: set[str] = set()
        self._asking = type(self.source).date is not Plugin.date
        self._link_warned = False
        # Whether a post with no owner is yet to be told of: a single post filed by owner.
        # A plugin with no profile route has no owners to find, so it never says so.
        self._ownerless = (_rooted(self.source, target, options)
                           and not (self._route is not None and self._route.categories)
                           and any(r.categories for r in self.source.routes))
        self._later: list[tuple[Path, str, str]] = []
        # The posts the `date` hook had no day for, read off the loop by `load`.
        self._asked: dict[str, float] = {}
        # The rows of files found on disk with none, written with their post's (`_held`).
        self._unrecorded: list[store.FileRow] = []
        # The `Skipped` events made off the loop, sent by `_tell`.
        self._told: list[Skipped] = []
        # Whether the files queued now are a retry's (`_retry_failed`), which their `Saved`
        # events say.
        self.retrying = False

    def load(self) -> None:
        """Read what the ledger keeps between runs: the posts the `date` hook had no day for.

        Off the loop: it reads `flnd.db`.
        """
        if self._asking:
            if self.run.asked is None:
                asked(self.run)
            self._asked = self.run.asked if self.run.asked is not None else {}

    async def take(self, post: Post, *, hold: bool = True) -> tuple[Outcome, list[_Queued]]:
        """Plan one post: the files to download, after skipping what is saved.

        1. Date it (`_dated`): an undated post may be held until its category ends.
        2. A lazy post (files listed only on demand) is checked by its keys first, so a saved
           one costs no request for its files.
        3. Place its files and queue those not saved (`_queue_post_files`).

        Args:
            post: the post, as the listing yielded it.
            hold: whether an undated post may be held for a day; False when it is released.

        Returns:
            What became of the post, and its files to download (empty unless `QUEUED`).
        """
        if self._hit:
            post = _Filed.bare(post)
        category = self._categories.get(post.category)
        first = self._first_key(post, category)
        dated, skipped = await self.run.off(self._before_files, post, category, hold)
        await self._tell()
        if dated is None:
            return Outcome.EMPTY, []
        if skipped is not None:
            return skipped, []
        listed = await _files(self.run, self.site, post)
        planned = await self.run.off(self._queue_post_files, dated, listed, category, first)
        await self._tell()
        return planned

    def _before_files(self, post: Post, category: Category | None,
                      hold: bool) -> tuple[Post | None, Outcome | None]:
        """`take`'s first two steps, off the loop: the post dated (None when it is held), and
        what became of a lazy one already saved (None when its files are to be listed)."""
        dated = self._dated(post, category, hold=hold)
        if dated is None or not dated.lazy:
            return dated, None
        skipped = self._saved_first(dated, category)
        if skipped is None and self._unkeyed(category) and self._db is not None \
                and not self._again \
                and self._db.post_saved(store.key(self.target), dated.id, dated.category):
            # Its keys are its files' paths, known only once listed (LOIBUS's archives): the
            # post's own rows say it was saved, so its files function, which may cost the
            # account a download from its quota, isn't called again.
            skipped = Outcome.SAVED
        if skipped is not None:
            self._skip(dated.category, dated.id)
            ui.debug("{post}: already saved", post=dated.id)
        return dated, skipped

    async def _tell(self) -> None:
        """Send the `Skipped` events the last hop made, on the loop."""
        told, self._told = self._told, []
        for event in told:
            await self.settings.emit(event)

    def _queue_post_files(
        self, post: Post, listed: Sequence[File], category: Category | None, first: str | None
    ) -> tuple[Outcome, list[_Queued]]:
        """Place each file of `post` and queue those not held, writing inline text files now."""
        found = 0
        todo: list[_Queued] = []
        if self._ownerless and not post.owner:
            self._ownerless = False
            _say(self.target, "info", "no owner found for post {post}; saved under its id",
                 post=post.id)
        for placed in place(
            self.source, post, listed, category=category, route=self._route,
            target=self.target, options=self._options,
        ):
            dest = checked_dest(self.folder, placed.path, check=self._custom_folder)
            if placed.file.content is not None:
                dest.parent.mkdir(parents=True, exist_ok=True)
                files.write(dest, placed.file.content)
                continue
            item_key = f"{self.source.code}:{placed.key}"
            held = self._held(dest, item_key, placed)
            if held is not None:
                if (
                    dest not in self._queued
                    and item_key not in self._queued
                    and item_key not in self._queued_keys
                ):
                    found += 1
                self._skip_held(post, placed.path, dest, item_key, held)
                continue
            # Another target of the run may have queued the same file meanwhile.
            holder = self._queued.claim(dest, item_key, placed.path)
            if holder is not None:
                self._skip_held(post, placed.path, dest, item_key, holder)
                continue
            self.tally.count(self.target, post.category, found=1)
            # The cursor of the page the post is on, which a failure keeps for its retry.
            listing = self.site.listing
            self._queued_keys.add(item_key)
            todo.append(_Queued(placed, dest, item_key, estimated=first in self._estimated,
                                cursor=listing.cursor if listing is not None else None,
                                retried=self.retrying))
        if self._unrecorded and self._db is not None:
            rows, self._unrecorded = self._unrecorded, []
            self._db.save(rows)
        if todo:
            todo.sort(key=lambda queued: queued.placed.kind != files.VIDEO)
            return Outcome.QUEUED, todo
        return (Outcome.SAVED if found else Outcome.EMPTY), []

    def _skip_held(self, post: Post, name: str, dest: Path, item_key: str, held: str) -> None:
        """Count a skipped file and link it when held in another category's folder."""
        category = post.category
        self._skip(category, post.id, dest)
        if held == name:
            ui.debug("skip {name}", name=name)
            return
        ui.debug("skip {name} -> {held}", name=name, held=held)
        if item_key in self._queued:
            self._later.append((dest, item_key, category))
            self.tally.count(self.target, category, later=1)
        else:
            candidate = self.folder.joinpath(*held.split("/"))
            self._link(dest, candidate if candidate.is_file() else Path(held), category, item_key)

    async def lost(self, post: Post, error: BaseException) -> None:
        """A lazy post whose files the site refused: a failure of that post alone.

        Its `failures` row is under the post's first key, so a later run retries it; a
        `NotFound` marks it gone, so later runs skip it unless told to ask again.

        Args:
            post: the post.
            error: what its files raised.
        """
        category = self._categories.get(post.category)
        failed = classify(error, target=str(self.target))
        key = self._first_key(post, category) or f"{self.source.code}:{post.id}"
        dest = self.folder / post.category / post.id
        listing = self.site.listing
        if self._db is not None:
            await self.run.off(functools.partial(
                self._db.fail, run=self.settings.run, item=key, target=store.key(self.target),
                error=failed, post=post.id, category=post.category, file=str(dest),
                cursor=listing.cursor if listing is not None else None))
        self.tally.count(self.target, post.category, found=1, failed=1)
        ui.debug("{post}: {error}", post=post.id, error=failed)
        await self.settings.emit(Failed(self.target, failed, post=post.id))

    def link_queued(self, written: Mapping[str, Path]) -> None:
        """Make the links held back because their file was still queued in this run.

        Called once the downloads finished, off the loop. The file's row gives its final path,
        since a step after the download (extracting an archive) may have moved it; a file that
        never landed gets no link.

        Args:
            written: each landed file's path, by history key.
        """
        for dest, item_key, category in self._later:
            holder = written.get(item_key)
            # A step after the download may have moved the file; its row says where.
            found = self._db.known(item_key) if self._db else None
            if found is not None and found[0]:
                holder = Path(found[0])
            if holder is not None:
                target_dest = dest.with_suffix(holder.suffix)
                if ((found_day := LEADING_DAY.match(holder.name)) is not None
                        and not LEADING_DAY.match(target_dest.name)):
                    target_dest = target_dest.with_name(f"{found_day[0]}{target_dest.name}")
                self._link(target_dest, holder, category, item_key)
            self.tally.count(self.target, category, later=-1)
        self._later = []

    def _link(self, dest: Path, holder: Path, category: str, item_key: str) -> None:
        """Link `holder` at `dest`, in another category's folder, and record the link."""
        if not holder.is_file() or not _distinct_folders(self.folder, dest, holder):
            return
        if dest.is_symlink() and not dest.exists():
            dest.unlink(missing_ok=True)
        elif dest.exists() or dest.is_symlink():
            return
        dest.parent.mkdir(parents=True, exist_ok=True)
        if not _try_link(dest, holder):
            if not self._link_warned:
                self._link_warned = True
                _say(self.target, "warn", "can't link files found in two folders here")
            return
        if self._db is not None:
            self._db.link(item_key, dest, category)
        self.tally.count(self.target, category, linked=1)
        ui.debug("link {name} -> {held}", name=self._name(dest), held=self._name(holder))

    async def release(self) -> list[Post]:
        """Hand back the posts held for a day, as their category ends, each dated if it can be.

        A post's day comes from the listing (its key seen dated later), the source's `date`
        hook, or else its nearest dated neighbour in listing order, marked estimated so a later
        run may date it for real (`redate`).

        Returns:
            The held posts, in the order they were held; one with no day at all stays undated.
        """
        waiting, self._waiting = self._waiting, {}
        nearest = self._nearest_dates() if waiting else {}
        released: list[Post] = []
        for item_key, post in waiting.items():
            found = self._dates.get(item_key) or await self._ask(post)
            if found is None and item_key in nearest:
                found = nearest[item_key]
                self._estimated.add(item_key)
            released.append(post if found is None else post.replace(date=found))
        self._order.clear()
        self._dates.clear()
        self._last_dated = None
        return released

    async def redate(self) -> int:
        """Date the target's saved files that have no day, or an estimated one (a sync's start).

        Only for a source with a `date` hook. Each such row's post is asked once; the answer
        renames the file when its filename template starts with `{date}_`, sets its modified
        time, and updates its row, off the loop. A post the hook had no day for isn't asked
        again for 30 days (`ASKED_TTL`).

        Returns:
            How many files were dated.
        """
        if not self._asking or self._db is None:
            return 0
        dated = 0
        # A partial index holds only undated rows, so a target with none costs one lookup.
        for row in await self.run.off(self._db.undated, store.key(self.target)):
            if row.post_id in self._asked:
                continue
            found = await self._ask(Post(row.post_id))
            if found is not None and await self.run.off(self._redate, row, found):
                dated += 1
        await self.run.off(self._save_asked)
        if dated:
            _say(self.target, "info", "{files} dated", files=text.count(dated, "saved file"))
        return dated

    def _redate(self, row: store.FileRow, found: datetime.date) -> bool:
        """Rename one saved file for its day, set its time and update its row; off the loop."""
        assert self._db is not None
        filename_tpl = _format(self.source, self._categories.get(row.category), "filename")
        path = _redated(Path(row.path), found, filename_tpl, estimated=row.estimated)
        if path is None:
            return False
        _set_mtime(path, found)
        self._db.redate(row.key, path, found)
        return True

    def _save_asked(self) -> None:
        """Persist the 30-day `asked` cache to `site.state` when non-empty; off the loop."""
        if self._asked:
            self.site.state[ASKED_KEY] = dict(self._asked)
    async def _ask(self, post: Post) -> datetime.date | None:
        """Ask the source's `date` hook for `post`'s day; a miss isn't asked again for 30 days."""
        if not self._asking:
            return None
        try:
            async with self.site.aside():
                found = day(await self.source.date(self.site, getattr(post, "post", post)))
        except (SiteError, NetworkError, LoginError) as error:
            blocked = isinstance(error, (LoginError, Blocked))
            if blocked or getattr(error, "status", None) in BLOCKS:
                self._asking = False
                _say(self.target, "warn", "dating stopped: {error}; the rest are estimated",
                     error=error)
            else:
                ui.debug("{post}: not dated: {error}", post=post.id, error=error)
            return None
        if found is None:
            self._asked[post.id] = time.time()
        return found

    def _nearest_dates(self) -> dict[str, datetime.date]:
        """For each held undated post, the day of its nearest dated post in listing order."""
        n = len(self._order)
        left: list[tuple[int, datetime.date] | None] = [None] * n
        last: tuple[int, datetime.date] | None = None
        for i, (_, known) in enumerate(self._order):
            if known is not None:
                last = (i, known)
            else:
                left[i] = last
        nearest: dict[str, datetime.date] = {}
        nxt: tuple[int, datetime.date] | None = None
        for i in range(n - 1, -1, -1):
            item_key, known = self._order[i]
            if known is not None:
                nxt = (i, known)
                continue
            prev = left[i]
            if prev is not None and (nxt is None or i - prev[0] <= nxt[0] - i):
                nearest.setdefault(item_key, prev[1])
            elif nxt is not None:
                nearest.setdefault(item_key, nxt[1])
        return nearest

    def _dated(self, post: Post, category: Category | None, *, hold: bool) -> Post | None:
        """Give `post` a day if one is known, or hold it until its category ends."""
        item_key = self._first_key(post, category)
        if item_key is None:
            return post
        known = day(post.date)
        if known is not None:
            if hold:
                if self._waiting:
                    self._order.append((item_key, known))
                    self._dates[item_key] = known
                else:
                    self._last_dated = (item_key, known)
            self._waiting.pop(item_key, None)
            return post
        found = self._dates.get(item_key)
        if found is None and self._db is not None:
            found = self._db.day(item_key)
        if found is not None:
            self._dates[item_key] = found
            if hold:
                if self._waiting:
                    self._order.append((item_key, found))
                else:
                    self._last_dated = (item_key, found)
            return post.replace(date=found)
        if not hold:
            return post
        if item_key in self._waiting:
            self._skip(post.category, post.id)
            ui.debug("{post}: listed twice", post=post.id)
            return None
        if not self._order and self._last_dated is not None:
            self._order.append(self._last_dated)
            self._dates[self._last_dated[0]] = self._last_dated[1]
        self._order.append((item_key, None))
        self._waiting[item_key] = post
        return None

    def _first_key(self, post: Post, category: Category | None) -> str | None:
        """The history key of `post`'s first file, from the post's fields alone."""
        template = _format(self.source, category, "key")
        if not template or _fields(template) & (LISTED_FIELDS | {"date"}):
            return None
        values = {**post.fields, "post": post.id, "category": post.category, "num": 0}
        built = fill(template, values, safe=False)
        return f"{self.source.code}:{built}" if built else None

    def _unkeyed(self, category: Category | None) -> bool:
        """Whether a file's key needs its listed path: no key from the post's fields alone."""
        template = _format(self.source, category, "key")
        return not template or bool(_fields(template) & LISTED_FIELDS)

    def _saved_first(self, post: Post, category: Category | None) -> Outcome | None:
        """Whether a lazy post's files are all saved, judged by their keys, before listing them."""
        template = _format(self.source, category, "key")
        if not template or _fields(template) & LISTED_FIELDS:
            return None
        values = {**post.fields, "post": post.id, "date": day(post.date),
                  "category": post.category}
        keys = [
            f"{self.source.code}:{fill(template, {**values, 'num': num}, safe=False)}"
            for num in range(2)
        ]
        if keys[0] == f"{self.source.code}:" or self._held(None, keys[0]) is None:
            return None
        count = self._db.files_of_post(keys[0]) if self._db is not None else None
        checked = [keys[0]]
        if isinstance(count, int) and count > 1 and keys[1] != keys[0]:
            checked += [
                f"{self.source.code}:{fill(template, {**values, 'num': num}, safe=False)}"
                for num in range(1, count)
            ]
            if any(self._held(None, k) is None for k in checked[1:]):
                return None
        return (
            Outcome.EMPTY
            if any(k in self._queued or k in self._queued_keys for k in checked)
            else Outcome.SAVED
        )

    def _skip(self, category: str, post: str, path: Path | None = None) -> None:
        """Count one file (or post) found and skipped, and make its `Skipped` event (`_tell`)."""
        self.tally.count(self.target, category, found=1, skipped=1)
        self._told.append(Skipped(self.target, category, post, path))

    def _held(self, dest: Path | None, item_key: str, placed: Placed | None = None) -> str | None:
        """The name of what holds a file already: queued, on disk, saved, or gone."""
        for known in (dest, item_key):
            holder = self._queued.get(known) if known is not None else None
            if holder is not None:
                return holder
        on_disk = _on_disk(dest) if dest is not None else None
        if dest is not None and on_disk is not None:
            if placed is not None and self._db is not None and self._db.known(item_key) is None:
                self._unrecorded.append(self._row(dest, item_key, placed, on_disk))
            return self._name(dest)
        found = self._db.known(item_key) if self._db is not None else None
        if found is None:
            return None
        path, state, folder = found
        if state == store.GONE:
            return None if self._again else self._name(Path(path))
        if Path(path).is_file():
            return self._name(Path(path))
        return self._name(Path(folder)) if folder and Path(folder).is_dir() else None

    def _row(self, dest: Path, item_key: str, placed: Placed,
             found: os.stat_result) -> store.FileRow:
        """The row of a file found on disk with none: its size and time from its `stat`."""
        posted = day(placed.post.date)
        count = placed.values.get("count", 1)
        return store.FileRow(
            item_key, store.key(self.target), str(dest), found.st_size, found.st_mtime_ns,
            post_id=placed.post.id, category=placed.post.category,
            date=posted.isoformat() if posted else None, kind=files.kind_of(dest.name),
            at=store.now(),
            files=count if placed.num == 0 and isinstance(count, int) and count > 1 else None)

    def _name(self, path: Path) -> str:
        """A path as the debug lines and links name it."""
        return (
            path.relative_to(self.folder).as_posix()
            if path.is_relative_to(self.folder)
            else str(path)
        )


class Claims:
    """The files a run has queued, by path and by key, shared by every target's `Ledger`.

    A file one target queued is held for the others, as it would be once saved. The Ledgers
    plan off the loop, several at once, so it keeps its map behind a lock.
    """

    def __init__(self) -> None:
        self._held: dict[Path | str, str] = {}
        self._lock = threading.Lock()

    def __contains__(self, known: object) -> bool:
        with self._lock:
            return known in self._held

    def get(self, known: Path | str) -> str | None:
        """The name of the queued file `known` (a path or a key) is; None for none."""
        with self._lock:
            return self._held.get(known)

    def claim(self, dest: Path, key: str, name: str) -> str | None:
        """Queue the file at `dest` keyed `key` as `name`, unless one of the two is queued.

        Returns:
            None once it is queued; the queued file's name when another target had it.
        """
        with self._lock:
            for known in (dest, key):
                if known in self._held:
                    return self._held[known]
            self._held[dest] = self._held[key] = name
            return None

    def release(self, dest: Path, key: str) -> None:
        """Release a finished file's claim so only in-flight files are held."""
        with self._lock:
            self._held.pop(dest, None)
            self._held.pop(key, None)


def asked(run: PluginRun) -> None:
    """Read the posts the plugin's `date` hook had no day for, asked in the last 30 days, once
    per plugin per run (`PluginRun.asked`), off the loop; nothing for a plugin with no hook.

    Entries older than `ASKED_TTL` are dropped.
    """
    if run.asked is not None or type(run.source).date is Plugin.date:
        return
    now = time.time()
    raw = run.values.get(ASKED_KEY)
    run.asked = {} if not isinstance(raw, dict) else {
        str(k): float(v) for k, v in raw.items()
        if isinstance(v, (int, float)) and now - v < ASKED_TTL
    }


def _on_disk(path: Path) -> os.stat_result | None:
    """The `stat` of a file with bytes at `path`, from one call (P8); None for none."""
    try:
        found = path.stat()
    except OSError:
        return None
    return found if found.st_size > 0 and stat.S_ISREG(found.st_mode) else None


def _distinct_folders(root: Path, dest: Path, holder: Path) -> bool:
    """Whether two files sit in different category folders of one target."""
    if not (dest.is_relative_to(root) and holder.is_relative_to(root)):
        return False
    here = dest.relative_to(root).parts
    there = holder.relative_to(root).parts
    return len(here) >= 2 and len(there) >= 2 and here[0] != there[0]


def _try_link(dest: Path, holder: Path) -> bool:
    """Link `dest` to `holder`: a hard link, else a relative symlink where hard links fail."""
    try:
        os.link(holder, dest)
        return True
    except OSError:
        try:
            dest.symlink_to(os.path.relpath(holder, dest.parent))
            return True
        except OSError:
            return False


class Downloads:
    """One target's downloads: each post's files handed to the run's save (`save.Saver`).

    1. The crawl hands each post's new files over at once (`post`). The queue holds `QUEUE`
       times the target's places (`jobs.Limits.places`) files; past that, `post` waits, so a
       listing never runs far ahead of its downloads and memory stays flat.
    2. Each post becomes a task on the run's loop (`save.Saver.post`), whose files are `FILE`
       jobs of the run's scheduler: each waits for one of the target's places, then for its
       host's cap. The post's rows commit together once its last file settled.
    3. An expired link of a lazy post lists the post's files again (`_fresh`), on a thread of
       the source with a tab of its own, at any time: the listing needn't be running.
    4. What stops the run (a full disk, a missing tool) is kept as `fatal`: the listing stops
       at its next post, and the target ends with it.

    Args:
        run: the source's run: its settings, clients and scheduler.
        target: the target the files belong to.
        tally: the run's live counts, which its counts go to.
    """

    def __init__(self, run: PluginRun, target: Target, tally: Tally, *,
                 swept: Awaitable[None] | None = None,
                 site: Ctx | None = None,
                 ledger: Ledger | None = None) -> None:
        self.run, self.target, self.tally = run, target, tally
        # The target's Ctx, whose `aside` tabs a refresh lists on; None for none.
        self._site = site
        self._ledger = ledger
        # The sweep of the target's folder, which the first file waits for.
        self._swept = swept
        self.source, self.settings = run.source, run.settings
        # What stopped the run while a post saved; the listing raises it at its next post.
        self.fatal: BaseException | None = None
        # Set once the run stopped, so a file that hasn't started ends at once.
        self._halted = False
        self._written: dict[str, Path] = {}
        self._landed: list[Path] = []
        # Set each time a post settles, for whoever waits for room or for the end.
        self._changed = asyncio.Event()
        # Files handed over and not settled, and the keys of those whose transfer started.
        self._pending = 0
        self._started: set[str] = set()
        self._tasks: set[asyncio.Task[None]] = set()

    @property
    def written(self) -> dict[str, Path]:
        """Return each file written so far by its history key."""
        return dict(self._written)

    async def post(self, todo: Sequence[_Queued]) -> bool:
        """Hand one post's files to the run's save, once the queue has room.

        Args:
            todo: the post's files, placed, with their destinations and keys.

        Returns:
            True once they are handed over; False when the run was told to stop, and nothing
            was.

        Raises:
            Error: what stopped the run while an earlier post saved (`fatal`).
        """
        if self._swept is not None:
            await self._swept
            self._swept = None
        if not await self._room():
            return False
        saver = self.settings.saver
        items = [self._item(queued) for queued in todo]
        for queued in todo:
            self.tally.count(self.target, queued.placed.post.category, queued=1)
        self._pending += len(todo)
        task = asyncio.get_running_loop().create_task(self._post(saver, todo, items))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return True

    async def wait(self) -> None:
        """Wait until every post handed over is saved.

        After the first Ctrl-C, a warning says how many of the target's files are finishing.

        Raises:
            KeyboardInterrupt: the second Ctrl-C (`stop.now`); the running files stop through
                the client's own stop.
        """
        shown: str | None = None
        while True:
            if self.settings.stop.now.is_set():
                raise KeyboardInterrupt
            if not self._pending:
                return
            note = (FINISHING.format(files=text.count(len(self._started), "file"))
                    if self.settings.stop.soon.is_set() else None)
            if note is not None and shown is None:
                ui.warn("{target}: {note}", target=str(self.target), note=note)
            shown = note
            await self._settled()

    async def finish(self, error: BaseException | None = None) -> tuple[Path, ...]:
        """Wait for the downloads and return what landed; after a stop, drop the ones not
        started.

        Args:
            error: what ended the listing. After a listing that failed, every file handed over
                runs to its end, so none of the files it found is wasted. After a stop (`Stopped`,
                Ctrl-C) the files still queued are dropped, and the running ones finish.

        Returns:
            The paths written, in the order they landed.
        """
        if not isinstance(error, (Stopped, KeyboardInterrupt)):
            await self.wait()
        else:
            # The files still queued are dropped; the running ones finish (a second Ctrl-C
            # stops them through the client's stop), so what they fetched isn't lost.
            self._halted = True
            while self._pending:
                await self._settled()
        return tuple(self._landed)

    async def below(self, places: int) -> None:
        """Wait until at most `places` of the target's files are queued or downloading, or
        the run stops: its next listing may start then."""
        while (self._pending > places and self.fatal is None
               and not self.settings.stop.soon.is_set()):
            await self._settled()

    async def _settled(self) -> None:
        """Wait for a post to settle, or `WAKE` seconds, so a Ctrl-C is seen at once."""
        self._changed.clear()
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(self._changed.wait(), WAKE)

    def _item(self, queued: _Queued) -> save.Item:
        """The save's `Item` for a queued file."""
        placed = queued.placed
        post = placed.post
        count = placed.values.get("count", 1)
        category = (
            self._ledger._categories.get(post.category) if self._ledger is not None else None
        )
        filename_tpl = _format(self.source, category, "filename")
        dated_name = placed.file.name is None and filename_tpl.startswith("{date}_")
        return save.Item(
            queued.key, self.target, post.id, post.category, placed.file, queued.dest,
            kind=placed.kind, day=day(post.date), headers=post.headers,
            count=count if placed.num == 0 and isinstance(count, int) and count > 1 else None,
            estimated=queued.estimated, dated_name=dated_name,
            cursor=queued.cursor, retried=queued.retried,
            refresh=(lambda: self._fresh(queued)) if self._refreshes(post) else None,
            label=placed.path, dropped=lambda: self._dropped(queued),
            domain=self.source.domains[0] if self.source.domains else "")

    def _dropped(self, queued: _Queued) -> bool:
        """Whether a file whose transfer is about to start is dropped instead: the first Ctrl-C
        came, or the listing was stopped. Asked once, as it starts."""
        if self._halted or self.settings.stop.soon.is_set():
            return True
        self._started.add(queued.key)
        return False

    async def _post(self, saver: Any, todo: Sequence[_Queued], items: list[save.Item]) -> None:
        """One post's save on the loop; never an exception out."""
        try:
            written = await saver.post(items, client=self.run.files, plugin=self.source.code)
            self._landed.extend(written.values())
            if self._ledger is not None:
                needed = {k for _, k, _ in self._ledger._later}
                self._written.update({k: p for k, p in written.items() if k in needed})
            else:
                self._written.update(written)
        except asyncio.CancelledError:
            raise
        except Exception as error:
            if jobs.stops(error):
                self.fatal = self.fatal or error
            else:
                ui.error("{post}: {kind} {error}", post=todo[0].placed.post.id,
                         kind=type(error).__name__, error=error)
                ui.debug("{post}: {chain}", post=todo[0].placed.post.id, chain=causes(error))
        except BaseException:
            pass
        finally:
            self._pending -= len(todo)
            self._started.difference_update(queued.key for queued in todo)
            self._changed.set()

    async def _room(self) -> bool:
        """Wait for room in the target's queue (`QUEUE` times its places)."""
        limit = QUEUE * max(self.run.jobs, 1)
        while True:
            if self.fatal is not None:
                raise self.fatal
            if self.settings.stop.soon.is_set():
                return False
            if self._pending < limit:
                return True
            await self._settled()

    def _refreshes(self, post: Post) -> bool:
        """Whether a file of `post` can get a fresh link: a lazy post's, or any post's of a
        plugin with a `refresh` hook of its own."""
        return callable(post.files) or (
            self._site is not None and type(self.source).refresh is not Plugin.refresh)

    async def _fresh(self, queued: _Queued) -> File | None:
        """A post's files listed again, for a fresh copy of one link: the plugin's `refresh`
        hook, which lists a lazy post's files again by default.

        Returns:
            The fresh file, at the same number in the post; None when no fresh link came (the
            listing failed, or the post has fewer files now).
        """
        try:
            site = self._site
            assert site is not None
            post = queued.placed.post
            async with site.aside():
                fresh = await self.source.refresh(site, getattr(post, "post", post))
                found = list(await fresh.files() if fresh.lazy else fresh.files)
        except Exception as error:
            ui.warn("{name}: no fresh link: {error}", name=queued.placed.path, error=error)
            return None
        num = queued.placed.num
        return found[num] if num < len(found) else None


async def download(run: PluginRun, target: Target, tally: Tally, options: Options, *,
                   listed: Callable[[Downloads], Awaitable[None]] | None = None,
                   turn: Turn | None = None) -> TargetReport:
    """Download one target of a source, counting on `tally`: one coroutine on the run's loop.

    A failure of the listing fails the target alone: it is in the report, never raised.

    1. Sweep stale parts, make its ledger and downloads, and read its row.
    2. List it and wait for its downloads (`_execute_listing`), then make the held links.
    3. Keep its listings' cursors, heads and completeness (`Home.progress`); a found
       target of a search keeps nothing, since the search's row keeps what it found.
    4. A target that ran to its end clears its whole-target failure and recounts its files.
    5. Report it, and end its rows (`_ended`).

    Args:
        run: the source's run: its session, clients, scheduler and settings.
        target: the target, resolved.
        tally: the run's live counts, which its counts go to.
        options: its options: `--when`, `--only`, `--limit`, `--again`, sync and retry.
        listed: awaited once its listings ended, before its downloads are waited for, so the
            run can start the next target meanwhile; None for nothing.
        turn: its turn at its first step, its route's call (`Turn`); None for none.

    Returns:
        Its report: the files written, the skips, the targets its listings yielded, and the
        error that ended it (`Stopped` when the run was stopped before it began).
    """
    if run.settings.stop.soon.is_set():
        return TargetReport(target, (), 0, error=Stopped(STOPPED))
    started = time.monotonic()
    # No lock on the target: the home's one run owns every part. The sweep runs while the
    # listing starts, and ends before the first file is handed over.
    sweep_dir = _sweep_folder(run.settings.home, target, options)
    sweeping = (asyncio.ensure_future(run.off(files.sweep, sweep_dir))
                if sweep_dir is not None else None)
    site = _site(run, target, options.since, options.until, again=options.again)
    ledger = Ledger(run, site, target, tally, options=options)
    downloads = Downloads(run, target, tally, swept=sweeping, site=site, ledger=ledger)
    work = _Work(ledger, downloads)
    db = run.settings.db
    row = await run.off(_opened, ledger, db, target)
    progress = _Progress(row, again=options.again)
    error = await _execute_listing(work, run, site, target, options, progress, listed, turn)
    if sweeping is not None:
        try:
            await sweeping
        except Exception as failed:
            error = error or failed
    written = await downloads.finish(error)
    if error is None and downloads.fatal is not None:
        error = downloads.fatal
    await run.off(ledger.link_queued, downloads.written)
    if db is not None:
        finished = error is None and not run.settings.stop.soon.is_set()
        await run.off(_keep, db, target, options, progress, row, finished, bool(written))
    # What the plugin changed of its state, written back as each target ends.
    await run.off(run.save_state)
    report = TargetReport(target, written, tally.totals(target).skipped,
                          found=tuple(dict.fromkeys(work.linked)))
    return await _ended(run, tally, report, error,
                        complete=", ".join(progress.complete) or "none", started=started)


def _opened(ledger: Ledger, db: store.Store | None, target: Target) -> store.TargetRow | None:
    """What a target starts from, off the loop: its row, and what its ledger keeps."""
    ledger.load()
    return db.target(store.key(target)) if db is not None else None


def _keep(db: store.Store, target: Target, options: Options, progress: _Progress,
          row: store.TargetRow | None, finished: bool, written: bool) -> None:
    """Write where a target's listings stand, off the loop: a write may wait for the lock."""
    # A target a search found has no row of its own: the search's row keeps what it found.
    if not options.query:
        db.progress(target, cursors=progress.cursors(), complete=progress.complete,
                    heads=progress.heads)
    if finished:
        # A failure of the whole target is over once it ran to its end.
        db.clear(store.key(target))
    # Counting reads every row of the target (35 ms for 100,000), so a run that saved
    # nothing keeps the count it has, unless it has none yet.
    if not options.query and (written or (finished and (row is None or not row.files))):
        db.tally(target)


class _Progress:
    """Where a target's listings stood after earlier runs, and where this run leaves them."""

    def __init__(self, row: store.TargetRow | None, *, again: bool) -> None:
        earlier = row if row is not None and not again else None
        self.again = again
        # Each listing an earlier run read to its end, with the day it did.
        self.listed: Mapping[str, str] = earlier.complete if earlier is not None else {}
        # The cursor each cut-short listing was reading.
        self.started: Mapping[str, str] = earlier.cursors if earlier is not None else {}
        # The newest post each listing had when an earlier run read it from the top.
        self.seen_heads: Mapping[str, Any] = earlier.heads if earlier is not None else {}
        # This run's listings, by name ("" for a route's own), the ones it read to their end,
        # and their heads.
        self.listings: dict[str, Listing] = {}
        self.complete: list[str] = []
        self.heads: dict[str, dict[str, Any]] = {}

    def cursors(self) -> dict[str, str | None]:
        """Each listing's cursor to keep: the page it was reading, or None when it ran out.

        Returns:
            Each listing's cursor by name, as `Home.progress` takes them.
        """
        return {
            name: None if name in self.complete else listing.cursor
            for name, listing in self.listings.items()
        }


async def _execute_listing(
    work: _Work, run: PluginRun, site: Ctx, target: Target, options: Options,
    progress: _Progress, listed: Callable[[Downloads], Awaitable[None]] | None = None,
    turn: Turn | None = None,
) -> BaseException | None:
    """List the target, then wait for its downloads; any error that ended it is returned."""
    try:
        try:
            if options.stop_at_saved:
                await work.ledger.redate()
            await _read(work, run, site, target, options, progress, turn)
            work.ledger.tally.downloading(target)
        except KeyboardInterrupt:
            if not run.settings.stop.soon.is_set() or run.settings.stop.now.is_set():
                raise
        finally:
            if not site._idle:
                await run.off(site.close)
        if listed is not None:
            await listed(work.downloads)
        await work.downloads.wait()
        return None
    except asyncio.CancelledError:
        raise
    except BaseException as caught:
        return caught


async def _ended(
    run: PluginRun, tally: Tally, report: TargetReport, error: BaseException | None, *,
    complete: str, started: float,
) -> TargetReport:
    """End a target's rows and report it: failed, stopped, or done."""
    target = report.target
    if error is not None:
        if not isinstance(error, Exception):
            raise error
        failed = failure(error, target, run.source)
        if run.settings.db is not None:
            await run.off(_failed, run, target, failed)
        await tell(target, failed, run.settings.emit)
        tally.end(target, failed=True)
        return replace(report, error=failed)
    if run.settings.stop.soon.is_set():
        ui.debug("{target}: stopped", target=str(target))
        tally.end(target, failed=True)
        return replace(report, error=Stopped(STOPPED))
    tally.end(target, failed=False)
    _summary(tally.totals(target), target, time.monotonic() - started)
    ui.debug(
        "{target}: {written} written, {skipped} skipped; complete: {complete}",
        target=str(report.target), written=len(report.written),
        skipped=report.skipped, complete=complete,
    )
    return report


def _failed(run: PluginRun, target: Target, failed: Error) -> None:
    """Record a target's failure, with a scrubbed sample of a `Changed` answer, off the loop."""
    assert run.settings.db is not None
    save.sampled(run.settings, failed)
    run.settings.db.fail(run=run.settings.run, item=store.key(target), target=store.key(target),
                         error=failed)


def _summary(totals: Counts, target: Target, seconds: float) -> None:
    """A debug line for a finished target: files saved, size, skips, failures and time."""
    parts = [text.count(totals.saved, "file") + " saved"] if totals.saved else []
    if totals.bytes:
        parts.append(text.size(totals.bytes))
    if totals.linked:
        parts.append(f"{totals.linked} linked")
    if totals.skipped:
        parts.append(f"{totals.skipped} skipped")
    if totals.failed:
        parts.append(f"{totals.failed} failed (retried on the next runs)")
    body = ", ".join(parts) or "nothing new"
    line = f"{body} in {text.duration(seconds)}"
    ui.debug("{target}: {line}", target=str(target), line=line)


def causes(error: BaseException) -> str:
    """An error and its causes on one line, for the debug log: `A: … <- B: …`.

    It follows `__cause__`, else `__context__` unless suppressed, and stops at a loop.

    Args:
        error: the error.

    Returns:
        Each error's type and message, the outermost first.
    """
    links: list[str] = []
    seen: set[int] = set()
    current: BaseException | None = error
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        links.append(f"{type(current).__name__}: {current}")
        current = current.__cause__ or (
            None if current.__suppress_context__ else current.__context__
        )
    return " <- ".join(links)


def _redated(
    path: Path, found: datetime.date, filename: str, *, estimated: bool
) -> Path | None:
    """Rename a saved file for its new day, when its filename template starts with the day."""
    name = path.name
    leading = LEADING_DAY.match(name)
    if estimated and leading:
        name = name[leading.end():]
    if filename.startswith("{date}_") and not LEADING_DAY.match(name):
        name = f"{found.isoformat()}_{name}"
    dest = path.with_name(name)
    if dest == path or not path.exists():
        return path if path.exists() else None
    if dest.exists():
        if not filecmp.cmp(path, dest, shallow=False):
            return None
        path.unlink()
        return dest
    path.rename(dest)
    return dest


def _set_mtime(path: Path, dated: datetime.date) -> None:
    """Set a file's modified time to noon UTC of its post's day."""
    noon = datetime.datetime.combine(dated, datetime.time(NOON), tzinfo=datetime.UTC)
    stamp = noon.timestamp()
    try:
        os.utime(path, (stamp, stamp))
    except OSError as error:
        ui.debug("{name}: modified time not set: {error}", name=path.name, error=error)


@dataclass
class _Queued:
    """One file handed from the crawl to `Downloads`."""

    placed: Placed
    dest: Path
    key: str
    estimated: bool = False
    # The cursor of the page its post was listed on; None for the first page.
    cursor: str | None = None
    # Whether a retry queued it (`_retry_failed`).
    retried: bool = False

    def __str__(self) -> str:
        return self.placed.path


class _Kept:
    """The posts a route or category yields, filtered by `--when`: an async iterator."""

    def __init__(self, yielded: Any, category: str, *, newest_first: bool,
                 span: Range, stops: _Stops | None = None) -> None:
        self.category, self.newest_first, self.time = category, newest_first, span
        self.ran_out = False
        self.stopped_at_since = False
        # Whether a sync met 4 old posts in a row (`_Stops`) and stopped there.
        self.reached = False
        self._stops = stops or _Stops()
        # The targets the listing yielded, in order, each as the source wrote it.
        self.linked: list[Target] = []
        # The newest post listed by its time (the first of a tie), as `{"id", "ts"}`; the first
        # post that isn't pinned while none has a time: the head a later sync goes by.
        self.head: dict[str, Any] | None = None
        self._best: Any = None
        # Posts it queued files of, counted by `_queue_listed`: none means nothing new.
        self.queued = 0
        # The ids of the pinned posts it yielded.
        self.pinned: set[str] = set()
        self._steps = _steps(yielded)
        self._iter: AsyncIterator[Post] | None = None

    def __aiter__(self) -> AsyncIterator[Post]:
        if self._iter is None:
            self._iter = self._kept()
        return self._iter

    async def _kept(self) -> AsyncIterator[Post]:
        """Yield kept posts, stopping early after a sync's 4 old posts in a row, or after 5 older
        than `--when`, on `newest_first` feeds."""
        older = 0
        counted: str | None = None
        old_run = 0
        # A post a source yields in parts (TikTok yields each photo of a photo post as a post of
        # the same id) is one post to the run of older posts: only its first part counts.
        last: str | None = None
        async for post in self._steps:
            if isinstance(post, Target):
                self.linked.append(post)
                continue
            if not isinstance(post, Post):
                raise TypeError(
                    f"{self.category or 'a route'} yielded {type(post).__name__}, "
                    "not a Post or a Target"
                )
            pinned = post.pinned
            # Posts are ordered by `listed`, which is `date` unless given.
            listed = day(post.listed)
            # A single route has no categories: its post names its own, with its owner.
            post = _Filed(post, self.category or (post.owner and post.category) or "")
            posted = day(post.date)
            stale = False
            if pinned:
                self.pinned.add(post.id)
            else:
                self._note(post)
                if self._stops.sync and post.id != counted:
                    counted = post.id
                    old_run = old_run + 1 if self._stops.old(post) else 0
                    stale = old_run >= OLD_IN_A_ROW
            if posted in self.time:
                older = older if pinned else 0
                last = post.id
                yield post
                if stale:
                    self.reached = True
                    return
                continue
            if stale:
                self.reached = True
                return
            since = self.time.since
            if self.newest_first and since and listed and listed < since and not pinned:
                older += post.id != last
                last = post.id
                if older >= OLDER_IN_A_ROW:
                    self.stopped_at_since = True
                    return
        self.ran_out = True

    def _note(self, post: Any) -> None:
        """Keep `post`, one that isn't pinned, as the head if it is the first, or the newest."""
        moment = stamp(post.listed)
        when = moment if moment is not None else day(post.listed)
        if self.head is None or (when is not None and (
                self._best is None or _newer(when, self._best))):
            self.head, self._best = _head(post), when

    async def aclose(self) -> None:
        """Close the listing, and the generator under it."""
        if self._iter is not None:
            await self._iter.aclose()  # type: ignore[attr-defined]
        await self._steps.aclose()


class _Filed:
    """A post as the crawl files it: the post its plugin yielded, and its category."""

    __slots__ = ("category", "post")

    def __init__(self, post: Post, category: str) -> None:
        self.post, self.category = post, category

    def __getattr__(self, name: str) -> Any:
        return getattr(self.post, name)

    @staticmethod
    def bare(post: Any) -> Any:
        """A single post as a search's hit: with no owner and no category of its own."""
        if isinstance(post, _Filed) and post.category:
            return _Filed(post.post.replace(owner=None, category=None), "")
        return post

    def replace(self, **changes: Any) -> _Filed:
        """The post with `changes`, in the same category."""
        return _Filed(self.post.replace(**changes), self.category)


class Outcome(Enum):
    """Outcome of queuing one post into `Ledger`."""

    QUEUED = "queued"
    SAVED = "saved"
    EMPTY = "empty"
    # A lazy post whose files the site refused: it failed alone.
    FAILED = "failed"
    STOPPED = "stopped"


class Ended(Enum):
    """How one category listing ended."""

    RAN_OUT = "ran out"
    CAUGHT_UP = "caught up"
    CUT = "cut"
    STOPPED = "stopped"


@dataclass(frozen=True)
class _Work:
    """A target's `Ledger` and `Downloads`, for queueing kept posts, and the targets it found."""

    ledger: Ledger
    downloads: Downloads
    # The targets its listings yielded (`_Kept.linked`), in order.
    linked: list[Target] = field(default_factory=list)

    async def queue(self, post: Post, *, hold: bool = True) -> Outcome:
        """Plan one post (`Ledger.take`), then submit each of its new files for download.

        A lazy post whose files the site refuses (gone, hidden, or an answer that can't be
        read) fails alone (`_lost_post`); the listing goes on.

        Args:
            post: the post.
            hold: whether an undated post may be held for a day.

        Returns:
            What became of the post; `STOPPED` when the run stopped while its files queued.
        """
        try:
            outcome, todo = await self.ledger.take(post, hold=hold)
        except _POST_ERRORS as error:
            if not post.lazy or isinstance(error, Blocked):
                raise
            await self.ledger.lost(post, error)
            return Outcome.FAILED
        if todo and not await self.downloads.post(todo):
            return Outcome.STOPPED
        return outcome


async def discover(
    run: PluginRun, target: Target, *, limit: int | None = None, time_range: Range = Range(),
    again: bool = False, head: str | None = None, listing: Listing | None = None,
) -> AsyncIterator[Target]:
    """The targets a search (or any route that yields targets) finds, each once, resolved.

    Called by the engine for a search, and for its dry run. It reads the whole listing, past
    the targets earlier syncs saw (the caller skips those), so a sync that takes a slice of
    what is new reaches the rest on the next syncs. It stops at `limit`, or at Ctrl-C.

    Args:
        run: the source's run.
        target: the search.
        limit: at most this many targets; None for all.
        time_range: `--when`, which the route's `Ctx` filters by.
        again: whether `--again` was set.
        head: the newest previously seen child key for short-circuiting.

    Yields:
        Each new target, resolved by the source that owns it.
    """
    route = next(r for r in run.source.routes if r.kind == target.kind)
    site = _site(run, target, time_range.since, time_range.until, again=again)
    yielded: set[Target] = set()
    try:
        if _at_limit(run, yielded, limit):
            return
        site.listing = listing or Listing(None, label=f"{target}: {route.kind}", head=head)
        context = await _context(run.source, route, site, target)
        async with contextlib.aclosing(_discovered(run, route, site, context,
                                                   Options())) as steps:
            async for item in steps:
                if run.settings.stop.soon.is_set():
                    return
                child = await resolved(run, item)
                if child is None or child in yielded:
                    continue
                yielded.add(child)
                yield child
                if _at_limit(run, yielded, limit):
                    return
    finally:
        site.listing = None
        if not site._idle:
            await run.off(site.close)


async def _discovered(run: PluginRun, route: Route, site: Ctx, context: Any,
                      options: Options) -> AsyncIterator[Any]:
    """What a discovering route yields: its own listing, or each wanted category's in turn
    (`wanted`), given what the route returned."""
    if not route.categories:
        async with contextlib.aclosing(_steps(context)) as items:
            async for item in items:
                yield item
        return
    for category in wanted(route, options):
        listed = await _called(getattr(run.source, category.name), site, context)
        async with contextlib.aclosing(_steps(listed)) as items:
            async for item in items:
                yield item


def _at_limit(run: PluginRun, yielded: set[Target], limit: int | None) -> bool:
    """Return whether discovery should stop due to Ctrl-C or `limit`."""
    return run.settings.stop.soon.is_set() or (limit is not None and len(yielded) >= limit)


def _head(post: Any) -> dict[str, Any]:
    """A listing's head from its newest post: its id, and its time (Unix seconds when the post
    has a time of day, else its day as `YYYY-MM-DD`, else None)."""
    moment = stamp(post.listed)
    posted = day(post.listed)
    return {"id": post.id,
            "ts": moment if moment is not None else posted.isoformat() if posted else None}


def _newer(a: Any, b: Any) -> bool:
    """Whether time `a` is strictly after `b`: Unix seconds, or a day (days compare as days)."""
    if isinstance(a, float) and isinstance(b, float):
        return a > b
    return (day(a) or datetime.date.min) > (day(b) or datetime.date.min)


class _Stops:
    """Where a sync of a `newest_first` listing stops: after 4 old posts in a row. A post is old
    when it is the head, or strictly older than the head's time (never equal: posts that share a
    second or a day with the head may be new). A post with no time is never old by time, and a
    marked pinned post is skipped. No pin mark is trusted: the head is the newest post by time,
    and 4 in a row can't be pins.

    Args:
        sync: whether this pass stops at its head; a first download doesn't.
        head: what an earlier run stored: `{"id", "ts"}`, or the bare id an older home kept.
    """

    def __init__(self, sync: bool = False, head: Any = None) -> None:
        self.sync, self.raw = sync, head
        self.id: str | None = None
        self.ts: float | None = None
        self.day: datetime.date | None = None
        if isinstance(head, str):
            self.id = head
        elif isinstance(head, dict):
            self.id = head.get("id")
            when = head.get("ts")
            if isinstance(when, str):
                self.day = datetime.date.fromisoformat(when)
            elif isinstance(when, (int, float)) and not isinstance(when, bool):
                self.ts, self.day = float(when), day(when)

    def old(self, post: Any) -> bool:
        """Whether `post`, one that isn't pinned, is the head or strictly older than it."""
        if post.id == self.id:
            if self.day is None:
                # A head with no time (a bare id): the head post gives it for the rest of the pass.
                self.ts, self.day = stamp(post.listed), day(post.listed)
            return True
        return self._older(post)

    def _older(self, post: Any) -> bool:
        if self.day is None:
            return False
        moment = stamp(post.listed) if self.ts is not None else None
        if moment is not None:
            return moment < self.ts
        posted = day(post.listed)
        return posted is not None and posted < self.day

    def syncing(self) -> _Stops:
        """The same head, for a pass that stops at it."""
        return _Stops(True, self.raw)

    @property
    def record(self) -> Mapping[str, Any] | None:
        """The head as a read-only `{"id", "ts"}` mapping for a plugin; None with no head."""
        if isinstance(self.raw, str):
            return MappingProxyType({"id": self.raw})
        if isinstance(self.raw, dict) and self.id is not None:
            return MappingProxyType(dict(self.raw))
        return None

    @property
    def known(self) -> bool:
        """Whether an earlier run stored a head."""
        return self.id is not None


async def _queue_all(
    work: _Work, kept: _Kept, *, limit: int | None = None, listing: Listing | None = None,
) -> tuple[Ended, int]:
    """Queue kept posts, then release held undated posts."""
    ended, taken = await _queue_listed(work, kept, limit=limit, listing=listing)
    if ended is Ended.STOPPED:
        return ended, taken
    for post in await work.ledger.release():
        if limit is not None and taken >= limit:
            return Ended.CUT, taken
        outcome = await work.queue(post, hold=False)
        if outcome is Outcome.STOPPED:
            return Ended.STOPPED, taken
        if outcome is not Outcome.EMPTY:
            taken += 1
    return ended, taken


async def _queue_listed(
    work: _Work, kept: _Kept, *, limit: int | None = None, listing: Listing | None = None,
) -> tuple[Ended, int]:
    """Queue each post in `kept` until it ends: it ran out, a sync met its old posts (`_Stops`),
    it hit `limit`, or the run stopped."""
    taken = 0
    # Consecutive parts of one post (the same id, as `_Kept` says) count once, for `--limit` too.
    last: str | None = None
    stop = work.downloads.settings.stop.soon
    try:
        async for post in kept:
            part = post.id == last
            if limit is not None and taken >= limit and not part:
                return Ended.CUT, taken
            outcome = Outcome.STOPPED if stop.is_set() else await work.queue(post)
            if outcome is Outcome.STOPPED:
                return Ended.STOPPED, taken
            if outcome is Outcome.EMPTY:
                continue
            last = post.id
            if post.id in kept.pinned:
                taken += not part
                kept.queued += outcome is Outcome.QUEUED
                continue
            if part:
                kept.queued += outcome is Outcome.QUEUED
                continue
            taken += 1
            kept.queued += outcome is Outcome.QUEUED
            if listing is not None and outcome is Outcome.QUEUED:
                listing.queued += 1
            # `--limit` is checked as the next post comes, not here: the next may be another part
            # of this one. That reads one more item of the listing, never one more post.
    finally:
        await kept.aclose()
    if kept.reached:
        return Ended.CAUGHT_UP, taken
    return (Ended.RAN_OUT if kept.ran_out else Ended.CUT), taken


async def _read(
    work: _Work, run: PluginRun, site: Ctx, target: Target, options: Options,
    progress: _Progress, turn: Turn | None = None,
) -> None:
    """List the target's route, or its wanted categories, into `work`."""
    route = next(r for r in run.source.routes if r.kind == target.kind)
    if route.discovers:
        if turn is not None:
            turn.release()
        context = await _context(run.source, route, site, target, options.link)
        await _run_discovered(run, options, _discovered(run, route, site, context, options))
        return
    listings: list[_Listed]
    if not route.categories:
        method = getattr(run.source, route.kind)
        listings = [_Listed("", route.newest_first,
                            _caller(method, site, options.link or target.value))]
    else:
        categories = wanted(route, options)
        if not categories:
            return
        context = await _taking(
            turn, run, lambda: _context(run.source, route, site, target, options.link))
        listings = [_Listed(category.name, category.newest_first,
                            _caller(getattr(run.source, category.name), site, context))
                    for category in categories]
    if turn is not None:
        turn.release()
    remaining = options.limit
    for listed in listings:
        await _retry_failed(work, run, site, target, options, [listed])
        if options.retry is Retry.ONLY:
            continue
        if remaining is not None and remaining <= 0:
            return
        ended, taken = await _list_one(
            work, run, site, target, options, progress, name=listed.name,
            newest_first=listed.newest_first, items=listed.items, remaining=remaining,
        )
        if remaining is not None:
            remaining -= taken
        if ended is Ended.STOPPED:
            return


class Turn:
    """A target's turn at its first step, its route's call: the run's targets call their routes
    in the order they started.

    A target's route waits until every target started before it has called its route, or
    waits itself: for a place (`release`), a hold, or the site (the route's first `site` verb,
    `release_soon` from its thread). So what stops the run at a target's first step (a full
    disk) stops it before a later target's route runs, and no target waits behind another
    that waits.

    Args:
        before: the turns of the targets started before it.
    """

    def __init__(self, before: Sequence[asyncio.Event] = ()) -> None:
        self.passed = asyncio.Event()
        self._before = list(before)
        self._loop = asyncio.get_running_loop()

    async def wait(self) -> None:
        """Wait for every earlier target's turn to pass."""
        for passed in self._before:
            await passed.wait()

    def release(self) -> None:
        """Let the later targets go on: this one called its route, waits, or ended."""
        self.passed.set()

    def release_soon(self) -> None:
        """`release`, from the route's thread."""
        if not self.passed.is_set():
            self._loop.call_soon_threadsafe(self.passed.set)


async def _taking(turn: Turn | None, run: PluginRun,
                 route: Callable[[], Awaitable[T]]) -> T:
    """Call a target's route in its turn; the turn passes once the route returned, or meets
    the site, or the route's plugin is held. The route is called only in its turn, so a target
    cancelled while it waits leaves no coroutine unawaited."""
    if turn is None:
        return await route()
    await turn.wait()
    if any(hold.plugin == run.source.code for hold in run.scheduler.holds):
        turn.release()
    token = met_turn.set(turn.release_soon)
    try:
        found = await route()
    except BaseException as error:
        met_turn.reset(token)
        # What stops the run (a full disk) keeps the later targets' routes from running.
        if not jobs.stops(error):
            turn.release()
        raise
    met_turn.reset(token)
    turn.release()
    return found


def _caller(method: Callable[..., Any], *args: Any) -> Callable[[], Awaitable[Any]]:
    """What calls a category's method with `args` (`_called`), each time."""
    return lambda: _called(method, *args)


@dataclass(frozen=True)
class _Listed:
    """One listing of a target: a category, or a route's own posts."""

    # The category's name; "" for a route's own posts.
    name: str
    newest_first: bool
    # Calls the category (or route) method, giving what it yields.
    items: Callable[[], Awaitable[Any]]


async def _list_one(
    work: _Work, run: PluginRun, site: Ctx, target: Target, options: Options,
    progress: _Progress, *, name: str, newest_first: bool, items: Callable[[], Awaitable[Any]],
    remaining: int | None,
) -> tuple[Ended, int]:
    """List one category (or a route's own posts) into `work`, and note where it ended."""
    ui.debug("{target}: list {name}", target=str(target), name=name or target.kind)
    stops = _Stops(newest_first and name in progress.listed, progress.seen_heads.get(name))
    if stops.sync:
        return await _sync_one(work, run, site, target, options, progress, name=name,
                               items=items, stops=stops, remaining=remaining)
    start = progress.started.get(name) if newest_first else None
    listing = progress.listings[name] = Listing(
        start, label=f"{target}: {name or target.kind}", sync=name in progress.listed)
    work.ledger.tally.start(target, name, listing)
    if start:
        ui.info("{target}: {name}: going on where an interrupted run stopped",
                target=str(target), name=name or target.kind)
        await run.settings.emit(RESUMING)
    try:
        ended, taken, kept = await _listed(work, site, name, newest_first, items, listing, stops,
                                           options, remaining)
    except BaseException:
        _keep_top(progress, name, listing, newest_first and not start)
        raise
    if ended is Ended.CUT and kept.stopped_at_since:
        listing.cursor = None
    if ended is Ended.RAN_OUT:
        progress.complete.append(name)
        if not start and kept.head is not None:
            progress.heads[name] = kept.head
    elif ended in (Ended.CUT, Ended.STOPPED):
        _keep_top(progress, name, listing, newest_first and not start)
    if ended is Ended.RAN_OUT and start and listing.taken and not listing.refused and stops.known:
        # The run that saved the cursor was cut short, so posts newer than those it saw may
        # have come since: list from the newest down to the head it kept.
        rest = None if remaining is None else remaining - taken
        if rest is None or rest > 0:
            ui.debug("{target}: list {name} from the newest", target=str(target),
                     name=name or target.kind)
            down = stops.syncing()
            ended, extra, newest = await _listed(
                work, site, name, True, items,
                Listing(head=down.id, sync=True, stored=down.record if name else None), down,
                options, rest)
            taken += extra
            if ended in (Ended.RAN_OUT, Ended.CAUGHT_UP) and newest.head is not None:
                progress.heads[name] = newest.head
    return ended, taken


def _keep_top(progress: _Progress, name: str, listing: Listing, fresh: bool) -> None:
    """Keep the head of a first download that was cut short (with its cursor), so that once it
    runs out, the newer posts that came meanwhile are read down to it."""
    if fresh and not progress.again and listing.cursor and listing.top is not None:
        progress.heads[name] = listing.top


async def _sync_one(
    work: _Work, run: PluginRun, site: Ctx, target: Target, options: Options,
    progress: _Progress, *, name: str, items: Callable[[], Awaitable[Any]], stops: _Stops,
    remaining: int | None,
) -> tuple[Ended, int]:
    """Sync a `newest_first` listing an earlier run read to its end: from the newest down to its
    head. The head moves only when the pass reaches it or the end; a pass cut short, or failed,
    leaves it, so the next one reads down to it again (the saved posts cost no request)."""
    listing = progress.listings[name] = Listing(
        None, label=f"{target}: {name or target.kind}", head=stops.id, sync=True,
        stored=stops.record if name else None)
    work.ledger.tally.start(target, name, listing)
    try:
        ended, taken, kept = await _listed(work, site, name, True, items, listing, stops,
                                           options, remaining)
    finally:
        # A cut sync keeps no cursor: the next one starts from the newest.
        listing.cursor = None
    if ended is Ended.CUT and kept.stopped_at_since:
        ended = Ended.RAN_OUT
    if ended in (Ended.RAN_OUT, Ended.CAUGHT_UP):
        progress.complete.append(name)
        if kept.head is not None:
            progress.heads[name] = kept.head
        if not kept.queued:
            ui.info("{category}: nothing new", category=name or "posts")
            await run.settings.emit(Skipped(target, name, "", None, reason=NOTHING_NEW))
    return ended, taken


async def _retry_failed(
    work: _Work, run: PluginRun, site: Ctx, target: Target, options: Options,
    listings: Sequence[_Listed],
) -> None:
    """List again each post earlier runs failed on, and queue its failed files, before new posts."""
    db = run.settings.db
    if run.settings.dry or options.again or db is None:
        return
    failed = await run.off(functools.partial(db.pending, store.key(target),
                                             every=options.retry is Retry.ONLY))
    by_name = {listed.name: listed for listed in listings}
    groups: dict[tuple[str, str | None], dict[str, list[store.FailureRow]]] = {}
    for row in failed:
        if row.category in by_name:
            groups.setdefault((row.category, row.cursor), {}).setdefault(row.post, []).append(row)
    if not groups:
        return
    count = sum(len(rows) for posts in groups.values() for rows in posts.values())
    ui.info("{target}: retrying {files} earlier runs failed on", target=str(target),
            files=text.count(count, "file"))
    work.ledger.retrying = True
    try:
        for (name, cursor), posts in groups.items():
            if run.settings.stop.soon.is_set():
                return
            listed = by_name[name]
            missing, ran_out = await _relist(work, run, site, listed, cursor, set(posts))
            for post in missing:
                await _lost(work, run, target, posts[post], ran_out=ran_out)
    finally:
        work.ledger.retrying = False


async def _relist(
    work: _Work, run: PluginRun, site: Ctx, listed: _Listed, cursor: str | None,
    posts: set[str],
) -> tuple[set[str], bool]:
    """List one listing from `cursor` until it has met every post of `posts`, queueing each."""
    left, past, last = set(posts), 0, None
    listing = site.listing = Listing(
        cursor, label=f"{site.target}: {listed.name or site.target.kind}")
    kept = _Kept(await listed.items(), listed.name, newest_first=listed.newest_first,
                 span=Range())
    try:
        async for post in kept:
            if run.settings.stop.soon.is_set():
                return set(), False
            # A post the source yields in parts (one id) is queued part by part.
            if post.id in left or post.id == last:
                # Not held for a day from the posts around it: a retry reads too few of them.
                if await work.queue(post, hold=False) is Outcome.STOPPED:
                    return set(), False
                left.discard(post.id)
                last, past = post.id, 0
                continue
            if not left:
                return left, False
            last, past = None, past + 1
            if cursor is not None and past > RETRY_WINDOW:
                return left, False
    finally:
        await kept.aclose()
        site.listing = None
    return left, kept.ran_out and not listing.stalled


async def _lost(
    work: _Work, run: PluginRun, target: Target, rows: Sequence[store.FailureRow], *,
    ran_out: bool,
) -> None:
    """Record that a retry didn't meet a failed post: a failure of each of its files."""
    db = run.settings.db
    for row in rows:
        error: SiteError
        if ran_out:
            error = NotFound(f"post {row.post} is no longer in its listing")
        else:
            error = SiteError(f"post {row.post} wasn't found near where it was listed",
                              hint="flnd target list, then r, tries again", retryable=True)
        if db is not None:
            await run.off(functools.partial(
                db.fail, run=run.settings.run, item=row.key, target=row.target, error=error,
                post=row.post, category=row.category, file=row.file, cursor=row.cursor))
        work.ledger.tally.count(target, row.category, found=1, failed=1)
        await run.settings.emit(Failed(target, error, post=row.post, path=Path(row.file)))


async def _listed(
    work: _Work, site: Ctx, name: str, newest_first: bool,
    items: Callable[[], Awaitable[Any]], listing: Listing, stops: _Stops, options: Options,
    remaining: int | None,
) -> tuple[Ended, int, _Kept]:
    """Run one listing with `listing` set on the site, queueing what it yields into `work`."""
    site.listing = listing
    kept = None
    try:
        kept = _Kept(await items(), name, newest_first=newest_first,
                     span=Range(options.since, options.until), stops=stops)
        ended, taken = await _queue_all(work, kept, limit=remaining, listing=listing)
        if ended is Ended.RAN_OUT and listing.stalled:
            # A feed the stall guard ended is cut short, so the category is not complete.
            ended = Ended.CUT
    finally:
        site.listing = None
        if kept is not None:
            listing.top = kept.head
    work.linked.extend(kept.linked)
    return ended, taken, kept


async def _run_discovered(run: PluginRun, options: Options, yielded: Any) -> None:
    """Run each target a discovering route yields, in turn, as a target of its own."""
    seen: set[Target] = set()
    child_options = replace(options, limit=None, link=None)
    follow = run.settings.follow
    async with contextlib.aclosing(_steps(yielded)) as items:
        async for item in items:
            if run.settings.stop.soon.is_set():
                return
            child = await resolved(run, item)
            if child is None or child in seen:
                continue
            seen.add(child)
            if follow is not None:
                await follow(child, child_options, inside=run.source.code)
            elif child.plugin == run.source.code:
                await download(run, child, run.settings.tally or _tally(), child_options)
            if options.limit is not None and len(seen) >= options.limit:
                return


async def _steps(yielded: Any) -> AsyncIterator[Any]:
    """Iterate what a route or category yields, closing it however it ends."""
    if inspect.iscoroutine(yielded):
        yielded = await yielded
    if yielded is None:
        return
    if not hasattr(yielded, "__anext__"):
        raise PluginError(f"a listing gave a {type(yielded).__name__}; make it an async def "
                          "that yields Post and Target")
    name = getattr(yielded, "__qualname__", "an async listing")

    def slow(seconds: float) -> None:
        ui.warn("{name} held the loop {seconds:.1f} s without awaiting; await, or run "
                "blocking code through ctx.call", name=name, seconds=seconds)
    try:
        while True:
            try:
                item = await _Held(yielded.__anext__(), HELD, slow)
            except StopAsyncIteration:
                return
            yield item
    finally:
        await yielded.aclose()


class _Held:
    """An awaitable that times each stretch the one it wraps runs without awaiting."""

    def __init__(self, awaitable: Awaitable[Any], limit: float,
                 slow: Callable[[float], None]) -> None:
        self._awaitable, self._limit, self._slow = awaitable, limit, slow

    def __await__(self) -> Generator[Any, Any, Any]:
        # What `yield from` does, one step at a time: each `send` or `throw` runs the inner
        # coroutine until it suspends (giving the future it waits on) or ends.
        inner = self._awaitable.__await__()
        sent: Any = None
        thrown: BaseException | None = None
        while True:
            start = time.monotonic()
            try:
                waits = inner.send(sent) if thrown is None else inner.throw(thrown)
            except StopIteration as done:
                self._check(start)
                return done.value
            except BaseException:
                self._check(start)
                raise
            self._check(start)
            try:
                sent, thrown = (yield waits), None
            except BaseException as error:
                # A cancel or a close reaches the inner coroutine, as `yield from` passes it.
                sent, thrown = None, error

    def _check(self, start: float) -> None:
        took = time.monotonic() - start
        if took >= self._limit:
            self._slow(took)


def planned(file: File, content_type: str = "") -> tuple[str, str | None]:
    """An API 2 file's extension and kind before its bytes are seen (5.3); the kit names by it.

    A name with its own extension keeps it, its content checked as it lands (`_extension`'s
    kinds). A stem takes `save.stem_extension`'s: `content_type`'s, else its URL's media
    extension, else none; it is named again as it lands (`save.STEM`), by the answer's
    `Content-Type`, then by its bytes. A DASH pair, a stream and a frames zip are videos.

    Args:
        file: the file.
        content_type: its answer's `Content-Type`, when known; "" for a plan.
    """
    if file.content is not None:
        return PurePosixPath(file.name or "").suffix, None
    if file.delays is not None:
        return DASH_EXTENSION, files.VIDEO
    own = own_extension(file.name)
    if own:
        return own, files.kind_of(file.name or "")
    if file.audio is not None:
        url = file.url or (file.mirrors[0] if file.mirrors else "")
        both = {_url_suffix(url), _url_suffix(file.audio)} == {".webm"}
        return (".webm" if both else DASH_EXTENSION), files.VIDEO
    if file.playlist is not None:
        return DASH_EXTENSION, files.VIDEO
    url = file.url or (file.mirrors[0] if file.mirrors else "")
    return save.stem_extension(content_type, url), save.STEM


def _url_suffix(url: str) -> str:
    """A URL's extension, lowercase; "" for none."""
    return PurePosixPath(urlsplit(url).path).suffix.lower()


def _fields(template: str) -> set[str]:
    """Return top-level field names referenced in `template`."""
    return {
        name.split(".")[0].split("[")[0] for _, name, _, _ in _FORMATTER.parse(template) if name
    }


def target_root(home: Path, target: Target, options: Options | None = None,
                source: Plugin | None = None) -> Path:
    """Base media folder for `target`: `media/<plugin>` when `options.folder` is set or the
    target is a single post that `source` files by owner (`place` adds the owner), else
    `media/<plugin>/<target>`."""
    if (options is not None and options.folder is not None) or _rooted(source, target, options):
        return home / store.MEDIA / target.plugin
    return store.target_folder(home, target)


def _route_of(source: Plugin | None, target: Target | None) -> Route | None:
    """The route that reads `target`, or None."""
    if source is None or target is None:
        return None
    return next((r for r in source.routes if r.kind == target.kind), None)


def _rooted(source: Plugin | None, target: Target | None, options: Options | None) -> bool:
    """Whether a single post (not a search's hit) is filed from `media/<plugin>/`: its route is
    a single one with no categories, or its folder starts with `{author}`."""
    route = _route_of(source, target)
    if route is None or source is None or not route.single:
        return False
    if _plugin_folder(source, route, None).startswith("{author}"):
        return True
    return not route.categories and not (options is not None and options.query)


def checked_dest(root: Path, relative: str, *, check: bool = False) -> Path:
    """Join slash-separated `relative` under `root`, ensuring containment when `check` is set."""
    parts = [part for part in relative.split("/") if part]
    if check and any(
        part in (".", "..") or "\\" in part or "\x00" in part or re.match(r"[A-Za-z]:", part)
        for part in parts
    ):
        raise UsageError("folder template escapes the plugin media folder")
    dest = root.joinpath(*parts)
    if check and not dest.resolve().is_relative_to(root.resolve()):
        raise UsageError("folder template escapes the plugin media folder")
    return dest


def _sweep_folder(home: Path, target: Target, options: Options) -> Path | None:
    """The folder whose stale parts `download` sweeps; None skips sweeping the plugin root."""
    if options.folder is None:
        return store.target_folder(home, target)
    table = _table(options.folder)
    # A category the value leaves out is placed under `{target}`: nothing is common then.
    templates = list(table.values()) if config.ANY in table else [*table.values(), "{target}"]
    prefixes = []
    for template in templates:
        prefix: list[str] = []
        for segment in template.split("/"):
            if not segment or "{" in segment or "}" in segment:
                break
            cleaned = text.filename(segment)
            if cleaned:
                prefix.append(cleaned)
        prefixes.append(prefix)
    common = prefixes[0]
    for prefix in prefixes[1:]:
        common = common[:next((i for i, (a, b) in enumerate(zip(common, prefix)) if a != b),
                              min(len(common), len(prefix)))]
    if not common:
        return None
    root = home / store.MEDIA / target.plugin
    return checked_dest(root, "/".join(common), check=True)


@functools.cache
def _table(value: str) -> dict[str, str]:
    """A folder value's templates by category (`config.folders`), parsed once."""
    return config.folders(value)


def _falls_back(category: Category | None, options: Options | None) -> bool:
    """Whether a folder value names neither `category` nor `*`: it keeps its default place."""
    if options is None or options.folder is None:
        return False
    table = _table(options.folder)
    return (category.name if category is not None else "") not in table and config.ANY not in table


def _folder_format(
    source: Plugin, route: Route | None, category: Category | None,
    options: Options | None = None,
) -> str:
    """The folder template a file is placed by: the call's `options.folder` if it names the
    category (or `*`), else the config's entry for it, its category's, its route's, its
    source's. A category the call's value leaves out keeps that default place under `{target}`.

    Args:
        source: the file's source.
        route: the target's route; None to skip it.
        category: the post's category; None to skip it.
        options: the target's options, whose `folder` overrides every other folder template.

    Returns:
        The template, such as `{category}`.
    """
    name = category.name if category is not None else ""
    if options is not None and options.folder is not None:
        table = _table(options.folder)
        found = table.get(name, table.get(config.ANY))
        if found is not None:
            return found
    own = _plugin_folder(source, route, category)
    if options is not None and options.folder is not None:
        return f"{{target}}/{own}" if own else "{target}"
    return own


def _plugin_folder(source: Plugin, route: Route | None, category: Category | None) -> str:
    """The folder the plugin's side places a category by: a config.toml entry naming it, else
    its category's, its route's, the config's `*` or single template, else the class's."""
    table = _table(source.folder)
    name = category.name if category is not None else ""
    if name in table:
        return table[name]
    if category is not None and category.folder is not None:
        return category.folder
    if route is not None:
        route_folder = getattr(route, "folder", None)
        if route_folder is not None:
            return route_folder
    return table.get(config.ANY, type(source).folder)


def _format(source: Plugin, category: Category | None, name: str) -> str:
    """Return format string `name` from `category` if set, else from `source`."""
    own = getattr(category, name, None)
    return own if own is not None else getattr(source, name)


async def resolved(run: PluginRun, item: Target | str) -> Target | None:
    """A target a source yielded, resolved: by the run's resolver, which knows every source.

    A target already resolved (its `kind` set) is taken as it is. Any other is resolved on a
    thread, since a short link is followed with a request of its own. One the run can't read
    is a warning, and None, so one bad link never ends the listing that yielded it.

    Args:
        run: the source's run; `settings.resolve` resolves any source's target text, and,
            outside an engine's run, the source's own `resolve` reads its own.
        item: a `Target` or its text.

    Returns:
        The resolved target, or None.
    """
    if isinstance(item, Target) and item.kind:
        return item
    text = str(item)
    try:
        if run.settings.resolve is not None:
            return await run.off(run.settings.resolve, text)
        own = Target(text)
        if own.plugin == run.source.code:
            text = f"?{own.value}" if own.query else own.value
        found = await run.off(run.source.resolve, text)
        if found is None:
            raise UsageError(f"{run.source.code}: no route reads it")
        return found
    except Error as error:
        ui.warn("{source}: can't follow {text}: {error}", source=run.source.code, text=text,
                error=error)
        return None



def _tally() -> Tally:
    """Live counts of their own, for a target run outside the engine; `run` imports this module."""
    from flnd.engine.run import Tally

    return Tally()
