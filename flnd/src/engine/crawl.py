"""The crawl: one target's listings on the run's loop, its smart sync and early stops, the Ledger.

src/docs/architecture explains it.
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
from typing import TYPE_CHECKING, Any, TypeVar
from urllib.parse import urlsplit

from flnd.engine import home, jobs, save
from flnd.engine.events import NOTHING_NEW, RESUMING, Failed, Skipped
from flnd.engine.options import Options, Retry
from flnd.engine.run import Counts, Tally
from flnd.engine.site import Listing, SourceRun, TargetSite, met_turn
from flnd.errors import (
    Blocked,
    Changed,
    Error,
    LoginError,
    NetworkError,
    SiteError,
    SourceError,
    Stopped,
    classify,
)
from flnd.source import Category, File, NotFound, Post, Range, Route, Site, Source, Target
from flnd.sources import SOURCES
from flnd.ui.log import ui
from flnkit import files, text
from flnkit.clock import day
from flnkit.text import fill

if TYPE_CHECKING:
    from flnd.engine.events import Event

__all__ = [
    "STOPPED", "Claims", "Downloads", "Ledger", "Placed", "TargetReport", "Turn", "asked",
    "causes",
    "discover", "download", "failure", "logged_out", "place", "plan", "posts", "tell", "wanted",
]

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
# Saved posts in a row, the head an earlier run saw counted, that end a sync once it met that
# head: the head itself, and the post after it, which shows the head wasn't pinned above new ones.
HEAD_IN_A_ROW = 2
# Template fields known only once a post's files are listed: a key that names one can't be
# built from the post alone, so a lazy post's files must be listed to tell if it is saved.
LISTED_FIELDS = frozenset({"count", "index", "extension", "path"})
# The extension a video joined with its audio track gets, unless both are WebM.
DASH_EXTENSION = ".mp4"
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

# Runs a source's plain code, `fn(*args)`: on a `CALL` thread in a run (`SourceRun.call`), at
# once in the test kit.
Call = Callable[..., Awaitable[Any]]
T = TypeVar("T")
# What `_steps` gives once a plain iterator is done.
_DONE = object()
# What a lazy post's files may raise that fails the post alone, never its target (PLAN 6.2).
_POST_ERRORS: tuple[type[BaseException], ...] = (NotFound, Changed, *jobs._HIDDEN)


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


def failure(error: BaseException, target: Target, source: Source | None = None) -> Error:
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
    # A bug in a plugin is its author's to fix, not flnd's: the hint says whose code it is.
    plugin = SOURCES.plugin(type(source)) if source is not None else None
    if plugin is not None and isinstance(made, SourceError) and made.__cause__ is error:
        made.hint = (f"a bug in the plugin {plugin.code} ({plugin.where}), not in flnd; "
                     "report it to its author")
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
    return LoginError(message or "not logged in", hint=f"run flnd login {target.source}",
                      target=str(target))


def _on_domains(error: SiteError, domains: Sequence[str]) -> bool:
    """Whether the response `error` carries came from one of `domains`.

    Args:
        error: a refusal, with the response it came with.
        domains: the source's domains; a host matches one, or is under it.

    Returns:
        True when the response's host is on `domains`, and when there is no response to tell by
        (a browser page's status): a source's own pages are what it mostly asks.
    """
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
    if names is None or ALL in names:
        return route.categories
    selected = set(names)
    return tuple(category for category in route.categories if category.name in selected)


async def posts(
    source: Source, site: Site, target: Target, options: Options, *, call: Call
) -> AsyncIterator[Post]:
    """Every post of `target`'s route, or of each wanted category, that `--when` keeps.

    It is the plain listing, with none of the crawl's stops at saved posts: `flnd.testing` and
    a plan use it. The route is called once, and each category gets what it returned.

    Args:
        source: the target's source.
        site: the `Site` the route and categories are given.
        target: the target.
        options: its options: `--when` and `--only`.
        call: runs the source's plain code (`Call`).

    Yields:
        Each post, filed under its category, in the order the source yields them.
    """
    span = Range(options.since, options.until)
    route = next(r for r in source.routes if r.kind == target.kind)
    categories = wanted(route, options)
    if route.categories and not categories:
        return
    context = await _context(source, route, site, target, call)
    if not route.categories:
        async with contextlib.aclosing(_Kept(context, "", newest_first=route.newest_first,
                                             span=span, call=call)) as kept:
            async for post in kept:
                yield post
        return
    for category in categories:
        method = getattr(source, category.name)
        async with contextlib.aclosing(_Kept(
                await _called(call, method, site, context), category.name,
                newest_first=category.newest_first, span=span, call=call)) as kept:
            async for post in kept:
                yield post


async def _context(source: Source, route: Route, site: Site, target: Target, call: Call) -> Any:
    """Call `target`'s route method: plain code through `call`, an `async def` on the loop.

    A route without categories returns its listing, which is left as it is (`_steps` iterates
    it); a route with categories returns its context, the value every category gets.

    Args:
        source: the target's source.
        route: its route.
        site: the `Site` the route is given.
        target: the target, whose `value` the route reads.
        call: runs the source's plain code.

    Returns:
        What the route returned, awaited when it is a coroutine.
    """
    context = await _called(call, getattr(source, route.kind), site, target.value)
    if route.categories and inspect.iscoroutine(context):
        context = await context
    return context


async def _called(call: Call, method: Callable[..., Any], *args: Any) -> Any:
    """`method(*args)`: through `call`, unless it is a generator function, whose call only
    makes the generator, so nothing of the source runs until it is stepped."""
    if inspect.isgeneratorfunction(method) or inspect.isasyncgenfunction(method):
        return method(*args)
    return await call(method, *args)


async def plan(run: SourceRun, target: Target, options: Options) -> AsyncIterator[Placed]:
    """Every file a download of `target` would save, placed, with nothing written (`--dry`).

    A lazy post's files are listed, which may cost a request each. `--limit` counts posts.

    Args:
        run: the source's run.
        target: the target.
        options: its options: `--when`, `--only`, `--limit`.

    Yields:
        Each file's `Placed`: its path under the target's folder and its history key.
    """
    site = TargetSite(run, target, time=Range(options.since, options.until))
    route = next(r for r in run.source.routes if r.kind == target.kind)
    categories = {c.name: c for c in route.categories}
    taken = 0
    try:
        async with contextlib.aclosing(posts(run.source, site, target, options,
                                             call=run.call)) as stream:
            async for post in stream:
                if options.limit is not None and taken >= options.limit:
                    break
                taken += 1
                for placed in place(run.source, post, await _files(run, post),
                                    category=categories.get(post.category), route=route,
                                    target=target, options=options):
                    yield placed
    finally:
        if not site._idle:
            await run.off(site.close)


async def _files(run: SourceRun, post: Post) -> list[File]:
    """A post's files: a lazy post's listed with tabs of their own (`SourceRun.aside`)."""
    if callable(post.files):
        return await run.aside(_listed_files, post)
    return list(post.files)


def _listed_files(post: Post) -> list[File]:
    assert callable(post.files)
    return list(post.files())


def place(
    source: Source, post: Post, files: Sequence[File], *, category: Category | None = None,
    route: Route | None = None, target: Target | None = None, options: Options | None = None,
) -> list[Placed]:
    """Give each file of `post` its path under the target's folder and its history key.

    The path is the folder template (category, else route, else source) filled with the
    file's fields, then its name (`_resolve_name`). Two files of one post with one name get
    the later one's number added, so neither is skipped as already queued. The key template's
    default is the path itself.

    Args:
        source: the post's source, whose templates apply when its category has none.
        post: the post.
        files: its files, in order; a file's number is its index.
        category: the post's category; None for a route's own posts.
        route: the target's route, whose `folder` comes before the source's.
        target: the target, for the `{source}` and `{target}` fields; None for the source's.
        options: the target's options, for the `{query}` field of a search's found target.

    Returns:
        One `Placed` per file, in order.
    """
    folder = _folder_format(source, route, category)
    filename, key = (_format(source, category, name) for name in ("filename", "key"))
    posted = day(post.date)
    placed: list[Placed] = []
    used: set[str] = set()
    for num, file in enumerate(files):
        extension, kind = _extension(file)
        values = _file_values(
            source, post, num, len(files), posted, extension, target, options,
        )
        name = _resolve_name(post, file, filename, values, extension)
        where = fill(folder, values)
        path = "/".join(part for part in (where, name) if part)
        # Files of one post may share a name (`cover.jpg`): a later one takes its number, or it
        # would be skipped as already queued.
        if path in used:
            name = f"{PurePosixPath(name).stem}_{num}{PurePosixPath(name).suffix}"
            path = "/".join(part for part in (where, name) if part)
        used.add(path)
        resolved_key = fill(key, {**values, "path": path}, safe=False) or path
        placed.append(Placed(post, file, num, path, resolved_key, kind, values))
    return placed


def _file_values(
    source: Source, post: Post, num: int, count: int, posted: datetime.date | None,
    extension: str, target: Target | None, options: Options | None,
) -> dict[str, Any]:
    """The fields a folder, filename or key template of one file may name.

    The post's own `fields` come first, so flnd's names (`post`, `date`, `num`…) win.

    Args:
        source: the post's source; its code is `{source}` when no target is given.
        post: the post.
        num: the file's number in the post, from 0.
        count: how many files the post has; `{index}` is "" for a post of one file.
        posted: the post's day; None when the site gave none.
        extension: the file's extension, with its dot; "" before it is known.
        target: the target, for `{source}` and `{target}`; None for "".
        options: the target's options, for `{query}`; None for "".

    Returns:
        The fields, by name.
    """
    code = target.source if target is not None else source.code
    target_val = target.value if target is not None else ""
    query_val = (options.query or "") if options is not None else ""
    return {
        **post.fields, "post": post.id, "date": posted, "category": post.category,
        "num": num, "count": count, "index": "" if count == 1 else f"_{num}",
        "extension": extension, "source": code, "target": target_val, "query": query_val,
    }


def _resolve_name(
    post: Post, file: File, filename: str, values: Mapping[str, Any], extension: str
) -> str:
    """The file's name: from the filename template, or the name the source gave it.

    A file with no name of its own is named by the template, plus its extension. A file the
    source named after the template (with or without the date) keeps the template's form and
    its own suffix. Any other name the source gave is kept, made safe for every file system.

    Args:
        post: the post; its id names the file when the template comes out empty.
        file: the file.
        filename: the filename template.
        values: the file's fields (`_file_values`).
        extension: the file's extension, with its dot.

    Returns:
        The file's name, with no folder.
    """
    formatted = fill(filename, values) or text.filename(post.id)
    if file.name is None:
        return formatted + extension
    if file.content is None and PurePosixPath(file.name).stem in _stems(filename, values):
        return formatted + PurePosixPath(file.name).suffix
    return text.filename(file.name)


def _stems(filename: str, values: Mapping[str, Any]) -> set[str]:
    """Return filename stems with and without the post's date."""
    return {fill(filename, values), fill(filename, {**values, "date": None})} - {""}


class Ledger:
    """One target's plan of what to download: which files are new, skipped, linked or held.

    The crawl hands it each post (`take`); it places the post's files, skips those the history
    or the disk already has, links a file saved in another category's folder, and returns the
    rest for `Downloads`. It also dates posts the site gave no day: a post is held until its
    category ends, then given the day its key's row has, the source's `date` hook's answer, or
    its nearest dated neighbour's in listing order (marked estimated).

    What reads or writes the disk or `flnd.db` runs off the loop, one hop a post
    (`SourceRun.off`): the crawl awaits it, so nothing else touches the ledger meanwhile. A
    lazy post's files and the `date` hook run on a thread of the source (`SourceRun.aside`).
    The `Skipped` events the plan makes are sent on the loop after each hop (`_tell`).

    Args:
        run: the source's run: its source and settings (the home, the database, the sink).
        site: the target's site, for warnings and the source's saved state.
        target: the target.
        tally: the run's live counts, which its counts go to.
        options: the target's options, for `--again` and the templates' fields; None for none.
    """

    def __init__(
        self, run: SourceRun, site: TargetSite, target: Target, tally: Tally, *,
        options: Options | None = None,
    ) -> None:
        """Initialize a target ledger with its stores, dates, and category state."""
        self.run, self.source, self.settings, self.site = run, run.source, run.settings, site
        self.target, self.tally, self._options = target, tally, options
        self.folder = home.target_folder(self.settings.home, target)
        # The home's database; None for a plan with nothing to read (`flnd.dry`).
        self._db = self.settings.db
        # Told to ask again for everything: files recorded gone are asked for once more.
        self._again = options is not None and options.again
        self._route = next((r for r in self.source.routes if r.kind == target.kind), None)
        self._categories = {c.name: c for r in self.source.routes for c in r.categories}
        # The files the run queued, by path and key: every target's (`Claims`).
        self._queued: Claims = self.settings.claims or Claims()
        self._dates: dict[str, datetime.date] = {}
        self._waiting: dict[str, Post] = {}
        self._order: list[tuple[str, datetime.date | None]] = []
        self._estimated: set[str] = set()
        self._asking = type(self.source).date is not Source.date
        self._link_warned = False
        self._later: list[tuple[Path, str, str]] = []
        # The posts the `date` hook had no day for, read off the loop by `load`.
        self._asked: dict[str, float] = {}
        # The rows of files found on disk with none, written with their post's (`_held`).
        self._unrecorded: list[home.FileRow] = []
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
        category = self._categories.get(post.category)
        first = self._first_key(post, category)
        dated, skipped = await self.run.off(self._before_files, post, category, hold)
        await self._tell()
        if dated is None:
            return Outcome.EMPTY, []
        if skipped is not None:
            return skipped, []
        listed = await _files(self.run, post)
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
                and self._db.post_saved(home.key(self.target), dated.id, dated.category):
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
        """Place each file of `post` and queue those not held, writing inline text files now.

        Off the loop. A file with `content` (a caption, a text post) is written at once, never
        queued. A held file is skipped, or linked from another category's folder. Videos go to
        the end of the post's queue, so its small files land first.

        Args:
            post: the post, dated.
            listed: its files.
            category: its category; None for a route's own posts.
            first: its first file's key, for whether its day is estimated; None for none.

        Returns:
            `QUEUED` with the files to download; else `SAVED` when every file was held (and
            not by this run's queue), or `EMPTY`.
        """
        found = 0
        todo: list[_Queued] = []
        for placed in place(
            self.source, post, listed, category=category, route=self._route,
            target=self.target, options=self._options,
        ):
            dest = self.folder.joinpath(*placed.path.split("/"))
            if placed.file.content is not None:
                dest.parent.mkdir(parents=True, exist_ok=True)
                files.write(dest, placed.file.content)
                continue
            item_key = f"{self.source.code}:{placed.key}"
            held = self._held(dest, item_key, placed)
            if held is not None:
                if dest not in self._queued and item_key not in self._queued:
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
        """Count a skipped file and link it when held in another category's folder.

        Args:
            post: the post the file belongs to.
            name: the file's path under the target's folder.
            dest: where the file would go.
            item_key: its history key.
            held: the name of the file that holds the same content.
        """
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
                self._db.fail, run=self.settings.run, item=key, target=home.key(self.target),
                error=failed, post=post.id, category=post.category, file=str(dest),
                cursor=listing.cursor if listing is not None else None))
        self.tally.count(self.target, post.category, found=1, failed=1)
        ui.debug("{post}: {error}", post=post.id, error=failed)
        await self.settings.emit(Failed(self.target, failed, post=post.id))

    def link_queued(self, written: Mapping[str, Path]) -> None:
        """Make the links held back because their file was still queued in this run.

        Called once the downloads finished, off the loop. The file's row gives its final path,
        since the compress step may have changed its suffix; a file that never landed gets no
        link.

        Args:
            written: each landed file's path, by history key.
        """
        for dest, item_key, category in self._later:
            holder = written.get(item_key)
            # The compress step may have moved the file to a new suffix; its row says where.
            found = self._db.held(item_key) if holder is not None and self._db else None
            if found is not None and found[0]:
                holder = Path(found[0])
            if holder is not None:
                self._link(dest.with_suffix(holder.suffix), holder, category, item_key)
            self.tally.count(self.target, category, later=-1)
        self._later = []

    def _link(self, dest: Path, holder: Path, category: str, item_key: str) -> None:
        """Link `holder` at `dest`, in another category's folder, and record the link.

        The link's row (`Home.link`) carries the holder's hash, so a later compress of the
        holder moves the link with it, and `check` knows the file.

        Args:
            dest: where the file would go in this category's folder.
            holder: the file saved in another category's folder.
            category: this category.
            item_key: the file's key, whose row is the holder's.
        """
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
                self.site.warn("can't link files found in two folders here")
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
        for row in await self.run.off(self._db.undated, home.key(self.target)):
            if row.post_id in self._asked:
                continue
            found = await self._ask(Post(row.post_id))
            if found is not None and await self.run.off(self._redate, row, found):
                dated += 1
        await self.run.off(self._save_asked)
        if dated:
            self.site.info("{files} dated", files=text.count(dated, "saved file"))
        return dated

    def _redate(self, row: home.FileRow, found: datetime.date) -> bool:
        """Rename one saved file for its day, set its time and update its row; off the loop.

        Returns:
            Whether it was dated: False when it is gone, or its new name holds another file.
        """
        assert self._db is not None
        filename_tpl = _format(self.source, self._categories.get(row.category), "filename")
        path = _redated(Path(row.path), found, filename_tpl, estimated=row.estimated)
        if path is None:
            return False
        _set_mtime(path, found)
        self._db.redate(row.key, path, found)
        return True

    def _save_asked(self) -> None:
        """Persist the 30-day `asked` cache to `site.state` when non-empty; off the loop.

        The cache is the plugin's, shared by its targets, so each saves every target's."""
        if self._asked:
            self.site.state[ASKED_KEY] = dict(self._asked)
    async def _ask(self, post: Post) -> datetime.date | None:
        """Ask the source's `date` hook for `post`'s day; a miss isn't asked again for 30 days.

        A refused login, a block, a 401, 403 or 429 stops asking for the rest of the run, with
        one warning; any other site or network error leaves this post undated.

        Args:
            post: the post.

        Returns:
            Its day; None when the hook has none, failed, or asking has stopped.
        """
        if not self._asking:
            return None
        try:
            found = day(await self.run.aside(self.source.date, self.site, post))
        except (SiteError, NetworkError, LoginError) as error:
            blocked = isinstance(error, (LoginError, Blocked))
            if blocked or getattr(error, "status", None) in BLOCKS:
                self._asking = False
                self.site.warn("dating stopped: {error}; the rest are estimated", error=error)
            else:
                ui.debug("{post}: not dated: {error}", post=post.id, error=error)
            return None
        if found is None:
            self._asked[post.id] = time.time()
        return found

    def _nearest_dates(self) -> dict[str, datetime.date]:
        """For each held undated post, the day of its nearest dated post in listing order.

        Two passes over the order: the first notes each undated post's nearest dated one
        before it, the second the nearest after it; the closer wins, the earlier on a tie.

        Returns:
            The day, by the undated post's key; a post with no dated neighbour is left out.
        """
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
        """Give `post` a day if one is known, or hold it until its category ends.

        A post whose first key can't be built from its fields alone (`_first_key`) can't be
        matched later, so it goes on as it is. A day comes from the post, an earlier post of
        the same key in this run, or the key's row in `flnd.db`.

        Args:
            post: the post.
            category: its category, whose key template applies.
            hold: whether it may be held; False when it is released, so it goes on undated.

        Returns:
            The post, dated when a day was found; None when it is held (or a repeat of a held
            post, which is skipped).
        """
        item_key = self._first_key(post, category)
        if item_key is None:
            return post
        known = day(post.date)
        if hold:
            self._order.append((item_key, known))
        if known is not None:
            self._dates[item_key] = known
            self._waiting.pop(item_key, None)
            return post
        found = self._dates.get(item_key)
        if found is None and self._db is not None:
            found = self._db.day(item_key)
        if found is not None:
            self._dates[item_key] = found
            return post.replace(date=found)
        if not hold:
            return post
        if item_key in self._waiting:
            self._skip(post.category, post.id)
            ui.debug("{post}: listed twice", post=post.id)
            return None
        self._waiting[item_key] = post
        return None

    def _first_key(self, post: Post, category: Category | None) -> str | None:
        """The history key of `post`'s first file, from the post's fields alone.

        Args:
            post: the post.
            category: its category, whose key template applies.

        Returns:
            `<code>:<key>`; None when the key template needs what only listing the files gives
            (`LISTED_FIELDS`) or the day, or fills to nothing.
        """
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
        """Whether a lazy post's files are all saved, judged by their keys, before listing them.

        The first file's row holds the post's count of files; each later key is checked too.

        Args:
            post: the lazy post.
            category: its category, whose key template applies.

        Returns:
            `SAVED` when every file is held; `EMPTY` when one is queued in this run already;
            None when it can't be told without listing the files.
        """
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
        return Outcome.EMPTY if any(k in self._queued for k in checked) else Outcome.SAVED

    def _skip(self, category: str, post: str, path: Path | None = None) -> None:
        """Count one file (or post) found and skipped, and make its `Skipped` event (`_tell`).

        Args:
            category: the category whose status line counts it.
            post: the post's id.
            path: the file skipped; None when the whole post was.
        """
        self.tally.count(self.target, category, found=1, skipped=1)
        self._told.append(Skipped(self.target, category, post, path))

    def _held(self, dest: Path | None, item_key: str, placed: Placed | None = None) -> str | None:
        """The name of what holds a file already: queued, on disk, saved, or gone.

        A file the site answered 404 or 410 for is recorded gone, and held by its planned name,
        so it isn't asked for again unless the run was told to (`again`). An archive removed
        once extracted is held by the folder it was extracted to. The disk is checked before
        the database, with one `stat`, since a file there with bytes is saved whatever the rows
        say; one whose key has no row (after `target clean` without `--media`) gets its row,
        with no hash, written with the post's others (`_unrecorded`).

        Args:
            dest: where the file would go; None to check by key only.
            item_key: its history key.
            placed: the file, placed, for the row of one on disk; None to record nothing.

        Returns:
            The holder's name (relative to the target's folder when inside it); None when the
            file is new.
        """
        for known in (dest, item_key):
            holder = self._queued.get(known) if known is not None else None
            if holder is not None:
                return holder
        on_disk = _on_disk(dest) if dest is not None else None
        if dest is not None and on_disk is not None:
            if placed is not None and self._db is not None and self._db.held(item_key) is None:
                self._unrecorded.append(self._row(dest, item_key, placed, on_disk))
            return self._name(dest)
        found = self._db.held(item_key) if self._db is not None else None
        if found is None:
            return None
        path, state, folder = found
        if state == home.GONE:
            return None if self._again else self._name(Path(path))
        if Path(path).is_file():
            return self._name(Path(path))
        return self._name(Path(folder)) if folder and Path(folder).is_dir() else None

    def _row(self, dest: Path, item_key: str, placed: Placed,
             found: os.stat_result) -> home.FileRow:
        """The row of a file found on disk with none: its size and time from its `stat`."""
        posted = day(placed.post.date)
        count = placed.values.get("count", 1)
        return home.FileRow(
            item_key, home.key(self.target), str(dest), found.st_size, found.st_mtime_ns,
            post_id=placed.post.id, category=placed.post.category,
            date=posted.isoformat() if posted else None, kind=files.kind_of(dest.name),
            at=home.now(),
            files=count if placed.num == 0 and isinstance(count, int) and count > 1 else None)

    def _name(self, path: Path) -> str:
        """A path as the debug lines and links name it.

        Args:
            path: a path.

        Returns:
            It relative to the target's folder, with `/`, when inside it; else in full.
        """
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


