"""API 1's temporary bridge: a sync source runs on a `CALL` thread, each `site.<verb>` a job.

src/docs/architecture explains it; it goes with plugin API 1.
"""

from __future__ import annotations

import asyncio
import contextvars
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
)
from contextlib import asynccontextmanager, contextmanager, nullcontext
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import (
    TYPE_CHECKING,
    Any,
    TypeVar,
)

from flnd.engine import home, jobs
from flnd.engine.options import SourceConfig, config
from flnd.errors import Blocked, Error, LoginError, SiteError, SourceError, Stopped, UsageError
from flnd.source import Range, Request, Source, Target, dig
from flnd.ui import forms
from flnd.ui.log import ui
from flnkit import browser, clock, net, text, vault

if TYPE_CHECKING:
    from flnd.engine.options import RunShared
    from flnd.engine.run import Stop
    from flnkit import cookies
    from flnkit.html import Document

__all__ = ["Bridge", "Listing", "LoginSite", "SourceRun", "TargetSite", "login_names", "met_turn",
           "opened", "paged", "scheduler", "shared_browser", "source_run"]

T = TypeVar("T")
# How soon before a login cookie expires a run warns of it, in seconds: a day, so a scheduled
# `flnd sync` warns the run before the one that meets the login wall.
EXPIRY_WARNING = 24 * 3600
# What a site's own domains answer for a login wall.
LOGIN_WALLS = frozenset({401, 403})
# Methods a server may receive twice without harm: only their failures are tried again.
SAFE = frozenset({"GET", "HEAD", "OPTIONS", "PUT", "DELETE"})
# A site's own wait in `back_off` is capped at an hour, as a 429's `Retry-After` is.
LONGEST_WAIT = jobs.MAX_WAIT


class _Screen(browser.Screen):
    """The browser's questions, asked as flnd's others are; its rows are a line each.

    A run's screen shows only what the run's snapshot holds, so a browser step that would draw
    a bar (Chromium's download, a wait for a person) warns its label instead: a run shows
    warnings, and the download takes minutes.
    """

    def confirm(self, question: str) -> bool:
        return forms.confirm(question, default=False)

    def progress(self, label: str, *, total: float | None = None) -> Any:
        ui.warn("{label}", label=label)
        return super().progress(label, total=total)


# The side tabs of the call running on this thread (`SourceRun.aside`): a lazy post's files,
# a refresh or a `date` opens its own tab, never the listing's. None outside such a call.
_ASIDE: contextvars.ContextVar[list[tuple[TargetSite, browser.Tabs]] | None] = \
    contextvars.ContextVar("flnd_aside", default=None)


# Releases the target's turn (`flnd.engine.crawl.Turn`) once its route meets the site; None
# outside a route's call.
met_turn: contextvars.ContextVar[Callable[[], None] | None] = \
    contextvars.ContextVar("flnd_turn", default=None)


def _met() -> None:
    """The route running on this thread met the site: its target's turn passes."""
    release = met_turn.get()
    if release is not None:
        release()


