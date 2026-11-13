"""The engine's `Site` and `Login`: what a source's routes, categories and login act through.

How it works: a run opens one `SourceRun` per source (`source_run`): its login's cookies, two
`http.Client`s over one httpx client that holds them, and its stores. `web` paces the source's own
requests: its `domains` share one pace named by the source's code (its floor and budgets, a
learned speed saved in its `state` between runs, and one back-off), and the source's `prepare`
signs each request to them. `files` downloads, with a back-off of its own, so a CDN's 429 never
slows the listing. Each target then gets a `TargetSite` over that run, and `flnd
login` a `LoginSite`.

- `TargetSite.fetch` is `web.fetch`, its refusals turned into `FetchError`; `in_page=True` sends
  it from the target's tab instead (`Page.fetch`), in the same pace.
- `TargetSite.goto` loads a page in the target's tab in the same pace; a 429 is waited out and the
  page loaded again, and another status of 400 or more warns and returns False.
- `TargetSite.back_off` counts a throttle on the source's pace and waits it out.
- `TargetSite.page` is the target's tab, opened on first use. Its scrolls are spaced by at least
  0.3 s times the learned speed.
- The first Ctrl-C (`net.stopping`) ends any wait at once with `KeyboardInterrupt`.

    with source_run(settings, source) as run:
        site = TargetSite(run, Target("ins", "user", "example_user"))
        profile = site.fetch("https://www.instagram.com/api/v1/users/web_profile_info/",
                             params={"username": "example_user"}).json
        site.close()

The job (flnd/job.py) drives a target; this module only acts for it. A source never imports it.
"""

from __future__ import annotations

import asyncio
import dataclasses
import threading
from contextlib import contextmanager
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import (TYPE_CHECKING, Any, Callable, Coroutine, Iterator, Literal, Mapping,
                    MutableMapping, TypeVar, overload)
from urllib.parse import urlencode

import httpx

from flnd.common import auth, files, net
from flnd.source import FetchError, Range, Source, Target
from shared import browser, clock, fmt, http, kv, log, prompt, theme

if TYPE_CHECKING:
    from shared import cookies

    from flnd.session import Settings

__all__ = ["LoginSite", "SourceRun", "TargetSite", "source_run"]