def asked(run: SourceRun) -> None:
    """Read the posts the plugin's `date` hook had no day for, asked in the last 30 days, once
    per plugin per run (`SourceRun.asked`), off the loop; nothing for a plugin with no hook.

    Entries older than `ASKED_TTL` are dropped.
    """
    if run.asked is not None or type(run.source).date is Source.date:
        return
    now = time.time()
    raw = run.state.get(ASKED_KEY)
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
    """Whether two files sit in different category folders of one target.

    Only then is one linked from the other: two names in one folder are the same file twice.

    Args:
        root: the target's folder.
        dest: where the link would go.
        holder: the file saved already.

    Returns:
        True when both are under `root`, at least one folder deep, in different first folders.
    """
    if not (dest.is_relative_to(root) and holder.is_relative_to(root)):
        return False
    here = dest.relative_to(root).parts
    there = holder.relative_to(root).parts
    return len(here) >= 2 and len(there) >= 2 and here[0] != there[0]


def _try_link(dest: Path, holder: Path) -> bool:
    """Link `dest` to `holder`: a relative symlink, else a hard link where symlinks fail.

    Windows refuses a symlink without developer mode, and some file systems (FAT, some network
    shares) refuse both.

    Args:
        dest: the link's path; its folder exists.
        holder: the file it points to.

    Returns:
        Whether a link was made.
    """
    try:
        dest.symlink_to(os.path.relpath(holder, dest.parent))
        return True
    except OSError:
        try:
            os.link(holder, dest)
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

    def __init__(self, run: SourceRun, target: Target, tally: Tally, *,
                 swept: Awaitable[None] | None = None) -> None:
        self.run, self.target, self.tally = run, target, tally
        # The sweep of the target's folder, which the first file waits for.
        self._swept = swept
        self.source, self.settings = run.source, run.settings
        # What stopped the run while a post saved; the listing raises it at its next post.
        self.fatal: BaseException | None = None
        # Set once the run stopped, so a file that hasn't started ends at once.
        self._halted = False
        self._written: dict[str, Path] = {}
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
        items = [self._item(queued, saver) for queued in todo]
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
                runs to its end: the gap's cursor is kept after them, so a file dropped here
                would never be listed again. After a stop (`Stopped`, Ctrl-C) the files still
                queued are dropped, and the running ones finish.

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
        return tuple(self._written.values())

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

    def _item(self, queued: _Queued, saver: Any) -> save.Item:
        """The save's `Item` for a queued file."""
        placed = queued.placed
        post = placed.post
        count = placed.values.get("count", 1)
        return save.Item(
            queued.key, self.target, post.id, post.category, placed.file, queued.dest,
            kind=placed.kind, day=day(post.date), headers=post.headers,
            count=count if placed.num == 0 and isinstance(count, int) and count > 1 else None,
            estimated=queued.estimated,
            meta=(_metadata(self.source, self.target, placed)
                  if saver is not None and saver.sidecars else None),
            cursor=queued.cursor, retried=queued.retried,
            refresh=(lambda: self._fresh(queued)) if callable(post.files) else None,
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
        """One post's save on the loop; never an exception out.

        A `KeyboardInterrupt` (the second Ctrl-C, `Stopped`) escaping a task would end the
        loop, so it is let go here: the crawl sees the stop through the run's stop state. What
        stops the run is kept as `fatal` for the crawl.
        """
        try:
            written = await saver.post(items, client=self.run.files, plugin=self.source.code)
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
        """Wait for room in the target's queue (`QUEUE` times its places).

        Returns:
            True once there is room; False once the run was told to stop.

        Raises:
            Error: what stopped the run while an earlier post saved (`fatal`).
        """
        limit = QUEUE * max(self.run.jobs, 1)
        while True:
            if self.fatal is not None:
                raise self.fatal
            if self.settings.stop.soon.is_set():
                return False
            if self._pending < limit:
                return True
            await self._settled()

    async def _fresh(self, queued: _Queued) -> File | None:
        """A lazy post's files listed again, for a fresh copy of one link.

        Returns:
            The fresh file, at the same number in the post; None when no fresh link came (the
            listing failed, or the post has fewer files now).
        """
        try:
            found = await self.run.aside(_listed_files, queued.placed.post)
        except Exception as error:
            ui.warn("{name}: no fresh link: {error}", name=queued.placed.path, error=error)
            return None
        num = queued.placed.num
        return found[num] if num < len(found) else None


async def download(run: SourceRun, target: Target, tally: Tally, options: Options, *,
                   listed: Callable[[Downloads], Awaitable[None]] | None = None,
                   turn: Turn | None = None) -> TargetReport:
    """Download one target of a source, counting on `tally`: one coroutine on the run's loop.

    A failure of the listing fails the target alone: it is in the report, never raised.

    1. Sweep stale parts, make its ledger and downloads, and read its row.
    2. List it and wait for its downloads (`_execute_listing`), then make the held links.
    3. Keep its listings' cursors, heads, gaps and completeness (`Home.progress`); a found
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
    # No lock on the target: each file's part locks itself, so two runs may share a target and
    # the sweep passes over the parts another run holds. It runs while the listing starts, and
    # ends before the first file is handed over.
    sweeping = asyncio.ensure_future(
        run.off(files.sweep, home.target_folder(run.settings.home, target)))
    site = TargetSite(run, target, time=Range(options.since, options.until))
    ledger = Ledger(run, site, target, tally, options=options)
    downloads = Downloads(run, target, tally, swept=sweeping)
    work = _Work(ledger, downloads)
    db = run.settings.db
    row = await run.off(_opened, ledger, db, target)
    progress = _Progress(row, again=options.again)
    error = await _execute_listing(work, run, site, target, options, progress, listed, turn)
    try:
        await sweeping
    except Exception as failed:
        error = error or failed
    written = await downloads.finish(error)
    if error is None and downloads.fatal is not None:
        error = downloads.fatal
    await run.off(ledger.link_queued, downloads.written)
    if db is not None:
        await run.off(_keep, db, target, options, progress, row,
                      error is None and not run.settings.stop.soon.is_set(), bool(written))
    report = TargetReport(target, written, tally.totals(target).skipped,
                          found=tuple(dict.fromkeys(work.linked)))
    return await _ended(run, tally, report, error,
                        complete=", ".join(progress.complete) or "none", started=started)


def _opened(ledger: Ledger, db: home.Home | None, target: Target) -> home.TargetRow | None:
    """What a target starts from, off the loop: its row, and what its ledger keeps."""
    ledger.load()
    return db.target(target) if db is not None else None


def _keep(db: home.Home, target: Target, options: Options, progress: _Progress,
          row: home.TargetRow | None, finished: bool, written: bool) -> None:
    """Write where a target's listings stand, off the loop: a write may wait for the lock."""
    # A target a search found has no row of its own: the search's row keeps what it found.
    if not options.query:
        db.progress(target, cursors=progress.cursors(), complete=progress.complete,
                    heads=progress.heads, gaps=progress.gaps)
    if finished:
        # A failure of the whole target is over once it ran to its end.
        db.clear(home.key(target))
        # Counting reads every row of the target (35 ms for 100,000), so a run that saved
        # nothing keeps the count it has, unless it has none yet.
        if written or row is None or not row.files:
            db.tally(target)


class _Progress:
    """Where a target's listings stood after earlier runs, and where this run leaves them.

    Args:
        row: the target's row in `flnd.db`; None for a target the home doesn't know yet.
        again: list every post again: the earlier runs' cursors, heads and completeness are
            ignored (they are still updated by this run).
    """

    def __init__(self, row: home.TargetRow | None, *, again: bool) -> None:
        earlier = row if row is not None and not again else None
        # Each listing an earlier run read to its end, with the day it did.
        self.listed: Mapping[str, str] = earlier.complete if earlier is not None else {}
        # The cursor each cut-short listing was reading.
        self.started: Mapping[str, str] = earlier.cursors if earlier is not None else {}
        # The newest post each listing had when an earlier run read it from the top.
        self.seen_heads: Mapping[str, str] = earlier.heads if earlier is not None else {}
        # Where earlier syncs of each listing were cut short, newest first.
        self._gaps: Mapping[str, list[str]] = earlier.gaps if earlier is not None else {}
        # This run's listings, by name ("" for a route's own), the ones it read to their end,
        # their heads, and the gaps the syncs leave.
        self.listings: dict[str, Listing] = {}
        self.complete: list[str] = []
        self.heads: dict[str, str] = {}
        self.gaps: dict[str, list[str]] = {}

    def gaps_of(self, name: str) -> list[str]:
        """A listing's gaps from earlier syncs, newest first.

        A cursor kept for a listing already read to its end is a gap too: a sync cut short,
        from a flnd that kept it as the listing's cursor.

        Args:
            name: the listing's name; "" for a route's own posts.

        Returns:
            The gaps' cursors; "" for a gap with no cursor.
        """
        found = list(self._gaps.get(name, ()))
        old = self.started.get(name)
        if old and name in self.listed and old not in found:
            found.append(old)
        return found

    def cursors(self) -> dict[str, str | None]:
        """Each listing's cursor to keep: the page it was reading, or None when it ran out.

        A sync keeps gaps instead (`gaps`), so its listing's cursor is dropped.

        Returns:
            Each listing's cursor by name, as `Home.progress` takes them.
        """
        return {
            name: None if name in self.complete or name in self.gaps else listing.cursor
            for name, listing in self.listings.items()
        }


async def _execute_listing(
    work: _Work, run: SourceRun, site: TargetSite, target: Target, options: Options,
    progress: _Progress, listed: Callable[[Downloads], Awaitable[None]] | None = None,
    turn: Turn | None = None,
) -> BaseException | None:
    """List the target, then wait for its downloads; any error that ended it is returned.

    Args:
        work: the target's ledger and downloads, which queue each post's files.
        run: the source's resources for the run.
        site: the target's site; its `listing` is set while a listing runs.
        target: the target.
        options: the target's options: `--when`, `--only`, `--limit`, `--again`.
        progress: where the earlier runs left the target's listings, and this run's.
        listed: awaited once the listings ended (`download`).
        turn: its turn at its route's call (`download`).

    Returns:
        What ended the target (a `KeyboardInterrupt` past the first Ctrl-C included); None
        when it finished.
    """
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
    run: SourceRun, tally: Tally, report: TargetReport, error: BaseException | None, *,
    complete: str, started: float,
) -> TargetReport:
    """End a target's rows and report it: failed, stopped, or done.

    A failure is classified (`failure`), recorded in `flnd.db` as a failure of the whole
    target (with a scrubbed sample of a `Changed` answer), and sent as `Failed`.

    Args:
        run: the source's run.
        tally: the run's live counts.
        report: the target's report so far.
        error: what ended the listing; None when it finished.
        complete: the listings it read to their end, for the debug line.
        started: when it started, from `time.monotonic`.

    Returns:
        The report, with its `error` set when it failed or was stopped.

    Raises:
        KeyboardInterrupt: the second Ctrl-C (any `error` that isn't an `Exception`).
    """
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


