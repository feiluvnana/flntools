"""Drive a real browser with Playwright's sync API (`browser.open()` -> `Browser`, `Page`).

`open` launches Chrome or Chromium, or connects over CDP (`attach`). `Browser` manages tabs and
`cookies` (`get`, `add`, `clear`); `Page` handles navigation (`goto`, `wait`), network iterators
(`responses`, `requests`), scrolling (`scroll`), hand-offs (`handoff`), `fetch`, and `document`.
"""

from __future__ import annotations

import base64
import functools
import json as jsonlib  # `json` is also `Page.fetch`'s keyword, as in `http.fetch`
import os
import random
import re
import shutil
import subprocess
import sys
import tempfile
import threading
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import (
    TYPE_CHECKING, Any, Callable, Iterable, Iterator, Literal, Mapping, Sequence, Union, overload,
)
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import shared
from shared import _clock as clock
from shared import _prompt as prompt
from shared import _text as text
from shared._cookies import Jar
from shared._ui import ui

if TYPE_CHECKING:
    from shared import _http as http
    from shared._selector import Selection
    from playwright.sync_api import BrowserContext, Locator, Playwright, Request, Response
    from playwright.sync_api import Page as RawPage

__all__ = [
    "Browser", "BrowserCookies", "Error", "Match", "MissingError", "Page",
    "Visit", "WaitError", "Watch", "attach", "open",
]

# Switches that hide the automation Chrome would otherwise show (AutomationControlled sets
# `navigator.webdriver`; infobars shows the automated-software bar) and prevent Chrome from
# throttling background tabs when multiple tabs run inside one browser window.
LAUNCH_ARGS = [
    "--disable-blink-features=AutomationControlled",
    "--disable-infobars",
    "--disable-background-timer-throttling",
    "--disable-backgrounding-occluded-windows",
    "--disable-renderer-backgrounding",
]
# `navigator.webdriver` is removed rather than set to false, matching how an ordinary browser acts.
HIDE_WEBDRIVER = "Object.defineProperty(navigator, 'webdriver', {get: () => undefined})"
# Background tabs inside one shared window must still look visible and focused to site scripts.
KEEP_VISIBLE = (
    "Object.defineProperties(Document.prototype, {"
    "visibilityState: {get: () => 'visible', configurable: true}, "
    "hidden: {get: () => false, configurable: true}"
    "});"
    "Document.prototype.hasFocus = () => true;"
)
PAGE_HEIGHT = "(document.scrollingElement || document.documentElement).scrollHeight"
# Half a screen from the end counts as the bottom, so a small scroll up doesn't reset the end rule.
NEAR_BOTTOM = ("(() => { const page = document.scrollingElement || document.documentElement;"
               " return page.scrollTop + innerHeight * 1.5 >= page.scrollHeight; })()")
# The same two reads for `scroll(within=…)`, on the element that scrolls instead of the page.
ELEMENT_HEIGHT = "element => element.scrollHeight"
ELEMENT_NEAR_BOTTOM = (
    "element => element.scrollTop + element.clientHeight * 1.5 >= element.scrollHeight"
)
VIEWPORT = "[innerWidth, innerHeight]"
# `Page.fetch`'s request, run in the page by its own `fetch()`. The body comes back as base64, since
# `evaluate` carries only JSON and a binary body would not survive as text. An AbortController ends
# the request, body included, after `ms`; every failure is returned as `error`, never thrown, so
# Python tells a timeout ("timeout") from a refused or blocked request (fetch's own TypeError).
IN_PAGE_FETCH = """async ({url, method, headers, body, form, ms}) => {
  const resolved = new URL(url, location.href).href;
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), ms);
  try {
    const init = {method, headers, credentials: "include", signal: controller.signal};
    if (form !== null) init.body = new URLSearchParams(form);
    else if (body !== null) init.body = Uint8Array.from(atob(body), c => c.charCodeAt(0));
    const response = await fetch(resolved, init);
    const bytes = new Uint8Array(await response.arrayBuffer());
    let text = "";
    // In slices: String.fromCharCode takes bytes as arguments, and a call has a limit on those.
    for (let i = 0; i < bytes.length; i += 0x8000) {
      text += String.fromCharCode(...bytes.subarray(i, i + 0x8000));
    }
    const hdrs = [...response.headers];
    return {resolved, status: response.status, url: response.url, headers: hdrs, body: btoa(text)};
  } catch (error) {
    const msg = error.name === "AbortError" ? "timeout" : `${error.name}: ${error.message}`;
    return {resolved, error: msg};
  } finally {
    clearTimeout(timer);
  }
}"""
# Where Chrome keeps its profiles, relative to the home folder, on each platform.
PROFILE_ROOTS = {
    "darwin": "Library/Application Support/Google/Chrome",
    "linux": ".config/google-chrome",
    "win32": "AppData/Local/Google/Chrome/User Data",
}
# A copy of an installed profile opens only in Chrome: its cookies are sealed with Chrome's key.
PROFILE_CHANNEL: str | None = "chrome"
# Where Playwright's `chrome` channel finds Google Chrome: the same paths as its registry
# (`_createChromiumChannel` in the driver, Playwright 1.65). On Windows each is tried under
# %LOCALAPPDATA%, %PROGRAMFILES% and %PROGRAMFILES(X86)%, as Playwright does.
CHROME_PATHS = {
    "darwin": "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    "linux": "/opt/google/chrome/chrome",
    "win32": "Google/Chrome/Application/chrome.exe",
}
# The environment variables whose folders Chrome may be installed under on Windows; each is tried.
WINDOWS_ROOTS = ("LOCALAPPDATA", "PROGRAMFILES", "PROGRAMFILES(X86)")
CHROMIUM_QUESTION = "Google Chrome isn't installed. Download Chromium for flnd (about 200 MB)?"
NEEDS_CHROME = "flnd needs Google Chrome: install it from https://www.google.com/chrome/"
# Playwright's installer prints a line such as "|■■■■      |  20% of 167.3 MiB" every few
# percent when its output isn't a terminal; the number is the progress row's.
INSTALL_PERCENT = re.compile(r"(\d+)% of")
# Caches are big and Chrome rebuilds them, so a profile copy leaves them out.
SKIP_IN_PROFILE = shutil.ignore_patterns("Cache", "Code Cache", "GPUCache", "Service Worker")
# What `on_response` and `on_request` take: a substring of the URL, a regex searched in it, or a
# predicate over it.
Match = Union[str, "re.Pattern[str]", Callable[[str], bool]]