class Bridge:
    """The run's loop, as a sync source's code on a `CALL` thread reaches it.

    In a run it is the run's own loop. Outside one (`flnd login`, `fl.site()`), it starts a loop
    of its own on a thread the first time it is used, and `close` ends it.

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

    @property
    def here(self) -> bool:
        """Whether the calling code runs on the loop, where it must await, not `run`."""
        try:
            return self._loop is not None and asyncio.get_running_loop() is self._loop
        except RuntimeError:
            return False

    def run(self, awaitable: Awaitable[T]) -> T:
        """Run `awaitable` on the loop from this thread, and return its value.

        Raises:
            RuntimeError: called on the loop's own thread.
        """
        _met()
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


def _aside(fn: Callable[..., T], *args: Any) -> T:
    """`fn(*args)` on this thread, with the tabs it opens its own, closed when it ends."""
    opened: list[tuple[TargetSite, browser.Tabs]] = []
    token = _ASIDE.set(opened)
    try:
        return fn(*args)
    finally:
        _ASIDE.reset(token)
        for _, tabs in opened:
            tabs.close()


@dataclass
class SourceRun:
    """One source's resources for a run: cookies, HTTP clients, `connect` client, stores, loop."""

    # The source, as one instance for the whole run.
    source: Source
    # What the run shares across sources: the home, its database, the stop flag.
    settings: RunShared
    # The login's cookies.
    jar: cookies.Jar
    # What sends: curl on the run's loop, with the cookies the site sets kept beside the login's.
    session: net.Session
    # The listing's requests: stopped by the first Ctrl-C.
    web: net.Client
    # The files' downloads, over the same session and limits: stopped by the second Ctrl-C.
    files: net.Client
    # The source's `[sources.<code>]` settings in the home's config.toml: pace, scroll, limits,
    # proxy and stop_after, each over what the source's class says.
    config: SourceConfig = field(default_factory=SourceConfig)
    # What the source's `connect` gave, entered once for the run.
    client: Any = None
    # The posts its `date` hook had no day for, read once for the run (`crawl.asked`).
    asked: dict[str, float] | None = None
    _state: home.Home | None = field(default=None, repr=False)
    _secrets: vault.Vault | None = field(default=None, repr=False)
    # A loop of the source's own, made only outside a run (`shared_browser` gives the run's).
    _own_loop: Bridge | None = field(default=None, repr=False)
    # A scheduler of the source's own, made only outside a run, on its own loop.
    _own_jobs: jobs.Scheduler | None = field(default=None, repr=False)
    _closed: bool = field(default=False, repr=False)
    # Whether a target already said that the browser doesn't use the config's proxy.
    _told_proxy: bool = field(default=False, repr=False)

    @property
    def loop(self) -> Bridge:
        """The run's loop, where the browser and async code run; the source's own outside a run."""
        shared = self.settings.browser_pool
        if shared is not None:
            return Bridge(shared.loop)
        if self._own_loop is None:
            self._own_loop = Bridge()
        return self._own_loop

    @property
    def scheduler(self) -> jobs.Scheduler:
        """The run's scheduler, which every request and file waits for; the source's own
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
    def state(self) -> home.State:
        """The source's own values in the home's `flnd.db`: the run's, or one opened here."""
        if self.settings.db is not None:
            return self.settings.db.state(self.source.code)
        if self._state is None:
            self._state = home.open(self.settings.home)
        return self._state.state(self.source.code)

    @property
    def secrets(self) -> vault.Vault:
        """The source's encrypted secrets in the home's `secrets.db`, opened on first use."""
        if self._secrets is None:
            self._secrets = home.secrets(self.settings.home, self.source.code)
        return self._secrets

    async def call(self, fn: Callable[..., T], *args: Any) -> T:
        """Run a step of the source's plain listing, `fn(*args)`, on a listing thread
        (`jobs.Scheduler.step`).

        It waits for the source's holds, as its listing did on its own thread, and is never
        tried again: what the code raises is raised here.
        """
        return await self.scheduler.step(self.source.code, None, fn, *args)

    async def aside(self, fn: Callable[..., T], *args: Any) -> T:
        """Run the source's plain code for a lazy post's files, a refresh or `date`, on a
        `CALL` thread, as a job of the run: any tab it opens is its own.

        Such a tab comes from the run's budget and closes when the call ends, so the listing's
        tab is never navigated away from its feed. The job waits for the source's holds and
        is never tried again.
        """
        scheduler = self.scheduler

        async def work() -> tuple[bool, Any]:
            try:
                return True, await scheduler.on(jobs.Kind.CALL, _aside, fn, *args)
            except Exception as error:
                return False, error

        ok, value = await scheduler.run(jobs.Job(kind=jobs.Kind.CALL, work=work,
                                                 plugin=self.source.code))
        if not ok:
            raise value
        return value

    async def off(self, fn: Callable[..., T], *args: Any) -> T:
        """Run the engine's own blocking step, `fn(*args)`, on a `CALL` thread, with no job.

        For what must run even while the source is held or the run is stopping: closing a
        target's tabs, writing back the cookies.
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
            for db in (self._state, self._secrets):
                if db is not None:
                    db.close()

    def close(self) -> None:
        """Close the session, stores and the source's own loop; once."""
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
            for db in (self._state, self._secrets):
                if db is not None:
                    db.close()
            if self._own_loop is not None:
                self._own_loop.close()


def _matches_domain(source: Source, host: str | None) -> bool:
    """Whether `host` belongs to one of `source.domains`."""
    if not host:
        return False
    low = host.lower()
    return any(low == d or low.endswith(f".{d}") for d in source.domains)


@contextmanager
def source_run(settings: RunShared, source: Source) -> Iterator[SourceRun]:
    """Open one source's resources for a run, and close them however the block ends.

    The login's cookies are read from the home once, here, and a cookie of the login that has
    expired or expires within `EXPIRY_WARNING` is a warning line. The source's
    `[sources.<code>]` settings in the home's config.toml are read here too, and give the
    run's scheduler its pace and its hosts' limits, and the session its proxy. Nothing is sent
    yet: the session connects on its first request.

    Args:
        settings: what the run shares across sources: the home, its database, the stop flag
            (whose first Ctrl-C stops the listing client, and the second the files client).
        source: the source, as the one instance the run uses.

    Yields:
        The `SourceRun`, tracked by the run's live stats while the block runs.

    Raises:
        UsageError: the home's config.toml has a value it doesn't take.
    """
    run = _made(settings, source)
    try:
        yield run
    finally:
        run.close()


@asynccontextmanager
async def opened(settings: RunShared, source: Source) -> AsyncIterator[SourceRun]:
    """`source_run` inside a run, on its loop: the source's resources, closed on the loop.

    Raises:
        UsageError: the home's config.toml has a value it doesn't take.
    """
    # The home's config.toml and the login's cookies are read off the loop.
    run = _made(settings, source, *await asyncio.to_thread(_read, settings, source))
    try:
        yield run
    finally:
        await run.aclose()


def _read(settings: RunShared, source: Source) -> tuple[SourceConfig, cookies.Jar]:
    """A source's `[sources.<code>]` settings and its login's cookies, from the home."""
    return config(settings.home).source(source.code), home.load(settings.home, source.code)