def _failed(run: SourceRun, target: Target, failed: Error) -> None:
    """Record a target's failure, with a scrubbed sample of a `Changed` answer, off the loop."""
    assert run.settings.db is not None
    save.sampled(run.settings, failed)
    run.settings.db.fail(run=run.settings.run, item=home.key(target), target=home.key(target),
                         error=failed)


def _summary(totals: Counts, target: Target, seconds: float) -> None:
    """A debug line for a finished target: files saved, size, skips, failures and time.

    The CLI's summary counts the whole run; this line adds each target's own time.

    Args:
        totals: the target's counts.
        target: the target.
        seconds: how long it took.
    """
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
    """Rename a saved file for its new day, when its filename template starts with the day.

    An estimated day at the front of its name is dropped first. When the new name is taken
    by the same bytes, the old file goes; by other bytes, nothing changes.

    Args:
        path: the saved file.
        found: its post's day.
        filename: the filename template.
        estimated: whether its name carries an estimated day.

    Returns:
        Where the file is now; None when it is gone, or the new name holds another file.
    """
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


def _metadata(source: Source, target: Target, placed: Placed) -> dict[str, Any]:
    """What a saved file's sidecar holds: its post's caption, author, tags, day and link.

    `caption`, `author` and `tags` are read from the post's fields under the names sources
    give them; `fields` keeps every field as the source gave it. A file's URL is kept without
    its query, which may carry a signature.

    Args:
        source: the file's source.
        target: the target it was saved for.
        placed: the file, with its post.

    Returns:
        The sidecar's fields, for JSON.
    """
    post = placed.post
    fields = dict(post.fields)

    def first(*names: str) -> Any:
        return next((fields[name] for name in names if fields.get(name)), None)

    posted = day(post.date)
    url = placed.file.url if isinstance(placed.file.url, str) else None
    return {
        "source": source.code, "target": str(target), "post": post.id,
        "category": post.category, "date": posted.isoformat() if posted else None,
        "url": first("url", "link", "permalink"),
        "file": text.redact(url) if url else None,
        "caption": first("caption", "text", "description", "title"),
        "author": first("author", "user", "username", "owner", "channel"),
        "tags": first("tags", "hashtags"),
        "fields": fields,
    }


