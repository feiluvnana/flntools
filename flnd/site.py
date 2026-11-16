"""The engine's `Site` and `Login`: what a source's routes, categories and login act through."""

from __future__ import annotations

import concurrent.futures
import dataclasses
import random
import sys
import threading
from collections import deque
from contextlib import contextmanager
from dataclasses import dataclass, field
from types import MappingProxyType, SimpleNamespace
from typing import (
    TYPE_CHECKING, Any, Callable, Coroutine, Iterable, Iterator, Mapping, MutableMapping, TypeVar,
)
from urllib.parse import urlencode

import httpx

from flnd.common import auth, net, paths
from flnd.source import FetchError, Range, Source, Target
from shared import browser, clock, http, prompt, store, text, ui, worker

if TYPE_CHECKING:
    from shared import cli, cookies

    from flnd.options import RunShared

__all__ = ["LoginSite", "SourceRun", "TargetSite", "shared_browser", "source_run"]

T = TypeVar("T")
# Tries in all for a site request, sent by the client or by the tab: a 5xx or a request that
# couldn't be sent may go through next time, after 1, 2 and 4 s.
ATTEMPTS = 4
# The least seconds between two bottom scrolls at 1x.
SCROLL_FLOOR = 0.05
# Most open tabs kept in a single target browser window when warming or nesting watches.
MAX_TABS = 6
# Full-page scrolling moves the DOM directly so background tabs never stall on mouse wheel.
SCROLL_BY = (
    "d => { window.scrollBy(0, d);"
    " window.dispatchEvent(new WheelEvent('wheel', {deltaY: d, bubbles: true})); }"
)
# Static asset resource types skipped when buffering background-warmed tab responses.
ASSET_TYPES = frozenset({"image", "media", "font", "stylesheet", "script", "manifest"})
# How soon before a login cookie expires a run warns of it, in seconds: a day, so a scheduled
# `flnd sync` warns the run before the one that meets the login wall.
EXPIRY_WARNING = 24 * 3600


@dataclass
class SourceRun:
    """One source's resources for a run: cookies, HTTP clients, `connect` client, and stores."""

    # The source, as one instance for the whole run.
    source: Source
    # What the run shares across sources: flnd's folder, the history, targets.db, the stop flag.
    settings: RunShared
    # The login's cookies.
    jar: cookies.Jar
    # The source's own requests, in its pace; `web.raw` holds the cookies the site sets.
    web: http.Client
    # The files' downloads, over the same cookies, each CDN host backing off on its own.
    files: http.Client
    # What the source's `connect` gave, entered once for the run.
    client: Any = None
    _state: store.Db | None = field(default=None, repr=False)
    _secrets: auth.Vault | None = field(default=None, repr=False)
    _loop: worker.Loop | None = field(default=None, repr=False)
    _closed: bool = field(default=False, repr=False)

    @property
    def state(self) -> store.Table[Any]:
        """The source's `store` table in `.flnd/metadata/sources.db`, opened on first use."""
        if self._state is None:
            self._state = store.open(self.settings.home / paths.SOURCE_STATE)
        return self._state[self.source.code]

    @property
    def secrets(self) -> auth.Vault:
        """The source's encrypted secrets in `.flnd/metadata/secrets.db`, opened on first use."""
        if self._secrets is None:
            self._secrets = auth.secrets(self.settings.home, self.source.code)
        return self._secrets

    def run_async(self, coroutine: Coroutine[Any, Any, T]) -> T:
        """Run a coroutine on this source's own event loop, started on first use."""
        if self._loop is None:
            self._loop = worker.Loop()
        return self._loop.run(coroutine)

    def close(self) -> None:
        """Close the client, stores and event loop; once."""
        if self._closed:
            return
        self._closed = True
        try:
            self.web.raw.close()
        finally:
            for db in (self._state, self._secrets):
                if db is not None:
                    db.close()
            if self._loop is not None:
                self._loop.close()


