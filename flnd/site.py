"""The engine's `Site` and `Login`: what a source's routes, categories and login act through.

- `source_run` opens one source's resources for a run (`SourceRun`): its cookies, its HTTP
  clients, its store, and the run's loop that its async code runs on.
- `TargetSite` is the `Site` a route or category gets for one target. `fetch` goes through the
  source's paced client. The browser verbs (`open`, `wait`, `scroll`, `feed`, `document`,
  `evaluate`, `click`) each hand one call to the run's loop, where `flnd._browser` drives the tab.
- `shared_browser` gives a run its loop and its one browser (`flnd._browser.RunBrowser`), shared
  by every source and target, and closes both when the run ends.

Threads: a `TargetSite` is used on its target's listing thread, or, in an `async def` category,
on the loop, where only `site.tab` (awaited) reaches the browser.
"""

from __future__ import annotations

import functools
import threading
from contextlib import contextmanager
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import (
    TYPE_CHECKING, Any, Callable, Coroutine, Iterable, Iterator, Mapping, MutableMapping, TypeVar,
)

import httpx

from flnd.common import net
from flnd.errors import Blocked, SourceError, Stopped, UsageError
from flnd.source import Range, Source, Target
from flnd import _browser, _home, _http, _loop, _prompt, _text, _time, _vault
from flnd._ui import ui

if TYPE_CHECKING:
    from flnd import _cookies
    from flnd._run import Stop
    from flnd._html import Selection

    from flnd.options import RunShared

__all__ = ["LoginSite", "SourceRun", "TargetSite", "shared_browser", "source_run"]

T = TypeVar("T")
# Tries in all for a site request, sent by the client or by the tab: a 5xx or a request that
# couldn't be sent may go through next time, after about 1, 2 and 4 s.
ATTEMPTS = 4
# How soon before a login cookie expires a run warns of it, in seconds: a day, so a scheduled
# `flnd sync` warns the run before the one that meets the login wall.
EXPIRY_WARNING = 24 * 3600


@dataclass
class SourceRun:
    """One source's resources for a run: cookies, HTTP clients, `connect` client, stores, loop."""

    # The source, as one instance for the whole run.
    source: Source
    # What the run shares across sources: the home, its database, the stop flag.
    settings: RunShared
    # The login's cookies.
    jar: _cookies.Jar
    # The source's own requests, in its pace; `web.raw` holds the cookies the site sets.
    web: _http.Client
    # The files' downloads, over the same cookies, each CDN host backing off on its own.
    files: _http.Client
    # What the source's `connect` gave, entered once for the run.
    client: Any = None
    _state: _home.Home | None = field(default=None, repr=False)
    _secrets: _vault.Vault | None = field(default=None, repr=False)
    # A loop of the source's own, made only outside a run (`shared_browser` gives the run's).
    _own_loop: _loop.Loop | None = field(default=None, repr=False)
    _closed: bool = field(default=False, repr=False)

    @property
    def loop(self) -> _loop.Loop:
        """The run's loop, where the browser and async code run; the source's own outside a run."""
        shared = self.settings.browser_pool
        if shared is not None:
            return shared.loop
        if self._own_loop is None:
            self._own_loop = _loop.Loop()
        return self._own_loop

    @property
    def state(self) -> _home.State:
        """The source's own values in the home's `flnd.db`: the run's, or one opened here."""
        if self.settings.db is not None:
            return self.settings.db.state(self.source.code)
        if self._state is None:
            self._state = _home.open(self.settings.home)
        return self._state.state(self.source.code)

    @property
    def secrets(self) -> _vault.Vault:
        """The source's encrypted secrets in the home's `secrets.db`, opened on first use."""
        if self._secrets is None:
            self._secrets = _home.secrets(self.settings.home, self.source.code)
        return self._secrets

    def run_async(self, coroutine: Coroutine[Any, Any, T]) -> T:
        """Run a coroutine on `loop`, wait for it, and return its value."""
        return self.loop.run(coroutine)

    def close(self) -> None:
        """Close the client, stores and the source's own loop; once."""
        if self._closed:
            return
        self._closed = True
        try:
            self.web.raw.close()
        finally:
            for db in (self._state, self._secrets):
                if db is not None:
                    db.close()
            if self._own_loop is not None:
                self._own_loop.close()


