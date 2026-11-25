"""The engine's `Ctx` for one target of a run, and a plugin's run: its session, tabs, listings.

SPEC.md#for-developers explains it; `kit.FakeCtx` answers the same way from fakes.
"""

from __future__ import annotations

import asyncio
import contextlib
import datetime
import functools
import json
import threading
import weakref
from collections.abc import (
    AsyncIterator,
    Awaitable,
    Callable,
    Iterable,
    Iterator,
    Mapping,
    MutableMapping,
    Sequence,
)
from contextvars import ContextVar
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import TYPE_CHECKING, Any, TypeVar
from urllib.parse import urlsplit

import flnd.plugin
from flnd.engine import auth, config, jobs, store
from flnd.engine.config import PluginConfig
from flnd.errors import (
    Blocked,
    LoginError,
    NetworkError,
    PluginError,
    SiteError,
    Stopped,
    UsageError,
)
from flnd.plugin import Landed, Plugin, Range, Request, Target
from flnd.ui import forms
from flnd.ui.log import ui
from flnkit import browser, clock, net, text

if TYPE_CHECKING:
    from flnd.engine.crawl import RunShared
    from flnd.engine.run import Stop
    from flnkit import cookies
    from flnkit.html import Document

__all__ = ["IDLE_TIMEOUT", "LOGGING_IN", "PAGE_LIMIT", "Bridge", "Ctx", "JsonState", "Listing",
           "PluginRun", "Secrets", "TargetTabs", "Time", "checked", "empty",
           "first_page", "granted", "met_turn", "opened", "plugin_run", "refused",
           "scheduler", "shared_browser", "start", "walled", "writable"]

T = TypeVar("T")
# While a plugin's `Login.start` runs, its leave to write `ctx.secrets` (6.7): what it starts
# inherits the leave, which ends when `start` returns, so a task it left running can't write.
LOGGING_IN: ContextVar[_Leave | None] = ContextVar("flnd.logging_in", default=None)
# The most a page body may hold: a file comes as a `File`, never through `ctx.fetch`.
PAGE_LIMIT = 32 << 20
# What `ctx.log` takes as its level.
LEVELS = ("debug", "info", "warn")
# What `_Feed.__anext__` gets from a feed that ended.
_DONE = object()
# Methods a server may receive twice without harm: only their failures are tried again.
SAFE = frozenset({"GET", "HEAD", "OPTIONS", "PUT", "DELETE"})
# How soon before a login cookie expires a run warns of it, in seconds: 3 days, so a daily
# `flnd get` warns a few runs before the one that meets the login wall.
EXPIRY_WARNING = 3 * 24 * 3600


class _Leave:
    """One login's leave to write secrets, given by `granted` and taken back as it ends."""

    def __init__(self) -> None:
        self.open = True


@contextlib.contextmanager
def granted() -> Iterator[None]:
    """Let the block, and what it starts, write `ctx.secrets`; the leave ends with it."""
    leave = _Leave()
    token = LOGGING_IN.set(leave)
    try:
        yield
    finally:
        leave.open = False
        LOGGING_IN.reset(token)


def writable() -> bool:
    """Whether `ctx.secrets` may be written here: inside a login that hasn't ended."""
    leave = LOGGING_IN.get()
    return leave is not None and leave.open


async def start(login: flnd.plugin.Login, ctx: Any, method: str) -> None:
    """Run `login.start(ctx, method)` under its own leave (`granted`) to write `ctx.secrets`.

    What it starts inherits the leave (an `asyncio` task, a `ctx.call`); a plain thread doesn't,
    and nothing writes once `start` returned.
    """
    with granted():
        await login.start(ctx, method)


# ---------------------------------------------------------------------------------------------
# How an answer is read: the same rules for the engine and the test kit
# ---------------------------------------------------------------------------------------------

def refused(plugin: Plugin, response: Any, *, own: bool) -> None:
    """Raise the `Blocked` the scheduler holds for: a block `plugin.blocked` reads, or a 429.

    Raises:
        Blocked: with the plugin's seconds (a block round), or a 429's wait.
    """
    seconds = plugin.blocked(response) if own else None
    if seconds is not None:
        raise Blocked(f"{plugin.code}: blocked, HTTP {response.status} for "
                      f"{text.redact(response.url)}", wait=seconds)
    if response.status == 429:
        error = net.refusal(response)
        assert error is not None
        raise error


def walled(plugin: Plugin, url: str, *, own: bool) -> None:
    """Raise the scheduler's wall when `url`, where a page or a redirect landed, is one.

    Raises:
        LoginError: a retryable one, which the scheduler waits out twice.
    """
    found = flnd.plugin.wall(url, plugin.walls) if own else None
    if found is not None:
        raise LoginError(f"{plugin.code}: landed on the wall {found}, at {text.redact(url)}",
                         retryable=True)


def checked(plugin: Plugin, response: Any, *, own: bool, every: bool = True) -> Any:
    """`response`, or the error the engine raises for its status.

    Args:
        plugin: the plugin whose request it answers.
        response: the answer.
        own: whether it came from the plugin's domains, where a 401 or 403 is a login's.
        every: raise for any other 4xx or 5xx too; False returns it (an `open`).

    Raises:
        LoginError: a 401 or 403 from the plugin's domains.
        Error: `net.refusal`'s for a 404 or 410, and with `every` for any other refusal.
    """
    status = response.status
    if status in (401, 403) and own:
        raise LoginError(f"{plugin.code}: HTTP {status} for {text.redact(response.url)}")
    if status in net.GONE or (every and status >= 400):
        error = net.refusal(response)
        assert error is not None
        raise error
    return response


def capped(response: Any) -> Any:
    """`response`, unless its body is over `PAGE_LIMIT`: a file comes as a `File`.

    Raises:
        SiteError: the body is over the cap.
    """
    if len(response.content) > PAGE_LIMIT:
        raise SiteError(f"{text.redact(response.url)}: the answer is over "
                        f"{text.size(PAGE_LIMIT)}, too big to read as a page",
                        status=response.status)
    return response


class Once:
    """What a request never sent twice (a POST) gives back for an error, past the scheduler's
    retries: the server may have acted on it (a code sent, a form posted)."""

    def __init__(self, error: Exception) -> None:
        self.error = error


def once(request: Request, error: Exception) -> Any:
    """For a request that isn't safe to send twice: what goes back past the retries.

    A 429, a block and a wall still go to the scheduler: the server didn't act. Any other
    error goes back at once, a 5xx's answer itself when `check` is off.

    Raises:
        Exception: `error`, for one the scheduler may wait out and send again.
    """
    if request.method.upper() in SAFE or isinstance(error, (Blocked, LoginError)):
        raise error
    if (isinstance(error, SiteError) and not request.check and error.response is not None
            and (error.status or 0) >= 500):
        return error.response
    return Once(error)


def domain_cookies(cookies: Iterable[Mapping[str, Any]], domains: Sequence[str]) -> dict[str, str]:
    """`{name: value}` of the cookies set for one of `domains` or a host under one."""
    return {cookie["name"]: cookie["value"] for cookie in cookies
            if _on(str(cookie.get("domain", "")), domains)}


def _on(domain: str, domains: Sequence[str]) -> bool:
    bare = domain.lstrip(".").lower()
    return bool(bare) and any(bare == own or bare.endswith("." + own) for own in domains)