def _make_client(
    raw: httpx.Client,
    *,
    interval: float = 0.0,
    stop: threading.Event | None = None,
) -> http.Client:
    """Wrap `raw` in an `http.Client` with `interval` and `stop`."""
    client = http.Client()
    client._raw.close()
    client._raw = raw
    client._interval = interval
    client._stop = stop
    return client


def _matches_domain(source: Source, host: str | None) -> bool:
    """Whether `host` belongs to one of `source.domains`."""
    if not host:
        return False
    low = host.lower()
    return any(low == d or low.endswith(f".{d}") for d in source.domains)


@contextmanager
def source_run(settings: RunShared, source: Source) -> Iterator[SourceRun]:
    """Open one source's resources for a run, and close them however the block ends."""
    jar = auth.load(settings.home, source.code)
    _warn_expiring(source, jar)
    interval = float(source.pace)

    def sign(request: httpx.Request) -> None:
        if _matches_domain(source, request.url.host):
            signed = source.prepare(request, _names(raw.cookies))
            if signed is not request:
                request.headers.update(signed.headers)

    raw = net.client(user_agent=source.user_agent, jar=jar, sign=sign)
    state = store.open(settings.home / paths.SOURCE_STATE)
    stop: cli.Stopping | None = getattr(settings, "stop", None)
    run = SourceRun(
        source, settings, jar,
        _make_client(raw, interval=interval, stop=stop.soon if stop else None),
        _make_client(raw, interval=0.0, stop=stop.now if stop else None),
        _state=state,
    )
    try:
        yield run
    finally:
        run.close()