# What Playwright says when what a call read is gone: the page navigated under an evaluation, or
# Chrome freed a response body (Network.getResponseBody) once its page moved on, seen 2026-11-13.
GONE = ("Execution context was destroyed", "page is navigating",
        "No resource with given identifier found")
# Seconds between two checks for a scroll's progress: short next to a page's load, cheap for DOM.
POLL = 0.05
DEFAULT_VIEWPORT = (1280, 900)
SCROLL_WAIT = 3.0
SCROLL_PAUSE = 0.3
HANDOFF_POLL = 1.0
# Set when the Chromium download was declined (or couldn't be asked), so it isn't asked again.
_chromium_declined = False
# The error the Chromium download failed with, re-raised without asking for the rest of the process.
_chromium_failed: MissingError | None = None
# Each source's session opens its browser on its own thread, so two can find Chromium missing at
# once: the check, the question and the download are one step, or both would ask and both download.
_chromium_offer = threading.Lock()
# Cached headful user-agent string keyed by browser executable path.
_HEADFUL_UA: dict[str, str] = {}


class Error(shared.Error):
    """The base of every error `shared.browser` raises."""


class _NeverRaised(Exception):
    """Stands for Playwright's error classes where Playwright isn't installed: nothing matches."""


@functools.cache
def _raw_error() -> type[Exception]:
    """Playwright's `Error`, imported once; a class nothing raises without Playwright."""
    try:
        from playwright.sync_api import Error
    except ImportError:  # no `browser` extra: no Playwright call can have raised
        return _NeverRaised
    return Error


@functools.cache
def _raw_timeout() -> type[Exception]:
    """Playwright's `TimeoutError`, imported once, as `_raw_error`."""
    try:
        from playwright.sync_api import TimeoutError
    except ImportError:  # no `browser` extra: no Playwright call can have raised
        return _NeverRaised
    return TimeoutError


def _gone(error: BaseException) -> bool:
    """Whether a Playwright error says what it read is gone: the page moved, or a body was freed."""
    return any(text in str(error) for text in GONE)


class MissingError(Error, shared.MissingError):
    """Neither Google Chrome nor a downloaded Chromium, and the download was declined or failed."""


class WaitError(Error):
    """`Page.wait`'s condition didn't come true within its `timeout`.

    Not a subclass of the builtin `TimeoutError`, which is an `OSError`: a page that is slow to
    show an element isn't a failure of the operating system, and `except OSError` mustn't catch it.
    """


def open(*, profile: str | Path | None = None, headless: bool = False,
         stealth: bool = True) -> Browser:
    """Launch Chrome or Chromium and return it as a `Browser` for a `with` block."""
    name = _profile_name(profile)
    # Copied before Playwright starts, so a misspelt name fails at once. From here the copy holds
    # the profile's logins, so it is deleted however open ends without a launched browser.
    copy = _copy_profile(name) if name is not None else None
    launched = False
    try:
        playwright = _start_playwright()
        try:
            target_profile = copy if copy is not None else profile
            started = _launch(
                playwright, target_profile, copied=copy is not None, headless=headless,
                viewport=DEFAULT_VIEWPORT, locale=None, timezone=None, args=(), stealth=stealth,
            )
            launched = True
            return started
        # Any exception, Ctrl-C included, stops Playwright's driver before it goes on up.
        except BaseException:
            playwright.stop()
            raise
    finally:
        # Once launched, Browser.close deletes the copy after Chrome stops writing into it.
        if copy is not None and not launched:
            shutil.rmtree(copy, ignore_errors=True)


def attach(cdp: str) -> Browser:
    """Attach to a running browser over CDP; it keeps running after `close()`."""
    playwright = _start_playwright()
    try:
        remote = playwright.chromium.connect_over_cdp(cdp)
        context = remote.contexts[0] if remote.contexts else remote.new_context()
        return Browser(context, close_steps=[remote.close, playwright.stop], headless=False)
    except BaseException:
        playwright.stop()
        raise


def _profile_name(profile: str | Path | None) -> str | None:
    """The installed Chrome profile `profile` names, or None when it is a folder (or None).

    A name is a `str` with no path separator: profile names never have one, and a folder can
    always be written with one (`./profile`) or as a `Path`.
    """
    if profile is None or not isinstance(profile, str):
        return None
    if not profile:
        raise ValueError(
            "profile is empty: give a Chrome profile's name, such as 'Default', or a folder"
        )
    separators = {"/", os.sep, os.altsep} - {None}
    if any(separator in profile for separator in separators) or profile in (".", ".."):
        return None
    return profile