def _made(settings: RunShared, source: Source, configured: SourceConfig | None = None,
          jar: cookies.Jar | None = None) -> SourceRun:
    """A source's `SourceRun`: its login's cookies warned of, its settings applied; both
    read here unless given."""
    if configured is None or jar is None:
        configured, jar = _read(settings, source)
    _warn_expiring(source, jar)
    stop: Stop | None = getattr(settings, "stop", None)
    # Outside a run, the source has a scheduler of its own.
    own = jobs.Scheduler(stop=stop) if scheduler(settings) is None else None
    used = scheduler(settings) or own
    used.pace[source.code] = source.pace if configured.pace is None else configured.pace
    used.limits.limits.update(_limits(source, configured))
    session = _network(source, jar, configured, handles=used.limits.handles)
    return SourceRun(source, settings, jar, session,
                     net.Client(session, stop=stop.soon if stop else None),
                     net.Client(session, stop=stop.now if stop else None), configured,
                     _own_jobs=own)


def _network(source: Source, jar: Iterable[Mapping[str, Any]],
             settings: SourceConfig, *, handles: int) -> net.Session:
    """A source's session.

    Args:
        source: the source; its `user_agent` and `sign` are read.
        jar: the login's cookies.
        settings: the source's config.toml settings, whose `proxy` is used.
        handles: transfers at once in all (`jobs.Limits.handles`).

    Returns:
        The session, with the source's `sign` hook on requests to its own domains and the
        config's proxy.
    """

    def prepare(request: net.Request) -> net.Request | None:
        # Runs on the loop before each try; the hook only adds headers, so it costs little. The
        # source sees a `flnd.source.Request`, never the HTTP client's own type, so the client
        # may change without breaking a source; what it sets in `headers` is copied back.
        if not _matches_domain(source, request.url.host):
            return None
        seen = Request(request.method, str(request.url), dict(request.headers),
                       request.content, session.names)
        source.sign(seen)
        for name, value in seen.headers.items():
            request.headers[name] = value
        return None

    headers = {"User-Agent": source.user_agent} if source.user_agent else None
    session = net.Session(headers=headers, cookies=jar, prepare=prepare, handles=handles,
                           proxy=settings.proxy)
    return session


def _limits(source: Source, settings: SourceConfig) -> Mapping[str, int]:
    """The most files at once per media host: the source's `limits`, the config's over them."""
    return {**source.limits, **settings.limits}


