"""The engine's `Site` and `Login`: what a source's routes, categories and login act through.

A run opens one `SourceRun` per source (`source_run`): login cookies, two `http.Client`s
over one httpx client, and stores. `web` paces the source's requests; `files` downloads files.
Each target gets a `TargetSite` over that run, and `flnd login` gets a `LoginSite`.

- `TargetSite.fetch` performs paced HTTP requests, raising `FetchError` on failure.
- `TargetSite.goto` loads a page in the browser tab within the same pace.
- `TargetSite.page` provides the browser tab, opened on first use.

    with source_run(settings, source) as run:
        site = TargetSite(run, Target("ins", "user", "example_user"))
        profile = site.fetch("https://example.com/api", params={"user": "example_user"}).json
        site.close()
"""

from __future__ import annotations

import asyncio
import dataclasses
import threading
from collections import deque
from contextlib import contextmanager
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import (
    TYPE_CHECKING, Any, Callable, Coroutine, Iterable, Iterator, Mapping, MutableMapping, TypeVar,
)
from urllib.parse import urlencode

import httpx

from flnd.common import auth, net, paths
from flnd.source import FetchError, Range, Source, Target
from shared import browser, clock, fmt, http, kv, log, prompt, theme

if TYPE_CHECKING:
    from shared import cookies

    from flnd.options import RunShared

__all__ = ["LoginSite", "SourceRun", "TargetSite", "source_run"]

T = TypeVar("T")
# Tries in all for a site request, sent by the client or by the tab: a 5xx or a request that
# couldn't be sent may go through next time, after 1, 2 and 4 s.
ATTEMPTS = 4
# The key of the learned speed in a source's `state`.
STATE_KEY = "pace.speed"
# The least seconds between two bottom scrolls at 1x, which the learned speed scales.
SCROLL_FLOOR = 0.05
# Most open tabs kept in a single target browser window when warming or nesting watches.
MAX_TABS = 6
# How soon before a login cookie expires a run warns of it, in seconds: a day, so a scheduled
# `flnd sync` warns the run before the one that meets the login wall.
EXPIRY_WARNING = 24 * 3600


@dataclass
class SourceRun:
    """One source's resources for a whole run: its cookies, HTTP clients, `connect` client, stores.

        with source_run(settings, source) as run:
            run.state["seen"] = True

    Made by `source_run`, which closes it. Shared by every target of the source, one after
    another, and by their downloads, which run on other threads.
    """

    # The source, as one instance for the whole run.
    source: Source
    # What the run shares across sources: flnd's folder, the history, targets.db, the compressor.
    settings: RunShared
    # The login's cookies.
    jar: cookies.Jar
    # The source's own requests, in its pace; `web.raw` holds the cookies the site sets.
    web: http.Client
    # The files' downloads, over the same cookies, each CDN host backing off on its own.
    files: http.Client
    # What the source's `connect` gave, entered once for the run.
    client: Any = None
    _state: kv.Store | None = field(default=None, repr=False)
    _secrets: auth.Vault | None = field(default=None, repr=False)
    _loop: asyncio.AbstractEventLoop | None = field(default=None, repr=False)
    _loop_thread: threading.Thread | None = field(default=None, repr=False)
    _closed: bool = field(default=False, repr=False)

    @property
    def state(self) -> kv.Store:
        """The source's `kv` table in `.flnd/metadata/sources.db`, opened on first use."""
        if self._state is None:
            self._state = kv.open(self.settings.home / paths.SOURCE_STATE, table=self.source.code)
        return self._state

    @property
    def secrets(self) -> auth.Vault:
        """The source's encrypted secrets in `.flnd/metadata/secrets.db`, opened on first use."""
        if self._secrets is None:
            self._secrets = auth.secrets(self.settings.home, self.source.code)
        return self._secrets

    @property
    def speed(self) -> float:
        """The source's learned speed now, from 1 (no throttle seen) to 8."""
        return self.web.speeds.get(self.source.code, 1.0)

    def run_async(self, coroutine: Coroutine[Any, Any, T]) -> T:
        """Run a coroutine on this source's own event loop, started on first use, and wait for it.

        One loop for the whole source, so an SDK client made by an async `connect` is used on the
        loop it belongs to: by the routes and categories (one step at a time, from the job's
        thread) and by every `fetch` download alike.
        """
        if self._loop is None:
            self._loop = asyncio.new_event_loop()
            self._loop_thread = threading.Thread(target=self._loop.run_forever, daemon=True,
                                                 name=f"flnd-{self.source.code}")
            self._loop_thread.start()
        return asyncio.run_coroutine_threadsafe(coroutine, self._loop).result()

    def close(self) -> None:
        """Save the learned speed, then close the client, the stores and the event loop; once."""
        if self._closed:
            return
        self._closed = True
        self.state[STATE_KEY] = self.speed
        self.web.raw.close()
        for store in (self._state, self._secrets):
            if store is not None:
                store.close()
        if self._loop is not None and self._loop_thread is not None:
            self._loop.call_soon_threadsafe(self._loop.stop)
            self._loop_thread.join()
            self._loop.close()