def _profile_root() -> Path:
    """The folder that holds Google Chrome's profiles on this platform."""
    # Any platform other than macOS and Windows gets the Linux path, the one Chrome uses there.
    key = sys.platform if sys.platform in ("darwin", "win32") else "linux"
    return Path.home() / PROFILE_ROOTS[key]


def _copy_profile(name: str) -> Path:
    """Copy the installed Chrome profile `name` to a new private temporary folder, as its `Default`.

    Raises `FileNotFoundError` when Chrome has no profile of that name. A copy that fails half
    way is deleted before the error goes on.
    """
    root = _profile_root()
    if not (root / name).is_dir():
        raise FileNotFoundError(f"no Chrome profile named {name!r} in {root}")
    # mkdtemp makes the folder with mode 0700, so on a shared machine no other user can read the
    # copy, which holds logins. Windows ignores the mode; the user's temp folder is private there.
    copy = Path(tempfile.mkdtemp(prefix="shared-chrome-"))
    try:
        # Whatever its name, the copy holds the profile as Default, and _launch opens Default.
        shutil.copytree(
            root / name, copy / "Default", ignore=SKIP_IN_PROFILE, ignore_dangling_symlinks=True
        )
        if (root / "Local State").is_file():
            shutil.copy2(root / "Local State", copy / "Local State")
    # Any exception, Ctrl-C included, deletes the half-made copy before it goes on up: nothing
    # else knows of it yet, and it may already hold the profile's logins.
    except BaseException:
        shutil.rmtree(copy, ignore_errors=True)
        raise
    return copy


@dataclass(frozen=True)
class Visit:
    """Where `Page.goto` landed: the document's status, its URL after redirects, and its headers."""

    status: int
    url: str
    # Read-only, with lowercase names, as Playwright gives them.
    headers: Mapping[str, str]

    def __post_init__(self) -> None:
        object.__setattr__(self, "headers", MappingProxyType(
            {str(name).lower(): value for name, value in self.headers.items()}))


class BrowserCookies:
    """Sub-namespace for managing browser context cookies (`get`, `add`, `clear`)."""

    def __init__(self, browser: Browser) -> None:
        self._browser = browser

    def get(self) -> Jar:
        """Every cookie in the browser as a `shared.cookies.Jar` snapshot."""
        return Jar(dict(cookie) for cookie in self._browser.raw.cookies())

    def add(self, jar: Jar | Iterable[dict[str, Any]]) -> None:
        """Add each cookie to the browser unchanged, except that `path` defaults to `/`."""
        for cookie in jar:
            try:
                entry: Any = {**cookie, "path": cookie.get("path") or "/"}
                self._browser.raw.add_cookies([entry])
            # Playwright's Error is what a cookie it won't take (no domain, a bad expiry) raises;
            # anything else is a real failure and goes on up.
            except _raw_error() as error:
                ui.warn("cookie {name} was rejected: {error}", name=cookie.get("name", ""),
                         error=_without(_first_line(error), str(cookie.get("value", ""))))

    def clear(self) -> None:
        """Delete every cookie in the browser."""
        self._browser.raw.clear_cookies()


class Browser:
    """A running browser: open tabs in it, and manage its cookies (`raw` is `BrowserContext`).

        with browser.open() as chrome:
            tab = chrome.open("https://example.com/")
            chrome.cookies.add(cookies.load("example_cookies.txt"))

    Made by `browser.open`, not directly. Use it only on the thread that opened it.
    """

    def __init__(self, context: BrowserContext, *, close_steps: list[Callable[[], Any]],
                 headless: bool, page_viewport: tuple[int, int] | None = None):
        """Wrap `context`; `close_steps` run in order on `close`; `handoff` checks `headless`."""
        self._raw = context
        self._close_steps = close_steps
        self._headless = headless
        self._page_viewport = page_viewport
        self._wrappers: dict[RawPage, Page] = {}
        self._opened: list[Page] = []
        self._closed = False
        self._cookies = BrowserCookies(self)

    @property
    def raw(self) -> BrowserContext:
        """Underlying `playwright.sync_api.BrowserContext`."""
        return self._raw

    @property
    def cookies(self) -> BrowserCookies:
        """Sub-namespace for managing browser context cookies."""
        return self._cookies

    def __enter__(self) -> Browser:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    def open(self, url: str | None = None, *, idle: bool = False, timeout: float = 30.0) -> Page:
        """Open a new tab, and go to `url` when given."""
        raw = self.raw.new_page()
        if self._page_viewport is not None:
            width, height = self._page_viewport
            raw.set_viewport_size({"width": width, "height": height})
        page = self._wrap(raw)
        self._opened.append(page)
        if url is not None:
            page.goto(url, idle=idle, timeout=timeout)
        return page

    def close(self) -> None:
        """Close the tabs, context, browser and Playwright in order (same as leaving `with`).

            chrome.close()

        An attached (`cdp=`) browser keeps running, and a copied profile is deleted once the
        browser has stopped. A failure while closing is logged once, and never raised, so it
        can't hide the error that made a `with` block exit. A second call does nothing.
        """
        if self._closed:
            return
        self._closed = True
        failures = []
        for step in [page.raw.close for page in list(self._opened)] + self._close_steps:
            try:
                step()
            # Every step runs even when one fails, so Playwright's driver is always stopped.
            except Exception as error:
                failures.append(error)
        if failures:
            ui.warn("closing the browser failed: {error}", error=_first_line(failures[0]))

    def _wrap(self, raw: RawPage) -> Page:
        if raw not in self._wrappers:
            self._wrappers[raw] = Page(raw, self)
            if hasattr(raw, "on"):
                raw.on("close", lambda *_args: self._forget(raw))
        return self._wrappers[raw]

    def _forget(self, raw: RawPage) -> None:
        page = self._wrappers.pop(raw, None)
        if page is not None and page in self._opened:
            self._opened.remove(page)


