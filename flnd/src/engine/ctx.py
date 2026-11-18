"""The engine's `Ctx`: what a plugin on API 2 acts through, for one target of a run.

src/docs/architecture explains it; `flnd.testing.FakeCtx` answers the same way from fakes.
"""

from __future__ import annotations

import contextlib
import datetime
import functools
import json
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
from types import MappingProxyType
from typing import TYPE_CHECKING, Any, TypeVar
from urllib.parse import urlsplit

import flnd.plugin
from flnd.engine import jobs, site
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
    from flnkit.html import Document

__all__ = ["LOGGING_IN", "PAGE_LIMIT", "Ctx", "JsonState", "Secrets", "Time", "checked",
           "empty", "granted", "native", "refused", "start", "walled", "writable"]

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


def native(source: object) -> bool:
    """Whether `source` (a class or an instance) is a plugin on API 2, which the engine runs
    natively; anything else is an API-1 source, run through `flnd.engine.site`'s bridge."""
    cls = source if isinstance(source, type) else type(source)
    return issubclass(cls, Plugin)


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
    if request.method.upper() in site.SAFE or isinstance(error, (Blocked, LoginError)):
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
    """An account's secrets: read any time, written only while `Login.start` runs (`start`).

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
    """The engine's `flnd.plugin.Ctx` for one target (or a plugin's account) in a run.

    Each request is a scheduler job: a page job on the plugin's domains, a file-pooled one
    elsewhere, retried and held by the scheduler's policy. The browser's verbs go through the
    target's `flnd.engine.site.TargetSite`, one call on a thread each, as API 1's do.

    Args:
        run: the plugin's resources for the run.
        target: the target it acts for; `<code>:` for the plugin's account.
        when: the run's `when`.
        login: for `flnd login`: `ask` asks in the terminal.
    """

    def __init__(self, run: site.SourceRun, target: Target, *, when: Range = Range(),
                 login: bool = False) -> None:
        self._run = run
        self._plugin: Plugin = run.source
        self._target = target
        self._when = when
        self._login = login
        self._site = site.TargetSite(run, target)
        self._settings = type(self._plugin).Settings()
        self._state = JsonState(run.state)
        self._secrets = Secrets(lambda: run.secrets)
        # The category's listing, which `pages` resumes and reports to; the crawl sets it.
        self.listing: site.Listing | None = None

    # -- what the crawl reads -------------------------------------------------------------

    @property
    def _idle(self) -> bool:
        """Whether it never opened a tab, so `close` has nothing to do."""
        return self._site._idle

    def close(self) -> None:
        """Give back the target's tabs; on a thread, as `TargetSite.close`."""
        self._site.close()

    @contextlib.asynccontextmanager
    async def aside(self) -> AsyncIterator[None]:
        """Tabs of its own for the block: a lazy post's files, `refresh` and `date` open their
        own, never the listing's, and they close when it ends."""
        opened: list[tuple[site.TargetSite, browser.Tabs]] = []
        token = site._ASIDE.set(opened)
        try:
            yield
        finally:
            site._ASIDE.reset(token)
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
            page = await self._run.off(self._page)
            return capped(await page.fetch(url, method=asked.method, headers=headers,
                                           body=body, check=False))
        if self._site._tabs is not None:
            # Cookies the browser set for the request's host reach the session too, as a
            # logged-in page sends them; another site's never do.
            cookies = host_cookies(await self._run.off(self._site._browser_cookies),
                                   net.URL(url).host)
            if cookies:
                self._run.session.add(cookies)
        return await self._run.web.fetch(url, method=asked.method, headers=headers, body=body,
                                         check=False, limit=PAGE_LIMIT)

    def _job(self, url: str, work: Callable[[], Awaitable[T]], *,
             bucket: str = "") -> Awaitable[T]:
        """`work` as a scheduler job: a `PAGE` job on the plugin's domains, paced in its
        bucket; a `FILE` job on any other host, under its cap, ahead of the target's files."""
        host = net.URL(url).host
        own = on_domains(self._plugin, url)
        job = jobs.Job(kind=jobs.Kind.PAGE if own else jobs.Kind.FILE, work=work,
                       plugin=self._plugin.code, host=host, bucket=bucket if own else "",
                       target=str(self._target),
                       priority=None if own else jobs.PRIORITY[jobs.Kind.PAGE])
        return self._run.scheduler.run(job)

    def pages(self, request: str | Request, *,
              cursor: str | Callable[[Any], str | None],
              send: str | Callable[[str], Mapping[str, Any]] = "cursor",
              bucket: str | None = None, in_page: bool = False) -> AsyncIterator[net.Response]:
        """See `flnd.plugin.Ctx.pages`: the category's first `pages` resumes its listing."""
        first = request if isinstance(request, Request) else Request(request)
        return self._pages(first, cursor, send, bucket, in_page)

    async def _pages(self, first: Request, cursor: Any, send: Any, bucket: str | None,
                     in_page: bool) -> AsyncIterator[net.Response]:
        listing = self.listing if self.listing is not None and not self.listing.taken else None
        if listing is not None:
            listing.taken = True

        async def ask(at: str | None) -> net.Response:
            asked = first
            if at is not None:
                more = send(at) if callable(send) else {send: at}
                asked = first.replace(params={**(first.params or {}), **more})
            return await self.fetch(asked, bucket=bucket, in_page=in_page)

        response = await site.first_page(ask, listing)
        seen: set[str] = set()
        while not empty(response):
            if listing is not None:
                listing.read += 1
            yield response
            following = (cursor(response) if callable(cursor)
                         else flnd.plugin.dig(response.json, cursor))
            if following is None or following == "" or str(following) in seen:
                return
            seen.add(str(following))
            if listing is not None:
                # The cursor of the page being read: a run cut short reads it again.
                listing.cursor = str(following)
            response = await ask(str(following))

    # -- the tab --------------------------------------------------------------------------

    def _page(self) -> browser.Page:
        """The tab the target works in now (a lazy post's own one in `aside`), on a thread."""
        return self._site._verbs().page

    async def open(self, url: str) -> Landed:
        """See `flnd.plugin.Ctx.open`: a warm tab loading `url` becomes the target's."""
        plugin = self._plugin
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
        tabs = self._site._verbs()
        warmed, first = tabs.take_warm(url)
        return tabs.page, first if warmed else None

    def feed(self, match: str | Any | Callable[[str], bool], *, scroll: bool = True,
             lines: bool = False,
             cursor: Callable[[Any], tuple[bool, str | None] | None] | None = None
             ) -> _Feed:
        """See `flnd.plugin.Ctx.feed`; it listens from `async with`, or its first item."""
        return _Feed(self, match, scroll=scroll, lines=lines, cursor=cursor)

    def load(self, url: str, match: str | Any | Callable[[str], bool], *,
             scroll: bool = True, lines: bool = False,
             cursor: Callable[[Any], tuple[bool, str | None] | None] | None = None
             ) -> AsyncIterator[Any]:
        """See `flnd.plugin.Ctx.load`."""
        return self._load(url, self.feed(match, scroll=scroll, lines=lines, cursor=cursor))

    async def _load(self, url: str, feed: _Feed) -> AsyncIterator[Any]:
        async with feed:
            await self.open(url)
            yield flnd.plugin.meta(await self.document())
            async for item in feed:
                yield item

    async def document(self) -> Document:
        """See `flnd.plugin.Ctx.document`."""
        return await self._run.off(self._site.document)

    async def click(self, css: str, *,
                    timeout: float = 10.0) -> None:  # noqa: ASYNC109 (the tab job's own limit)
        """See `flnd.plugin.Ctx.click`."""
        await self._run.off(functools.partial(self._site.click, css, timeout=timeout))

    async def handoff(self, message: str, *, until: Callable[[], Awaitable[bool]],
                      timeout: float | None = None) -> bool:  # noqa: ASYNC109 (a person's)
        """See `flnd.plugin.Ctx.handoff`."""
        loop = self._run.loop.loop

        def done() -> bool:
            return bool(browser.run_on(loop, until()))

        return await self._run.off(functools.partial(
            self._site.handoff, until=done, message=message, timeout=timeout))

    async def warm(self, urls: Sequence[str]) -> None:
        """Start loading `urls` in warm tabs, while the run's tab budget has room.

        For pages a listing opens next that no category's `url=` names (an album's pages, a
        profile's story buckets); an empty URL opens nothing.
        """
        await self._run.off(self._site.warm, list(urls))

    async def extension(self, op: str, **args: Any) -> Any:
        """See `flnd.plugin.Ctx.extension`: the relay to the extension isn't wired in yet.

        Raises:
            UsageError: always, for now: the extension isn't connected.
        """
        raise UsageError(f"{self._plugin.code}: the extension isn't connected",
                         hint="flnd extension install")

    async def storage(self, origin: str) -> Mapping[str, str]:
        """See `flnd.plugin.Ctx.storage`: read in a hidden copy of Chrome's default profile."""
        found = await self._run.off(functools.partial(browser.local_storage, origin,
                                                      profile="Default"))
        return MappingProxyType(dict(found or {}))

    # -- waits, threads, people -----------------------------------------------------------

    async def pause(self, seconds: float, reason: str) -> None:
        """See `flnd.plugin.Ctx.pause`.

        Raises:
            Stopped: the run's first Ctrl-C came while waiting.
        """
        if seconds <= 0:
            return
        tally = self._run.settings.tally
        held = (tally.hold(self._plugin.code, None, "pages", seconds, reason)
                if tally is not None else contextlib.nullcontext())
        with held:
            if not await clock.pause(seconds, stop=self._run.web.stop):
                raise Stopped(f"{self._target}: stopped while waiting: {reason}")

    async def hold(self, reason: str, *, seconds: float | None = None,
                   bucket: str | None = None) -> None:
        """See `flnd.plugin.Ctx.hold`: a block round, which the plugin's next job waits out.

        Raises:
            Blocked: past the last round.
        """
        code = self._plugin.code
        try:
            held = self._run.scheduler.hold(code, None, seconds=seconds or 0.0, reason=reason,
                                            bucket=bucket)
        except Blocked as error:
            raise Blocked(f"{code}: {reason}, after {len(jobs.ROUND_WAITS)} holds",
                          hint="try again later, or raise the plugin's pace in config.toml",
                          target=str(self._target)) from error
        ui.warn("{target}: {reason}; waiting {time}", target=str(self._target), reason=reason,
                time=text.duration(round(held)))

    async def call(self, fn: Callable[..., T], *args: Any) -> T:
        """See `flnd.plugin.Ctx.call`: a `CALL` job, on the thread pool."""
        return await self._run.call(fn, *args)

    async def ask(self, message: str, *, secret: bool = False, description: str = "",
                  choices: Sequence[str] = ()) -> str:
        """See `flnd.plugin.Ctx.ask`: only `flnd login` asks, in a terminal.

        Raises:
            UsageError: a run, or no terminal: nobody can answer.
        """
        code = self._plugin.code
        if not (self._login and self._run.settings.interactive and forms.interactive()):
            raise UsageError(f"{code}: nobody can answer {message!r} here",
                             hint=f"run flnd login {code} in a terminal")
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
        """The plugin's `Settings`: its defaults; config.toml's aren't read yet."""
        return self._settings

    @property
    def account(self) -> flnd.plugin.Account | None:
        """None: accounts aren't rows of their own yet."""
        return None

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
        return self._state

    @property
    def secrets(self) -> MutableMapping[str, str]:
        return self._secrets