def _set_mtime(path: Path, dated: datetime.date) -> None:
    """Set a file's modified time to noon UTC of its post's day.

    Noon, so the day reads the same in every time zone from UTC-11 to UTC+11. A failure (a
    read-only file system) is a debug line: the file is saved, only its time is off.

    Args:
        path: the file.
        dated: its post's day.
    """
    noon = datetime.datetime.combine(dated, datetime.time(NOON), tzinfo=datetime.UTC)
    stamp = noon.timestamp()
    try:
        os.utime(path, (stamp, stamp))
    except OSError as error:
        ui.debug("{name}: modified time not set: {error}", name=path.name, error=error)


@dataclass
class _Queued:
    """One file handed from the crawl to `Downloads`.

    `refreshed` is set on the loop by the file's own task, once, when its link expired; no
    other thread reads it.
    """

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
    """The posts a route or category yields, filtered by `--when`: an async iterator.

    It is where the crawl reads a listing as it goes: it stops a `newest_first` listing after
    `OLDER_IN_A_ROW` posts older than `--when`, notes the newest post (the listing's head), and
    keeps the `Target`s a listing yields (`linked`) for the engine, rather than as posts. A
    pinned post (`pinned` on the post) never counts toward the older posts in a row and is
    never the head; its id is kept in `pinned` for the sync's stops.

    Args:
        yielded: the generator (plain or async), or a coroutine giving one.
        category: the category's name; "" for a route's own posts.
        newest_first: whether the listing comes newest first.
        span: `--when`.
        call: runs the source's plain code (`Call`).
        close: closes a plain generator, even while the run stops; None uses `call`.
    """

    def __init__(self, yielded: Any, category: str, *, newest_first: bool, span: Range,
                 call: Call, close: Call | None = None) -> None:
        self.category, self.newest_first, self.time = category, newest_first, span
        self.ran_out = False
        # The targets the listing yielded, in order, each as the source wrote it.
        self.linked: list[Target] = []
        # The newest post listed, by its day (the first one, while none has a day): the head a
        # later sync stops at.
        self.newest: str | None = None
        self._newest_day: datetime.date | None = None
        # Posts it queued files of, counted by `_queue_listed`: none means nothing new.
        self.queued = 0
        # The ids of the pinned posts it yielded.
        self.pinned: set[str] = set()
        self._steps = _steps(yielded, call, close or call)
        self._iter: AsyncIterator[Post] | None = None

    def __aiter__(self) -> AsyncIterator[Post]:
        if self._iter is None:
            self._iter = self._kept()
        return self._iter

    async def _kept(self) -> AsyncIterator[Post]:
        """Yield kept posts, stopping early after 5 older posts on `newest_first` feeds."""
        older = 0
        # A post a source yields in parts (TikTok yields each photo of a photo post as a post of
        # the same id) is one post to the streaks: only its first part counts.
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
            pinned = _pinned(post)
            if self.category:
                post = post.filed(self.category)
            posted = day(post.date)
            if pinned:
                self.pinned.add(post.id)
            else:
                self._note(post.id, posted)
            if posted in self.time:
                older = older if pinned else 0
                last = post.id
                yield post
                continue
            since = self.time.since
            if self.newest_first and since and posted and posted < since and not pinned:
                older += post.id != last
                last = post.id
                if older >= OLDER_IN_A_ROW:
                    return
        self.ran_out = True

    async def aclose(self) -> None:
        """Close the listing, and the generator under it."""
        if self._iter is not None:
            await self._iter.aclose()  # type: ignore[attr-defined]
        await self._steps.aclose()

    def _note(self, post: str, posted: datetime.date | None) -> None:
        """Keep `post` as the newest so far when it is the first, or newer than the newest."""
        if self.newest is None or (
                posted is not None and (self._newest_day is None or posted > self._newest_day)):
            self.newest, self._newest_day = post, posted