def _without(text: str, secret: str) -> str:
    """`text` with every occurrence of `secret` replaced by `…`, so a log line can't leak it."""
    return text.replace(secret, "…") if secret else text


class Watch:
    """An iterator over matching responses or requests from `Page.responses` / `Page.requests`."""

    def __init__(self, stop: Callable[[], None], step: Callable[[], bool] | None = None):
        self._stop = stop
        self._step = step
        self._stopped = False
        self._queue: deque[tuple[Any, Request | None]] = deque()
        self._request: Request | None = None

    @property
    def request(self) -> Request | None:
        """The Playwright `Request` that produced the current item, or None."""
        return self._request

    def _push(self, item: Any, request: Request | None) -> None:
        self._queue.append((item, request))

    def __enter__(self) -> Watch:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.stop()

    def stop(self) -> None:
        """Remove the listener and stop iterating; a second call does nothing."""
        if not self._stopped:
            self._stopped = True
            self._stop()

    def __iter__(self) -> Iterator[Any]:
        try:
            while True:
                while self._queue:
                    item, self._request = self._queue.popleft()
                    yield item
                if self._stopped or self._step is None or not self._step():
                    break
            while self._queue:
                item, self._request = self._queue.popleft()
                yield item
        finally:
            if self._step is not None:
                self.stop()