class TargetSite:
    """The engine's `Site` for one target; see `flnd.source.Site` for each member.

        site = TargetSite(run, Target("exm:example_user", kind="user"))
        profile = site.fetch("https://api.example.com/users/example_user").json
        site.close()

    Args:
        run: the source's resources for the run.
        target: the target it acts for.
        time: the target's `--when`, as days.

    Used on a thread of the source (`SourceRun.call`). Each browser verb makes one call to the
    run's loop; the tabs are the target's share of the run's browser (`flnkit.browser.Tabs`),
    opened on first use.
    `close()` gives them back, and closes a browser the site launched for itself outside a run.
    """

    def __init__(self, run: SourceRun, target: Target, *, time: Range = Range()):
        self.target, self.time = target, time
        self._run = run
        self._source = run.source
        # The run's browser; outside a run, one of the site's own, made on first use.
        self._shared: browser.RunBrowser | None = run.settings.browser_pool
        self._own = False
        self._tabs: browser.Tabs | None = None
        # The category being listed, which `pages` resumes and reports its cursor to; set by the
        # job for each category, None between them.
        self.listing: Listing | None = None

    # -- requests -------------------------------------------------------------------------------

    @property
    def cookies(self) -> Mapping[str, str]:
        """See `flnd.source.Site.cookies`."""
        merged = {cookie["name"]: cookie["value"] for cookie in self._run.jar}
        merged.update(self._run.session.names)
        merged.update({cookie["name"]: cookie["value"] for cookie in self._browser_cookies()})
        return MappingProxyType(merged)

    def fetch(self, url: str, *, method: str = "GET", headers: Mapping[str, str] | None = None,
              check: bool = True, in_page: bool = False, **request: Any) -> net.Response:
        """Make one request, paced and retried, on the run's loop; see `flnd.source.Site.fetch`.

        The request is a job of the run's scheduler (`_request`), which paces and retries it,
        sent through the run's `web` client, which stops at the first Ctrl-C. Cookies the
        browser has set are added to the session first, so a request sends what a logged-in
        page would. With `in_page`, the tab's own `fetch` sends it instead, as the same job.

        Args:
            url: the URL; `params` from `request` are added to its query.
            method: the HTTP method.
            headers: headers of the source's own, besides the session's and the `sign` hook's.
            check: raise for a refusal; False returns the refusal's response.
            in_page: send it from the target's tab, as the page's own script would.
            **request: `params` (a mapping, the query) and `body` (bytes, text or JSON).

        Returns:
            The response.

        Raises:
            SourceError: an option other than `params` and `body`, or a call from code on the
                loop, which would wait for the loop it runs on.
            LoginError: a 401 or 403 from the source's own domains.
            SiteError: another refusal (`NotFound`, `Blocked` and the rest), when `check`.
            NetworkError: no answer after the tries.
            Stopped: the run was stopped while it waited.
        """
        options = dict(request)
        url = net.with_query(url, options.pop("params", None))
        body = options.pop("body", None)
        if options:
            raise SourceError(f"fetch takes params and body, not {', '.join(sorted(options))}")
        if self._run.loop.here:
            raise SourceError("site.fetch is for a plain `def` route or category, not for code "
                              "on the run's loop")
        web = self._run.web
        if in_page:
            page = self._verbs().page

            def send() -> Awaitable[net.Response]:
                return page.fetch(url, method=method, headers=headers, body=body)
        else:
            # Cookies set in the browser reach the session too, as a logged-in page sends them.
            if cookies := self._browser_cookies():
                self._run.session.add(cookies)

            def send() -> Awaitable[net.Response]:
                return web.fetch(url, method=method, headers=headers, body=body)

        async def once() -> net.Response | Error:
            try:
                return await send()
            except Error as error:
                if not check and isinstance(error, SiteError) and error.response is not None \
                        and not (error.retryable or error.status == 429):
                    return error.response
                wall = self._walled(url, error)
                if wall is not None:
                    raise wall from error
                if method.upper() not in SAFE and getattr(error, "status", None) != 429:
                    # A server may have acted on a request that isn't safe to send twice, so
                    # the failure goes past the scheduler, which would try it again.
                    return error
                raise

        try:
            sent = self._run.loop.run(self._request(url, once))
            if isinstance(sent, Error):
                raise sent
            return sent
        except SiteError as error:
            if not check and error.response is not None:
                return error.response
            raise

    def _request(self, url: str, work: Callable[[], Awaitable[T]]) -> Awaitable[T]:
        """`work` as a job of the run's scheduler: a `PAGE` job on the source's own domains,
        paced; a `FILE` job on any other host, under its cap, ahead of the target's files."""
        host = net.URL(url).host
        own = _matches_domain(self._source, host)
        job = jobs.Job(kind=jobs.Kind.PAGE if own else jobs.Kind.FILE, work=work,
                       plugin=self._source.code, host=host, target=str(self.target),
                       priority=None if own else jobs.PRIORITY[jobs.Kind.PAGE])
        return self._run.scheduler.run(job)

    def _walled(self, url: str, error: Error) -> LoginError | None:
        """The login error a 401 or 403 from the source's own domains is; None for anything
        else, such as a 401 or 403 from a CDN (an expired link), which stays a `SiteError`."""
        if (type(error) is SiteError and error.status in LOGIN_WALLS
                and _matches_domain(self._source, net.URL(url).host)):
            return LoginError(f"{self._source.code}: the site refused the login "
                              f"(HTTP {error.status})",
                              hint=f"run flnd login {self._source.code}",
                              target=str(self.target))
        return None

    def pages(self, url: str, *, cursor: str | Callable[[Any], Any],
              params: Mapping[str, Any] | None = None,
              send: str | Callable[[Any], Mapping[str, Any]] = "cursor") -> Iterator[Any]:
        """Each page of a paged JSON listing; see `flnd.source.Site.pages` and `paged`."""
        return paged(self.fetch, url, cursor=cursor, params=params, send=send,
                     listing=self.listing)

    def html(self, text: str | bytes) -> Document:
        """Parse HTML text; see `flnd.source.Site.html`."""
        from flnkit.html import parse

        return parse(text)

    # -- the browser ----------------------------------------------------------------------------

    def open(self, url: str, *, idle: bool = False, timeout: float = 30.0) -> bool:
        """Load `url` in the target's tab; see `flnd.source.Site.open`.

        A warm tab already loading `url` (`flnkit.browser.Tabs.warm`) becomes the target's tab,
        and only its load is waited for: its request went out when it was warmed, so it isn't
        paced again. Any other load is a job of the run's scheduler, which paces it, waits out a
        429 and tries again.

        Args:
            url: the page.
            idle: also wait for the network to go quiet.
            timeout: seconds before the page counts as not loaded.

        Returns:
            Whether it loaded with a status under 400; a page that didn't is a warning line.
        """
        tabs = self._verbs()
        warmed, first = tabs.take_warm(url)
        if warmed and first is not None:
            # A warm tab already had its first answer, so it isn't paced again: wait for its load.
            try:
                tabs.run(tabs.page.wait(idle=idle, timeout=timeout))
            except browser.WaitError as error:
                self.warn("could not open {url}: {error}", url=url, error=error)
                return False
            visit: browser.Visit | None = first
        else:
            page = tabs.page
            visit = self._run.loop.run(
                self._request(url, lambda: self._visit(page, url, idle, timeout)))
        if visit is None:
            return False
        if visit.status >= 400:
            self.warn("{url}: HTTP {status}", url=url, status=visit.status)
            return False
        return True

    async def _visit(self, page: browser.Page, url: str, idle: bool,
                     timeout: float) -> browser.Visit | None:  # noqa: ASYNC109 (Playwright's own)
        """Load `url` in the target's tab: one try, which the run's scheduler paces and repeats.

        Runs on the loop.

        Args:
            page: the target's tab.
            url: the page.
            idle: also wait for the network to go quiet.
            timeout: seconds before the page counts as not loaded.

        Returns:
            Where it landed, or None when it didn't load.

        Raises:
            Blocked: the page answered 429, with its `Retry-After`, so the scheduler waits it out.
        """
        visit = await page.goto(url, idle=idle, timeout=timeout)
        if visit is not None and visit.status == 429:
            raw_wait = visit.headers.get("retry-after")
            wait = float(raw_wait) if raw_wait and raw_wait.isdigit() else None
            raise Blocked(f"GET {text.redact(url)}: HTTP 429", wait=wait, status=429)
        return visit

    @property
    def url(self) -> str:
        """See `flnd.source.Site.url`."""
        tabs = self._aside() or self._tabs
        if tabs is None or not tabs.opened:
            return "about:blank"
        return tabs.page.url

    def wait(self, css: str | None = None, *, url: browser.Match | None = None,
             idle: bool = False, timeout: float = 30.0) -> None:
        """See `flnd.source.Site.wait`."""
        tabs = self._verbs()
        tabs.run(tabs.page.wait(css, url=url, idle=idle, timeout=timeout))

    def scroll(self, *, until: Callable[[], bool] | None = None,
               idle: float = browser.IDLE) -> int:
        """Scroll the target's tab until its feed ends or `until()`; see `flnd.source.Site.scroll`.

        It scrolls in the source's `scroll` style, as its feeds do.

        Args:
            until: asked here, on the source's thread, before each scroll; True stops.
            idle: seconds the bottom may stay quiet, after a whole wait for more brought nothing,
                before the feed counts as ended.

        Returns:
            How many scrolls it made.
        """
        tabs = self._verbs()
        scroller = tabs.run(tabs.page.scroller(idle=idle, style=tabs.scroll))
        # One step per call to the loop, with `until` asked here, on the source's thread.
        while not (until is not None and until()) and tabs.run(scroller.step()):
            pass
        return scroller.scrolls

    def feed(self, match: browser.Match, *, scroll: bool = False, lines: bool = False,
             cursor: browser.Reader | None = None) -> browser.Feed:
        """See `flnd.source.Site.feed`; it scrolls in the source's `scroll` style."""
        return browser.Feed(self._verbs(), match, scroll=scroll, lines=lines, cursor=cursor)

    def document(self) -> Document:
        """The tab's page now, parsed; see `flnd.source.Site.document`.

        Returns:
            The page as a `flnkit.html.Document`, parsed on the source's thread, not on the loop.
        """
        from flnkit.html import parse

        tabs = self._verbs()
        return parse(tabs.run(tabs.page.content()))

    def evaluate(self, js: str, *, arg: Any = None) -> Any:
        """See `flnd.source.Site.evaluate`."""
        tabs = self._verbs()
        return tabs.run(tabs.page.evaluate(js, arg))

    def click(self, css: str, *, timeout: float = 10.0) -> None:
        """See `flnd.source.Site.click`."""
        tabs = self._verbs()
        tabs.run(tabs.page.click(css, timeout=timeout))

    def handoff(
        self, *, until: Callable[[], bool], message: str, timeout: float | None = None
    ) -> bool:
        """See `flnd.source.Site.handoff`."""
        return self._verbs().handoff(until=until, message=message, timeout=timeout,
                                       stop=self._run.web.stop)

    @property
    async def tab(self) -> Any:
        """See `flnd.source.Site.tab`."""
        return (await self._browser().current()).raw

    def warm(self, urls: Iterable[str]) -> None:
        """Start loading `urls` in background tabs; see `flnd.source.Site.warm`.

        An empty URL (a page the profile lacks) opens no tab.
        """
        self._verbs().warm([url for url in urls if url])

    def _verbs(self) -> browser.Tabs:
        """`_browser()`, for a browser verb, which only plain `def` code may call.

        Returns:
            The target's tabs.

        Raises:
            SourceError: called from async code on the run's loop, where the verb would wait for
                the loop it runs on; that code awaits `site.tab` instead.
        """
        if self._run.loop.here:
            raise SourceError("the browser verbs of `site` are for a plain `def` route or "
                              "category; an `async def` one awaits `site.tab` instead")
        _met()
        aside = _ASIDE.get()
        if aside is None:
            return self._browser()
        tabs = self._aside()
        if tabs is None:
            tabs = self._made()
            aside.append((self, tabs))
        return tabs

    def _aside(self) -> browser.Tabs | None:
        """This site's own tabs in the side call running on this thread (`SourceRun.aside`)."""
        return next((tabs for site, tabs in _ASIDE.get() or () if site is self), None)

    def _browser(self) -> browser.Tabs:
        """The target's tabs, made on first use.

        Returns:
            The target's `Tabs` in the run's browser, or, outside a run, in a browser of the
            site's own, which `close` closes. They scroll in the config's `scroll` for the
            source, else the source's own.
        """
        if self._tabs is None:
            self._tabs = self._made()
        return self._tabs

    def _made(self) -> browser.Tabs:
        """A share of the run's browser for this target, which scrolls in the config's `scroll`
        for the source, else the source's own."""
        if self._shared is None:
            self._shared = browser.RunBrowser(self._run.loop.loop, stealth=self._source.stealth,
                                              screen=_Screen())
            self._own = True
        settings = self._run.config
        if settings.proxy is not None and not self._run._told_proxy:
            self._run._told_proxy = True
            # The browser is one window every source of the run shares, so it can't take
            # one source's proxy: say so, rather than let page loads leave unproxied
            # without a word.
            ui.warn("{code}: the browser doesn't use the proxy in config.toml; only the "
                    "source's HTTP requests do", code=self._source.code)
        style = settings.scroll or self._source.scroll
        budget = jobs.Budget(self._run.scheduler, self._source.code, None)
        return browser.Tabs(self._shared, self._run.jar, scroll=style, budget=budget)

    def _browser_cookies(self) -> list[Mapping[str, Any]]:
        """The browser's cookies, or none when this target never opened a tab."""
        if self._tabs is None or self._shared is None or not self._shared.launched:
            return []
        return list(self._tabs.run(self._shared.cookies()))

    # -- the rest -------------------------------------------------------------------------------

    @property
    def client(self) -> Any:
        """See `flnd.source.Site.client`."""
        return self._run.client

    @property
    def state(self) -> MutableMapping[str, Any]:
        """See `flnd.source.Site.state`."""
        return self._run.state

    @property
    def secrets(self) -> MutableMapping[str, str]:
        """See `flnd.source.Site.secrets`."""
        return self._run.secrets

    def info(self, message: str, **fields: Any) -> None:
        """Log an info line naming the target."""
        # Target's name is put in front of the message as a field so text cannot break format.
        ui.info("{target}: " + message, target=str(self.target), **fields)

    def warn(self, message: str, **fields: Any) -> None:
        """Log a warning line naming the target."""
        ui.warn("{target}: " + message, target=str(self.target), **fields)

    @property
    def interactive(self) -> bool:
        """A run never asks: only `flnd login` does (`LoginSite`)."""
        return False

    def ask(self, message: str, *, secret: bool = False, description: str = "") -> str:
        """A run has nobody to ask; see `flnd.source.Site.ask`.

        Args:
            message: the question the source wanted to ask.
            secret: whether it would be hidden.
            description: the lines under it.

        Raises:
            UsageError: always: a question belongs in the source's `Login.start`.
        """
        raise UsageError(f"{self._source.code}: a run can't ask {message!r}",
                         hint=f"run flnd login {self._source.code} in a terminal first")

    def back_off(self, reason: str, *, retry_after: float | None = None) -> None:
        """Wait out a block a page showed, by rounds; see `flnd.source.Site.back_off`.

        It is a block round of the run's scheduler (`jobs.Scheduler.hold`): the source's
        account is held for the larger of the site's wait and the round's (30, 120, 480 s),
        which holds its other listings too, and this call waits it out.

        Args:
            reason: what the site did, shown with the wait.
            retry_after: the seconds the site asked for, capped at `LONGEST_WAIT`.

        Raises:
            Blocked: a fourth round in a row (`jobs.RESET` seconds without one start again).
            Stopped: the run's first Ctrl-C came while waiting.
        """
        code, scheduler = self._source.code, self._run.scheduler
        asked = min(retry_after, LONGEST_WAIT) if retry_after is not None else 0.0
        try:
            seconds = scheduler.hold(code, None, seconds=asked, reason=reason)
        except Blocked as error:
            raise Blocked(f"{code}: {reason}, after {len(jobs.ROUND_WAITS)} waits",
                          hint="try again later, or raise the source's pace in config.toml",
                          target=str(self.target)) from error
        ui.warn("{target}: {reason}; waiting {time}", target=str(self.target),
                reason=reason, time=text.duration(round(seconds)))
        tally = self._run.settings.tally
        count = scheduler._holds[(code, None, None)].rounds
        held = (tally.hold(code, None, "pages", seconds, reason, count=count)
                if tally is not None else nullcontext())
        with held:
            try:
                self._run.loop.run(scheduler.wait(code, None))
            except Stopped:
                raise Stopped(f"{self.target}: stopped while waiting: {reason}") from None

    def pause(self, seconds: float, reason: str) -> None:
        """Wait `seconds`, shown with `reason` as the source's wait in the live stats.

        See `flnd.source.Site.pause`.

        Args:
            seconds: how long; 0 or less doesn't wait.
            reason: why, in the source's words.

        Raises:
            Stopped: the run's first Ctrl-C came while waiting.
        """
        if seconds <= 0:
            return
        tally = self._run.settings.tally
        held = (tally.hold(self._source.code, None, "pages", seconds, reason)
                if tally is not None else nullcontext())
        with held:
            if not clock.sleep(seconds, stop=self._run.web.stop):
                raise Stopped(f"{self.target}: stopped while waiting: {reason}")

    @property
    def _idle(self) -> bool:
        """Whether it never opened a tab, so `close` has nothing to do."""
        return self._tabs is None and not self._own

    def close(self) -> None:
        """Give back the target's tabs, handing the client the cookies set in them."""
        tabs, cookies = self._tabs, self._browser_cookies()
        self._tabs = None
        try:
            if cookies:
                self._run.session.add(cookies)
            if tabs is not None:
                tabs.close()
        finally:
            if self._own and self._shared is not None:
                self._shared.close()
                self._shared, self._own = None, False