def _pinned(post: Post) -> bool:
    """Whether the site shows `post` above its date order (API 2's `Post.pinned`)."""
    return getattr(post, "pinned", False) is True


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
            if not post.lazy:
                raise
            await self.ledger.lost(post, error)
            return Outcome.FAILED
        if todo and not await self.downloads.post(todo):
            return Outcome.STOPPED
        return outcome


async def discover(
    run: SourceRun, target: Target, *, limit: int | None = None, time_range: Range = Range(),
) -> AsyncIterator[Target]:
    """The targets a search (or any route that yields targets) finds, each once, resolved.

    Called by the engine for a search, and for its dry run. It reads the whole listing, past
    the targets earlier syncs saw (the caller skips those), so a sync that takes a slice of
    what is new reaches the rest on the next syncs. It stops at `limit`, or at Ctrl-C.

    Args:
        run: the source's run.
        target: the search.
        limit: at most this many targets; None for all.
        time_range: `--when`, which the route's `Site` filters by.

    Yields:
        Each new target, resolved by the source that owns it.
    """
    route = next(r for r in run.source.routes if r.kind == target.kind)
    site = TargetSite(run, target, time=time_range)
    yielded: set[Target] = set()
    try:
        if _at_limit(run, yielded, limit):
            return
        context = await _context(run.source, route, site, target, run.call)
        async with contextlib.aclosing(_steps(context, run.call, run.off)) as steps:
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
        if not site._idle:
            await run.off(site.close)


