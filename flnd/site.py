"""The engine's `Site` and `Login`: what a source's routes, categories and login act through.

How it works: a run opens one `SourceRun` per source (`source_run`): its login's cookies, one
httpx client whose transport is net.py's one retry layer, its `Backoff`, its `Pacer`, and its
stores. Each target then gets a `TargetSite` over that run, and `flnd login` a `LoginSite`.

- `TargetSite.fetch` sends a request through the client: the source's `prepare` signs it when it
  goes to the source's own `domains` (never a CDN), the transport retries a dead connection or a
  5xx and waits out a 429, and the pacer's `wait_turn` comes before and `record` after, for the
  source's own domains only. `in_page=True` sends it from the target's tab instead (`Page.fetch`).
- `TargetSite.goto` loads a page in the target's tab, paced the same way. A 429 is waited out with
  `back_off` (the page's `Retry-After` when it sent one) and loaded again; another status of 400
  or more warns and returns False.
- `TargetSite.back_off` counts a rate limit on the source's `Backoff`, which shows the countdown
  and slows the pacer down, and waits it out. A second call during the wait adds nothing.
- `TargetSite.page` is the target's tab, opened on first use. Its `scroll` keeps the source's
  pace: the pause between scrolls is the pacer's scroll floor, and each scroll counts as a clean one.
- The pacer's learned speed is loaded from the source's `state` when the run opens, and saved
  there when it closes. A pacing wait over 15 s shows on the target's row.

    with source_run(settings, source) as run:
        site = TargetSite(run, Target("ins", "user", "example_user"), row=row)
        profile = site.fetch("https://www.instagram.com/api/v1/users/web_profile_info/",
                             params={"username": "example_user"}).json
        site.close()

The job (flnd/job.py) drives a target; this module only acts for it. A source never imports it.
"""

from __future__ import annotations

import asyncio
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from types import MappingProxyType, SimpleNamespace
from typing import TYPE_CHECKING, Any, Callable, Coroutine, Iterator, Literal, Mapping, MutableMapping, TypeVar, overload
from urllib.parse import urlencode

import httpx

from flnd.common import auth, files, net
from flnd.common.pace import SHOWN_WAIT, Pacer
from flnd.source import FetchError, Range, Source, Target
from shared import browser, fmt, http, kv, log, prompt, theme

if TYPE_CHECKING:
    from shared import cookies

    from flnd.session import Settings

__all__ = ["LoginSite", "SourceRun", "TargetSite", "source_run"]

T = TypeVar("T")
# A 401, 403 or 404 is "not there for us", which `soft=True` turns into None.
SOFT_STATUSES = (401, 403, 404)

# The pacer waits and reads time through these, so tests can replace them.
_sleep = time.sleep
_clock = time.monotonic
# The wall clock a cookie's expiry is compared with; tests replace it.
_now = time.time
# How soon before a login cookie expires a run warns of it, in seconds: a day, so a scheduled
# `flnd sync` warns the run before the one that meets the login wall (fixes #9).
EXPIRY_WARNING = 24 * 3600