def host_cookies(cookies: Iterable[Mapping[str, Any]], host: str) -> list[Mapping[str, Any]]:
    """The cookies a request to `host` would carry: set for it, or for a domain above it."""
    host = host.lower()
    return [cookie for cookie in cookies if (bare := str(cookie.get("domain", "")).lstrip(".")
                                             .lower()) and (host == bare
                                                            or host.endswith("." + bare))]


def empty(response: Any) -> bool:
    """Whether a page holds nothing: no body, or JSON that is null, {} or []."""
    if not response.content:
        return True
    try:
        return response.json in (None, {}, [])
    except SiteError:
        return False


def on_domains(plugin: Plugin, url: str) -> bool:
    """Whether `url` is on the plugin's domains; a path alone is."""
    if "://" not in url:
        return True
    host = (urlsplit(url).hostname or "").lower()
    return any(host == domain or host.endswith("." + domain) for domain in plugin.domains)


# ---------------------------------------------------------------------------------------------
# What a plugin reads
# ---------------------------------------------------------------------------------------------

class Time:
    """A run's clock as `flnd.plugin.Clock` reads it: `now()`, `wall()` and `day()`.

    Args:
        current: gives the clock to read, on each call: the active one in the engine, a
            context's fake one in the test kit.
    """

    def __init__(self, current: Callable[[], clock.Clock] = clock.current) -> None:
        self._current = current

    def now(self) -> float:
        return self._current().now

    def wall(self) -> float:
        return self._current().wall

    def day(self) -> datetime.date:
        return self._current().day()


class JsonState(MutableMapping[str, Any]):
    """A plugin's state over a mapping: each value must come back from JSON as it went in.

    Raises:
        PluginError: a value that JSON can't hold as it is (a set, a tuple, a non-text key).
    """

    def __init__(self, data: MutableMapping[str, Any]) -> None:
        self._data = data

    def __getitem__(self, key: str) -> Any:
        return self._data[key]

    def __setitem__(self, key: str, value: Any) -> None:
        try:
            same = json.loads(json.dumps(value)) == value and _plain(value)
        except (TypeError, ValueError):
            same = False
        if not isinstance(key, str) or not same:
            raise PluginError(f"state[{key!r}]: {type(value).__name__} isn't a JSON value; store "
                              "lists, dicts with text keys, text, numbers, booleans or None")
        self._data[key] = value

    def __delitem__(self, key: str) -> None:
        del self._data[key]

    def __iter__(self) -> Iterator[str]:
        return iter(self._data)

    def __len__(self) -> int:
        return len(self._data)


def _plain(value: Any) -> bool:
    """Whether `value` is made of JSON's own types only (a tuple compares equal to no list)."""
    if isinstance(value, dict):
        return all(isinstance(key, str) and _plain(item) for key, item in value.items())
    if isinstance(value, list):
        return all(_plain(item) for item in value)
    return value is None or isinstance(value, (str, int, float, bool))


class Secrets(MutableMapping[str, str]):
    """A plugin's secrets: read any time, written only while `Login.start` runs (`start`).

    Args:
        data: gives the mapping that keeps them, opened on first use.

    Raises:
        PluginError: a write or a delete outside `Login.start`.
    """

    def __init__(self, data: Callable[[], MutableMapping[str, str]]) -> None:
        self._open = data
        self._data: MutableMapping[str, str] | None = None

    @property
    def _kept(self) -> MutableMapping[str, str]:
        if self._data is None:
            self._data = self._open()
        return self._data

    @staticmethod
    def _writable() -> None:
        if not writable():
            raise PluginError("ctx.secrets is written only during Login.start")

    def __getitem__(self, key: str) -> str:
        return self._kept[key]

    def __setitem__(self, key: str, value: str) -> None:
        self._writable()
        self._kept[key] = value

    def __delitem__(self, key: str) -> None:
        self._writable()
        del self._kept[key]

    def __iter__(self) -> Iterator[str]:
        return iter(self._kept)

    def __len__(self) -> int:
        return len(self._kept)


# ---------------------------------------------------------------------------------------------
# The context
# ---------------------------------------------------------------------------------------------