class Page:
    """One tab. `raw` is Playwright's `Page`, for `click`, `fill`, `evaluate` and everything else.

        tab = chrome.open("https://example.com/")
        with tab.on_response("graphql", handler):
            tab.scroll(count=lambda: len(posts))
        title = tab.document.css("h1").text

    Made by `Browser.open` or `Browser.pages`, not directly. Use it only on the browser's thread.
    `with chrome.open(url) as tab:` closes the tab at the end of the block.
    """

    def __init__(self, raw: RawPage, browser: Browser):
        self.raw = raw
        self.browser = browser
        self._responses_seen = 0

    def goto(self, url: str, *, idle: bool = False, timeout: float = 30.0) -> Visit | None:
        """Go to `url`, and return its main document's status, final URL and headers as a `Visit`.

            visit = tab.goto("https://example.com/", idle=True)
            if visit is None or visit.status >= 400: ...

        Args:
            url: the address to load in this tab.
            idle: also wait until the network has been quiet for a moment, not only for `load`.
            timeout: seconds before the navigation counts as failed.

        Returns:
            None when it didn't load (a navigation error, which is logged, not raised), and also
            when no response came: Playwright reports none for a move within the same document,
            such as a new `#fragment`.
        """
        try:
            wait_state: Literal["networkidle", "load"] = "networkidle" if idle else "load"
            response = self.raw.goto(url, wait_until=wait_state, timeout=timeout * 1000)
        # Playwright's Error covers DNS failures, aborted requests and timeouts ("not loaded").
        except _raw_error() as error:
            clean_url = text.clean.url(url)
            msg = _first_line(error).replace(url, clean_url)
            ui.warn("could not open {url}: {error}", url=clean_url, error=msg)
            return None
        if response is None:
            return None
        return Visit(response.status, response.url, dict(response.headers))

    def wait(self, css: str | None = None, *,
             url: str | re.Pattern[str] | Callable[[str], bool] | None = None,
             idle: bool = False, timeout: float = 30.0) -> None:
        """Wait until `css` matches an element, `url` matches the URL, and (`idle`) network stops.

            tab.wait(".grid")
            tab.wait(url="**/reels/")
            tab.wait(idle=True)
            tab.wait()                     # the `load` event

        Args:
            css: a CSS selector; waits until a visible element matches it. None doesn't wait.
            url: a glob ("**/feed/"), a compiled regex or a predicate over the URL; waits until the
                tab's URL matches it. None doesn't wait for one.
            idle: also wait until the network has been quiet for 500 ms. With neither `css` nor
                `url`, False waits for the `load` event instead.
            timeout: seconds for every condition together, not for each.

        Raises:
            WaitError: `timeout` seconds passed first. Its message names what was waited for.

        The conditions are waited for in the order above. Handlers keep running during the wait.
        """
        deadline = clock.now() + timeout

        def left() -> float:
            # Playwright reads a timeout of 0 as "no timeout", so the last moment is 1 ms, not 0.
            return max(1.0, (deadline - clock.now()) * 1000)

        what = "the page to load"
        try:
            if css is not None:
                what = f"an element to match {css!r}"
                self.raw.wait_for_selector(css, timeout=left())
            if url is not None:
                target = "a predicate" if callable(url) else repr(url)
                what = f"the URL to match {target}"
                self.raw.wait_for_url(url, timeout=left())
            if idle or (css is None and url is None):
                what = "the network to go quiet" if idle else "the page to load"
                self.raw.wait_for_load_state("networkidle" if idle else "load", timeout=left())
        # Only Playwright's TimeoutError is "too slow"; any other error (closed tab) goes on up.
        except _raw_timeout():
            raise WaitError(f"waited {timeout:g} s for {what}") from None

    def responses(self, match: Match) -> Watch:
        """Watch matching responses and iterate their parsed JSON bodies."""
        matches = _matcher(match)
        watch = Watch(lambda: self.raw.remove_listener("response", listener))

        def listener(response: Response) -> None:
            if not matches(response.url):
                return
            self._responses_seen += 1
            for parsed in _json_bodies(response, False):
                watch._push(parsed, response.request)

        self.raw.on("response", listener)
        return watch

    def requests(self, match: Match) -> Watch:
        """Watch matching requests and iterate them as Playwright `Request` objects."""
        matches = _matcher(match)
        watch = Watch(lambda: self.raw.remove_listener("request", listener))

        def listener(request: Request) -> None:
            if matches(request.url):
                watch._push(request, request)

        self.raw.on("request", listener)
        return watch

    @overload
    def fetch(self, url: str, *, method: str = ..., headers: Mapping[str, str] | None = ...,
              params: Mapping[str, Any] | None = ..., data: Any = ..., json: Any = ...,
              soft: Literal[False] = ..., timeout: float = ...) -> http.Response: ...
    @overload
    def fetch(self, url: str, *, method: str = ..., headers: Mapping[str, str] | None = ...,
              params: Mapping[str, Any] | None = ..., data: Any = ..., json: Any = ...,
              soft: Literal[True], timeout: float = ...) -> http.Response | None: ...
    @overload
    def fetch(self, url: str, *, method: str = ..., headers: Mapping[str, str] | None = ...,
              params: Mapping[str, Any] | None = ..., data: Any = ..., json: Any = ...,
              soft: bool, timeout: float = ...) -> http.Response | None: ...
    def fetch(self, url: str, *, method: str = "GET", headers: Mapping[str, str] | None = None,
              params: Mapping[str, Any] | None = None, data: Any = None, json: Any = None,
              soft: bool = False, timeout: float = 30.0) -> http.Response | None:
        """Execute an HTTP request inside the browser context."""
        return self._fetch(url, method=method, headers=headers, params=params, data=data,
                           json=json, soft=soft, timeout=timeout)

    def _fetch(self, url: str, *, method: str, headers: Mapping[str, str] | None,
               params: Mapping[str, Any] | None, data: Any, json: Any, soft: bool,
               timeout: float) -> http.Response | None:
        from shared import _http

        if data is not None and json is not None:
            raise ValueError("pass data or json, not both")
        if params:
            parts = urlsplit(url)
            pairs = parse_qsl(parts.query, keep_blank_values=True)
            pairs.extend((str(k), str(v)) for k, v in params.items() if v is not None)
            url = urlunsplit(parts._replace(query=urlencode(pairs)))
        # Prepared here as bytes or form fields, then handed to the page's fetch() in one piece.
        sent = dict(headers or {})
        body = form = None
        if json is not None:
            body = jsonlib.dumps(json).encode("utf-8")
            if not any(name.lower() == "content-type" for name in sent):
                sent["Content-Type"] = "application/json"
        elif isinstance(data, Mapping):
            form = {str(key): str(value) for key, value in data.items()}
        elif isinstance(data, str):
            body = data.encode("utf-8")
        elif data is not None:
            body = bytes(data)
        method = method.upper()
        encoded_body = None if body is None else base64.b64encode(body).decode("ascii")
        request = {
            "url": url, "method": method, "headers": sent, "form": form, "ms": timeout * 1000,
            "body": encoded_body,
        }
        # Runs inside the page, carrying the page's cookies, origin and TLS fingerprint.
        try:
            result = self.raw.evaluate(IN_PAGE_FETCH, request)
        # A tab that navigated, closed or crashed meanwhile: the page is gone, not refused.
        except _raw_error() as error:
            raise Error(_without_values(f"{method} {_http._where(url)}: the page couldn't send it: "
                                        f"{_first_line(error)}", sent)) from None
        where = f"{method} {_http._where(result['resolved'])}"
        if "error" in result:
            if result["error"] == "timeout":
                raise _http.Error(f"{where}: no answer within {timeout:g} s")
            raise _http.Error(_without_values(f"{where}: {result['error']}", sent))
        response = _http.Response(
            status=result["status"], url=result["url"], headers=dict(result["headers"]),
            content=base64.b64decode(result["body"]),
        )
        return _http._checked(response, method, soft=soft)

    def scroll(self, *, until: Callable[[], bool] | None = None,
               within: str | None = None, idle: float = 10.0) -> int:
        """Scroll like a person until the feed ends, and return the number of scrolls."""
        scroller = _Scroller(self, until, within, idle)
        while scroller():
            pass
        return scroller.scrolls

    def handoff(self, *, until: Callable[[], bool], message: str,
                timeout: float | None = None) -> bool:
        """Wait for a person to complete a step (e.g. a captcha) and return whether done."""
        if until():
            return True
        if self.browser._headless:
            raise Error(f"cannot hand off to a person in a headless browser: {message}")
        ui.warn(message)
        deadline = None if timeout is None else clock.now() + timeout
        with ui.progress(message) as bar:
            while not until():
                if deadline is not None and clock.now() >= deadline:
                    bar.fail()
                    return False
                self.raw.wait_for_timeout(HANDOFF_POLL * 1000)
        return True

    @property
    def document(self) -> Selection:
        """The live page as a `selector.Selection`, read fresh each time."""
        # Imported here, so a script that only drives the browser doesn't need parsel.
        from shared._selector import parse

        return parse(self._steady(self.raw.content, "<html></html>", "read the page"))

    @property
    def url(self) -> str:
        """The tab's current URL."""
        return self.raw.url

    def __enter__(self) -> Page:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    def close(self) -> None:
        """Close the tab."""
        self.raw.close()

    def _steady(self, read: Callable[[], Any], fallback: Any, what: str, *, tries: int = 3) -> Any:
        """Run a page evaluation; if the page navigates meanwhile, wait for load and retry.

        Playwright fails an evaluation with "Execution context was destroyed" (or says the page is
        navigating) when the page moves on under it. Wait for the load, retry, and after `tries`
        attempts log a warning and return `fallback`. Other errors go up.
        """
        for attempt in range(tries):
            try:
                return read()
            except _raw_error() as error:
                if not _gone(error):
                    raise
                if attempt + 1 == tries:
                    ui.warn("could not {what}, the page kept navigating: {error}", what=what,
                             error=_first_line(error))
                    return fallback
                try:
                    # Up to 10 s for the navigation to finish, in milliseconds.
                    self.raw.wait_for_load_state("load", timeout=10000)
                except _raw_error():
                    # Still moving, or closed; the next read decides.
                    pass
        return fallback

    def _progress_marker(self, target: Locator | None,
                         count: Callable[[], int] | None) -> tuple[int, int, int]:
        """Numbers whose rise is progress: scrolled height, `count()` and responses seen."""
        height = self._steady(lambda: self.raw.evaluate(PAGE_HEIGHT) if target is None
                              else target.evaluate(ELEMENT_HEIGHT), 0, "measure the page")
        return height, count() if count is not None else 0, self._responses_seen

    def _at_bottom(self, target: Locator | None) -> bool:
        return self._steady(lambda: self.raw.evaluate(NEAR_BOTTOM) if target is None
                            else target.evaluate(ELEMENT_NEAR_BOTTOM), False, "measure the page")

    def _wait_for_progress(self, target: Locator | None, before: tuple[int, ...],
                           count: Callable[[], int] | None, longest: float) -> tuple[int, int, int]:
        """Poll every `POLL` s until one of `before` rises or `longest` s pass; return marker."""
        deadline = clock.now() + longest
        while True:
            marker = self._progress_marker(target, count)
            if any(now > then for now, then in zip(marker, before)) or clock.now() >= deadline:
                return marker
            self.raw.wait_for_timeout(POLL * 1000)

    def _area(self, target: Locator | None) -> tuple[float, float, float, float]:
        """The visible part of what scrolls, as (x, y, width, height) in the viewport's pixels."""
        width, height = self._steady(
            lambda: self.raw.evaluate(VIEWPORT), (1280, 720), "measure the page"
        )
        if target is None:
            return 0.0, 0.0, width, height
        try:
            # Short timeout: the element was there a moment ago, so if gone now it won't come back.
            box = target.bounding_box(timeout=1000)
        # Only Playwright's TimeoutError means "gone"; it is reported as the element missing, below.
        except _raw_timeout():
            box = None
        if box is None:
            raise Error("scroll(within=…) found no visible element to scroll")
        # Only the part inside the viewport can be under the mouse.
        top, bottom = max(box["y"], 0.0), min(box["y"] + box["height"], height)
        left, right = max(box["x"], 0.0), min(box["x"] + box["width"], width)
        if bottom <= top or right <= left:
            raise Error("scroll(within=…) found its element outside the viewport")
        return left, top, right - left, bottom - top

    def _move(self, target: Locator | None) -> None:
        """One scroll: move mouse over the area and wheel down in a fast burst."""
        left, top, width, height = self._area(target)
        # The mouse lands somewhere in the middle of the area, never on one pixel each time.
        self.raw.mouse.move(left + width * random.uniform(0.25, 0.75),
                            top + height * random.uniform(0.3, 0.7), steps=random.randint(1, 3))
        self._wheel(height * random.uniform(1.2, 1.8))

    def _wheel(self, distance: float) -> None:
        """Turn the wheel by `distance` pixels in 4 to 7 uneven frame-rate steps."""
        # Uneven frame-rate steps, so the wheel glides smoothly without ticking at one fixed rate.
        weights = [random.uniform(0.6, 1.4) for _ in range(random.randint(4, 7))]
        total = sum(weights)
        for weight in weights:
            self.raw.mouse.wheel(0, distance * weight / total)
            self.raw.wait_for_timeout(random.uniform(5, 15))

    def _pause(self, floor: float) -> None:
        # Jitter only adds: a floor is the least a site is given, so it is never cut short.
        self.raw.wait_for_timeout(floor * random.uniform(1.0, 1.4) * 1000)

    def _jiggle(self, target: Locator | None, floor: float) -> None:
        # Most feeds load the next page when their end comes back into view, so up a screen and down
        # wakes them. The mouse is still over the area from the scroll before.
        height = self._area(target)[3]
        self._wheel(-height)
        self._pause(floor)
        self._wheel(height)