class LoginSite:
    """The `Site` a source's own `Login` asks through (Telegram's), during `flnd login`.

    It has what a login needs: `ask` and `interactive`, the source's `secrets` and `state`, its
    `cookies`, `info`, `warn` and `pause`. A login never lists, so the listing verbs aren't here.

    Args:
        run: the source's resources for the login.
    """

    def __init__(self, run: SourceRun):
        self._run = run
        self.target = Target(f"{run.source.code}:", kind="account")

    @property
    def cookies(self) -> Mapping[str, str]:
        """The login's cookies, `{name: value}`, read-only."""
        return MappingProxyType({cookie["name"]: cookie["value"] for cookie in self._run.jar})

    @property
    def state(self) -> MutableMapping[str, Any]:
        """This source's own values in the home's `flnd.db`."""
        return self._run.state

    @property
    def client(self) -> Any:
        """What the source's `connect` gave; None during a login."""
        return self._run.client

    @property
    def interactive(self) -> bool:
        """Whether `ask` can ask: `flnd login` in a terminal."""
        return self._run.settings.interactive and forms.interactive()

    def ask(self, message: str, *, secret: bool = False, description: str = "") -> str:
        """Ask a question in the terminal (`flnd.ui.forms.ask`), and return what was typed.

        Args:
            message: the question, shown before the cursor.
            secret: hide what is typed (a password, a code).
            description: lines shown above the question, as an info line; "" for none.

        Returns:
            What the person typed, without the line's end.

        Raises:
            UsageError: there is no terminal to ask in, or the run said not to ask.
        """
        if not self.interactive:
            raise UsageError(f"{self._run.source.code}: logging in needs a terminal to ask in",
                             hint=f"run flnd login {self._run.source.code} in a terminal")
        if description:
            ui.info(description)
        return forms.ask(message, secret=secret)

    @property
    def secrets(self) -> MutableMapping[str, str]:
        """This source's secrets, encrypted in the home's `secrets.db`."""
        return self._run.secrets

    def info(self, message: str, **fields: Any) -> None:
        """Log an info line naming the source."""
        ui.info("{code}: " + message, code=self._run.source.code, **fields)

    def warn(self, message: str, **fields: Any) -> None:
        """Log a warning naming the source."""
        ui.warn("{code}: " + message, code=self._run.source.code, **fields)

    def storage(self, url: str) -> dict[str, str] | None:
        """A web app's saved local storage, read in a hidden copy of Chrome's default profile.

        For a login that starts from a web app's own session (Telegram Web's). The copy is
        made, read and removed here; the profile itself is never changed.

        Args:
            url: the web app's page.

        Returns:
            Its `localStorage` as `{key: value}`; None when the page didn't load on its host.

        Raises:
            NetworkError: the page couldn't be read.
            ToolError: Chrome isn't installed.
            FileNotFoundError: Chrome has no default profile.
        """
        return browser.local_storage(url, profile="Default")

    def pause(self, seconds: float, reason: str) -> None:
        """Wait `seconds` during the login (a QR code's refresh); a stop ends it."""
        if seconds > 0 and not clock.sleep(seconds, stop=self._run.web.stop):
            raise Stopped(f"{self._run.source.code}: stopped while waiting: {reason}")