class Ctx:
    """The engine's `flnd.plugin.Ctx` for one target (or the plugin itself) in a run.

    Each request is a scheduler job: a page job on the plugin's domains, a file-pooled one
    elsewhere, retried and held by the scheduler's policy. The browser's verbs go through the
    target's `TargetTabs`, one call on a thread each.

    Args:
        run: the plugin's resources for the run.
        target: the target it acts for; `<code>:` for the plugin itself.
        when: the run's `when`.
        login: for `flnd auth login`: `ask` asks in the terminal.
    """

    def __init__(self, run: PluginRun, target: Target, *, when: Range = Range(),
                 login: bool = False, again: bool = False) -> None:
        self._run = run
        self._plugin: Plugin = run.source
        self._target = target
        self._when = when
        self._login = login
        self._again = again
        self._tabs = TargetTabs(run)
        # The plugin's own keys of config.toml (`[plugins.<code>]`), else its defaults.
        configured = getattr(run.config, "settings", None)
        self._settings = (configured if configured is not None
                          else type(self._plugin).Settings())
        self._state = JsonState(run.values)
        self._secrets = Secrets(lambda: run.secrets)
        # The category's listing, which `pages` resumes and reports to; the crawl sets it.
        self.listing: Listing | None = None
        self._request: Request | None = None
        self._opened = ""
        # The feeds made, each listening from its call: `open` and `click` wait for them.
        self._feeds: list[_Feed] = []
        # The last feed being made: each waits for the one before, since a target's tabs are
        # used from one thread at a time (two at once would each open a tab of their own).
        self._making: asyncio.Future[Any] | None = None

    # -- what the crawl reads -------------------------------------------------------------

    @property
    def _idle(self) -> bool:
        """Whether it never opened a tab, so `close` has nothing to do."""
        return self._tabs.idle

    def close(self) -> None:
        """Stop the feeds still listening and give back the target's tabs; on a thread."""
        feeds, self._feeds = self._feeds, []
        for feed in feeds:
            made, feed._feed, feed._done = feed._feed, None, True
            if made is not None:
                made.close()
        self._tabs.close()

    async def _listened(self) -> None:
        """Wait until every feed made so far listens."""
        self._feeds = [feed for feed in self._feeds if not feed._done]
        for feed in list(self._feeds):
            if feed._opening is not None:
                await feed._open()

    @contextlib.asynccontextmanager
    async def aside(self) -> AsyncIterator[None]:
        """Tabs of its own for the block: a lazy post's files, `refresh` and `date` open their
        own, never the listing's, and they close when it ends."""
        opened: list[tuple[TargetTabs, Any]] = []
        token = _ASIDE.set(opened)
        try:
            yield
        finally:
            _ASIDE.reset(token)
            if opened:
                await self._run.off(_close_tabs, opened)

    # -- HTTP -----------------------------------------------------------------------------

    async def fetch(self, request: str | Request, *, bucket: str | None = None,
                    in_page: bool = False) -> net.Response:
        """See `flnd.plugin.Ctx.fetch`.

        A page body over `PAGE_LIMIT` is refused (`SiteError`), so a file never comes this way.
        """
        asked = request if isinstance(request, Request) else Request(request)
        url = net.with_query(asked.url, asked.params)
        plugin = self._plugin

        async def attempt() -> Any:
            try:
                response = await self._send(asked, url, in_page)
                own = on_domains(plugin, response.url)
                refused(plugin, response, own=own)
                if response.url.split("#", 1)[0] != url.split("#", 1)[0]:
                    walled(plugin, response.url, own=own)
                # A 5xx is the server's trouble, tried again even when the plugin reads
                # refusals; a POST's never is.
                if asked.check or response.status >= 500:
                    return checked(plugin, response, own=own)
                return response
            except Exception as error:
                return once(asked, error)

        try:
            got = await self._job(url, attempt, bucket=bucket or "")
        except SiteError as error:
            # With `check` off, the last 5xx is handed back once the tries ran out.
            if asked.check or (error.status or 0) < 500 or error.response is None:
                raise
            return error.response
        if isinstance(got, Once):
            raise got.error
        return got

    async def _send(self, asked: Request, url: str, in_page: bool) -> net.Response:
        """One try of a request: from the target's tab, or through the run's client."""
        headers, body = dict(asked.headers or {}), asked.body
        if body is not None and not isinstance(body, (bytes, str)):
            # Anything but bytes or text goes as JSON, on any method (`flnd.plugin.Request`).
            body = json.dumps(body).encode()
            if not any(name.lower() == "content-type" for name in headers):
                headers["Content-Type"] = "application/json"
        if in_page:
            page = await self._run.off(self._fetch_page, url)
            if page.url in ("", "about:blank") and "://" in url:
                # A fresh tab (`aside`) has a null origin, which a site refuses to answer: the
                # page loads the site's own robots.txt first, so the fetch is same-origin.
                parts = urlsplit(url)
                await page.goto(f"{parts.scheme}://{parts.netloc}/robots.txt")
            await self._sync_cookies(url)
            _sign(self._plugin, url, asked.method, headers, body, self._run.session.cookies)
            return capped(await page.fetch(url, method=asked.method, headers=headers,
                                           body=body, check=False))
        await self._sync_cookies(url)
        return await self._run.web.fetch(url, method=asked.method, headers=headers, body=body,
                                         check=False, limit=PAGE_LIMIT)

    async def _sync_cookies(self, url: str) -> None:
        """Copy cookies the browser set for `url`'s host into the run's session."""
        if not self._tabs.opened:
            return
        cookies = host_cookies(await self._run.off(self._tabs.cookies), net.URL(url).host)
        if cookies:
            self._run.session.add(cookies)

    def _job(self, url: str, work: Callable[[], Awaitable[T]], *,
             bucket: str = "") -> Awaitable[T]:
        """`work` as a scheduler job of the target's plugin: a `PAGE` job on the plugin's
        domains, paced in its bucket; a `FILE` job on any other host, under its cap, ahead of
        the target's files."""
        host = net.URL(url).host
        own = on_domains(self._plugin, url)
        job = jobs.Job(kind=jobs.Kind.PAGE if own else jobs.Kind.FILE, work=work,
                       plugin=self._plugin.code, host=host,
                       bucket=bucket if own else "", target=str(self._target),
                       priority=None if own else jobs.PRIORITY[jobs.Kind.PAGE])
        return self._run.scheduler.run(job)

    def pages(self, request: str | Request, *,
              cursor: str | Callable[[Any], str | None],
              send: str | Callable[[str], Mapping[str, Any] | Request] = "cursor",
              bucket: str | None = None, in_page: bool = False,
              resume: bool = False) -> AsyncIterator[net.Response]:
        """See `flnd.plugin.Ctx.pages`: the category's first `pages` resumes its listing."""
        first = request if isinstance(request, Request) else Request(request)
        return self._pages(first, cursor, send, bucket, in_page, resume)

    async def _pages(self, first: Request, cursor: Any, send: Any, bucket: str | None,
                     in_page: bool, resume: bool) -> AsyncIterator[net.Response]:
        listing = self.listing if self.listing is not None and not self.listing.taken else None
        if listing is not None:
            listing.taken = True

        async def ask(at: str | None) -> net.Response:
            asked = first
            if at is not None:
                more = send(at) if callable(send) else {send: at}
                asked = more if isinstance(more, Request) else first.replace(
                    params={**(first.params or {}), **more})
            return await self.fetch(asked, bucket=bucket, in_page=in_page)

        came = [False]
        asking = ask_again(self, ask, bucket, came) if resume else ask
        response = await first_page(asking, listing)
        seen: set[str] = set()
        while not empty(response):
            if listing is not None:
                listing.read += 1
            came[0] = True
            yield response
            following = (cursor(response) if callable(cursor)
                         else flnd.plugin.dig(response.json, cursor))
            if following is None or following == "" or str(following) in seen:
                return
            seen.add(str(following))
            if listing is not None:
                # The cursor of the page being read: a run cut short reads it again.
                listing.cursor = str(following)
            response = await asking(str(following))

    # -- the tab --------------------------------------------------------------------------

    def _page(self) -> browser.Page:
        """The tab the target works in now (a lazy post's own one in `aside`), on a thread."""
        return self._tabs.verbs().page

    def _fetch_page(self, url: str) -> browser.Page:
        """An open same-origin tab of the target for an in-page fetch, else `self._page()`."""
        tabs = self._tabs._tabs
        if tabs is not None and tabs.opened and "://" in url:
            page = tabs.page
            if urlsplit(page.url)[:2] == urlsplit(url)[:2]:
                return page
        return self._page()

    async def open(self, url: str) -> Landed:
        """See `flnd.plugin.Ctx.open`: a warm tab loading `url` becomes the target's."""
        plugin = self._plugin
        self._opened = url
        await self._listened()
        page, warm = await self._run.off(self._take, url)

        async def attempt() -> Landed:
            nonlocal warm
            if warm is not None:
                visit, warm = warm, None
                try:
                    await page.wait(timeout=30.0)
                except browser.WaitError as error:
                    raise NetworkError(f"{plugin.code}: could not open {text.redact(url)}: "
                                       f"{error}", retryable=True) from None
            else:
                visit = await page.goto(url)
            if visit is None:
                raise NetworkError(f"{plugin.code}: could not open {text.redact(url)}",
                                   retryable=True)
            own = on_domains(plugin, visit.url)
            # The page's HTML for a `blocked` hook to read; none is read without one.
            content = (await page.content()).encode() if own and _reads_blocks(plugin) else b""
            response = net.Response(visit.status, visit.url, dict(visit.headers), content)
            refused(plugin, response, own=own)
            walled(plugin, visit.url, own=own)
            checked(plugin, response, own=own, every=False)
            return Landed(visit.url, visit.status)

        return await self._job(url, attempt)

    def _take(self, url: str) -> tuple[browser.Page, browser.Visit | None]:
        """The target's tab for `url`, a warm one if it loads `url`, and the warm one's first
        answer; on a thread."""
        tabs = self._tabs.verbs()
        warmed, first = tabs.take_warm(url)
        return tabs.page, first if warmed else None

    def feed(self, match: str | Any | Callable[[str], bool], *, scroll: bool = True,
             lines: bool = False,
             cursor: Callable[[Any], tuple[bool, str | None] | None] | None = None,
             resume: bool = False, bucket: str | None = None) -> _Feed:
        """See `flnd.plugin.Ctx.feed`; it listens from the call."""
        made = _Feed(self, match, scroll=scroll, lines=lines, cursor=cursor, resume=resume,
                     bucket=bucket)
        made.listen()
        self._feeds.append(made)
        return made

    def load(self, url: str, match: str | Any | Callable[[str], bool], *,
             scroll: bool = True, lines: bool = False,
             cursor: Callable[[Any], tuple[bool, str | None] | None] | None = None
             ) -> AsyncIterator[Any]:
        """See `flnd.plugin.Ctx.load`."""
        return self._load(url, self.feed(match, scroll=scroll, lines=lines, cursor=cursor))

    async def _load(self, url: str, feed: _Feed) -> AsyncIterator[Any]:
        async with feed:
            await self.open(url)
            self._request = None
            yield flnd.plugin.meta(await self.document())
            async for item in feed:
                yield item

    async def document(self) -> Document:
        """See `flnd.plugin.Ctx.document`."""
        return await self._run.off(self._tabs.document)

    async def click(self, css: str, *,
                    timeout: float = 10.0) -> None:  # noqa: ASYNC109 (the tab job's own limit)
        """See `flnd.plugin.Ctx.click`."""
        await self._listened()
        await self._run.off(functools.partial(self._tabs.click, css, timeout=timeout))

    async def shown(self, css: str) -> bool:
        """See `flnd.plugin.Ctx.shown`."""
        return await self._run.off(self._shown, css)

    def _shown(self, css: str) -> bool:
        tabs = self._tabs.verbs()
        return tabs.run(tabs.page.shown(css))

    async def handoff(self, message: str, *, until: Callable[[], Awaitable[bool]],
                      timeout: float | None = None) -> bool:  # noqa: ASYNC109 (a person's)
        """See `flnd.plugin.Ctx.handoff`."""
        loop = self._run.loop.loop

        def done() -> bool:
            return bool(browser.run_on(loop, until()))

        return await self._run.off(functools.partial(
            self._tabs.handoff, until=done, message=message, timeout=timeout))

    async def warm(self, urls: Sequence[str]) -> None:
        """Start loading `urls` in warm tabs, while the run's tab budget has room.

        For pages a listing opens next that no category's `url=` names (an album's pages, a
        profile's story buckets); an empty URL opens nothing.
        """
        await self._run.off(self._tabs.warm, list(urls))

    # -- waits, threads, people -----------------------------------------------------------

    async def pause(self, seconds: float, reason: str) -> None:
        """See `flnd.plugin.Ctx.pause`.

        Raises:
            Stopped: the run's first Ctrl-C came while waiting.
        """
        if seconds <= 0:
            return
        tally = self._run.settings.tally
        held = (tally.hold(self._plugin.code, "pages", seconds, reason)
                if tally is not None else contextlib.nullcontext())
        with held:
            if not await clock.pause(seconds, stop=self._run.web.stop):
                raise Stopped(f"{self._target}: stopped while waiting: {reason}")

    async def hold(self, reason: str, *, seconds: float | None = None,
                   bucket: str | None = None, reset: bool = False) -> None:
        """See `flnd.plugin.Ctx.hold`: a block round, waited out here before the plugin goes
        on, as its next job would wait it out: a listing that reads a feed or an SDK, which
        aren't jobs, resumes only once the hold ends.

        Raises:
            Blocked: past the last round.
            Stopped: the run's first Ctrl-C came while waiting.
        """
        code = self._plugin.code
        try:
            held = self._run.scheduler.hold(code, seconds=seconds or 0.0, reason=reason,
                                            bucket=bucket, reset=reset)
        except Blocked as error:
            raise Blocked(f"{code}: {reason}, after {len(jobs.ROUND_WAITS)} holds",
                          hint="try again later, or raise the plugin's pace in config.toml",
                          target=str(self._target)) from error
        ui.warn("{target}: {reason}; waiting {time}", target=str(self._target), reason=reason,
                time=text.duration(round(held)))
        tally = self._run.settings.tally
        shown = (tally.hold(code, "pages", held, reason) if tally is not None
                 else contextlib.nullcontext())
        with shown:
            try:
                await self._run.scheduler.wait(code, bucket=bucket)
            except Stopped:
                raise Stopped(f"{self._target}: stopped while waiting: {reason}") from None

    async def call(self, fn: Callable[..., T], *args: Any) -> T:
        """See `flnd.plugin.Ctx.call`: a `CALL` job, on the thread pool."""
        return await self._run.call(fn, *args)

    async def ask(self, message: str, *, secret: bool = False, description: str = "",
                  choices: Sequence[str] = ()) -> str:
        """See `flnd.plugin.Ctx.ask`: only `flnd auth login` asks, in a terminal.

        Raises:
            UsageError: a run, or no terminal: nobody can answer.
        """
        code = self._plugin.code
        if not (self._login and self._run.settings.interactive and forms.interactive()):
            raise UsageError(f"{code}: nobody can answer {message!r} here",
                             hint=f"run flnd auth login {code} in a terminal")
        if description:
            ui.info("{description}", description=description)
        # On a thread, so the loop (a Telethon client's) runs on while a person types.
        if choices:
            return str(await self._run.off(forms.choose, message, list(choices)))
        return await self._run.off(functools.partial(forms.ask, message, secret=secret))

    def log(self, level: str, message: str, **fields: Any) -> None:
        """See `flnd.plugin.Ctx.log`.

        Raises:
            PluginError: `level` isn't "debug", "info" or "warn".
        """
        if level not in LEVELS:
            raise PluginError(f"{self._plugin.code}: log level {level!r}; use one of "
                              f"{', '.join(LEVELS)}")
        line = message.format(**fields) if fields else message
        getattr(ui, level)("{target}: {line}", target=str(self._target), line=line)

    # -- what it reads --------------------------------------------------------------------

    @property
    def target(self) -> Target:
        return self._target

    @property
    def when(self) -> Range:
        return self._when

    @property
    def time(self) -> Time:
        return Time()

    @property
    def settings(self) -> flnd.plugin.Settings:
        """The plugin's `Settings`: its keys of config.toml, over its defaults."""
        return self._settings

    @property
    def cookies(self) -> Mapping[str, str]:
        """The login's cookies with those the run's session was given, on the plugin's domains
        only, read-only."""
        domains = self._plugin.domains
        merged = domain_cookies(self._run.jar, domains)
        merged.update(domain_cookies(self._run.session.cookies, domains))
        return MappingProxyType(merged)

    @property
    def state(self) -> MutableMapping[str, Any]:
        """The plugin's state, in memory: the engine reads and writes flnd.db off the loop."""
        return self._state

    @property
    def secrets(self) -> MutableMapping[str, str]:
        return self._secrets

    @property
    def request(self) -> Request | None:
        """See `flnd.plugin.Ctx.request`."""
        return self._request