def _reads_blocks(plugin: Plugin) -> bool:
    """Whether the plugin has a `blocked` hook of its own, which reads a page's HTML."""
    return type(plugin).blocked is not Plugin.blocked


def _close_tabs(opened: Sequence[tuple[site.TargetSite, browser.Tabs]]) -> None:
    for _, tabs in opened:
        tabs.close()


class _Feed:
    """What `Ctx.feed` returns: a `flnkit.browser.Feed`, made and read on a thread."""

    def __init__(self, ctx: Ctx, match: Any, *, scroll: bool, lines: bool,
                 cursor: Any) -> None:
        self._ctx = ctx
        self._make = functools.partial(self._made, match, scroll, lines, cursor)
        self._feed: browser.Feed | None = None
        self._done = False

    def _made(self, match: Any, scroll: bool, lines: bool, cursor: Any) -> browser.Feed:
        return browser.Feed(self._ctx._site._verbs(), match, scroll=scroll, lines=lines,
                            cursor=cursor)

    async def _open(self) -> browser.Feed:
        if self._feed is None:
            self._feed = await self._ctx._run.off(self._make)
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
        feed = await self._open()
        item = await self._ctx._run.off(next, feed, _DONE)
        if item is _DONE:
            await self.aclose()
            raise StopAsyncIteration
        return item

    async def aclose(self) -> None:
        """Stop listening; a second call does nothing."""
        self._done = True
        if self._feed is not None:
            feed, self._feed = self._feed, None
            await self._ctx._run.off(feed.close)