def _at_limit(run: SourceRun, yielded: set[Target], limit: int | None) -> bool:
    """Return whether discovery should stop due to Ctrl-C or `limit`."""
    return run.settings.stop.soon.is_set() or (limit is not None and len(yielded) >= limit)


@dataclass(frozen=True)
class _Stops:
    """When a sync of a listing has caught up: the engine's early stop at saved posts.

    Args:
        sync: whether the listing stops at saved posts at all: any run (but `--again`) of a
            `newest_first` listing an earlier run read to its end, not resumed from a cursor.
        streak: saved posts in a row that end it (`stop_after`); None for no such stop, only
            the head's: a sync with a gap it has no cursor for reads down to the head.
        head: the newest post an earlier run saw in this listing; None when none did.
    """

    sync: bool = False
    streak: int | None = 5
    head: str | None = None


async def _queue_all(
    work: _Work, kept: _Kept, *, stops: _Stops = _Stops(), limit: int | None = None
) -> tuple[Ended, int]:
    """Queue kept posts, then release held undated posts.

    Args:
        work: the target's ledger and downloads.
        kept: the listing, filtered by `--when`.
        stops: when a sync has caught up.
        limit: posts the listing may still take; None for no limit.

    Returns:
        How the listing ended, and how many posts it took.
    """
    ended, taken = await _queue_listed(work, kept, stops=stops, limit=limit)
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
    work: _Work, kept: _Kept, *, stops: _Stops, limit: int | None = None
) -> tuple[Ended, int]:
    """Queue each post in `kept` until stopped, `limit`, or a sync caught up (`_Stops`).

    A sync has caught up after `stops.streak` saved posts in a row, or, once it met the head an
    earlier run saw, after `HEAD_IN_A_ROW`: the head and the post after it. Requiring the post
    after it keeps a head the site later pinned above new posts from ending the sync too soon.
    A pinned post (`_Kept.pinned`) is queued like any other, and never counts toward either.

    Args:
        work: the target's ledger and downloads.
        kept: the listing, filtered by `--when`.
        stops: when a sync has caught up.
        limit: posts the listing may still take; None for no limit.

    Returns:
        How the listing ended, and how many posts it took (queued or saved).
    """
    saved_in_a_row = 0
    taken = 0
    met_head = False
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
                # A part saved like the rest leaves the streak as it was; a new part breaks it.
                if outcome is Outcome.QUEUED:
                    kept.queued, saved_in_a_row = kept.queued + 1, 0
                continue
            taken += 1
            kept.queued += outcome is Outcome.QUEUED
            saved_in_a_row = saved_in_a_row + 1 if outcome is Outcome.SAVED else 0
            met_head = met_head or (stops.head is not None and post.id == stops.head)
            # `--limit` is checked as the next post comes, not here: the next may be another part
            # of this one. That reads one more item of the listing, never one more post.
            if not stops.sync:
                continue
            if met_head and saved_in_a_row >= HEAD_IN_A_ROW:
                # The caller says "nothing new", or jumps into the listing's gaps.
                return Ended.CAUGHT_UP, taken
            if stops.streak is not None and saved_in_a_row >= stops.streak:
                ui.info("{category}: {count} saved posts in a row, caught up",
                        category=kept.category or "posts", count=saved_in_a_row)
                return Ended.CAUGHT_UP, taken
    finally:
        await kept.aclose()
    return (Ended.RAN_OUT if kept.ran_out else Ended.CUT), taken