def _reads_blocks(plugin: Plugin) -> bool:
    """Whether the plugin has a `blocked` hook of its own, which reads a page's HTML."""
    return type(plugin).blocked is not Plugin.blocked


def _close_tabs(opened: Sequence[tuple[TargetTabs, Any]]) -> None:
    for _, tabs in opened:
        tabs.close()


class _Feed:
    """What `Ctx.feed` returns: a `flnkit.browser.Feed`, made and read on a thread."""

    def __init__(self, ctx: Ctx, match: Any, *, scroll: bool, lines: bool,
                 cursor: Any, resume: bool = False, bucket: str | None = None) -> None:
        self._ctx = ctx
        self._make = functools.partial(self._made, match, scroll, lines, cursor)
        self._feed: browser.Feed | None = None
        # The feed being made on a thread, from `listen` until it is made.
        self._opening: asyncio.Future[Any] | None = None
        self._done = False
        self.headers: Mapping[str, str] = {}
        self._resume = Resume(bucket)
        self._on = resume

    def _made(self, match: Any, scroll: bool, lines: bool, cursor: Any) -> Any:
        tabs = self._ctx._tabs.verbs()
        return browser.Feed(tabs, match, scroll=scroll, lines=lines, cursor=cursor, requests=True)

    def listen(self) -> None:
        """Start making the feed, so it listens from now on; on the loop."""
        if self._feed is None and self._opening is None and not self._done:
            before, ctx = self._ctx._making, self._ctx

            async def make() -> Any:
                if before is not None:
                    await asyncio.wait([before])
                return await ctx._run.off(self._make)

            self._opening = ctx._making = asyncio.ensure_future(make())

    async def _open(self) -> browser.Feed:
        if self._feed is None:
            self.listen()
            opening = self._opening
            assert opening is not None
            try:
                self._feed = await opening
            finally:
                if self._opening is opening:
                    self._opening = None
        return self._feed

    async def __aenter__(self) -> _Feed:
        await self._open()
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        await self.aclose()

    def __aiter__(self) -> _Feed:
        return self

    async def __anext__(self) -> Any:
        if self._done:
            raise StopAsyncIteration
        try:
            while True:
                feed = await self._open()
                try:
                    item = await self._ctx._run.off(next, feed, _DONE)
                except Blocked as error:
                    if not self._on:
                        raise
                    await self._again(error)
                    continue
                if item is _DONE:
                    if getattr(feed, "stalled", False):
                        # The list was cut short, not read to its end: the crawl keeps it
                        # incomplete (`Listing.stalled`).
                        if self._ctx.listing is not None:
                            self._ctx.listing.stalled = True
                        ui.warn("{target}: the list got no new answer in {time}; it ends here",
                                target=str(self._ctx._target),
                                time=text.duration(browser.STALL))
                    await self.aclose()
                    raise StopAsyncIteration
                if self._on and not self._resume.new(item):
                    continue
                raw = getattr(feed, "request", None)
                self._ctx._request = (Request(raw[0], method=raw[1], headers=raw[2],
                                              body=raw[3]) if raw is not None else None)
                self.headers: Mapping[str, str] = getattr(feed, "headers", {})
                return item
        except BaseException:
            await self.aclose()
            raise

    async def _again(self, error: Blocked) -> None:
        """A 429: hold, then listen again on the page the tab was opened to, past what was read."""
        feed, self._feed = self._feed, None
        if feed is not None:
            await self._ctx._run.off(feed.close)
        await self._resume.hold(self._ctx, error)
        self._ctx._reloaded = True
        await self._open()
        if self._ctx._opened:
            await self._ctx.open(self._ctx._opened)

    async def aclose(self) -> None:
        """Stop listening; a second call does nothing."""
        self._done = True
        opening, self._opening = self._opening, None
        if self._feed is None and opening is not None:
            self._feed = await opening
        if self._feed is not None:
            feed, self._feed = self._feed, None
            await self._ctx._run.off(feed.close)