class TargetSite:
    """The engine's `Site` for one target; see `flnd.source.Site` for each member.

        site = TargetSite(run, Target("loi", "post", "5930"))
        post = site.fetch("https://api.moegoat.com/api/lois/5930").json["data"]
        site.close()

    Args:
        run: the source's resources for the run.
        target: the target it acts for.
        time: the target's `--time`.

    Used on the job's thread only: the browser is Playwright's sync API, which is single-threaded.
    `close()` closes the browser.
    """

    def __init__(self, run: SourceRun, target: Target, *, time: Range = Range()):
        self.target, self.time = target, time
        self._run = run
        self._source = run.source
        self._browser: Any = None
        self._page: _PacedPage | None = None
        self._stack: list[_PacedPage] = []
        self._idle_tabs: deque[_PacedPage] = deque()
        self._warmed: dict[str, tuple[_PacedPage, browser.Visit | None]] = {}
        self._pending_warm: deque[str] = deque()
        self._active_watches: list[tuple[Any, bool, bool]] = []
        self._warm_buffers: dict[_PacedPage, tuple[list[tuple[Any, str]], Any]] = {}
        self._warm_lock = threading.Lock()
        # Any URL on the source's own domains names its pace, for a throttle seen on a page.
        self._home = f"https://{run.source.domains[0]}/"

    @property
    def cookies(self) -> Mapping[str, str]:
        """See `flnd.source.Site.cookies`."""
        merged = {cookie["name"]: cookie["value"] for cookie in self._run.jar}
        merged.update(_names(self._run.web.raw.cookies))
        if self._browser is not None:
            merged.update(
                {cookie["name"]: cookie["value"] for cookie in self._browser.cookies.get()}
            )
        return MappingProxyType(merged)

    def fetch(self, url: str, *, method: str = "GET", headers: Mapping[str, str] | None = None,
              in_page: bool = False, **request: Any) -> http.Response:
        """Make one request, paced and retried."""
        response = self._fetch(url, method=method, headers=headers, soft=False, in_page=in_page,
                               request=request)
        assert response is not None
        return response

    def try_fetch(self, url: str, *, method: str = "GET", headers: Mapping[str, str] | None = None,
                  in_page: bool = False, **request: Any) -> http.Response | None:
        """Like `fetch`, returning None for a 401, 403 or 404."""
        return self._fetch(url, method=method, headers=headers, soft=True, in_page=in_page,
                           request=request)

    def _fetch(self, url: str, *, method: str, headers: Mapping[str, str] | None, soft: bool,
               in_page: bool, request: dict[str, Any]) -> http.Response | None:
        """Send one request via `Page.fetch` or `http.Client.fetch`, paced and retried."""
        if in_page:
            return self._fetch_in_page(url, method, headers, soft, request)
        if self._browser is not None:
            # Cookies set in the browser reach the client too, as a logged-in page would send them.
            self._run.web.raw.cookies.update(net.cookie_jar(self._browser.cookies.get()))
        options = dict(request)
        options.pop("follow_redirects", None)
        unknown = options.keys() - {"params", "data", "json", "timeout"}
        if unknown:
            raise TypeError(f"fetch takes params, data, json, timeout and follow_redirects, "
                            f"not {', '.join(sorted(unknown))}")
        with _fetch_errors():
            return self._run.web.fetch(url, method=method, headers=headers, soft=soft,
                                       attempts=ATTEMPTS, **options)

    def goto(self, url: str, *, idle: bool = False, timeout: float = 30.0) -> bool:
        """Load `url` in the target's tab, paced."""
        if url in self._pending_warm:
            self._pending_warm.remove(url)
        warmed = self._warmed.pop(url, None)
        if warmed is not None:
            page, pre_visit = warmed
            self._switch_tab(page)
            self._fill_warm_tabs()
        else:
            page, pre_visit = self.page, None
        with _stoppable():
            if pre_visit is not None:
                visit = self._visit_tab(
                    page, url=url, pre_visit=pre_visit, idle=idle, timeout=timeout,
                )
            else:
                visit = self._run.web._call(
                    url,
                    lambda: self._visit_tab(page, url=url, pre_visit=None,
                                            idle=idle, timeout=timeout),
                    attempts=3,
                )
        if visit is None:
            return False
        if visit.status >= 400:
            self.warn("{url}: HTTP {status}", url=url, status=visit.status)
            return False
        return True

    def handoff(
        self, *, until: Callable[[], bool], message: str, timeout: float | None = None
    ) -> bool:
        """Wait for a person to complete a step in the browser tab.

        See `flnd.source.Site.handoff`.
        """
        return self.page.handoff(until=until, message=message, timeout=timeout)

    @property
    def page(self) -> browser.Page:
        """See `flnd.source.Site.page`."""
        if self._stack:
            return self._stack[-1]
        if self._page is None:
            self._page = self._new_tab()
        return self._page

    @property
    def _current_watch(self) -> Any | None:
        """The innermost active response watcher, if any."""
        return self._active_watches[-1][0] if self._active_watches else None

    def _new_tab(self) -> _PacedPage:
        """Return an idle tab in this browser window, or open a new tab."""
        if self._idle_tabs:
            return self._idle_tabs.popleft()
        if self._browser is None:
            self._browser = self._connect()
        opened = self._browser.open()
        return _PacedPage(opened.raw, opened.browser, speed=lambda: 1.0)

    def _recycle_tab(self, tab: _PacedPage) -> None:
        """Keep `tab` in `_idle_tabs` for reuse without opening another window."""
        if tab not in self._idle_tabs:
            self._idle_tabs.append(tab)

    def _switch_tab(self, tab: _PacedPage) -> None:
        """Make `tab` active for `self.page`, recycle the old tab, and rebind any watcher."""
        if self._stack:
            old = self._stack[-1]
            self._stack[-1] = tab
        else:
            old = self._page
            self._page = tab
        if old is not None and old is not tab:
            self._recycle_tab(old)
        watch = self._current_watch
        if watch is not None:
            watch._adopt(tab)
        else:
            self._stop_warm_watch(tab)

    def _push_tab(self, watch: Any, *, scroll: bool = False) -> None:
        """Register `watch`, opening another tab in the same window when nested scrolling."""
        pushed = scroll and any(active_scroll for _, _, active_scroll in self._active_watches)
        if pushed:
            _ = self.page
            self._stack.append(self._new_tab())
        self._active_watches.append((watch, pushed, scroll))

    def _pop_tab(self, watch: Any) -> None:
        """Unregister `watch` and recycle any nested tab slot it pushed."""
        for index in range(len(self._active_watches) - 1, -1, -1):
            if self._active_watches[index][0] is watch:
                _, pushed, _ = self._active_watches.pop(index)
                if pushed and self._stack:
                    self._recycle_tab(self._stack.pop())
                return

    def _warm_tabs(self, urls: Iterable[str]) -> None:
        """Queue `urls` and open background tabs in the same window up to `MAX_TABS`."""
        for url in urls:
            if url not in self._warmed and url not in self._pending_warm:
                self._pending_warm.append(url)
        self._fill_warm_tabs()

    def _fill_warm_tabs(self) -> None:
        """Start loading queued warm URLs into idle or new tabs up to `MAX_TABS`."""
        _ = self.page
        while self._pending_warm:
            held = (1 if self._page is not None else 0) + len(self._stack) + len(self._warmed)
            if held >= MAX_TABS:
                return
            url = self._pending_warm.popleft()
            if url in self._warmed:
                continue
            tab = self._new_tab()
            ok, visit = self._start_warm(tab, url)
            if not ok:
                self._recycle_tab(tab)
                continue
            self._warmed[url] = (tab, visit)

    def _start_warm(self, tab: _PacedPage, url: str) -> tuple[bool, browser.Visit | None]:
        """Begin non-blocking navigation on `tab`, returning `(ok, pre_visit)`."""
        raw_goto = getattr(tab.raw, "goto", None)
        if not callable(raw_goto):
            return True, None
        self._start_warm_watch(tab)
        try:
            response: Any = raw_goto(url, wait_until="commit", timeout=15_000)
        # Network errors shouldn't break warming; `goto` falls back to loading normally.
        except Exception:
            self._stop_warm_watch(tab)
            return False, None
        if response is not None and isinstance(getattr(response, "status", None), int):
            return True, browser.Visit(response.status, response.url, dict(response.headers))
        return True, None

    def _start_warm_watch(self, tab: _PacedPage) -> None:
        """Buffer non-asset responses arriving while `tab` loads in the background."""
        self._stop_warm_watch(tab)
        if not callable(getattr(tab.raw, "remove_listener", None)):
            return
        buf: list[tuple[Any, str]] = []

        def collect(response: Any) -> None:
            request = getattr(response, "request", None)
            if getattr(request, "resource_type", "xhr") in ASSET_TYPES:
                return
            try:
                body = response.text()
            except Exception:
                return
            with self._warm_lock:
                buf.append((response, body))

        tab.raw.on("response", collect)
        with self._warm_lock:
            self._warm_buffers[tab] = (buf, collect)

    def _stop_warm_watch(self, tab: Any) -> list[tuple[Any, str]]:
        """Stop buffering background responses on `tab` and return what arrived."""
        with self._warm_lock:
            entry = self._warm_buffers.pop(tab, None)
        if entry is None:
            return []
        buf, collect = entry
        remove = getattr(tab.raw, "remove_listener", None)
        if callable(remove):
            try:
                remove("response", collect)
            except Exception:
                pass
        with self._warm_lock:
            return list(buf)

    def _take_warm_responses(self, page: Any) -> list[tuple[Any, str]]:
        """Stop warming listener on `page` and return its captured responses."""
        return self._stop_warm_watch(page)

    def _visit_tab(
        self,
        page: browser.Page,
        *,
        url: str,
        pre_visit: browser.Visit | None,
        idle: bool,
        timeout: float,
    ) -> browser.Visit | None:
        """Finish waiting for a pre-warmed tab, or navigate `page` normally."""
        if pre_visit is None:
            visit = page.goto(url, idle=idle, timeout=timeout)
            if visit is not None and visit.status == 429:
                raw_wait = visit.headers.get("retry-after") if visit.headers else None
                wait = float(raw_wait) if raw_wait and raw_wait.isdigit() else None
                raise http.RateLimitedError(wait, f"GET {url}: HTTP 429")
            return visit
        try:
            page.wait(idle=idle, timeout=timeout)
        except browser.WaitError as error:
            self.warn("could not open {url}: {error}", url=url, error=error)
            return None
        return pre_visit

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

    def back_off(self, reason: str, *, retry_after: float | None = None) -> None:
        """Count one rate limit and wait it out."""
        with _stoppable():
            seconds = retry_after if retry_after is not None else 5.0
            ui.warn("{target}: {reason}; waiting {time}", target=str(self.target),
                    reason=reason, time=text.duration(round(seconds)))
            if seconds > 0 and not clock.sleep(seconds, stop=self._run.web._stop):
                raise http.StoppedError(f"{self.target}: stopped")

    def _held_pages(self) -> list[_PacedPage]:
        pages = [*self._stack, *self._idle_tabs, *(tab for tab, _ in self._warmed.values())]
        if self._page is not None:
            pages.append(self._page)
        return pages

    def close(self) -> None:
        """Close or release the target's tabs, giving the client the cookies set in them."""
        held = self._held_pages()
        try:
            for tab in held:
                self._stop_warm_watch(tab)
            pool = self._run.settings.browser_pool
            if self._browser is not None:
                self._run.web.raw.cookies.update(net.cookie_jar(self._browser.cookies.get()))
                if pool is None:
                    self._browser.close()
                else:
                    pool.release(tab.raw for tab in held)
        finally:
            self._browser = self._page = None
            self._stack.clear()
            self._idle_tabs.clear()
            self._warmed.clear()
            self._pending_warm.clear()
            self._active_watches.clear()
            with self._warm_lock:
                self._warm_buffers.clear()

    def _fetch_in_page(self, url: str, method: str, headers: Mapping[str, str] | None, soft: bool,
                       request: dict[str, Any]) -> http.Response | None:
        """`fetch(in_page=True)`: one request the tab sends, in the source's pace."""
        options = dict(request)
        params = options.pop("params", None)
        data, body = options.pop("data", None), options.pop("json", None)
        timeout = options.pop("timeout", 30.0)
        if options:
            bad = ", ".join(options)
            raise TypeError(f"fetch(in_page=True) takes params, data, json and timeout, not {bad}")
        if params:
            url = f"{url}{'&' if '?' in url else '?'}{urlencode(params)}"
        page = self.page

        def send() -> http.Response | None:
            return page.fetch(url, method=method, headers=headers, data=data, json=body, soft=soft,
                              timeout=timeout)

        with _fetch_errors():
            return self._run.web._call(url, send, attempts=ATTEMPTS)

    def _connect(self) -> Any:
        """The target's browser, with the source's settings and the login's cookies."""
        if (pool := self._run.settings.browser_pool) is not None:
            return pool.browser_for(self._run.jar)
        source = self._source
        # Visible (open's default), so a person can solve a captcha in it.
        opened = browser.open(stealth=source.stealth)
        opened.cookies.add(self._run.jar)
        return opened


