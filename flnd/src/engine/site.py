"""The engine's `Site`: what a source's routes, categories and login act through.

This is flnd's side of the source API: it implements `flnd.source.Site` on the libraries. It
holds:
- `source_run` opens one source's resources for a run (`SourceRun`): its cookies, its HTTP
  session and clients (`flnkit.net`), its store, and the run's loop that its async code and every
  transfer run on.
- `TargetSite` is the `Site` a route or category gets for one target. `fetch` hands one request
  to the run's loop, through the source's listing client. The browser verbs (`open`, `wait`,
  `scroll`, `feed`, `document`, `evaluate`, `click`) each hand one call to the run's loop,
  where `flnkit.browser` drives the tab.
- `shared_browser` gives a run its loop and its one browser (`flnkit.browser.RunBrowser`), shared
  by every source and target, and closes both when the run ends.
- `LoginSite` is the `Site` a source's `Login.start` gets during `flnd login`: it can ask in the
  terminal, and has no target.
- `paged` walks a paged JSON listing for `TargetSite.pages`, and `Listing` carries a category's
  saved cursor in and its last cursor out.

Who calls it: the engine (`flnd.engine.core` opens `shared_browser` and one `source_run` per source,
and hands `flnd login` a `LoginSite`), the job (`flnd.engine.job` makes a `TargetSite` per target
and sets its `listing`), the library (`flnd._library` opens a `TargetSite` on a source's account),
and the test kit (`flnd.testing` reuses `paged`). What it calls: the libraries `flnkit.net`
(HTTP), `flnkit.browser` (tabs), `flnkit.loop` (the run's event loop), `flnkit.vault` and
`flnd.engine.home` (secrets, state, cookies), `flnd.ui.forms` and `flnd.ui.log`.

A run, step by step:
1. `shared_browser` makes the run's one event loop and its `RunBrowser`, which launches nothing
   until a target asks for a tab.
2. `source_run` reads the source's `[sources.<code>]` settings from the home's config.toml
   (`flnd.engine.options.config`), loads its login cookies from the home, warns of any about to
   expire, and builds its `net.Session` and two clients over one `net.Limits` (below).
3. Each target gets a `TargetSite`. Its `fetch` and browser verbs run on the target's listing
   thread, and each hands one coroutine to the run's loop and waits for it.
4. A refusal from the source's own domains with 401 or 403 becomes a `LoginError`; every other
   error from `flnkit.net` passes through as it is.
5. When the source's last target is done, the `SourceRun` closes its session, its stores and any
   loop of its own; when the run ends, `shared_browser` closes the browser, then the loop.

Threads: a `TargetSite` is used on its target's listing thread, or, in an `async def` category,
on the loop, where only `site.tab` (awaited) reaches the browser. A `Listing` belongs to the
listing thread too: the job sets it before a category is listed and reads it after. A blocking
call (`fetch`, `open`, …) made from code on the loop is refused, since it would wait for itself.

The network side of a source (src/docs/architecture, "Network"): one `flnkit.net.Session` with
the login's cookies, the source's user agent and its `sign` hook (for requests to its own
domains), and one `flnkit.net.Limits` with two pools:
- *pages*, the listing's requests to the source's own `domains`: a token bucket at the
  source's `pace` (or the config's `pace` for it), which a 429's wait holds and halves;
- *media*, every file download, and the listing's requests to other hosts: a cap per host
  that starts at `MEDIA_CAP` transfers at once and adapts (`flnkit.net.Cap`): it halves when
  the host is overloaded, and grows up to `MEDIA_MOST`, or the `limits` for that host (the
  source's, with the config's over them), while the host's throughput still rises. A site's
  files often come from its own domain (TikTok's CDN hosts), and holding them to the
  listing's pace would serialize them.
Two clients share them: `web` lists (paced), and stops at the first Ctrl-C; `files` downloads
(capped), and stops at the second. A `proxy` in the config carries every request of the
session; the browser, shared by every source, isn't proxied.
"""

from __future__ import annotations

from collections.abc import Callable, Coroutine, Iterable, Iterator, Mapping, MutableMapping
from contextlib import contextmanager, nullcontext
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import (
    TYPE_CHECKING,
    Any,
    TypeVar,
)