# ---------------------------------------------------------------------------------------------
# A plugin's run: its cookies, session, clients, stores and loop
# ---------------------------------------------------------------------------------------------

class _Screen(browser.Screen):
    """The browser's questions, asked as flnd's others are; its rows are a line each."""

    def confirm(self, question: str) -> bool:
        return forms.confirm(question, default=False)

    def progress(self, label: str, *, total: float | None = None) -> Any:
        ui.warn("{label}", label=label)
        return super().progress(label, total=total)


# The side tabs of the block running in this context (`Ctx.aside`): a lazy post's files, a
# refresh or a `date` opens its own tab, never the listing's. None outside such a block.
_ASIDE: ContextVar[list[tuple[TargetTabs, Any]] | None] = \
    ContextVar("flnd_aside", default=None)
# Releases the target's turn (`flnd.engine.crawl.Turn`) once its route meets the site; None
# outside a route's call.
met_turn: ContextVar[Callable[[], None] | None] = ContextVar("flnd_turn", default=None)


def _met() -> None:
    """The route running here met the site: its target's turn passes."""
    release = met_turn.get()
    if release is not None:
        release()


class Bridge:
    """The run's loop, as code on a `CALL` thread reaches it.

    In a run it is the run's own loop. Outside one (`flnd auth login`), it starts a loop of its own
    on a thread the first time it is used, and `close` ends it.

    Args:
        loop: the run's loop; None for one of its own.
    """

    def __init__(self, loop: asyncio.AbstractEventLoop | None = None) -> None:
        self._loop = loop
        self._own = loop is None
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()

    @property
    def loop(self) -> asyncio.AbstractEventLoop:
        """The asyncio loop; one of its own is started on first use."""
        with self._lock:
            if self._loop is None:
                self._loop = asyncio.new_event_loop()
                self._thread = threading.Thread(target=self._loop.run_forever,
                                                name="flnd-bridge", daemon=True)
                self._thread.start()
            return self._loop

    def run(self, awaitable: Awaitable[T]) -> T:
        """Run `awaitable` on the loop from this thread, and return its value.

        Raises:
            RuntimeError: called on the loop's own thread.
        """
        return browser.run_on(self.loop, awaitable)

    def close(self) -> None:
        """End a loop of its own, cancelling what still runs on it; nothing for the run's."""
        with self._lock:
            loop, thread, self._thread = self._loop, self._thread, None
        if not self._own or loop is None or thread is None:
            return
        browser.run_on(loop, _cancel_others())
        loop.call_soon_threadsafe(loop.stop)
        thread.join()
        loop.close()


async def _cancel_others() -> None:
    current = asyncio.current_task()
    pending = [task for task in asyncio.all_tasks() if task is not current]
    for task in pending:
        task.cancel()
    await asyncio.gather(*pending, return_exceptions=True)
    await asyncio.get_running_loop().shutdown_asyncgens()