class LoginSite:
    """The engine's `Login` for a source's own login (tlg's), during `flnd login`."""

    def __init__(self, run: SourceRun):
        self._run = run

    @property
    def interactive(self) -> bool:
        """Whether `ask` can ask: `flnd login` in a terminal."""
        return self._run.settings.interactive and prompt.interactive()

    def ask(self, message: str, *, secret: bool = False, description: str = "") -> str:
        """Ask a question with `prompt.ask`, and return what was typed."""
        if not self.interactive:
            raise RuntimeError(f"{self._run.source.code}: logging in needs a terminal to ask in")
        if description:
            ui.info(description)
        return prompt.ask(message, secret=secret)

    @property
    def secrets(self) -> MutableMapping[str, str]:
        """This source's secrets, encrypted in `.flnd/metadata/secrets.db`."""
        return self._run.secrets

    def info(self, message: str, **fields: Any) -> None:
        """Log an info line naming the source."""
        ui.info("{code}: " + message, code=self._run.source.code, **fields)


class _PacedPage(browser.Page):
    """The target's tab: a `Page` whose bottom scrolls wait at least `SCROLL_FLOOR` * speed."""

    def __init__(self, raw: Any, owner: Any, *, speed: Callable[[], float]):
        super().__init__(raw, owner)
        self._speed = speed
        self._scrolling_within = False

    def scroll(
        self, *, until: Callable[[], bool] | None = None,
        within: str | None = None, idle: float = 10.0,
    ) -> int:
        """`Page.scroll` tracking whether scrolling within a container."""
        self._scrolling_within = within is not None
        try:
            return super().scroll(until=until, within=within, idle=idle)
        finally:
            self._scrolling_within = False

    def _pause(self, floor: float) -> None:
        super()._pause(max(floor, SCROLL_FLOOR * self._speed()))

    def _move(self, target: Any) -> None:
        if target is not None:
            super()._move(target)
            return
        _, _, _, height = self._area(None)
        self._wheel(height * random.uniform(1.8, 2.6))

    def _wheel(self, distance: float) -> None:
        if self._scrolling_within:
            super()._wheel(distance)
            return
        self._steady(lambda: self.raw.evaluate(SCROLL_BY, distance), None, "scroll the page")