async def _read(
    work: _Work, run: SourceRun, site: TargetSite, target: Target, options: Options,
    progress: _Progress, turn: Turn | None = None,
) -> None:
    """List the target's route, or its wanted categories, into `work`.

    A discovering route runs each target it yields as a target of its own; a route without
    categories is one listing (named ""); otherwise the route is called once, and each wanted
    category listed with what it returned, sharing `--limit`. The files earlier runs failed on
    in a listing go first, right before it (`_retry_failed`): no other category runs in the
    route's tab before the first one, which may read the page the route left open (fbk's
    `posts`).

    Args:
        work: the target's ledger and downloads, which queue each post's files.
        run: the source's resources for the run.
        site: the target's site; its `listing` is set while a listing runs.
        target: the target.
        options: the target's options: `--when`, `--only`, `--limit`, `--again`, and what to do
            with earlier failures (`retry`).
        progress: where the earlier runs left the target's listings, and this run's.
        turn: its turn at its route's call (`Turn`).
    """
    route = next(r for r in run.source.routes if r.kind == target.kind)
    if route.discovers:
        if turn is not None:
            turn.release()
        await _run_discovered(run, options,
                              await _context(run.source, route, site, target, run.call))
        return
    listings: list[_Listed]
    if not route.categories:
        method = getattr(run.source, route.kind)
        listings = [_Listed("", route.newest_first, _caller(run, method, site, target.value))]
    else:
        categories = wanted(route, options)
        if not categories:
            return
        context = await _taking(turn, run, _context(run.source, route, site, target,
                                                    run.call))
        listings = [_Listed(category.name, category.newest_first,
                            _caller(run, getattr(run.source, category.name), site, context))
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


async def _taking(turn: Turn | None, run: SourceRun, route: Awaitable[T]) -> T:
    """Call a target's route in its turn; the turn passes once the route returned, or meets
    the site, or the route's plugin is held."""
    if turn is None:
        return await route
    await turn.wait()
    if any(hold.plugin == run.source.code for hold in run.scheduler.holds):
        turn.release()
    token = met_turn.set(turn.release_soon)
    try:
        return await route
    finally:
        met_turn.reset(token)
        turn.release()


def _caller(run: SourceRun, method: Callable[..., Any], *args: Any) -> Callable[[], Awaitable[Any]]:
    """What calls a category's method with `args` (`_called`), each time."""
    return lambda: _called(run.call, method, *args)


@dataclass(frozen=True)
class _Listed:
    """One listing of a target: a category, or a route's own posts."""

    # The category's name; "" for a route's own posts.
    name: str
    newest_first: bool
    # Calls the category (or route) method, giving what it yields.
    items: Callable[[], Awaitable[Any]]


async def _list_one(
    work: _Work, run: SourceRun, site: TargetSite, target: Target, options: Options,
    progress: _Progress, *, name: str, newest_first: bool, items: Callable[[], Awaitable[Any]],
    remaining: int | None,
) -> tuple[Ended, int]:
    """List one category (or a route's own posts) into `work`, and note where it ended.

    How it goes:
    1. A `newest_first` listing an earlier run read to its end is synced (`_sync_one`): from
       the newest, stopping at saved posts, then into its gaps.
    2. Any other `newest_first` listing an earlier run cut short resumes from its saved
       cursor: the site's `Listing` hands it to the first `pages` call. Any other listing reads
       to its end, `--limit` or `--when`.
    3. A listing that ran out is complete; one read from the top keeps its head.
    4. A resumed listing that ran out lists again from the newest, down to the posts it saved
       before, since new posts may have come while it was interrupted.

    Args:
        work: the target's ledger and downloads, which queue each post's files.
        run: the source's resources for the run.
        site: the target's site; its `listing` is set while a listing runs.
        target: the target.
        options: the target's options: `--when`, `--only`, `--limit`, `--again`.
        progress: where the earlier runs left the target's listings, and this run's.
        name: the category's name; "" for a route's own posts.
        newest_first: whether the listing comes newest first.
        items: calls the category (or route) method, giving what it yields.
        remaining: posts the target may still take under `--limit`; None for no limit.

    Returns:
        How the listing ended, and how many posts it took.
    """
    ui.debug("{target}: list {name}", target=str(target), name=name or target.kind)
    stops = _Stops(
        sync=newest_first and name in progress.listed,
        streak=run.config.stop_after,
        head=progress.seen_heads.get(name),
    )
    if stops.sync:
        return await _sync_one(work, run, site, target, options, progress, name=name,
                               items=items, stops=stops, remaining=remaining)
    start = progress.started.get(name) if newest_first else None
    listing = progress.listings[name] = Listing(start, label=f"{target}: {name or target.kind}")
    work.ledger.tally.start(target, name, listing)
    if start:
        ui.info("{target}: {name}: going on where an interrupted run stopped",
                target=str(target), name=name or target.kind)
        await run.settings.emit(RESUMING)
    ended, taken, kept = await _listed(work, run, site, name, newest_first, items, listing,
                                       stops, options, remaining)
    if ended in (Ended.RAN_OUT, Ended.CAUGHT_UP):
        progress.complete.append(name)
        if not start and kept.newest is not None:
            progress.heads[name] = kept.newest
    if ended is Ended.RAN_OUT and start and listing.taken and not listing.refused:
        # The run that saved the cursor was cut short, so posts newer than those it saw may
        # have come since: list from the newest down to the saved posts.
        rest = None if remaining is None else remaining - taken
        if rest is None or rest > 0:
            ui.debug("{target}: list {name} from the newest", target=str(target),
                     name=name or target.kind)
            ended, extra, newest = await _listed(work, run, site, name, True, items, Listing(),
                                                 replace(stops, sync=True), options, rest)
            taken += extra
            if ended in (Ended.RAN_OUT, Ended.CAUGHT_UP) and newest.newest is not None:
                progress.heads[name] = newest.newest
    return ended, taken


async def _sync_one(
    work: _Work, run: SourceRun, site: TargetSite, target: Target, options: Options,
    progress: _Progress, *, name: str, items: Callable[[], Awaitable[Any]], stops: _Stops,
    remaining: int | None,
) -> tuple[Ended, int]:
    """Sync a `newest_first` listing an earlier run read to its end: the newest, then its gaps.

    A gap is where an earlier sync was cut short (Ctrl-C, an error, `--limit`) before it met
    the posts saved before it: the posts between are unsaved, and a sync that stopped at the
    saved posts on top would never reach them. Every gap lies above the listing's head, since a
    head is kept only by a run that leaves no gap.

    How it goes:
    1. List from the newest, stopping at saved posts (`_Stops`). A gap with no cursor (a feed
       that isn't paged by cursor, or one cut on its first page) turns the stop at 5 saved posts
       off, so the listing reads down to the head, past every gap; with no head, to its end.
    2. Once it met saved posts on top, jump to each gap's cursor, newest first, and list from
       there until it meets saved posts again ("resuming from the last run").
    3. Keep what is left: a listing cut short at the top leaves a new gap above the old ones,
       and one cut inside a gap leaves that gap from where it was cut.
    4. A listing that met saved posts with nothing queued and no gap had nothing new: one page,
       on a site that pages, so one request. It says so with a `Skipped` event.

    Args:
        work: the target's ledger and downloads.
        run: the source's resources for the run.
        site: the target's site.
        target: the target.
        options: the target's options.
        progress: where the earlier runs left the target's listings; this run's gaps, heads and
            completeness are written to it.
        name: the category's name; "" for a route's own posts.
        items: calls the category (or route) method, giving what it yields.
        stops: the sync's stops, with the head an earlier run saw.
        remaining: posts the target may still take under `--limit`; None for no limit.

    Returns:
        How the listing ended, and how many posts it took.
    """
    gaps = progress.gaps_of(name)
    label = f"{target}: {name or target.kind}"
    cursorless = "" in gaps
    if cursorless:
        stops = replace(stops, streak=None) if stops.head is not None else replace(stops,
                                                                                    sync=False)
    listing = progress.listings[name] = Listing(None, label=label)
    work.ledger.tally.start(target, name, listing)
    # 1. The newest posts. A listing that raises (the site failed it) is cut short like any:
    # where it was is kept as a gap before the error goes on to fail the target.
    try:
        ended, taken, kept = await _listed(work, run, site, name, True, items, listing, stops,
                                           options, remaining)
    except BaseException:
        progress.gaps[name] = list(dict.fromkeys([listing.cursor or "", *gaps]))
        raise
    queued = kept.queued
    left: list[str]
    if ended is Ended.RAN_OUT or (ended is Ended.CAUGHT_UP and cursorless):
        left = []
    elif ended is Ended.CAUGHT_UP:
        # 2. Into each gap, newest first.
        left = list(gaps)
        while left:
            rest = None if remaining is None else remaining - taken
            if rest is not None and rest <= 0:
                ended = Ended.CUT
                break
            ui.info("{target}: resuming from the last run", target=label)
            await run.settings.emit(RESUMING)
            jump = progress.listings[name] = Listing(left[0], label=label)
            work.ledger.tally.start(target, name, jump)
            try:
                ended, extra, filled = await _listed(work, run, site, name, True, items, jump,
                                                     stops, options, rest)
            except BaseException:
                progress.gaps[name] = [jump.cursor or left[0], *left[1:]]
                raise
            taken, queued = taken + extra, queued + filled.queued
            if jump.refused:
                # The site no longer pages from the gap's cursor, and listed from the newest
                # instead: the next sync reads down to the head, past the gap.
                left = list(dict.fromkeys(["", *left[1:]]))
                break
            if ended is Ended.RAN_OUT:
                # The listing's end is below every gap.
                left = []
            elif ended is Ended.CAUGHT_UP:
                left = left[1:]
            else:
                left = [jump.cursor or left[0], *left[1:]]
                break
    else:
        # 3. Cut short at the top: a new gap above the old ones.
        left = list(dict.fromkeys([listing.cursor or "", *gaps]))
    progress.gaps[name] = left
    if ended in (Ended.RAN_OUT, Ended.CAUGHT_UP):
        progress.complete.append(name)
        if not left and kept.newest is not None:
            progress.heads[name] = kept.newest
    if ended in (Ended.RAN_OUT, Ended.CAUGHT_UP) and not queued and not gaps:
        # 4. Nothing new.
        ui.info("{category}: nothing new", category=name or "posts")
        await run.settings.emit(Skipped(target, name, "", None, reason=NOTHING_NEW))
    return ended, taken


async def _retry_failed(
    work: _Work, run: SourceRun, site: TargetSite, target: Target, options: Options,
    listings: Sequence[_Listed],
) -> None:
    """List again each post earlier runs failed on, and queue its failed files, before new posts.

    A retry works from `(target, post, file)` and never from a stored URL: `flnd.db` keeps only
    a redacted one, and a CDN's signed URL expires within hours. So each failed post is listed
    again through its source, for fresh URLs.

    How it goes:
    1. Read the target's failures (`Home.pending`): those failed on at most `home.RETRIES`
       runs, or every one for `flnd retry`. A gone file (`NotFound`) is never among them.
    2. Group them by listing, and by the cursor of the page their post was on.
    3. For each group, list from that cursor (`_relist`): each failed post met is queued like
       any post, which downloads only its files that aren't saved: the ones that failed.
    4. A post the listing no longer has is a failure of its own: gone when the listing ran out
       without it, else one more run tried, so the post is given up after its last.

    Args:
        work: the target's ledger and downloads.
        run: the source's resources for the run.
        site: the target's site; its `listing` is set while a retry lists.
        target: the target.
        options: the target's options: `retry` says which failures; `--only` which listings.
        listings: the listings this run reads; a failure of another one waits.
    """
    db = run.settings.db
    if run.settings.dry or options.again or db is None:
        return
    failed = await run.off(functools.partial(db.pending, home.key(target),
                                             every=options.retry is Retry.ONLY))
    by_name = {listed.name: listed for listed in listings}
    groups: dict[tuple[str, str | None], dict[str, list[home.FailureRow]]] = {}
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
    work: _Work, run: SourceRun, site: TargetSite, listed: _Listed, cursor: str | None,
    posts: set[str],
) -> tuple[set[str], bool]:
    """List one listing from `cursor` until it has met every post of `posts`, queueing each.

    From a cursor, it gives up `RETRY_WINDOW` posts past the last one it met: the posts were on
    that cursor's page. From the newest (no cursor), it reads until it met them all or the
    listing ran out. `--when` doesn't apply: the posts were within it when they failed.

    Args:
        work: the target's ledger and downloads.
        run: the source's resources for the run.
        site: the target's site, whose `listing` is set for the listing, then cleared.
        listed: the listing.
        cursor: where to start; None for the newest.
        posts: the ids of the posts to meet.

    Returns:
        The posts it didn't meet, and whether the listing ran out (rather than being given
        up or stopped). A run told to stop returns none missing.
    """
    left, past, last = set(posts), 0, None
    site.listing = Listing(cursor, label=f"{site.target}: {listed.name or site.target.kind}")
    kept = _Kept(await listed.items(), listed.name, newest_first=listed.newest_first,
                 span=Range(), call=run.call, close=run.off)
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
    return left, kept.ran_out