T = TypeVar("T")
# Tries in all for a site request, sent by the client or by the tab: a 5xx or a request that
# couldn't be sent may go through next time, after 1, 2 and 4 s.
ATTEMPTS = 4
# The key of the learned speed in a source's `state`.
STATE_KEY = "pace.speed"
# The least seconds between two scrolls at 1x, which the learned speed scales.
SCROLL_FLOOR = 0.3
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
    # What the run shares across sources: flnd's folder, the options, the history, the compressor.
    settings: Settings
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
            self._state = kv.open(self.settings.home / files.SOURCE_STATE, table=self.source.code)
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
def source_run(settings: Settings, source: Source) -> Iterator[SourceRun]:
    """Open one source's resources for a run, and close them however the block ends.

        with source_run(settings, Instagram()) as run: ...

    Args:
        settings: the run's shared settings.
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
    state = kv.open(settings.home / files.SOURCE_STATE, table=source.code)
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

    Used on the job's thread only: the browser is Playwright's sync API, which is single-threaded.
    `close()` closes the browser.
    """

    def __init__(self, run: SourceRun, target: Target):
        self.target = target
        self._run = run
        self._source = run.source
        self._browser: browser.Browser | None = None
        self._page: _PacedPage | None = None
        # Any URL on the source's own domains names its pace, for a throttle seen on a page.
        self._home = f"https://{run.source.domains[0]}/"

    @property
    def cookies(self) -> Mapping[str, str]:
        """The login's cookies as a read-only `{name: value}`."""
        return MappingProxyType({cookie["name"]: cookie["value"] for cookie in self._run.jar})

    @property
    def time(self) -> Range:
        """`--time`, as a `Range`."""
        options = self._run.settings.options
        return Range(options.since, options.until)

    @overload
    def fetch(self, url: str, *, method: str = ..., headers: Mapping[str, str] | None = ...,
              soft: Literal[False] = ..., in_page: bool = ..., **request: Any) -> http.Response: ...
    @overload
    def fetch(self, url: str, *, method: str = ..., headers: Mapping[str, str] | None = ...,
              soft: Literal[True], in_page: bool = ..., **request: Any) -> http.Response | None: ...
    @overload
    def fetch(self, url: str, *, method: str = ..., headers: Mapping[str, str] | None = ...,
              soft: bool, in_page: bool = ..., **request: Any) -> http.Response | None: ...
    def fetch(self, url: str, *, method: str = "GET", headers: Mapping[str, str] | None = None,
              soft: bool = False, in_page: bool = False, **request: Any) -> http.Response | None:
        """Make one request, paced and retried, and return its `http.Response`; see `Site.fetch`.

            data = site.fetch("https://x.com/i/api/graphql/…", params={"variables": "…"}).json

        Args:
            url: the URL to request.
            method: the HTTP method.
            headers: headers on top of the client's for this request; None adds none.
            soft: True returns None for a 401, 403 or 404.
            in_page: send it from the target's tab with `Page.fetch`, which takes `data`, `json`,
                `params` and `timeout`.
            **request: `params`, `data`, `json`, `timeout` and `follow_redirects`.

        Returns:
            The response; None only with `soft`.

        Raises:
            FetchError: no response after the retries, a 4xx other than 429 (with its `status` and
                `response`), or with `in_page`, a request the page couldn't send.
            TypeError: an option it doesn't take.
            KeyboardInterrupt: Ctrl-C during a wait.
        """
        if in_page:
            return self._fetch_in_page(url, method, headers, soft, request)
        if self._browser is not None:
            # Cookies the site set in the browser reach the client too, as a logged-in page would send them.
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
        """Load `url` in the target's tab, paced; see `Site.goto`.

            if not site.goto("https://www.instagram.com/example_user/"):
                return

        Args:
            url: the page to load.
            idle: also wait until the network has been quiet, not only for the `load` event.
            timeout: seconds before the load counts as failed.

        Returns:
            True when it loaded with a status under 400. False when it didn't load (which
            `Page.goto` logs) or answered 400 or more (which is warned of here). A 429 is never
            returned: it is waited out, for the page's `Retry-After`, and the page loaded again,
            as long as it takes.

        Raises:
            KeyboardInterrupt: Ctrl-C during a wait.
        """
        page = self.page
        with _stoppable():
            visit = self._run.web.call(url, lambda: page.goto(url, idle=idle, timeout=timeout))
        if visit is None:
            return False
        if visit.status >= 400:
            self.warn("{url}: HTTP {status}", url=url, status=visit.status)
            return False
        return True

    def handoff(self, *, until: Callable[[], bool], message: str, timeout: float | None = None) -> bool:
        """`Page.handoff` on the target's tab, for a captcha only a person can solve; False on timeout.

        Args:
            until: True once the step is done, polled every second.
            message: what the person should do, shown with a spinner.
            timeout: seconds before giving up; None waits as long as it takes.
        """
        return self.page.handoff(until=until, message=message, timeout=timeout)

    @property
    def page(self) -> browser.Page:
        """The target's tab, opened (with the browser, on first use) blank; see `Site.page`."""
        if self._page is None:
            if self._browser is None:
                self._browser = self._connect()
            opened = self._browser.open()
            self._page = _PacedPage(opened.raw, opened.browser, speed=lambda: self._run.speed)
        return self._page

    @property
    def client(self) -> Any:
        """What the source's `connect` gave, such as an SDK client; None without one."""
        return self._run.client

    @property
    def state(self) -> MutableMapping[str, Any]:
        """A `kv` table for this source, kept between runs in `.flnd/metadata/sources.db`."""
        return self._run.state

    @property
    def secrets(self) -> MutableMapping[str, str]:
        """This source's secrets, encrypted in `.flnd/metadata/secrets.db`."""
        return self._run.secrets

    def info(self, message: str, **fields: Any) -> None:
        """A `log.info` line naming the target: `site.info("{n} stories", n=3)`.

        Args:
            message: the text, with `{name}` fields filled in from `fields`.
            **fields: the values the message's fields take.
        """
        # The target's name is put in front of the message as a field, so its text can't break the format.
        log.info("{target}: " + message, target=str(self.target), **fields)

    def warn(self, message: str, **fields: Any) -> None:
        """A `log.warn` line naming the target, like `info`.

        Args:
            message: the text, with `{name}` fields filled in from `fields`.
            **fields: the values the message's fields take.
        """
        log.warn("{target}: " + message, target=str(self.target), **fields)

    def back_off(self, reason: str, *, retry_after: float | None = None) -> None:
        """Count one rate limit and wait it out; see `Site.back_off`.

        Args:
            reason: what the site did, shown with the countdown.
            retry_after: the seconds the site asked for; None takes the back-off's next step.

        A second call during a wait waits with it and adds nothing; each counted one doubles
        the learned speed. Ctrl-C ends the wait with `KeyboardInterrupt`.
        """
        with _stoppable():
            self._run.web.back_off(self._home, reason=reason, retry_after=retry_after)

    def close(self) -> None:
        """Close the target's browser, giving the client the cookies the site set in it."""
        if self._browser is not None:
            self._run.web.raw.cookies.update(net.cookie_jar(self._browser.cookies))
            self._browser.close()
        self._browser = self._page = None

    def _fetch_in_page(self, url: str, method: str, headers: Mapping[str, str] | None, soft: bool,
                       request: dict[str, Any]) -> http.Response | None:
        """`fetch(in_page=True)`: one request the tab sends, in the source's pace."""
        options = dict(request)
        params = options.pop("params", None)
        data, body, timeout = options.pop("data", None), options.pop("json", None), options.pop("timeout", 30.0)
        if options:
            raise TypeError(f"fetch(in_page=True) takes params, data, json and timeout, not {', '.join(options)}")
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
    """The engine's `Login` for a source's own login (tlg's), during `flnd login`; see `flnd.source.Login`.

        Telegram().login(LoginSite(run), "phone")

    Args:
        run: the source's resources, opened for the login.
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
    """The target's tab: a `Page` whose scrolls are at least 0.3 s times the learned speed apart."""

    def __init__(self, raw: Any, owner: browser.Browser, *, speed: Callable[[], float]):
        super().__init__(raw, owner)
        self._speed = speed

    def scroll(self, *, within: Any = None, until: Callable[[], bool] | None = None,
               count: Callable[[], int] | None = None, idle: float = 10.0, wait: float = 3.0, pause: float = 0.3,
               on_step: Callable[[], Any] | None = None) -> int:
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
    """Warn of each login cookie (`source.needs`) that has expired, or will within `EXPIRY_WARNING`, naming it, never its value."""
    now = clock.wall()
    for cookie in jar:
        expires = cookie.get("expires", -1)
        # -1 (or 0) is a session cookie, which lasts as long as the browser it came from said.
        if cookie.get("name") not in source.needs or not isinstance(expires, (int, float)) or expires <= 0:
            continue
        if expires <= now:
            log.warn("{code}: the login cookie {name} has expired; run flnd login again", code=source.code,
                     name=cookie["name"])
        elif expires - now <= EXPIRY_WARNING:
            log.warn("{code}: the login cookie {name} expires in {time}; run flnd login again soon",
                     code=source.code, name=cookie["name"], time=fmt.duration(expires - now))

def _names(cookies: httpx.Cookies) -> Mapping[str, str]:
    """A client's cookies as `{name: value}`; of two with one name on different domains, the last wins."""
    return MappingProxyType({cookie.name: cookie.value or "" for cookie in cookies.jar})