@asynccontextmanager
async def shared_browser(settings: RunShared) -> AsyncIterator[None]:
    """Give a run its scheduler and its one browser, on the run's loop; then close both.

        async with shared_browser(settings):
            ...                     # settings.browser_pool is the run's `RunBrowser`

    Entered again inside a run, it changes nothing. The browser launches only when a target first
    asks for a tab, so a run that never needs one never starts it. On the way out the browser is
    closed before the scheduler, even when closing it fails.

    Args:
        settings: the run's; `browser_pool` holds the `flnkit.browser.RunBrowser` while it runs.
    """
    if settings.browser_pool is not None:
        yield
        return
    shared = browser.RunBrowser(asyncio.get_running_loop(), screen=_Screen())
    found = await asyncio.to_thread(config, settings.home)
    scheduler = _SCHEDULERS[shared] = jobs.Scheduler(
        stop=getattr(settings, "stop", None), rate=found.rate, slowest=found.slowest)
    settings.browser_pool = shared
    try:
        yield
    finally:
        settings.browser_pool = None
        try:
            await shared.aclose()
        finally:
            await scheduler.close()


# Each run's scheduler, by the run's browser, so every `RunShared` given that browser shares it.
_SCHEDULERS: weakref.WeakKeyDictionary[browser.RunBrowser, jobs.Scheduler] = \
    weakref.WeakKeyDictionary()