@dataclass
class PluginRun:
    """One plugin's resources for a run: cookies, HTTP clients, stores, loop and scheduler."""

    # The plugin, as one instance for the whole run.
    source: Plugin
    # What the run shares across plugins: the home, its database, the stop flag.
    settings: RunShared
    # The login's cookies.
    jar: cookies.Jar
    # What sends: curl on the run's loop, with the cookies the site sets kept beside the login's.
    session: net.Session
    # The listing's requests: stopped by the first Ctrl-C.
    web: net.Client
    # The files' downloads, over the same session and limits: stopped by the second Ctrl-C.
    files: net.Client
    # The plugin's `[plugins.<code>]` settings in config.toml: pace, limits, proxy, stop_after,
    # its templates and its own `Settings`, each over what its class says.
    config: PluginConfig = field(default_factory=PluginConfig)
    # The posts its `date` hook had no day for, read once for the run (`crawl.asked`).
    asked: dict[str, float] | None = None
    _state: store.Store | None = field(default=None, repr=False)
    _secrets: MutableMapping[str, str] | None = field(default=None, repr=False)
    # A loop of the plugin's own, made only outside a run (`shared_browser` gives the run's).
    _own_loop: Bridge | None = field(default=None, repr=False)
    # A scheduler of the plugin's own, made only outside a run, on its own loop.
    _own_jobs: jobs.Scheduler | None = field(default=None, repr=False)
    _closed: bool = field(default=False, repr=False)
    # Whether a target already said that the browser doesn't use the config's proxy.
    _told_proxy: bool = field(default=False, repr=False)
    # The plugin's state for the run, read from flnd.db once, off the loop (`load_state`); the
    # keys changed since the last `save_state`; and a lock, as the loop and the engine's
    # threads both reach them.
    _values: dict[str, Any] | None = field(default=None, repr=False)
    _changed: set[str] = field(default_factory=set, repr=False)
    _guard: threading.Lock = field(default_factory=threading.Lock, repr=False)

    @property
    def loop(self) -> Bridge:
        """The run's loop, where the browser and async code run; the plugin's own outside a
        run."""
        shared = self.settings.browser_pool
        if shared is not None:
            return Bridge(shared.loop)
        if self._own_loop is None:
            self._own_loop = Bridge()
        return self._own_loop

    @property
    def scheduler(self) -> jobs.Scheduler:
        """The run's scheduler, which every request and file waits for; the plugin's own
        outside a run."""
        shared = scheduler(self.settings)
        if shared is not None:
            return shared
        if self._own_jobs is None:
            self._own_jobs = jobs.Scheduler(stop=self.settings.stop)
        return self._own_jobs

    @property
    def jobs(self) -> int:
        """The most files of one target downloading at once (`jobs.Limits.places`). A target's
        queue holds 4 times as many before its listing waits."""
        return self.scheduler.limits.places

    @property
    def state(self) -> store.State:
        """The plugin's own values in the home's `flnd.db`: the run's, or one opened here."""
        if self.settings.db is not None:
            return self.settings.db.state(self.source.code)
        if self._state is None:
            self._state = store.open(self.settings.home)
        return self._state.state(self.source.code)

    @property
    def values(self) -> MutableMapping[str, Any]:
        """The plugin's state as the run reads and writes it: in memory, read from flnd.db
        once (`load_state`) and written back by `save_state`, both off the loop."""
        return _Values(self)

    def load_state(self) -> None:
        """Read the plugin's state from flnd.db, once; off the loop."""
        with self._guard:
            if self._values is None:
                self._values = dict(self.state.items())

    def save_state(self) -> None:
        """Write the values changed since the last save back to flnd.db; off the loop."""
        with self._guard:
            if self._values is None or not self._changed:
                return
            changed, self._changed = self._changed, set()
            kept = {key: self._values[key] for key in changed if key in self._values}
        stored = self.state
        for key in sorted(changed):
            if key in kept:
                stored[key] = kept[key]
            else:
                stored.pop(key, None)

    @property
    def secrets(self) -> MutableMapping[str, str]:
        """The plugin's saved secrets (`logins/<code>.json`), read on first use."""
        if self._secrets is None:
            self._secrets = auth.secrets(self.source.code)
        return self._secrets

    async def call(self, fn: Callable[..., T], *args: Any) -> T:
        """Run the plugin's blocking `fn(*args)` (`ctx.call`) on a `CALL` thread, as a step of
        the run (`jobs.Scheduler.step`): it waits for the plugin's holds, and is never tried
        again."""
        return await self.scheduler.step(self.source.code, fn, *args)

    async def off(self, fn: Callable[..., T], *args: Any) -> T:
        """Run the engine's own blocking step, `fn(*args)`, on a `CALL` thread, with no job.

        For what must run even while the plugin is held or the run is stopping: a browser
        verb, closing a target's tabs, writing back the cookies.
        """
        return await self.scheduler.on(jobs.Kind.CALL, fn, *args)

    async def aclose(self) -> None:
        """Close the session and the stores, on the run's loop; once."""
        if self._closed:
            return
        self._closed = True
        try:
            if self.session.started:
                await self.session.close()
        finally:
            _close(self._state, self._secrets)

    def close(self) -> None:
        """Close the session, stores and the plugin's own loop; once."""
        if self._closed:
            return
        self._closed = True
        try:
            # On the loop the session's transfers ran on; a loop already closed took them.
            try:
                if self.session.started:
                    self.loop.run(self.session.close())
                if self._own_jobs is not None:
                    self.loop.run(self._own_jobs.close())
            except RuntimeError:
                pass
        finally:
            _close(self._state, self._secrets)
            if self._own_loop is not None:
                self._own_loop.close()


def _close(*kept: Any) -> None:
    for each in kept:
        close = getattr(each, "close", None)
        if close is not None:
            close()


class _Values(MutableMapping[str, Any]):
    """A plugin's state in memory (`PluginRun.values`): each change is noted for `save_state`."""

    def __init__(self, run: Any) -> None:
        self._run = run

    def _data(self) -> dict[str, Any]:
        if self._run._values is None:
            self._run.load_state()
        return self._run._values

    def __getitem__(self, key: str) -> Any:
        data = self._data()
        with self._run._guard:
            return data[key]

    def __setitem__(self, key: str, value: Any) -> None:
        data = self._data()
        with self._run._guard:
            data[key] = value
            self._run._changed.add(key)

    def __delitem__(self, key: str) -> None:
        data = self._data()
        with self._run._guard:
            del data[key]
            self._run._changed.add(key)

    def __iter__(self) -> Iterator[str]:
        data = self._data()
        with self._run._guard:
            return iter(list(data))

    def __len__(self) -> int:
        data = self._data()
        with self._run._guard:
            return len(data)

@contextlib.contextmanager
def plugin_run(settings: RunShared, plugin: Plugin, *, jar: cookies.Jar | None = None,
               secrets: MutableMapping[str, str] | None = None) -> Iterator[PluginRun]:
    """Open one plugin's resources outside a run (a login), and close them however the block
    ends.

    The plugin's cookies are read from the logins folder once, here, unless `jar` is given, and
    a cookie of the login that has expired or expires within `EXPIRY_WARNING` is a warning
    line. The plugin's `[plugins.<code>]` settings in config.toml are read here too, and give
    the scheduler its pace and its hosts' limits, the session its proxy, and the instance its
    `folder` and `filename` templates. Nothing is sent yet.

    Args:
        settings: what the run shares.
        plugin: the plugin.
        jar: the cookies to use instead of the saved ones (a login's, being checked).
        secrets: what `ctx.secrets` keeps instead of the saved ones (a login's, written once
            it is checked).

    Raises:
        UsageError: the home's config.toml has a value it doesn't take.
    """
    configured, kept = _read(settings, plugin)
    run = _made(settings, plugin, configured, kept if jar is None else jar)
    run._secrets = secrets
    try:
        yield run
    finally:
        run.close()


@contextlib.asynccontextmanager
async def opened(settings: RunShared, plugin: Plugin) -> AsyncIterator[PluginRun]:
    """One plugin's resources in a run, on its loop, closed on the loop.

    Raises:
        UsageError: the home's config.toml has a value it doesn't take.
        LoginError: the login file can't be read.
    """
    # The home's config.toml and the plugin's cookies are read off the loop.
    run = _made(settings, plugin, *await asyncio.to_thread(_read, settings, plugin))
    try:
        yield run
    finally:
        await run.aclose()


def _read(settings: RunShared, plugin: Plugin) -> tuple[PluginConfig, cookies.Jar]:
    """A plugin's `[plugins.<code>]` settings (the run's, or read here) and its saved cookies."""
    found = getattr(settings, "config", None) or config.settings(
        settings.home, plugins={plugin.code: type(plugin)})
    return found.plugin(plugin.code), auth.load(plugin.code)