def _make_client(raw: httpx.Client, *, rate: float | None,
                 stop: threading.Event | None) -> _http.Client:
    """An `_http.Client` over the source's `raw` client, in its pace, trying `ATTEMPTS` times."""
    client = _http.Client(rate=rate, retry=_http.Retry(ATTEMPTS), stop=stop)
    client._raw.close()
    client._raw = raw
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
    jar = _home.load(settings.home, source.code)
    _warn_expiring(source, jar)
    rate = 1.0 / source.pace if source.pace > 0 else None

    def sign(request: httpx.Request) -> None:
        if _matches_domain(source, request.url.host):
            signed = source.prepare(request, _names(raw.cookies))
            if signed is not request:
                request.headers.update(signed.headers)

    raw = net.client(user_agent=source.user_agent, jar=jar, sign=sign)
    stop: Stop | None = getattr(settings, "stop", None)
    run = SourceRun(
        source, settings, jar,
        _make_client(raw, rate=rate, stop=stop.soon if stop else None),
        _make_client(raw, rate=None, stop=stop.now if stop else None),
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

    Used on the target's listing thread. Each browser verb makes one call to the run's loop; the
    tabs are the target's share of the run's browser (`flnd._browser.Tabs`), opened on first use.
    `close()` gives them back, and closes a browser the site launched for itself outside a run.
    """

    def __init__(self, run: SourceRun, target: Target, *, time: Range = Range()):
        self.target, self.time = target, time
        self._run = run
        self._source = run.source
        # The run's browser; outside a run, one of the site's own, made on first use.
        self._shared: _browser.RunBrowser | None = run.settings.browser_pool
        self._own = False
        self._tabs: _browser.Tabs | None = None

    # -- requests -------------------------------------------------------------------------------

    @property
    def cookies(self) -> Mapping[str, str]:
        """See `flnd.source.Site.cookies`."""
        merged = {cookie["name"]: cookie["value"] for cookie in self._run.jar}
        merged.update(_names(self._run.web.raw.cookies))
        merged.update({cookie["name"]: cookie["value"] for cookie in self._browser_cookies()})
        return MappingProxyType(merged)

    def fetch(self, url: str, *, method: str = "GET", headers: Mapping[str, str] | None = None,
              check: bool = True, in_page: bool = False, **request: Any) -> _http.Response:
        """Make one request, paced and retried."""
        options = dict(request)
        url = _with_query(url, options.pop("params", None))
        body = options.pop("body", None)
        if options:
            raise SourceError(f"fetch takes params and body, not {', '.join(sorted(options))}")
        if in_page:
            tabs = self._verbs()

            def send() -> _http.Response:
                # A new coroutine for each try: the client may send it again.
                return tabs.run(tabs.page.fetch(url, method=method, headers=headers, body=body))
        else:
            # Cookies set in the browser reach the client too, as a logged-in page sends them.
            if cookies := self._browser_cookies():
                self._run.web.raw.cookies.update(net.cookie_jar(cookies))
            send = functools.partial(self._run.web._send, method.upper(), url, headers, body)
        # The client raises flnd's errors as they are (`flnd._http`): a refusal is a `SiteError`
        # with its status and response, no answer a `NetworkError`, and a stop `Stopped`.
        return self._run.web._call(url, send, method=method.upper(), check=check)

    # -- the browser ----------------------------------------------------------------------------

    def open(self, url: str, *, idle: bool = False, timeout: float = 30.0) -> bool:
        """Load `url` in the target's tab; see `flnd.source.Site.open`.

        A warm tab already loading `url` (`flnd._browser.Tabs.warm`) becomes the target's tab,
        and only its load is waited for: its request went out when it was warmed, so it isn't
        paced again. Any other load waits its turn under the source's pace, through the source's
        client, which waits out a 429 and tries again.

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
            except _browser.WaitError as error:
                self.warn("could not open {url}: {error}", url=url, error=error)
                return False
            visit: _browser.Visit | None = first
        else:
            visit = self._run.web._call(url, lambda: self._visit(tabs, url, idle, timeout))
        if visit is None:
            return False
        if visit.status >= 400:
            self.warn("{url}: HTTP {status}", url=url, status=visit.status)
            return False
        return True

    def _visit(self, tabs: _browser.Tabs, url: str, idle: bool,
               timeout: float) -> _browser.Visit | None:
        """Load `url` in the target's tab: one try, which the source's client paces and repeats.

        Args:
            tabs: the target's tabs.
            url: the page.
            idle: also wait for the network to go quiet.
            timeout: seconds before the page counts as not loaded.

        Returns:
            Where it landed, or None when it didn't load.

        Raises:
            Blocked: the page answered 429, with its `Retry-After`, so the client waits it out.
        """
        visit = tabs.run(tabs.page.goto(url, idle=idle, timeout=timeout))
        if visit is not None and visit.status == 429:
            raw_wait = visit.headers.get("retry-after")
            wait = float(raw_wait) if raw_wait and raw_wait.isdigit() else None
            raise Blocked(f"GET {_text.redact(url)}: HTTP 429", wait=wait, status=429)
        return visit

    @property
    def url(self) -> str:
        """See `flnd.source.Site.url`."""
        if self._tabs is None or not self._tabs.opened:
            return "about:blank"
        return self._tabs.page.url

    def wait(self, css: str | None = None, *, url: _browser.Match | None = None,
             idle: bool = False, timeout: float = 30.0) -> None:
        """See `flnd.source.Site.wait`."""
        tabs = self._verbs()
        tabs.run(tabs.page.wait(css, url=url, idle=idle, timeout=timeout))

    def scroll(self, *, until: Callable[[], bool] | None = None,
               idle: float = _browser.IDLE) -> int:
        """Scroll the target's tab until its feed ends or `until()`; see `flnd.source.Site.scroll`.

        It scrolls in the source's `scroll` style, as its feeds do.

        Args:
            until: asked here, on the listing thread, before each scroll; True stops.
            idle: seconds the bottom may stay quiet, after a whole wait for more brought nothing,
                before the feed counts as ended.

        Returns:
            How many scrolls it made.
        """
        tabs = self._verbs()
        scroller = tabs.run(tabs.page.scroller(idle=idle, style=tabs.scroll))
        # One step per call to the loop, with `until` asked here, on the listing thread.
        while not (until is not None and until()) and tabs.run(scroller.step()):
            pass
        return scroller.scrolls

    def feed(self, match: _browser.Match, *, scroll: bool = False, lines: bool = False,
             cursor: _browser.Reader | None = None) -> _browser.Feed:
        """See `flnd.source.Site.feed`; it scrolls in the source's `scroll` style."""
        return _browser.Feed(self._verbs(), match, scroll=scroll, lines=lines, cursor=cursor)

    def document(self) -> Selection:
        """The tab's page now, parsed; see `flnd.source.Site.document`.

        Returns:
            The page as a `Selection`, parsed here, on the listing thread, not on the loop.
        """
        from flnd._html import parse

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
                                       stop=self._run.web._stop)

    @property
    async def tab(self) -> Any:
        """See `flnd.source.Site.tab`."""
        return (await self._browser().current()).raw

    def _warm_tabs(self, urls: Iterable[str]) -> None:
        """Start loading `urls` in background tabs; see `flnd.sources.common.feed.warm_tabs`."""
        self._verbs().warm(urls)

    def _verbs(self) -> _browser.Tabs:
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
        return self._browser()

    def _browser(self) -> _browser.Tabs:
        """The target's tabs, made on first use.

        Returns:
            The target's `Tabs` in the run's browser, or, outside a run, in a browser of the
            site's own, which `close` closes. They scroll in the source's `scroll` style.
        """
        if self._tabs is None:
            if self._shared is None:
                self._shared = _browser.RunBrowser(self._run.loop, stealth=self._source.stealth)
                self._own = True
            # The source's own `scroll` until the config file can set one per source.
            self._tabs = _browser.Tabs(self._shared, self._run.jar, scroll=self._source.scroll)
        return self._tabs

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

    def back_off(self, reason: str, *, retry_after: float | None = None) -> None:
        """Count one rate limit and wait it out."""
        seconds = retry_after if retry_after is not None else 5.0
        ui.warn("{target}: {reason}; waiting {time}", target=str(self.target),
                reason=reason, time=_text.duration(round(seconds)))
        self.pause(seconds, reason)

    def pause(self, seconds: float, reason: str) -> None:
        """See `flnd.source.Site.pause`."""
        if seconds > 0 and not _time.sleep(seconds, stop=self._run.web._stop):
            raise Stopped(f"{self.target}: stopped while waiting: {reason}")

    def close(self) -> None:
        """Give back the target's tabs, handing the client the cookies set in them."""
        tabs, cookies = self._tabs, self._browser_cookies()
        self._tabs = None
        try:
            if cookies:
                self._run.web.raw.cookies.update(net.cookie_jar(cookies))
            if tabs is not None:
                tabs.close()
        finally:
            if self._own and self._shared is not None:
                self._shared.close()
                self._shared, self._own = None, False