def scheduler(settings: RunShared) -> jobs.Scheduler | None:
    """The scheduler of the run `settings` belong to; None outside a run."""
    pool = settings.browser_pool
    return _SCHEDULERS.get(pool) if pool is not None else None


def login_names(source: Source) -> tuple[str, ...]:
    """The cookies that prove a source's login (`cookies(...)`'s names); none for other logins."""
    return source.login.names if source.login is not None else ()


def _warn_expiring(source: Source, jar: Any) -> None:
    """Warn of login cookies (`login_names`) that have expired or will expire soon.

    Only the cookies that prove the login are read, and a session cookie (expiry 0 or -1) never
    warns. The warning names the cookie, never its value.

    Args:
        source: the source whose login it is; its code names it in the warning.
        jar: the login's cookies, in Playwright's shape (`name`, `value`, `expires` in Unix
            seconds).
    """
    now = clock.wall()
    names = login_names(source)
    for cookie in jar:
        expires = cookie.get("expires", -1)
        # -1 (or 0) is a session cookie, which lasts as long as the browser it came from said.
        if (
            cookie.get("name") not in names
            or not isinstance(expires, (int, float))
            or expires <= 0
        ):
            continue
        if expires <= now:
            ui.warn(
                "{code}: the login cookie {name} has expired; run flnd login {code} again",
                code=source.code, name=cookie["name"],
            )
        elif expires - now <= EXPIRY_WARNING:
            ui.warn(
                "{code}: the login cookie {name} expires in {time}; "
                "run flnd login {code} again soon",
                code=source.code, name=cookie["name"], time=text.duration(expires - now),
            )