def _made(settings: RunShared, plugin: Plugin, configured: PluginConfig,
          jar: cookies.Jar) -> PluginRun:
    """A plugin's `PluginRun`: its login's cookies warned of, its settings
    applied. A configured template replaces its class's on this instance; a category's or a
    route's own still wins (`crawl._format`)."""
    _warn_expiring(plugin, jar)
    for name in ("folder", "filename"):
        if getattr(configured, name) is not None:
            setattr(plugin, name, getattr(configured, name))
    stop: Stop | None = getattr(settings, "stop", None)
    # Outside a run, the plugin has a scheduler of its own.
    own = jobs.Scheduler(stop=stop) if scheduler(settings) is None else None
    used = scheduler(settings) or own
    assert used is not None
    used.pace[plugin.code] = plugin.pace if configured.pace is None else configured.pace
    used.limits.limits.update({**plugin.limits, **configured.limits})
    session = _network(plugin, jar, configured, handles=used.limits.handles)
    return PluginRun(plugin, settings, jar, session,
                     net.Client(session, stop=stop.soon if stop else None),
                     net.Client(session, stop=stop.now if stop else None), configured,
                     _own_jobs=own)


def _sign(plugin: Plugin, url: str, method: str, headers: MutableMapping[str, str],
          body: bytes | str | None, cookies: Any) -> None:
    """Apply `plugin.user_agent` and `plugin.sign` to `headers` for `plugin.domains`."""
    if not _on(net.URL(url).host or "", plugin.domains):
        return
    if plugin.user_agent:
        headers["User-Agent"] = plugin.user_agent
    asked = Request(url, method=method, headers=dict(headers), body=body)
    given = plugin.sign(asked, MappingProxyType(domain_cookies(cookies, plugin.domains)))
    if not isinstance(given, Request):
        # The type alone: what `sign` gave may hold a token.
        raise PluginError(f"{plugin.code}.sign: return the request, not a "
                          f"{type(given).__name__}")
    for name, value in (given.headers or {}).items():
        headers[name] = value


def _network(plugin: Plugin, jar: Iterable[Mapping[str, Any]],
             settings: PluginConfig, *, handles: int) -> net.Session:
    """A plugin's session: its `sign` hook and `user_agent` on requests to its own domains
    only, and the config's proxy.

    Args:
        plugin: the plugin; its `user_agent` and `sign` are read.
        jar: the login's cookies.
        settings: the plugin's config.toml settings, whose `proxy` is used.
        handles: transfers at once in all (`jobs.Limits.handles`).
    """

    def prepare(request: net.Request) -> net.Request | None:
        # Runs on the loop before each try. The plugin sees its API's own request, never the
        # HTTP client's type; what it sets in `headers` is copied back.
        _sign(plugin, str(request.url), request.method, request.headers, request.content,
              session.cookies)
        return None

    session = net.Session(cookies=jar, prepare=prepare, handles=handles, proxy=settings.proxy)
    return session


@contextlib.asynccontextmanager
async def shared_browser(settings: RunShared) -> AsyncIterator[None]:
    """Give a run its scheduler and its one browser, on the run's loop; then close both.

    Entered again inside a run, it changes nothing. The browser launches only when a target
    first asks for a tab. On the way out the browser is closed before the scheduler, even when
    closing it fails.

    Args:
        settings: the run's; `browser_pool` holds the `flnkit.browser.RunBrowser` while it runs.
    """
    if settings.browser_pool is not None:
        yield
        return
    found = getattr(settings, "config", None) or await asyncio.to_thread(
        config.settings, settings.home)
    shared = browser.RunBrowser(asyncio.get_running_loop(), screen=_Screen())
    made = _SCHEDULERS[shared] = jobs.Scheduler(
        stop=getattr(settings, "stop", None), rate=config.speed(found.defaults.rate),
        slowest=config.speed(found.defaults.slowest), tally=getattr(settings, "tally", None))
    settings.browser_pool = shared
    try:
        yield
    finally:
        settings.browser_pool = None
        timer = _IDLE_TIMERS.pop(shared, None)
        if timer is not None:
            timer.cancel()
        _ACTIVE_TABS.pop(shared, None)
        try:
            await shared.aclose()
        finally:
            await made.close()


# Each run's scheduler, by the run's browser, so every `RunShared` given that browser shares it.
_SCHEDULERS: weakref.WeakKeyDictionary[browser.RunBrowser, jobs.Scheduler] = \
    weakref.WeakKeyDictionary()
IDLE_TIMEOUT = 20.0
jobs.TABS, jobs.WARM_FREE = 6, 2
_ACTIVE_TABS: weakref.WeakKeyDictionary[browser.RunBrowser, set[TargetTabs]] = \
    weakref.WeakKeyDictionary()
_IDLE_TIMERS: weakref.WeakKeyDictionary[browser.RunBrowser, asyncio.TimerHandle] = \
    weakref.WeakKeyDictionary()


def _release_active(shared: browser.RunBrowser, target_tabs: TargetTabs) -> None:
    active = _ACTIVE_TABS.get(shared)
    if active is not None:
        active.discard(target_tabs)
        if not active and shared.launched:
            _schedule_idle_close(shared)


def _close_idle(shared: browser.RunBrowser) -> None:
    active = _ACTIVE_TABS.get(shared)
    if not active and shared.launched:
        shared.close()


def _schedule_idle_close(shared: browser.RunBrowser, timeout: float = IDLE_TIMEOUT) -> None:
    timer = _IDLE_TIMERS.pop(shared, None)
    if timer is not None:
        timer.cancel()

    def on_timeout() -> None:
        _IDLE_TIMERS.pop(shared, None)
        _close_idle(shared)

    try:
        loop = shared.loop
        if loop.is_running():
            _IDLE_TIMERS[shared] = loop.call_later(timeout, on_timeout)
    except Exception:
        pass


def scheduler(settings: RunShared) -> jobs.Scheduler | None:
    """The scheduler of the run `settings` belong to; None outside a run."""
    pool = settings.browser_pool
    return _SCHEDULERS.get(pool) if pool is not None else None


def _warn_expiring(plugin: Plugin, jar: Any) -> None:
    """Warn of login cookies (the login's `names`) that have expired or will expire soon."""
    for name, left in auth.expiring(plugin, jar, within=EXPIRY_WARNING):
        if left <= 0:
            ui.warn("{code}: the login cookie {name} has expired; run flnd auth login {code} again",
                    code=plugin.code, name=name)
        else:
            ui.warn("{code}: the login cookie {name} expires in {time}; "
                    "run flnd auth login {code} again soon",
                    code=plugin.code, name=name, time=text.duration(left))


# ---------------------------------------------------------------------------------------------
# A target's tabs
# ---------------------------------------------------------------------------------------------