class LoginSite:
    """The engine's `Login` for a source's own login (tlg's), during `flnd login`."""

    def __init__(self, run: SourceRun):
        self._run = run

    @property
    def interactive(self) -> bool:
        """Whether `ask` can ask: `flnd login` in a terminal."""
        return self._run.settings.interactive and _prompt.interactive()

    def ask(self, message: str, *, secret: bool = False, description: str = "") -> str:
        """Ask a question in the terminal (`flnd._prompt.ask`), and return what was typed."""
        if not self.interactive:
            raise UsageError(f"{self._run.source.code}: logging in needs a terminal to ask in",
                             hint=f"run flnd login {self._run.source.code} in a terminal")
        if description:
            ui.info(description)
        return _prompt.ask(message, secret=secret)

    @property
    def secrets(self) -> MutableMapping[str, str]:
        """This source's secrets, encrypted in the home's `secrets.db`."""
        return self._run.secrets

    def info(self, message: str, **fields: Any) -> None:
        """Log an info line naming the source."""
        ui.info("{code}: " + message, code=self._run.source.code, **fields)


@contextmanager
def shared_browser(settings: RunShared) -> Iterator[None]:
    """Give a run its loop and its one browser, shared by every source and target; then close both.

        with shared_browser(settings):
            ...                     # settings.browser_pool is the run's `RunBrowser`

    Args:
        settings: the run's; `browser_pool` holds the `flnd._browser.RunBrowser` while it runs.

    Entered again inside a run, it changes nothing. The browser launches only when a target first
    asks for a tab, so a run that never needs one never starts it.
    """
    if settings.browser_pool is not None:
        yield
        return
    loop = _loop.Loop()
    shared = _browser.RunBrowser(loop)
    settings.browser_pool = shared
    try:
        yield
    finally:
        settings.browser_pool = None
        try:
            shared.close()
        finally:
            loop.close()


def _with_query(url: str, params: Mapping[str, Any] | None) -> str:
    """`url` with `params` added to its query string."""
    return str(httpx.URL(url).copy_merge_params(params)) if params else url


def _warn_expiring(source: Source, jar: Any) -> None:
    """Warn of login cookies (`source.needs`) that have expired or will expire soon."""
    now = _time.wall()
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
                "{code}: the login cookie {name} has expired; run flnd login {code} again",
                code=source.code, name=cookie["name"],
            )
        elif expires - now <= EXPIRY_WARNING:
            ui.warn(
                "{code}: the login cookie {name} expires in {time}; "
                "run flnd login {code} again soon",
                code=source.code, name=cookie["name"], time=_text.duration(expires - now),
            )


def _names(cookies: httpx.Cookies) -> Mapping[str, str]:
    """A client's cookies as `{name: value}`; last wins for duplicates on different domains."""
    return MappingProxyType({cookie.name: cookie.value or "" for cookie in cookies.jar})