@dataclass
class SourceRun:
    """One source's resources for a whole run: its cookies, HTTP client, back-off, pacer, client and stores.

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
    # The source's client: its cookies, signing, retries and 429 waits.
    http: httpx.Client
    # Pauses the source after a rate limit, however long it takes.
    backoff: net.Backoff
    # Spaces out requests to the source's own domains.
    pacer: Pacer | None = None
    # What the source's `connect` gave, entered once for the run.
    client: Any = None
    # The row of the target being downloaded, which a long pacing wait captions; None between targets.
    row: Any = None
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

    def show_wait(self, seconds: float) -> None:
        """Say on the current target's row that the next request waits `seconds`, the pacer's long waits."""
        if self.row is not None:
            self.row.caption = f"pacing: next request in {fmt.duration(round(seconds))}"

    def close(self) -> None:
        """Save the pacer's speed, then close the client, the stores and the event loop; a second call does nothing."""
        if self._closed:
            return
        self._closed = True
        if self.pacer is not None:
            self.pacer.save(self.state)
        self.http.close()
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

    A source's client calls its `prepare` for its own domains only, and its pacer starts from the
    speed `state` saved.
    """
    jar = auth.load(settings.home, source.code)
    _warn_expiring(source, jar)
    # The pacer's and the client's callbacks reach the run, which is made after both.
    made: list[SourceRun] = []
    pacer = Pacer(source.pace, source.domains, on_wait=lambda seconds: made[0].show_wait(seconds),
                  clock=lambda: _clock(), sleep=lambda seconds: _sleep(seconds))
    backoff = net.Backoff(source.code, on_hit=pacer.slow_down)

    def prepare(request: httpx.Request) -> httpx.Request:
        # A CDN never sees the source's headers or signature: its own domains only.
        if not pacer.paces(str(request.url)):
            return request
        return source.prepare(request, _names(made[0].http.cookies))

    client = net.client(user_agent=source.user_agent, headers={}, jar=jar, sign=prepare,
                        wait=type(source).wait, backoff=backoff)
    run = SourceRun(source, settings, jar, client, backoff, pacer=pacer)
    made.append(run)
    pacer.load(run.state)
    try:
        yield run
    finally:
        run.close()


class TargetSite:
    """The engine's `Site` for one target; see `flnd.source.Site` for each member.

        site = TargetSite(run, Target("loi", "post", "5930"), row=row)
        post = site.fetch("https://api.moegoat.com/api/lois/5930").json["data"]
        site.close()

    Args:
        run: the source's resources for the run.
        target: the target it acts for.
        row: the target's progress row, which a long pacing wait captions; None for none.

    Used on the job's thread only: the browser is Playwright's sync API, which is single-threaded,
    and the pacer counts one request at a time. `close()` closes the browser.
    """

    def __init__(self, run: SourceRun, target: Target, *, row: Any = None):
        self.target = target
        self._run = run
        self._source = run.source
        self._browser: browser.Browser | None = None
        self._page: _PacedPage | None = None
        # The headers of the tab's last main document, read by `goto` for a 429's Retry-After.
        self._document: dict[str, str] | None = None
        run.row = row

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
            **request: httpx's request options, such as `params`, `data`, `json` and `timeout`.

        Returns:
            The response; None only with `soft`.

        Raises:
            FetchError: no response after the retries, a 4xx other than 429 (with its `status` and
                `response`), or with `in_page`, a request the page couldn't send.
            TypeError: with `in_page`, an option `Page.fetch` doesn't take.
        """
        if in_page:
            return self._fetch_in_page(url, method, headers, soft, request)
        if self._browser is not None:
            # Cookies the site set in the browser reach the client too, as a logged-in page would send them.
            self._run.http.cookies.update(net.cookie_jar(self._browser.cookies))
        self._before(url)
        try:
            response = self._run.http.request(method, url, headers=dict(headers or {}), **request)
        except httpx.HTTPError as error:
            raise FetchError(f"{type(error).__name__} {error} for {url}".strip()) from None
        self._after(url, response)
        answer = http.Response(status=response.status_code, url=str(response.url), headers=response.headers,
                               content=response.content)
        if response.status_code >= 400:
            if soft and response.status_code in SOFT_STATUSES:
                return None
            raise FetchError(f"HTTP {response.status_code} for {url}", status=response.status_code, response=answer)
        return answer

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
            returned: it is waited out with `back_off`, with the page's `Retry-After`, and the
            page loaded again, as long as it takes.
        """
        page = self.page
        while True:
            self._before(url)
            self._document = None
            status = page.goto(url, idle=idle, timeout=timeout)
            # A page load doesn't go through the client's transport, so its 429 is counted here, and the
            # page's own headers stand in for a response for the pacer and the Retry-After.
            if self._document is not None:
                self._after(url, SimpleNamespace(headers=self._document))
            if status == 429:
                wait = net.retry_after_seconds((self._document or {}).get("retry-after"))
                self.back_off("HTTP 429", retry_after=wait)
                continue
            if status is None:
                return False
            if status >= 400:
                self.warn("{url}: HTTP {status}", url=url, status=status)
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
            self._page = _PacedPage(opened.raw, opened.browser, pacer=self._run.pacer)
            self._page.raw.on("response", self._on_document)
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

        The `Backoff` counts it only when no wait is running, so a second call during a wait
        waits with it and adds nothing; each counted one also slows the pacer down. Ctrl-C ends
        the wait with `KeyboardInterrupt`.
        """
        self._run.backoff.hit(reason, retry_after=retry_after)
        self._run.backoff.wait_turn()

    def close(self) -> None:
        """Close the target's browser, giving the client the cookies the site set in it."""
        if self._browser is not None:
            self._run.http.cookies.update(net.cookie_jar(self._browser.cookies))
            self._browser.close()
        self._browser = self._page = None
        self._run.row = None

    def _fetch_in_page(self, url: str, method: str, headers: Mapping[str, str] | None, soft: bool,
                       request: dict[str, Any]) -> http.Response | None:
        """`fetch(in_page=True)`: one request sent by the tab, paced, retried like the client's, its errors as `FetchError`."""
        options = dict(request)
        params = options.pop("params", None)
        data, body, timeout = options.pop("data", None), options.pop("json", None), options.pop("timeout", 30.0)
        if options:
            raise TypeError(f"fetch(in_page=True) takes params, data, json and timeout, not {', '.join(options)}")
        if params:
            url = f"{url}{'&' if '?' in url else '?'}{urlencode(params)}"
        page = self.page

        def attempt() -> http.Response | None:
            # A 429 is waited out and sent again without costing an attempt, as the client's transport does.
            while True:
                self._before(url)
                try:
                    response = page.fetch(url, method=method, headers=headers, data=data, json=body, soft=soft,
                                          timeout=timeout)
                except http.RateLimitedError as error:
                    self.back_off("HTTP 429", retry_after=error.retry_after)
                    continue
                if response is not None:
                    self._after(url, response)
                return response

        try:
            # A 5xx or a request the browser couldn't send may go through next time; a refusal won't.
            return net.retrying(attempt, wait=type(self._source).wait, retryable=(http.Error,),
                                final=(http.RefusedError,))
        except http.RefusedError as error:
            raise FetchError(str(error), status=error.status, response=error.response) from None
        except (http.Error, browser.Error) as error:
            raise FetchError(str(error), status=getattr(error, "status", None)) from None

    def _before(self, url: str) -> None:
        """Wait for the pacer's turn before a request to the source's own domains; a CDN URL waits for nothing."""
        pacer = self._run.pacer
        if pacer is None or not pacer.paces(url):
            return
        if pacer.wait_turn(url) > SHOWN_WAIT and self._run.row is not None:
            # The pacer captioned the row before its wait (`show_wait`); the wait is over now.
            self._run.row.caption = ""

    def _after(self, url: str, response: Any) -> None:
        """Let the pacer read a response's rate-limit headers, for the source's own domains only."""
        pacer = self._run.pacer
        if pacer is not None and pacer.paces(url):
            pacer.record(response)

    def _on_document(self, response: Any) -> None:
        """Keep the headers of the tab's main document as it arrives, for `goto`'s 429 and the pacer."""
        request = response.request
        if self._page is not None and request.is_navigation_request() and request.frame == self._page.raw.main_frame:
            self._document = {str(name).lower(): value for name, value in dict(response.headers).items()}

    def _connect(self) -> browser.Browser:
        """The target's browser, with the source's settings and the login's cookies."""
        source = self._source
        # Visible (open's default), so a person can solve a captcha in it; no locale or timezone, so
        # the browser keeps the computer's own, which match where the connection comes from. The
        # engine asked about downloading Chromium before the run started, when Chrome was missing.
        opened = browser.open(viewport=source.viewport, args=source.browser_args, stealth=source.stealth)
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
    """The target's tab: a `Page` whose `scroll` keeps the source's pace.

    The pause between two scrolls is at least the pacer's scroll floor (0.3 s times its learned
    speed), and each scroll counts as a clean one, so 50 in a row ease a slowed-down source back.
    """

    def __init__(self, raw: Any, owner: browser.Browser, *, pacer: Pacer | None):
        super().__init__(raw, owner)
        self._pacer = pacer

    def scroll(self, *, within: Any = None, until: Callable[[], bool] | None = None,
               count: Callable[[], int] | None = None, idle: float = 10.0, wait: float = 3.0, pause: float = 0.3,
               on_step: Callable[[], Any] | None = None) -> int:
        """`Page.scroll`, its `pause` raised to the pacer's scroll floor and each scroll counted clean; see `Page.scroll`."""
        pacer = self._pacer
        if pacer is None:
            return super().scroll(within=within, until=until, count=count, idle=idle, wait=wait, pause=pause,
                                  on_step=on_step)

        def step() -> None:
            pacer.clean()
            if on_step is not None:
                on_step()

        return super().scroll(within=within, until=until, count=count, idle=idle, wait=wait,
                              pause=max(pause, pacer.scroll_floor), on_step=step)



def _warn_expiring(source: Source, jar: Any) -> None:
    """Warn of each login cookie (`source.needs`) that has expired, or will within `EXPIRY_WARNING`, naming it, never its value."""
    now = _now()
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