@contextmanager
def shared_browser(settings: RunShared) -> Iterator[None]:
    """Share one browser window across every source and target running under `settings`."""
    if settings.browser_pool is not None:
        yield
        return
    pool = _BrowserPool()
    settings.browser_pool = pool
    try:
        yield
    finally:
        settings.browser_pool = None
        pool.close()


def _wrap_raw(value: Any, call: Callable[..., Any]) -> Any:
    if type(value).__module__.startswith("playwright."):
        return _RawProxy(value, call)
    return value


def _unwrap_raw(value: Any) -> Any:
    if isinstance(value, _RawProxy):
        return object.__getattribute__(value, "_obj")
    return value


class _RawProxy:
    """Forward Playwright page, locator and mouse calls onto `_BrowserPool`'s thread."""

    def __init__(self, obj: Any, call: Callable[..., Any]):
        object.__setattr__(self, "_obj", obj)
        object.__setattr__(self, "_call", call)

    def __getattr__(self, name: str) -> Any:
        if name == "wait_for_timeout" and callable(
            self._call(getattr, self._obj, "wait_for_timeout", None)
        ):
            return self._wait_for_timeout
        value = self._call(getattr, self._obj, name)
        if callable(value):
            return lambda *args, **kwargs: _wrap_raw(
                self._call(value, *args, **kwargs), self._call,
            )
        return _wrap_raw(value, self._call)

    def _wait_for_timeout(self, ms: float) -> Any:
        if ms > 2:
            clock.sleep((ms - 2) / 1000.0)
        return self._call(lambda n: self._obj.wait_for_timeout(n), min(ms, 2))