@contextmanager
def source_run(settings: RunShared, source: Source) -> Iterator[SourceRun]:
    """Open one source's resources for a run, and close them however the block ends.

        with source_run(settings, Instagram()) as run: ...

    Args:
        settings: the run's shared targets.
        source: the source, one instance for the run.
    """
    jar = auth.load(settings.home, source.code)
    _warn_expiring(source, jar)
    declared = source.pace if isinstance(source.pace, http.Pace) else http.Pace(float(source.pace))
    pace = dataclasses.replace(declared, domains=source.domains, label=source.code)

    def sign(request: httpx.Request) -> None:
        # A CDN never sees the source's headers or signature: its own domains only.
        if pace.paces(request.url.host):
            signed = source.prepare(request, _names(raw.cookies))
            if signed is not request:
                request.headers.update(signed.headers)

    raw = net.client(user_agent=source.user_agent, jar=jar, sign=sign)
    state = kv.open(settings.home / paths.SOURCE_STATE, table=source.code)
    saved = state.get(STATE_KEY)
    speeds = {source.code: float(saved)} if isinstance(saved, (int, float)) else {}
    # The files' client labels every host with the source's code, so its messages name the site.
    run = SourceRun(source, settings, jar,
                    http.Client(raw, pace=pace, speeds=speeds, stop=net.stopping),
                    http.Client(raw, pace=http.Pace(label=source.code), stop=net.stopping),
                    _state=state)
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
        self._browser: browser.Browser | None = None
        self._page: _PacedPage | None = None
        self._stack: list[_PacedPage] = []
        self._idle_tabs: deque[_PacedPage] = deque()
        self._warmed: dict[str, tuple[_PacedPage, browser.Visit | None]] = {}
        self._pending_warm: deque[str] = deque()
        self._active_watches: list[tuple[Any, bool, bool]] = []
        # Any URL on the source's own domains names its pace, for a throttle seen on a page.
        self._home = f"https://{run.source.domains[0]}/"

    @property
    def cookies(self) -> Mapping[str, str]:
        """See `flnd.source.Site.cookies`."""
        merged = {cookie["name"]: cookie["value"] for cookie in self._run.jar}
        merged.update(_names(self._run.web.raw.cookies))
        if self._browser is not None:
            merged.update({cookie["name"]: cookie["value"] for cookie in self._browser.cookies})
        return MappingProxyType(merged)

    def fetch(self, url: str, *, method: str = "GET", headers: Mapping[str, str] | None = None,
              in_page: bool = False, **request: Any) -> http.Response:
        """Make one request, paced and retried.

        See `flnd.source.Site.fetch`.
        """
        response = self._fetch(url, method=method, headers=headers, soft=False, in_page=in_page,
                               request=request)
        assert response is not None
        return response

    def try_fetch(self, url: str, *, method: str = "GET", headers: Mapping[str, str] | None = None,
                  in_page: bool = False, **request: Any) -> http.Response | None:
        """Like `fetch`, returning None for a 401, 403 or 404.

        See `flnd.source.Site.try_fetch`.
        """
        return self._fetch(url, method=method, headers=headers, soft=True, in_page=in_page,
                           request=request)

    def _fetch(self, url: str, *, method: str, headers: Mapping[str, str] | None, soft: bool,
               in_page: bool, request: dict[str, Any]) -> http.Response | None:
        """Send one request via `Page.fetch` or `http.Client.fetch`, paced and retried."""
        if in_page:
            return self._fetch_in_page(url, method, headers, soft, request)
        if self._browser is not None:
            # Cookies set in the browser reach the client too, as a logged-in page would send them.
            self._run.web.raw.cookies.update(net.cookie_jar(self._browser.cookies))
        options = dict(request)
        redirects = options.pop("follow_redirects", None)
        unknown = options.keys() - {"params", "data", "json", "timeout"}
        if unknown:
            raise TypeError(f"fetch takes params, data, json, timeout and follow_redirects, "
                            f"not {', '.join(sorted(unknown))}")
        with _fetch_errors():
            return self._run.web.fetch(url, method=method, headers=headers, soft=soft,
                                       redirects=redirects, attempts=ATTEMPTS, **options)

    def goto(self, url: str, *, idle: bool = False, timeout: float = 30.0) -> bool:
        """Load `url` in the target's tab, paced.

        See `flnd.source.Site.goto`.
        """
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
            visit = self._run.web.call(
                url,
                lambda: self._visit_tab(page, url=url, pre_visit=pre_visit,
                                        idle=idle, timeout=timeout),
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
        return _PacedPage(opened.raw, opened.browser, speed=lambda: self._run.speed)

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
        try:
            response: Any = raw_goto(url, wait_until="commit", timeout=15_000)
        # Network errors shouldn't break warming; `goto` falls back to loading normally.
        except Exception:
            return False, None
        if response is not None and isinstance(getattr(response, "status", None), int):
            return True, browser.Visit(response.status, response.url, dict(response.headers))
        return True, None

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
            return page.goto(url, idle=idle, timeout=timeout)
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
        """Log an info line naming the target.

        See `flnd.source.Site.info`.
        """
        # Target's name is put in front of the message as a field so text cannot break format.
        log.info("{target}: " + message, target=str(self.target), **fields)

    def warn(self, message: str, **fields: Any) -> None:
        """Log a warning line naming the target.

        See `flnd.source.Site.warn`.
        """
        log.warn("{target}: " + message, target=str(self.target), **fields)

    def back_off(self, reason: str, *, retry_after: float | None = None) -> None:
        """Count one rate limit and wait it out.

        See `flnd.source.Site.back_off`.
        """
        with _stoppable():
            self._run.web.back_off(self._home, reason=reason, retry_after=retry_after)

    def close(self) -> None:
        """Close the target's browser, giving the client the cookies the site set in it."""
        if self._browser is not None:
            self._run.web.raw.cookies.update(net.cookie_jar(self._browser.cookies))
            self._browser.close()
        self._browser = self._page = None
        self._stack.clear()
        self._idle_tabs.clear()
        self._warmed.clear()
        self._pending_warm.clear()
        self._active_watches.clear()

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
            return self._run.web.call(url, send, attempts=ATTEMPTS)

    def _connect(self) -> browser.Browser:
        """The target's browser, with the source's settings and the login's cookies."""
        source = self._source
        # Visible (open's default), so a person can solve a captcha in it; no locale or timezone, so
        # the browser keeps the computer's own, which match where the connection comes from. The
        # engine asked about downloading Chromium before the run started, when Chrome was missing.
        opened = browser.open(stealth=source.stealth)
        opened.add_cookies(self._run.jar)
        return opened


class LoginSite:
    """The engine's `Login` for a source's own login (tlg's), during `flnd login`.

        Telegram().login(LoginSite(run), "phone")
    """

    def __init__(self, run: SourceRun):
        self._run = run

    @property
    def interactive(self) -> bool:
        """Whether `ask` can ask: `flnd login` in a terminal."""
        return self._run.settings.interactive and theme.screen.interactive

    def ask(self, message: str, *, secret: bool = False, description: str = "") -> str:
        """Ask a question with `prompt.ask`, and return what was typed.

            code = login.ask("Code", description="Sent to your phone")

        Args:
            message: the question.
            secret: hide what is typed, as for a password; the done line shows only the message.
            description: more lines under the question.

        Raises:
            RuntimeError: not in a terminal, where nobody could answer.
        """
        if not self.interactive:
            raise RuntimeError(f"{self._run.source.code}: logging in needs a terminal to ask in")
        return prompt.ask(message, secret=secret, description=description)

    @property
    def secrets(self) -> MutableMapping[str, str]:
        """This source's secrets, encrypted in `.flnd/metadata/secrets.db`."""
        return self._run.secrets

    def info(self, message: str, **fields: Any) -> None:
        """A `log.info` line naming the source, the way `TargetSite.info` names a target.

        Args:
            message: the text, with `{name}` fields filled in from `fields`.
            **fields: the values the message's fields take.
        """
        log.info("{code}: " + message, code=self._run.source.code, **fields)


class _PacedPage(browser.Page):
    """The target's tab: a `Page` whose bottom scrolls wait at least `SCROLL_FLOOR` * speed."""

    def __init__(self, raw: Any, owner: browser.Browser, *, speed: Callable[[], float]):
        super().__init__(raw, owner)
        self._speed = speed

    def scroll(
        self, *, within: Any = None, until: Callable[[], bool] | None = None,
        count: Callable[[], int] | None = None, idle: float = 10.0, wait: float = 3.0,
        pause: float = 0.05, on_step: Callable[[], Any] | None = None,
    ) -> int:
        """`Page.scroll`, its `pause` raised to the source's scroll floor; see `Page.scroll`."""
        return super().scroll(within=within, until=until, count=count, idle=idle, wait=wait,
                              pause=max(pause, SCROLL_FLOOR * self._speed()), on_step=on_step)


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
            log.warn(
                "{code}: the login cookie {name} has expired; run flnd login again",
                code=source.code, name=cookie["name"],
            )
        elif expires - now <= EXPIRY_WARNING:
            log.warn(
                "{code}: the login cookie {name} expires in {time}; run flnd login again soon",
                code=source.code, name=cookie["name"], time=fmt.duration(expires - now),
            )


def _names(cookies: httpx.Cookies) -> Mapping[str, str]:
    """A client's cookies as `{name: value}`; last wins for duplicates on different domains."""
    return MappingProxyType({cookie.name: cookie.value or "" for cookie in cookies.jar})