# ---------------------------------------------------------------------------------------------
# Paged listings
# ---------------------------------------------------------------------------------------------

class Listing:
    """One category's listing in a run: where `pages` starts, and the cursor of the page it reads.

    The job makes one per category it lists and sets it as `TargetSite.listing`; the first
    `pages` call of the category starts from `start` and tells it the cursor of each page it
    reads, which the job saves when the listing is cut short. Later `pages` calls of the same
    category (a source paging a second list) start from their beginning.

    Args:
        start: the cursor an interrupted run saved; None starts from the newest.
        label: names the listing in the line that says a saved cursor was refused.

    Example:
        >>> listing = Listing("c42", label="exm:example_user: posts")
        >>> listing.cursor, listing.taken
        ('c42', False)
    """

    def __init__(self, start: str | None = None, *, label: str = "") -> None:
        self.start = start
        self.label = label
        # The cursor of the page being read, for the job to save if the listing is cut short.
        self.cursor: str | None = start
        # Whether a `pages` call took this listing already; only the first one resumes.
        self.taken = False
        # Whether the site refused `start`, so the listing began again from the newest.
        self.refused = False
        # The pages `pages` has read for it so far, for the live stats.
        self.read = 0


def paged(fetch: Callable[..., Any], url: str, *, cursor: str | Callable[[Any], Any],
          params: Mapping[str, Any] | None, send: str | Callable[[Any], Mapping[str, Any]],
          listing: Listing | None) -> Iterator[Any]:
    """Each page of a paged JSON listing, read through `fetch`: `Site.pages` over any site.

    How it goes:
    1. The first request sends `params`, plus the saved cursor when `listing` resumes.
    2. Each page is yielded, then its cursor read (`cursor`: a `dig` path or a function).
    3. The listing ends on a missing, empty or repeated cursor, or an empty page; otherwise the
       next request sends the cursor where `send` says, and `listing.cursor` names that page.

    Args:
        fetch: the site's `fetch`; called as `fetch(url, params=…)`, its response's `.json` read.
        url: the listing's URL.
        cursor: a `dig` path to the next cursor, or a function from a page to it (None ends).
        params: the query every request sends; None for none.
        send: the cursor's query parameter, or a function from the cursor to more params.
        listing: the category's `Listing`; None (or one taken already) neither resumes nor
            reports.

    Yields:
        Each page, the response's parsed JSON.
    """
    if listing is not None and listing.taken:
        listing = None
    if listing is not None:
        listing.taken = True

    def ask(at: Any) -> Any:
        query = dict(params or {})
        if at is not None:
            query.update(send(at) if callable(send) else {send: at})
        return fetch(url, params=query or None).json

    page = _first_page(ask, listing)
    seen: set[str] = set()
    while page not in (None, {}, []):
        if listing is not None:
            listing.read += 1
        yield page
        following = cursor(page) if callable(cursor) else dig(page, cursor)
        if following is None or following == "" or str(following) in seen:
            return
        seen.add(str(following))
        if listing is not None:
            # The cursor of the page being fetched and read: a run cut short reads it again, and
            # the posts it already saved are skipped.
            listing.cursor = str(following)
        page = ask(following)


def _first_page(ask: Callable[[Any], Any], listing: Listing | None) -> Any:
    """The first page: from the saved cursor if any, else from the newest.

    A saved cursor the site refuses with a 4xx has gone stale, which is the site's answer for
    good: it is dropped, said once, and the listing starts from the newest. Any other failure is
    the run's, and is raised.

    Args:
        ask: fetches one page, given its cursor (None for the newest).
        listing: the category's `Listing`; None starts from the newest.

    Returns:
        The first page's JSON.
    """
    start = listing.start if listing is not None else None
    if not start:
        return ask(None)
    try:
        return ask(start)
    except SiteError as error:
        if error.status is None or not 400 <= error.status < 500:
            raise
    assert listing is not None
    ui.info("{label}: the cursor an interrupted run saved was refused; listing from the newest",
            label=listing.label)
    listing.refused, listing.cursor = True, None
    return ask(None)