async def _lost(
    work: _Work, run: SourceRun, target: Target, rows: Sequence[home.FailureRow], *,
    ran_out: bool,
) -> None:
    """Record that a retry didn't meet a failed post: a failure of each of its files.

    Args:
        work: the target's ledger and downloads, whose tally counts each file failed.
        run: the source's resources for the run, with its database and event sink.
        target: the target.
        rows: the post's failures, one per file.
        ran_out: whether its listing ran out without it: it is gone (`NotFound`), never asked
            for again unless told to. Otherwise this run counts as one more that tried it.
    """
    db = run.settings.db
    for row in rows:
        error: SiteError
        if ran_out:
            error = NotFound(f"post {row.post} is no longer in its listing")
        else:
            error = SiteError(f"post {row.post} wasn't found near where it was listed",
                              hint="flnd retry tries again", retryable=True)
        if db is not None:
            await run.off(functools.partial(
                db.fail, run=run.settings.run, item=row.key, target=row.target, error=error,
                post=row.post, category=row.category, file=row.file, cursor=row.cursor))
        work.ledger.tally.count(target, row.category, found=1, failed=1)
        await run.settings.emit(Failed(target, error, post=row.post, path=Path(row.file)))


async def _listed(
    work: _Work, run: SourceRun, site: TargetSite, name: str, newest_first: bool,
    items: Callable[[], Awaitable[Any]], listing: Listing, stops: _Stops, options: Options,
    remaining: int | None,
) -> tuple[Ended, int, _Kept]:
    """Run one listing with `listing` set on the site, queueing what it yields into `work`.

    Args:
        work: the target's ledger and downloads; the targets the listing yields join its
            `linked`.
        run: the source's resources for the run.
        site: the target's site, whose `listing` is set for the listing, then cleared.
        name: the category's name; "" for a route's own posts.
        newest_first: whether the listing comes newest first.
        items: calls the category (or route) method, giving what it yields.
        listing: the `Listing` the first `pages` call resumes and reports to.
        stops: when a sync has caught up.
        options: the target's options, for `--when`.
        remaining: posts it may take under `--limit`; None for no limit.

    Returns:
        How it ended, how many posts it took, and the `_Kept` that read it (its head, its
        linked targets).
    """
    site.listing = listing
    try:
        kept = _Kept(await items(), name, newest_first=newest_first,
                     span=Range(options.since, options.until), call=run.call, close=run.off)
        ended, taken = await _queue_all(work, kept, stops=stops, limit=remaining)
    finally:
        site.listing = None
    work.linked.extend(kept.linked)
    return ended, taken, kept


async def _run_discovered(run: SourceRun, options: Options, yielded: Any) -> None:
    """Run each target a discovering route yields, in turn, as a target of its own.

    Such a route (an author's page listing their works) has no posts of its own; each target
    it yields runs with the parent's options, through the run's `follow`, on its own plugin's
    instance, so a link to another site runs on that site's plugin. `--limit` counts targets.
    The engine runs most of these as searches; this is the path for one reached by `download`
    directly, as a found target can be.

    Args:
        run: the parent's source run.
        options: the parent's options.
        yielded: what the route gave.
    """
    seen: set[Target] = set()
    child_options = replace(options, limit=None)
    follow = run.settings.follow
    async with contextlib.aclosing(_steps(yielded, run.call, run.off)) as items:
        async for item in items:
            if run.settings.stop.soon.is_set():
                return
            child = await resolved(run, item)
            if child is None or child in seen:
                continue
            seen.add(child)
            if follow is not None:
                await follow(child, child_options, inside=run.source.code)
            elif child.source == run.source.code:
                await download(run, child, run.settings.tally or Tally(), child_options)
            if options.limit is not None and len(seen) >= options.limit:
                return


async def _steps(yielded: Any, call: Call, close: Call) -> AsyncIterator[Any]:
    """Iterate what a route or category yields, plain or async, closing it however it ends.

    A plain iterator is stepped through `call` (a thread of the source in a run), one item at a
    time, and closed through `close`. An async generator is stepped on the loop; each stretch it
    runs without awaiting is timed, and one past `HELD` seconds is warned about, since it stalls
    every download. None yields nothing; a coroutine is awaited for what it gives.

    Args:
        yielded: a generator, an iterable, an async generator, a coroutine, or None.
        call: runs the source's plain code.
        close: runs a plain generator's `close`, even while the run stops.

    Yields:
        Each item, as the source yielded it.
    """
    if inspect.iscoroutine(yielded):
        yielded = await yielded
    if not hasattr(yielded, "__anext__"):
        iterator = iter(yielded if yielded is not None else ())
        try:
            while (item := await call(next, iterator, _DONE)) is not _DONE:
                yield item
        finally:
            stop = getattr(iterator, "close", None)
            if stop is not None:
                await close(stop)
        return
    name = getattr(yielded, "__qualname__", "an async listing")

    def slow(seconds: float) -> None:
        ui.warn("{name} held the loop {seconds:.1f} s without awaiting; await, or use a plain "
                "def, which runs on its own thread", name=name, seconds=seconds)
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
    """An awaitable that times each stretch the one it wraps runs without awaiting.

    A coroutine runs in stretches, from one `await` that suspends it to the next, and nothing
    else on the loop runs meanwhile. Each stretch is measured on the real monotonic clock (a
    fake clock can't see a blocking call), and `slow(seconds)` is told of one over `limit`.

    Args:
        awaitable: the awaitable to run.
        limit: the seconds a stretch may take.
        slow: told the seconds of each stretch over `limit`; it must not raise.
    """

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


def _extension(file: File) -> tuple[str, str | None]:
    """A file's extension and kind (`files.VIDEO`, …), guessed before it is downloaded.

    From its name, else its URL's path. A video with a separate audio track (DASH) is joined
    into `.webm` when both are WebM, else `.mp4`. A file whose kind can't be told yet is
    sniffed from its first bytes when it lands (`files.SNIFF`).

    Args:
        file: the file.

    Returns:
        The extension with its dot ("" when unknown), and the kind (None for inline content).
    """
    if file.content is not None:
        return PurePosixPath(file.name or "").suffix, None
    if file.name is not None:
        return PurePosixPath(file.name).suffix, files.kind_of(file.name)
    url = (
        file.url if isinstance(file.url, str)
        else file.url[0] if isinstance(file.url, tuple)
        else ""
    )
    extension = PurePosixPath(urlsplit(url).path).suffix.lower() if url else ""
    kind = files.kind_of(f"file{extension}") if extension else None
    if file.audio is not None:
        # A video and its audio track are joined without re-encoding: a WebM pair into WebM,
        # anything else into MP4, which takes every codec the sites send.
        audio = PurePosixPath(urlsplit(file.audio).path).suffix.lower()
        both = extension == ".webm" and audio == ".webm"
        return (".webm" if both else DASH_EXTENSION), files.VIDEO
    if kind is None:
        return "", files.SNIFF
    return extension, kind


def _fields(template: str) -> set[str]:
    """Return top-level field names referenced in `template`."""
    return {
        name.split(".")[0].split("[")[0] for _, name, _, _ in _FORMATTER.parse(template) if name
    }


def _folder_format(source: Source, route: Route | None, category: Category | None) -> str:
    """The folder template a file is placed by: its category's, else its route's, else its
    source's.

    Args:
        source: the file's source.
        route: the target's route; None to skip it.
        category: the post's category; None to skip it.

    Returns:
        The template, such as `{category}`.
    """
    if category is not None and category.folder is not None:
        return category.folder
    if route is not None:
        route_folder = getattr(route, "folder", None)
        if route_folder is not None:
            return route_folder
    return source.folder


def _format(source: Source, category: Category | None, name: str) -> str:
    """Return format string `name` from `category` if set, else from `source`."""
    own = getattr(category, name, None)
    return own if own is not None else getattr(source, name)


async def resolved(run: SourceRun, item: Target | str) -> Target | None:
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
        if own.source == run.source.code:
            text = f"?{own.value}" if own.query else own.value
        return await run.off(run.source.resolve, text)
    except Error as error:
        ui.warn("{source}: can't follow {text}: {error}", source=run.source.code, text=text,
                error=error)
        return None