def _start_playwright() -> Playwright:
    """Start Playwright's driver."""
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        raise ImportError(
            "shared.browser needs playwright: pip install 'flntools[browser]'"
        ) from None
    return sync_playwright().start()


class _Scroller:
    """Drives `Page.scroll` one step at a time."""

    def __init__(self, page: Page, until: Callable[[], bool] | None,
                 within: str | Locator | None, idle: float):
        self._page = page
        self._until = until
        self._idle = idle
        target = page.raw.locator(within).first if isinstance(within, str) else within
        if target is not None and not target.is_visible():
            raise Error("scroll(within=…) found no visible element to scroll")
        self._target = target
        self._marker = page._progress_marker(target, None)
        self._quiet_since: float | None = None
        self.scrolls = 0

    def __call__(self) -> bool:
        if self._until is not None and self._until():
            return False
        started = clock.now()
        self._page._move(self._target)
        self.scrolls += 1
        at_bottom = self._page._at_bottom(self._target)
        if not at_bottom:
            longest = 0.0
        elif self._quiet_since is None:
            longest = SCROLL_WAIT
        else:
            longest = min(SCROLL_WAIT, self._quiet_since + self._idle - clock.now())
        previous, self._marker = self._marker, self._page._wait_for_progress(
            self._target, self._marker, None, longest
        )
        if at_bottom:
            self._page._pause(SCROLL_PAUSE)
        if any(now > before for now, before in zip(self._marker, previous)) or not at_bottom:
            self._quiet_since = None
            return True
        if self._quiet_since is None:
            self._quiet_since = started
        if clock.now() - self._quiet_since >= self._idle:
            return False
        self._page._jiggle(self._target, SCROLL_PAUSE)
        return True