class TargetTabs:
    """A target's share of the run's browser, which `Ctx`'s browser verbs drive on a thread.

    Made on first use; outside a run, in a browser of its own, which `close` closes.

    Args:
        run: the plugin's resources for the run.
    """

    def __init__(self, run: PluginRun) -> None:
        self._run = run
        self._source = run.source
        # The run's browser; outside a run, one of its own, made on first use.
        self._shared: browser.RunBrowser | None = run.settings.browser_pool
        self._own = False
        self._tabs: browser.Tabs | None = None

    def verbs(self) -> browser.Tabs:
        """The tabs a browser verb acts in: the block's own in `Ctx.aside`, else the
        target's; on a thread."""
        _met()
        aside = _ASIDE.get()
        if aside is None:
            return self._browser()
        tabs = next((tabs for owner, tabs in aside if owner is self), None)
        if tabs is None:
            tabs = self._made()
            aside.append((self, tabs))
        return tabs

    def _browser(self) -> browser.Tabs:
        """The target's tabs, made on first use."""
        if self._tabs is None:
            self._tabs = self._made()
        return self._tabs

    def _made(self) -> browser.Tabs:
        """A share of the run's browser for this target, which scrolls like a person."""
        if self._shared is None:
            self._shared = browser.RunBrowser(self._run.loop.loop, stealth=self._source.stealth,
                                              screen=_Screen())
            self._own = True
        else:
            active = _ACTIVE_TABS.setdefault(self._shared, set())
            active.add(self)
            timer = _IDLE_TIMERS.pop(self._shared, None)
            if timer is not None:
                timer.cancel()
        settings = self._run.config
        if settings.proxy is not None and not self._run._told_proxy:
            self._run._told_proxy = True
            # The browser is one window every plugin of the run shares, so it can't take one
            # plugin's proxy: say so, rather than let page loads leave unproxied silently.
            ui.warn("{code}: the browser doesn't use the proxy in config.toml; only the "
                    "plugin's HTTP requests do", code=self._source.code)
        budget = jobs.Budget(self._run.scheduler, self._source.code)
        return browser.Tabs(self._shared, self._run.jar, scroll=browser.HUMAN, budget=budget,
                            assets=self._source.assets, hidden=self._source.hidden)

    @property
    def opened(self) -> bool:
        """Whether the target's own tabs were made."""
        return self._tabs is not None

    def cookies(self) -> list[Mapping[str, Any]]:
        """Its browser's cookies, or none when this target never opened a tab."""
        if self._tabs is None or self._shared is None or not self._shared.launched:
            return []
        return list(self._tabs.run(self._shared.cookies(self._run.jar)))

    def document(self) -> Document:
        """The tab's page now, parsed on this thread, not on the loop."""
        from flnkit.html import parse

        tabs = self.verbs()
        return parse(tabs.run(tabs.page.content()))

    def click(self, css: str, *, timeout: float = 10.0) -> None:
        tabs = self.verbs()
        tabs.run(tabs.page.click(css, timeout=timeout))

    def handoff(self, *, until: Callable[[], bool], message: str,
                timeout: float | None = None) -> bool:
        return self.verbs().handoff(until=until, message=message, timeout=timeout,
                                    stop=self._run.web.stop)

    def warm(self, urls: Iterable[str]) -> None:
        """Start loading `urls` in warm tabs; an empty URL opens none."""
        self.verbs().warm([url for url in urls if url])

    @property
    def idle(self) -> bool:
        """Whether it never opened a tab, so `close` has nothing to do."""
        return self._tabs is None and not self._own

    def close(self) -> None:
        """Give back the target's tabs, handing the session the cookies set in them."""
        tabs, found = self._tabs, self.cookies()
        self._tabs = None
        shared = self._shared
        try:
            if found:
                self._run.session.add(found)
            if tabs is not None:
                tabs.close()
        finally:
            if self._own and self._shared is not None:
                self._shared.close()
                self._shared, self._own = None, False
            elif shared is not None:
                _release_active(shared, self)


# ---------------------------------------------------------------------------------------------
# Paged listings
# ---------------------------------------------------------------------------------------------

class Listing:
    """One category's listing in a run: where `pages` starts, and the cursor of the page it
    reads.

    The crawl makes one per category it lists and sets it as `Ctx.listing`; the first `pages`
    call of the category starts from `start` and tells it the cursor of each page it reads,
    which the crawl saves when the listing is cut short. Later `pages` calls of the same
    category start from their beginning.

    Args:
        start: the cursor an interrupted run saved; None starts from the newest.
        label: names the listing in the line that says a saved cursor was refused.

    Example:
        >>> listing = Listing("c42", label="exm:example_user: posts")
        >>> listing.cursor, listing.taken
        ('c42', False)
    """

    def __init__(self, start: str | None = None, *, label: str = "",
                 head: str | None = None, sync: bool = False) -> None:
        self.start = start
        self.label = label
        self.head = head
        self.sync = sync
        # Files queued while reading this listing (`crawl._queue_listed`).
        self.queued = 0
        # The cursor of the page being read, for the crawl to save if the listing is cut short.
        self.cursor: str | None = start
        # Whether a `pages` call took this listing already; only the first one resumes.
        self.taken = False
        # Whether the site refused `start`, so the listing began again from the newest.
        self.refused = False
        # The pages `pages` has read for it so far, for the live stats.
        self.read = 0
        # Whether a feed of it ended on the stall guard: the list was cut short, not complete.
        self.stalled = False
        # Whether a discovering query stopped early because it caught up to its head/seen.
        self.caught_up = False


class Resume:
    """What a resuming feed keeps: the bodies read, and whether any came since the last hold."""

    def __init__(self, bucket: str | None) -> None:
        self.bucket = bucket
        self.read: set[int] = set()
        self.came = False
        self.held = False

    def new(self, item: Any) -> bool:
        """Whether `item` is for the plugin: one read before a hold is not given again."""
        key = hash(json.dumps(item, sort_keys=True, default=str))
        if key in self.read:
            return not self.held
        self.read.add(key)
        self.came = True
        return True

    async def hold(self, ctx: Any, error: Blocked) -> None:
        """Hold through `ctx.hold`: the site's wait, else the engine's; `reset` after progress.

        Raises:
            Blocked: past the engine's last round.
        """
        await ctx.hold("rate limited", seconds=error.wait, bucket=self.bucket,
                       reset=self.came)
        self.came, self.held = False, True


def ask_again(ctx: Any, ask: Callable[[Any], Awaitable[Any]], bucket: str | None,
              came: list[bool]) -> Callable[[Any], Awaitable[Any]]:
    """`ask`, holding through `ctx.hold` on a `Blocked` and asking the same cursor again, once
    a page was read since the last hold (`came[0]`); else the `Blocked` ends it."""
    async def asking(at: Any) -> Any:
        while True:
            try:
                return await ask(at)
            except Blocked as error:
                # The engine's rounds ran out on this page with none read since the last hold.
                if not came[0]:
                    raise
                await ctx.hold("rate limited", seconds=error.wait, bucket=bucket or "",
                               reset=True)
                came[0] = False
    return asking


async def first_page(ask: Callable[[Any], Awaitable[Any]], listing: Listing | None) -> Any:
    """The first page of `ctx.pages`: from the saved cursor if any, else from the newest.

    A saved cursor the site refuses with a 4xx has gone stale, which is the site's answer for
    good: it is dropped, said once, and the listing starts from the newest. Any other failure
    (a 429 or a block says nothing of the cursor, S6) is raised.
    """
    start = listing.start if listing is not None else None
    if not start:
        return await ask(None)
    try:
        return await ask(start)
    except SiteError as error:
        if (isinstance(error, Blocked) or error.status is None
                or not 400 <= error.status < 500 or error.status == 429):
            raise
    assert listing is not None
    ui.info("{label}: the cursor an interrupted run saved was refused; listing from the newest",
            label=listing.label)
    listing.refused, listing.cursor = True, None
    return await ask(None)