from flnd.engine import home
from flnd.engine.options import SourceConfig, config
from flnd.errors import Blocked, LoginError, SiteError, SourceError, Stopped, UsageError
from flnd.source import Range, Request, Source, Target, dig
from flnd.ui import forms
from flnd.ui.log import ui
from flnkit import browser, clock, loop, net, text, vault

if TYPE_CHECKING:
    from flnd.engine.options import RunShared
    from flnd.engine.run import Stop
    from flnkit import cookies
    from flnkit.html import Document

__all__ = ["Listing", "LoginSite", "SourceRun", "TargetSite", "login_names", "paged",
           "shared_browser", "source_run"]

T = TypeVar("T")
# Transfers at once to one media host (a CDN): where its cap starts, and the most it grows to
# while the host's throughput still rises. A source's `limits` sets the most for its hosts.
# 32 is the ceiling the real-CDN check in src/docs/performance ("Concurrency per host") confirms.
MEDIA_CAP = 8
MEDIA_MOST = 32
# How soon before a login cookie expires a run warns of it, in seconds: a day, so a scheduled
# `flnd sync` warns the run before the one that meets the login wall.
EXPIRY_WARNING = 24 * 3600
# What a site's own domains answer for a login wall.
LOGIN_WALLS = frozenset({401, 403})
# `back_off`'s waits for a block a page showed, in seconds, for the first, second and third round
# in a row; a fourth round raises `Blocked` (src/docs/errors). A site's own wait is capped at an
# hour, as a 429's `Retry-After` is.
BACK_OFF = (30.0, 120.0, 480.0)
LONGEST_WAIT = 3600.0
# Seconds without a block after which the next one counts as the first round again.
QUIET = 600.0


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
    _state: home.Home | None = field(default=None, repr=False)
    _secrets: vault.Vault | None = field(default=None, repr=False)
    # A loop of the source's own, made only outside a run (`shared_browser` gives the run's).
    _own_loop: loop.Loop | None = field(default=None, repr=False)
    _closed: bool = field(default=False, repr=False)
    # Whether a target already said that the browser doesn't use the config's proxy.
    _told_proxy: bool = field(default=False, repr=False)

    @property
    def loop(self) -> loop.Loop:
        """The run's loop, where the browser and async code run; the source's own outside a run."""
        shared = self.settings.browser_pool
        if shared is not None:
            return shared.loop
        if self._own_loop is None:
            self._own_loop = loop.Loop()
        return self._own_loop

    @property
    def jobs(self) -> int:
        """The most files of one target downloading at once: as many as one host's cap may
        grow to (`MEDIA_MOST`, or the largest `limits` above it, the source's or the config's),
        so a target never holds a host below its cap. A target's queue holds 4 times as many
        before its listing waits."""
        return max([MEDIA_MOST, *_limits(self.source, self.config).values()])

    def gate(self) -> Any:
        """A place under the cap of the source's first domain, for a file the source fetches
        itself (a `fetch` function, such as Telegram's): `async with run.gate(): ...`."""
        return self.files.limits.slot(self.source.domains[0], self.files.stop, pace=False)

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

    def run_async(self, coroutine: Coroutine[Any, Any, T]) -> T:
        """Run a coroutine on `loop`, wait for it, and return its value."""
        return self.loop.run(coroutine)

    def close(self) -> None:
        """Close the session, stores and the source's own loop; once."""
        if self._closed:
            return
        self._closed = True
        try:
            if self.session.started:
                # On the loop the session's transfers ran on; a loop already closed took them.
                try:
                    self.loop.run(self.session.close())
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
    `[sources.<code>]` settings in the home's config.toml are read here too, and set its pace,
    its hosts' caps and its proxy. Nothing is sent yet: the session connects on its first
    request.

    Args:
        settings: what the run shares across sources: the home, its database, the stop flag
            (whose first Ctrl-C stops the listing client, and the second the files client).
        source: the source, as the one instance the run uses.

    Yields:
        The `SourceRun`, tracked by the run's live stats while the block runs.

    Raises:
        UsageError: the home's config.toml has a value it doesn't take.
    """
    configured = config(settings.home).source(source.code)
    jar = home.load(settings.home, source.code)
    _warn_expiring(source, jar)
    session, limits = _network(source, jar, configured)
    stop: Stop | None = getattr(settings, "stop", None)
    # `run.suspend()` holds every new request of the source, listing and downloads alike.
    limits.suspended = stop.suspended if stop else None
    run = SourceRun(
        source, settings, jar, session,
        net.Client(session, limits=limits, stop=stop.soon if stop else None),
        net.Client(session, limits=limits, stop=stop.now if stop else None, pace=False),
        configured,
    )
    try:
        yield run
    finally:
        run.close()


def _network(source: Source, jar: Iterable[Mapping[str, Any]],
             settings: SourceConfig) -> tuple[net.Session, net.Limits]:
    """A source's session and limits: its pace on its own domains, caps on every other host.

    Args:
        source: the source; its `pace`, `limits`, `user_agent` and `sign` are read.
        jar: the login's cookies.
        settings: the source's config.toml settings, whose `pace`, `limits` and `proxy` win
            over the source's own.

    Returns:
        The session, with the source's `sign` hook on requests to its own domains and the
        config's proxy, and the limits both of its clients share.
    """
    pace = source.pace if settings.pace is None else settings.pace
    rate = 1.0 / pace if pace > 0 else None
    limits = net.Limits(paced=lambda host: _matches_domain(source, host), rate=rate,
                         caps=_limits(source, settings), cap=MEDIA_CAP, most=MEDIA_MOST)

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
    session = net.Session(headers=headers, cookies=jar, prepare=prepare, handles=limits.total,
                           proxy=settings.proxy)
    return session, limits


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

    Used on the target's listing thread. Each browser verb makes one call to the run's loop; the
    tabs are the target's share of the run's browser (`flnkit.browser.Tabs`), opened on first use.
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
        # `back_off`'s rounds in a row, and when the last one ended.
        self._rounds = 0
        self._last_block = 0.0

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

        The request goes through the run's `web` client, so it waits its turn under the
        source's pace and stops at the first Ctrl-C. Cookies the browser has set are added to
        the session first, so a request sends what a logged-in page would. With `in_page`, the
        tab's own `fetch` sends it instead, the client still pacing and retrying it.

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

            def send() -> Any:
                # A new coroutine for each try: the client may send it again.
                return page.fetch(url, method=method, headers=headers, body=body)

            sending = web.call(url, send, method=method.upper(), check=check)
        else:
            # Cookies set in the browser reach the session too, as a logged-in page sends them.
            if cookies := self._browser_cookies():
                self._run.session.add(cookies)
            sending = web.fetch(url, method=method, headers=headers, body=body, check=check)
        # The client raises flnd's errors as they are (`flnkit.net`): a refusal is a `SiteError`
        # with its status and response, no answer a `NetworkError`, and a stop `Stopped`.
        try:
            return self._run.loop.run(sending)
        except SiteError as error:
            # A login wall: the site's own domains refuse the login. A 401 or 403 from another
            # host (a CDN's expired link) stays the `SiteError` it is.
            if (error.status in LOGIN_WALLS and type(error) is SiteError
                    and _matches_domain(self._source, net.URL(url).host)):
                raise LoginError(f"{self._source.code}: the site refused the login "
                                 f"(HTTP {error.status})",
                                 hint=f"run flnd login {self._source.code}",
                                 target=str(self.target)) from error
            raise

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
            except browser.WaitError as error:
                self.warn("could not open {url}: {error}", url=url, error=error)
                return False
            visit: browser.Visit | None = first
        else:
            page = tabs.page
            visit = self._run.loop.run(
                self._run.web.call(url, lambda: self._visit(page, url, idle, timeout)))
        if visit is None:
            return False
        if visit.status >= 400:
            self.warn("{url}: HTTP {status}", url=url, status=visit.status)
            return False
        return True

    async def _visit(self, page: browser.Page, url: str, idle: bool,
                     timeout: float) -> browser.Visit | None:  # noqa: ASYNC109 (Playwright's own)
        """Load `url` in the target's tab: one try, which the source's client paces and repeats.

        Runs on the loop.

        Args:
            page: the target's tab.
            url: the page.
            idle: also wait for the network to go quiet.
            timeout: seconds before the page counts as not loaded.

        Returns:
            Where it landed, or None when it didn't load.

        Raises:
            Blocked: the page answered 429, with its `Retry-After`, so the client waits it out.
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
        if self._tabs is None or not self._tabs.opened:
            return "about:blank"
        return self._tabs.page.url

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

    def feed(self, match: browser.Match, *, scroll: bool = False, lines: bool = False,
             cursor: browser.Reader | None = None) -> browser.Feed:
        """See `flnd.source.Site.feed`; it scrolls in the source's `scroll` style."""
        return browser.Feed(self._verbs(), match, scroll=scroll, lines=lines, cursor=cursor)

    def document(self) -> Document:
        """The tab's page now, parsed; see `flnd.source.Site.document`.

        Returns:
            The page as a `flnkit.html.Document`, parsed on the listing thread, not on the loop.
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
        """Start loading `urls` in background tabs; see `flnd.source.Site.warm`."""
        self._verbs().warm(urls)

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
        return self._browser()

    def _browser(self) -> browser.Tabs:
        """The target's tabs, made on first use.

        Returns:
            The target's `Tabs` in the run's browser, or, outside a run, in a browser of the
            site's own, which `close` closes. They scroll in the config's `scroll` for the
            source, else the source's own.
        """
        if self._tabs is None:
            if self._shared is None:
                self._shared = browser.RunBrowser(self._run.loop, stealth=self._source.stealth,
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
            self._tabs = browser.Tabs(self._shared, self._run.jar, scroll=style)
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

        Args:
            reason: what the site did, shown with the wait.
            retry_after: the seconds the site asked for, capped at `LONGEST_WAIT`; None waits
                `BACK_OFF`'s seconds for this round.

        Raises:
            Blocked: a fourth round in a row (`QUIET` seconds without a block start again).
        """
        if clock.now() - self._last_block > QUIET:
            self._rounds = 0
        self._rounds += 1
        if self._rounds > len(BACK_OFF):
            self._rounds = 0
            raise Blocked(f"{self._source.code}: {reason}, after {len(BACK_OFF)} waits",
                          hint="try again later, or raise the source's pace in config.toml",
                          target=str(self.target))
        rounds = BACK_OFF[self._rounds - 1]
        seconds = min(retry_after, LONGEST_WAIT) if retry_after is not None else rounds
        ui.warn("{target}: {reason}; waiting {time}", target=str(self.target),
                reason=reason, time=text.duration(round(seconds)))
        try:
            self._wait(seconds, reason, count=self._rounds)
        finally:
            # The wait itself isn't quiet time: the next block counts from its end.
            self._last_block = clock.now()

    def pause(self, seconds: float, reason: str) -> None:
        """Wait `seconds`, shown with `reason` as the source's wait in the live stats.

        See `flnd.source.Site.pause`.

        Args:
            seconds: how long; 0 or less doesn't wait.
            reason: why, in the source's words.

        Raises:
            Stopped: the run's first Ctrl-C came while waiting.
        """
        self._wait(seconds, reason, count=1)

    def _wait(self, seconds: float, reason: str, *, count: int) -> None:
        """Wait `seconds`, shown as a hold of the source's listing in the run's snapshot.

        Args:
            seconds: how long; 0 or less doesn't wait.
            reason: why, in the site's or the source's words.
            count: the round of waits in a row this one is, from 1.

        Raises:
            Stopped: the run's first Ctrl-C came while waiting.
        """
        if seconds <= 0:
            return
        tally = self._run.settings.tally
        held = (tally.hold(self._source.code, None, "pages", seconds, reason, count=count)
                if tally is not None else nullcontext())
        with held:
            if not clock.sleep(seconds, stop=self._run.web.stop):
                raise Stopped(f"{self.target}: stopped while waiting: {reason}")

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


@contextmanager
def shared_browser(settings: RunShared) -> Iterator[None]:
    """Give a run its loop and its one browser, shared by every source and target; then close both.

        with shared_browser(settings):
            ...                     # settings.browser_pool is the run's `RunBrowser`

    Entered again inside a run, it changes nothing. The browser launches only when a target first
    asks for a tab, so a run that never needs one never starts it. On the way out the browser is
    closed before the loop it runs on, even when closing it fails.

    Args:
        settings: the run's; `browser_pool` holds the `flnkit.browser.RunBrowser` while it runs.

    Yields:
        Nothing: the browser is reached through `settings.browser_pool`.
    """
    if settings.browser_pool is not None:
        yield
        return
    run_loop = loop.Loop()
    shared = browser.RunBrowser(run_loop, screen=_Screen())
    settings.browser_pool = shared
    try:
        yield
    finally:
        settings.browser_pool = None
        try:
            shared.close()
        finally:
            run_loop.close()


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