def _launch(playwright: Playwright, profile: str | Path | None, *, copied: bool, headless: bool,
            viewport: tuple[int, int], locale: str | None, timezone: str | None,
            args: Sequence[str], stealth: bool) -> Browser:
    """Launch a browser for `open`; `copied` says `profile` is a copy of an installed profile.

    `open` deletes the copy when this raises; on success, the Browser's close deletes it.
    """
    launch_options: dict[str, Any] = {"headless": headless, "args": [*LAUNCH_ARGS, *args]}
    context_options: dict[str, Any] = {"viewport": {"width": viewport[0], "height": viewport[1]}}
    if locale is not None:
        context_options["locale"] = locale
    if timezone is not None:
        context_options["timezone_id"] = timezone
    # A copy of Chrome's profile opens only in Chrome, so it never asks to download Chromium.
    channel = PROFILE_CHANNEL if copied else _channel(playwright)
    if copied:
        # `_copy_profile` always stores the copy as `Default`, whatever the profile was called.
        launch_options["args"].append("--profile-directory=Default")
    context, close_steps = _open_context(
        playwright, profile, channel, launch_options, context_options
    )
    if stealth:
        context.add_init_script(HIDE_WEBDRIVER)
    context.add_init_script(KEEP_VISIBLE)
    if copied:
        copy = Path(str(profile))
        # Removed after Chrome has closed, or Chrome would still be writing into it.
        close_steps.append(lambda: shutil.rmtree(copy, ignore_errors=True))
    return Browser(context, close_steps=close_steps, headless=headless)


def _channel(playwright: Playwright) -> str:
    """The channel `open` launches: `chrome` when Google Chrome is installed, `chromium` otherwise.

    Without Chrome or a downloaded Chromium, it asks to download Chromium, and raises
    `MissingError(NEEDS_CHROME)` on no or without a terminal. A failed download raises its
    `MissingError`, and the same one again, without asking, for the rest of the process.
    """
    global _chromium_declined, _chromium_failed
    if _chrome_installed():
        return "chrome"
    # Where Playwright keeps the Chromium it downloads, in the user cache: found here, it was
    # downloaded on an earlier run, so it is used without asking again.
    chromium = Path(playwright.chromium.executable_path)
    if chromium.is_file():
        return "chromium"
    with _chromium_offer:
        # Looked at again under the lock: a thread that waited here finds what the first one did.
        if chromium.is_file():
            return "chromium"
        if _chromium_failed is not None:
            raise _chromium_failed
        # confirm opens log's question, which holds other threads' live rows until it's answered.
        # Without a terminal it answers its default, no, at once. A no is kept for the process, so
        # a run that opens a browser once per source asks only once.
        if _chromium_declined or not prompt.confirm(CHROMIUM_QUESTION, default=False):
            _chromium_declined = True
            raise MissingError(NEEDS_CHROME)
        try:
            _install_chromium()
        except MissingError as error:
            # A failed download (no space, no network) would likely fail again after asking again.
            _chromium_failed = error
            raise
    return "chromium"


def _chrome_executable() -> Path | None:
    """The installed Google Chrome executable path, or None if not found."""
    platform = sys.platform if sys.platform in CHROME_PATHS else "linux"
    if platform != "win32":
        candidate = Path(CHROME_PATHS[platform])
        return candidate if candidate.is_file() else None
    roots = [os.environ[name] for name in WINDOWS_ROOTS if os.environ.get(name)]
    for root in roots:
        candidate = Path(root) / CHROME_PATHS["win32"]
        if candidate.is_file():
            return candidate
    return None


def _chrome_installed() -> bool:
    """Whether Google Chrome is where Playwright's `chrome` channel looks for it."""
    return _chrome_executable() is not None