class _SharedCookies:
    """Cookie view over a shared `_BrowserPool` matching `browser.BrowserCookies`."""

    def __init__(self, pool: _BrowserPool, jar: Any):
        self._pool = pool
        self._jar = jar

    def get(self) -> Any:
        """Current cookies of the shared browser."""
        return self._pool.cookies()

    def add(self, jar: Any) -> None:
        """Merge `jar` into the shared browser context."""
        self._pool.add_cookies(jar)

    def clear(self) -> None:
        """Clear cookies on the shared browser."""


class _SharedBrowser:
    """Browser view over a shared `_BrowserPool` for one source's cookies."""

    def __init__(self, pool: _BrowserPool, jar: Any):
        self._pool = pool
        self._jar = jar
        self.cookies = _SharedCookies(pool, jar)

    @property
    def _headless(self) -> bool:
        return self._pool.headless()

    def open(self) -> Any:
        """Open or reuse a tab in the shared browser window."""
        return self._pool.open_tab(self._jar, self)

    def close(self) -> None:
        """Leave the shared window open until `shared_browser` exits."""


class _BrowserPool:
    """One browser window and Playwright thread shared across every source in a run."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._executor: concurrent.futures.ThreadPoolExecutor | None = None
        self._thread_id: int | None = None
        self._browser: browser.Browser | None = None
        self._idle: deque[Any] = deque()
        self._jars: list[Any] = []

    def browser_for(self, jar: Any) -> _SharedBrowser:
        return _SharedBrowser(self, jar)

    def _call(self, fn: Callable[..., T], *args: Any, **kwargs: Any) -> T:
        if self._thread_id == threading.get_ident():
            return fn(*args, **kwargs)
        with self._lock:
            if self._executor is None:
                self._executor = concurrent.futures.ThreadPoolExecutor(
                    max_workers=1, thread_name_prefix="flnd-browser",
                )
                self._thread_id = self._executor.submit(threading.get_ident).result()
            executor = self._executor
        return executor.submit(fn, *args, **kwargs).result()

    def _ensure_browser(self, jar: Any) -> browser.Browser:
        if self._browser is None:
            self._browser = browser.open(stealth=True)
        if not any(seen is jar for seen in self._jars):
            self._browser.cookies.add(jar)
            self._jars.append(jar)
        return self._browser

    def open_tab(self, jar: Any, owner: _SharedBrowser) -> Any:
        """Open or reuse a tab on the pool thread and return a proxied page handle."""
        def acquire() -> Any:
            chrome = self._ensure_browser(jar)
            raw = self._idle.popleft() if self._idle else chrome.open().raw
            return SimpleNamespace(raw=_RawProxy(raw, self._call), browser=owner)

        return self._call(acquire)

    def release(self, raws: Iterable[Any]) -> None:
        """Keep at most one idle tab so the window stays open, closing extra tabs."""
        if self._browser is None:
            return
        targets = list(raws)

        def drop() -> None:
            seen: set[int] = set()
            for proxied in targets:
                raw = _unwrap_raw(proxied)
                if id(raw) in seen or any(item is raw for item in self._idle):
                    continue
                seen.add(id(raw))
                if not self._idle:
                    self._idle.append(raw)
                elif callable(close := getattr(raw, "close", None)):
                    try:
                        close()
                    except Exception:
                        pass

        self._call(drop)

    def cookies(self) -> Any:
        if self._browser is None:
            return []
        return self._call(lambda: self._browser.cookies.get() if self._browser else [])

    def headless(self) -> bool:
        return bool(getattr(self._browser, "_headless", False))

    def add_cookies(self, jar: Any) -> None:
        if self._browser is None:
            return
        self._call(lambda: self._ensure_browser(jar))

    def close(self) -> None:
        """Close the shared browser window and stop the Playwright thread."""
        try:
            if self._browser is not None:
                self._call(self._browser.close)
        finally:
            self._browser = None
            self._idle.clear()
            self._jars.clear()
            if self._executor is not None:
                self._executor.shutdown(wait=True)
                self._executor = None
                self._thread_id = None


@contextmanager
def _stoppable() -> Iterator[None]:
    """Turn a wait the first Ctrl-C ended into `KeyboardInterrupt`, as the job expects."""
    try:
        yield
    except http.StoppedError:
        raise KeyboardInterrupt from None


@contextmanager
def _fetch_errors() -> Iterator[None]:
    """Turn a request's errors into `FetchError`, keeping a refusal's status and response."""
    try:
        with _stoppable():
            yield
    except http.RefusedError as error:
        raise FetchError(str(error), status=error.status, response=error.response) from None
    except (http.Error, browser.Error) as error:
        raise FetchError(str(error), status=getattr(error, "status", None)) from None


def _warn_expiring(source: Source, jar: Any) -> None:
    """Warn of login cookies (`source.needs`) that have expired or will expire soon."""
    now = clock.wall()
    for cookie in jar:
        expires = cookie.get("expires", -1)
        # -1 (or 0) is a session cookie, which lasts as long as the browser it came from said.
        if (
            cookie.get("name") not in source.needs
            or not isinstance(expires, (int, float))
            or expires <= 0
        ):
            continue
        if expires <= now:
            ui.warn(
                "{code}: the login cookie {name} has expired; run flnd login again",
                code=source.code, name=cookie["name"],
            )
        elif expires - now <= EXPIRY_WARNING:
            ui.warn(
                "{code}: the login cookie {name} expires in {time}; run flnd login again soon",
                code=source.code, name=cookie["name"], time=text.duration(expires - now),
            )


def _names(cookies: httpx.Cookies) -> Mapping[str, str]:
    """A client's cookies as `{name: value}`; last wins for duplicates on different domains."""
    return MappingProxyType({cookie.name: cookie.value or "" for cookie in cookies.jar})