def _install_chromium() -> None:
    """Download Chromium with Playwright's installer under a progress row into the user cache.

    `--no-shell` skips the separate headless shell: `open` runs Chromium itself in Chrome's
    new headless mode (the `chromium` channel), so the shell would never be used.

    Raises:
        MissingError: the installer failed, with its last line of output.
    """
    # A bundled flnd has no `python -m playwright`, so this runs the driver Playwright ships.
    from playwright._impl._driver import compute_driver_executable, get_driver_env

    node, cli_js = compute_driver_executable()
    lines: list[str] = []
    with ui.progress("Downloading Chromium", total=100) as bar:
        # The installer's output is read here, not shown: its own bars would tear the live area.
        cmd = [node, cli_js, "install", "--no-shell", "chromium"]
        with subprocess.Popen(cmd, env=get_driver_env(), stdout=subprocess.PIPE,
                              stderr=subprocess.STDOUT, text=True, encoding="utf-8",
                              errors="replace") as installer:
            for line in installer.stdout or ():
                lines.append(line.strip())
                if found := INSTALL_PERCENT.search(line):
                    bar.count = int(found[1])
        if installer.returncode:
            last = next((line for line in reversed(lines) if line), "no output")
            raise MissingError(f"Chromium was not downloaded: Playwright's installer exited with "
                               f"{installer.returncode}: {last}")


def _open_context(playwright: Playwright, profile: str | Path | None, channel: str | None,
                  launch_options: dict[str, Any],
                  context_options: dict[str, Any]) -> tuple[BrowserContext, list]:
    """Launch the browser with or without a profile folder, returning `(context, close_steps)`.

    With no `profile` the browser is an incognito context of its own; with one, Chrome's persistent
    profile in that folder. The returned steps run in order when the `Browser` closes.
    """
    headless = launch_options["headless"]
    # Playwright's bundled Chromium, launched headless with no channel, runs as the separate
    # headless shell, which flnd never installs (`install --no-shell`). The `chromium` channel runs
    # the full Chromium in Chrome's new headless mode instead. `chrome` is left as it is.
    channel = channel or "chromium"
    if profile is None:
        remote = playwright.chromium.launch(channel=channel, **launch_options)
        options = (
            {**context_options, "user_agent": _cached_headful_ua(playwright, channel, remote)}
            if headless else context_options
        )
        context = remote.new_context(**options)
        return context, [context.close, remote.close, playwright.stop]
    args = launch_options["args"]
    if headless:
        args = [*args, f"--user-agent={_headful_user_agent_of(playwright, channel)}"]
    context = playwright.chromium.launch_persistent_context(
        str(profile), channel=channel, **{**launch_options, "args": args}, **context_options)
    return context, [context.close, playwright.stop]


def _executable_key(playwright: Playwright, channel: str) -> str | None:
    if channel == "chrome":
        path = _chrome_executable()
        return str(path) if path is not None else None
    exe = playwright.chromium.executable_path
    return exe if isinstance(exe, str) else None


def _cached_headful_ua(playwright: Playwright, channel: str, remote: Any) -> str:
    key = _executable_key(playwright, channel)
    if key is not None and key in _HEADFUL_UA:
        return _HEADFUL_UA[key]
    agent = _headful_user_agent(remote)
    if key is not None:
        _HEADFUL_UA[key] = agent
    return agent


def _headful_user_agent(remote: Any) -> str:
    """The user agent `remote` reports, with "HeadlessChrome" replaced by "Chrome"."""
    # Headless Chrome names itself "HeadlessChrome", which any site can read.
    probe = remote.new_context()
    try:
        agent = probe.new_page().evaluate("navigator.userAgent")
    finally:
        probe.close()
    return agent.replace("HeadlessChrome", "Chrome")


def _headful_user_agent_of(playwright: Playwright, channel: str | None) -> str:
    """The headed user agent for `channel`, cached per executable path."""
    ch = channel or "chromium"
    key = _executable_key(playwright, ch)
    if key is not None and key in _HEADFUL_UA:
        return _HEADFUL_UA[key]
    remote = playwright.chromium.launch(channel=ch, headless=True)
    try:
        agent = _headful_user_agent(remote)
    finally:
        remote.close()
    if key is not None:
        _HEADFUL_UA[key] = agent
    return agent


def _without_values(text: str, headers: Mapping[str, str]) -> str:
    """`text` with every header value in `headers` replaced by `***`, hiding tokens."""
    for value in headers.values():
        if value:
            text = text.replace(value, "***")
    return text


def _first_line(error: BaseException) -> str:
    return (str(error).splitlines() or [""])[0]


def _matcher(match: Match) -> Callable[[str], bool]:
    """`match` as a predicate over a URL."""
    if isinstance(match, str):
        return lambda url: match in url
    if isinstance(match, re.Pattern):
        return lambda url: match.search(url) is not None
    if callable(match):
        return match
    raise TypeError(
        f"match must be a str, a compiled regex, or a function, not {type(match).__name__}"
    )


def _json_bodies(response: Response, lines: bool) -> list[Any]:
    """The JSON values in `response`'s body: the whole body, or each non-blank line if `lines`."""
    try:
        body = response.text()
    # Skip unreadable bodies so one odd response doesn't stop the handler's other calls.
    except _raw_error() as error:
        # Chrome frees a page's response bodies when the page moves on (a source going from one tab
        # to the next), and a listener runs only when Playwright next pumps its events, often after
        # that: the body is gone, and it belonged to the page being left. That is expected, so it
        # is skipped without a line; any other unreadable body is still worth a debug line.
        if not _gone(error):
            ui.debug(
                "responses: no body for {url}: {error}",
                url=text.clean.url(response.url), error=_first_line(error),
            )
        return []
    pieces = [line for line in body.splitlines() if line.strip()] if lines else [body]
    parsed = []
    for piece in pieces:
        try:
            parsed.append(jsonlib.loads(piece))
        # A piece that isn't JSON (an HTML error page, say) is skipped; the others still count.
        except ValueError:
            ui.debug(
                "responses: skipped a body that is not JSON from {url}",
                url=text.clean.url(response.url),
            )
    return parsed
