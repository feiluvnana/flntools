"""Drive a real browser with Playwright's sync API: `browser.open()` gives a `Browser`, and `Browser.open(url)` a `Page`.

How it works: `open` starts Playwright's driver and launches Google Chrome through Playwright's
`chrome` channel, or attaches to a running browser over CDP. The browser runs incognito, in a
profile folder, or in a private copy of an installed Chrome profile named by `profile="Default"`.
A `Browser` wraps Playwright's `BrowserContext`: it opens tabs, and moves cookies in and out as
`shared.cookies.Jar`s. A `Page` wraps one Playwright `Page`: it loads URLs (`goto`), waits for an
element, a URL or a quiet network (`wait`), watches JSON responses and requests (`on_response`,
`on_request`, each a `Subscription`), scrolls a feed at a person's pace until it ends (`scroll`),
hands a captcha to a person (`handoff`), sends a request from the page itself (`fetch`, an
`http.Response`), and reads the live page as a `shared.selector.Selection` (`document`). Everything else is on `.raw`: the `BrowserContext` of a `Browser`, and the
`playwright.sync_api.Page` of a `Page`. Importing this module doesn't load Playwright; `open`
does, and raises `ImportError` naming the `browser` extra when it's missing.

    from shared import browser

    with browser.open(profile="Default") as chrome:          # a copy of your Chrome's profile
        tab = chrome.open("https://example.com/", idle=True)
        tab.wait(".grid")
        with tab.on_response("graphql", lambda data, response: found.append(data)):
            tab.scroll(until=lambda: len(found) > 100)
        tab.fetch("/api/items", headers={"x-csrf": token}).json     # with the tab's cookies and origin
        tab.document.css("h1").text, tab.url, tab.title

Threads: Playwright's sync API is single-threaded. A `Browser`, its `Page`s and their
`Subscription`s may be used only on the thread that called `open`, and handlers run on that
thread, inside whichever call is waiting (`goto`, `wait`, `scroll`, `handoff`, `fetch`). Several threads
may each open their own browser, as flnd's sources do; the only thing they share is the offer to
download Chromium, which is behind a lock.
"""

from __future__ import annotations

import base64
import json as jsonlib  # `json` is also `Page.fetch`'s keyword, as in `http.fetch`
import os
import random
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable, Iterable, Literal, Mapping, Sequence, Union, overload

import shared
from shared import _prompt as prompt
from shared._cookies import Jar
from shared._log import log

if TYPE_CHECKING:
    from shared import _http as http
    from shared._selector import Selection
    from playwright.sync_api import BrowserContext, Locator, Playwright, Request, Response
    from playwright.sync_api import Page as RawPage

__all__ = ["Browser", "Error", "Page", "Subscription", "WaitError", "open"]

LAUNCH_ARGS = ["--disable-blink-features=AutomationControlled", "--disable-infobars"]
HIDE_WEBDRIVER = "Object.defineProperty(navigator, 'webdriver', {get: () => undefined})"
PAGE_HEIGHT = "(document.scrollingElement || document.documentElement).scrollHeight"
# Half a screen from the end counts as the bottom, so the odd small scroll up doesn't reset the end rule.
NEAR_BOTTOM = ("(() => { const page = document.scrollingElement || document.documentElement;"
               " return page.scrollTop + innerHeight * 1.5 >= page.scrollHeight; })()")
# The same two reads for `scroll(within=…)`, on the element that scrolls instead of the page.
ELEMENT_HEIGHT = "element => element.scrollHeight"
ELEMENT_NEAR_BOTTOM = "element => element.scrollTop + element.clientHeight * 1.5 >= element.scrollHeight"
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
    // In slices: String.fromCharCode takes its bytes as arguments, and a call has a limit on those.
    for (let i = 0; i < bytes.length; i += 0x8000) text += String.fromCharCode(...bytes.subarray(i, i + 0x8000));
    return {resolved, status: response.status, url: response.url, headers: [...response.headers], body: btoa(text)};
  } catch (error) {
    return {resolved, error: error.name === "AbortError" ? "timeout" : `${error.name}: ${error.message}`};
  } finally {
    clearTimeout(timer);
  }
}"""
PROFILE_ROOTS = {"darwin": "Library/Application Support/Google/Chrome", "linux": ".config/google-chrome",
                 "win32": "AppData/Local/Google/Chrome/User Data"}
# A copy of an installed profile opens only in Google Chrome: its cookies are sealed with Chrome's key.
PROFILE_CHANNEL: str | None = "chrome"
# Where Playwright's `chrome` channel finds Google Chrome: the same paths as its registry
# (`_createChromiumChannel` in the driver, Playwright 1.65). On Windows each is tried under
# %LOCALAPPDATA%, %PROGRAMFILES% and %PROGRAMFILES(X86)%, as Playwright does.
CHROME_PATHS = {"darwin": "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
                "linux": "/opt/google/chrome/chrome", "win32": "Google/Chrome/Application/chrome.exe"}
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


class _NeverRaised(Exception):
    """Stands for Playwright's error classes until `open` imports them, so nothing is caught by mistake."""


# open sets these to Playwright's error classes, so importing this module never loads Playwright.
PlaywrightError: type[Exception] = Exception
PlaywrightTimeout: type[Exception] = _NeverRaised
# Every random choice in scrolling comes from here, so a test can seed it.
_random = random.Random()
# Scrolling reads the time here, and waits on Playwright's clock, so a test can fake both.
_clock = time.monotonic
# Seconds between two checks for a scroll's progress: short next to a page's load, cheap for the page.
POLL = 0.05
# Set when the Chromium download was declined (or couldn't be asked), so it isn't asked again.
_chromium_declined = False
# The error the Chromium download failed with, raised again without asking for the rest of the process.
_chromium_failed: RuntimeError | None = None
# Each source's session opens its browser on its own thread, so two can find Chromium missing at
# once: the check, the question and the download are one step, or both would ask and both download.
_chromium_offer = threading.Lock()


class Error(shared.Error):
    """The base of every error `shared.browser` raises."""


class WaitError(Error):
    """`Page.wait`'s condition didn't come true within its `timeout`.

    Not a subclass of the builtin `TimeoutError`, which is an `OSError`: a page that is slow to
    show an element isn't a failure of the operating system, and `except OSError` mustn't catch it.
    """


def open(*, profile: str | Path | None = None, cdp: str | None = None, headless: bool = False,
         viewport: tuple[int, int] = (1280, 900), locale: str | None = None, timezone: str | None = None,
         args: Sequence[str] = (), stealth: bool = True) -> Browser:
    """Launch or attach to a browser, and return it as a `Browser` for a `with` block.

        with browser.open() as chrome:                       # a visible, incognito Google Chrome
            tab = chrome.open("https://example.com/")
        with browser.open(profile="Profile 1") as chrome: ...  # a copy of that installed profile
        with browser.open(profile=Path(".flnd/browser")) as chrome: ...  # a folder, kept between runs

    Args:
        profile: None runs incognito, and nothing is kept. A `str` with no path separator is the
            name of an installed Google Chrome profile ("Default", "Profile 1"): it is copied to a
            private temporary folder, without its caches, and opened in Google Chrome itself, so
            Chrome can stay open and nothing is written back; the copy is deleted on close, or
            when `open` fails. A `Path`, or a `str` with a separator ("./profile"), is a folder
            used as a persistent profile, created when missing, in whichever browser `open`
            launches. Pass `Path("Default")` for a folder of that name in the current directory.
        cdp: a running browser's DevTools endpoint (`http://127.0.0.1:9222`, or a cloud browser's
            `wss://` URL) to attach to instead of launching one; it keeps running after close. A
            local Chrome allows that only when started with `--remote-debugging-port` and its own
            `--user-data-dir`, not your default profile.
        headless: run without a window. False, the default, shows one, so a person can solve a
            captcha; headless mode reports "Chrome" where it would say "HeadlessChrome".
        viewport: the tabs' (width, height) in CSS pixels; with `cdp=`, set on each tab opened.
        locale: the browser's language, such as "fr-FR"; None keeps the system's.
        timezone: the browser's clock, such as "Europe/Paris"; None keeps the system's.
        args: more command-line switches for the browser, after `LAUNCH_ARGS`.
        stealth: hide `navigator.webdriver` and the "controlled by automated software" bar.

    Returns:
        A `Browser`; `close()` or the end of `with` stops it.

    Raises:
        ValueError: `cdp=` together with `profile`, `headless=True`, `locale`, `timezone`, `args`
            or `stealth=False`, which only apply to a browser launched here; or an empty `profile`.
        FileNotFoundError: no installed Chrome profile has the name `profile`.
        ImportError: Playwright isn't installed (the `browser` extra).
        RuntimeError: Google Chrome isn't installed and the Chromium download was declined, wasn't
            possible to ask (no terminal), or failed.

    It launches Google Chrome, installed the usual way, through Playwright's `chrome` channel:
    Playwright's own Chromium differs in codecs and fonts, which fingerprinting sees. Without
    Chrome, it uses the Chromium it downloaded before, in Chrome's new headless mode when
    headless. With neither, it asks once, in a terminal, whether to download Chromium (about
    200 MB) with Playwright's own installer, under a progress row; that question holds the live
    rows while it's open. A copied profile never falls back to Chromium. The browser keeps its
    own user agent. Call it from the thread that will use the browser (see the module's docstring).
    """
    launch_only = profile is not None or headless or args or not stealth or locale or timezone
    if cdp is not None and launch_only:
        raise ValueError("open(cdp=...) uses the running browser as it is: "
                         "profile, headless, locale, timezone, args and stealth don't apply")
    name = _profile_name(profile)
    # Copied before Playwright starts, so a misspelt name fails at once. From here the copy holds
    # the profile's logins (Telegram Web's among them), so it is deleted however open ends
    # without a launched browser.
    copy = _copy_profile(name) if name is not None else None
    launched = False
    try:
        playwright = _start_playwright()
        try:
            if cdp is None:
                started = _launch(playwright, copy if copy is not None else profile, copied=copy is not None,
                                  headless=headless, viewport=viewport, locale=locale, timezone=timezone,
                                  args=args, stealth=stealth)
            else:
                remote = playwright.chromium.connect_over_cdp(cdp)
                context = remote.contexts[0] if remote.contexts else remote.new_context()
                # Closing a browser reached over CDP only disconnects; the context is the user's, so it stays open.
                started = Browser(context, close_steps=[remote.close, playwright.stop], headless=False,
                                  page_viewport=viewport)
            launched = True
            return started
        # Any exception, Ctrl-C included, stops Playwright's driver before it goes on up.
        except BaseException:
            playwright.stop()
            raise
    finally:
        # Once launched, the Browser's close deletes the copy, after Chrome has stopped writing into it.
        if copy is not None and not launched:
            shutil.rmtree(copy, ignore_errors=True)


def _profile_name(profile: str | Path | None) -> str | None:
    """The installed Chrome profile `profile` names, or None when it is a folder (or None).

    A name is a `str` with no path separator: profile names never have one, and a folder can
    always be written with one (`./profile`) or as a `Path`.
    """
    if profile is None or not isinstance(profile, str):
        return None
    if not profile:
        raise ValueError("profile is empty: give a Chrome profile's name, such as 'Default', or a folder")
    separators = {"/", os.sep, os.altsep} - {None}
    if any(separator in profile for separator in separators) or profile in (".", ".."):
        return None
    return profile


def _profile_root() -> Path:
    """The folder that holds Google Chrome's profiles on this platform."""
    return Path.home() / PROFILE_ROOTS[sys.platform if sys.platform in ("darwin", "win32") else "linux"]


def _copy_profile(name: str) -> Path:
    """Copy the installed Chrome profile `name` to a new private temporary folder, as its `Default`.

    Raises `FileNotFoundError` when Chrome has no profile of that name. A copy that fails half
    way is deleted before the error goes on.
    """
    root = _profile_root()
    if not (root / name).is_dir():
        raise FileNotFoundError(f"no Chrome profile named {name!r} in {root}")
    # mkdtemp makes the folder with mode 0700, so on a shared machine no other user can read the
    # copy, which holds the profile's logins. Windows ignores the mode; the user's temp folder is private there.
    copy = Path(tempfile.mkdtemp(prefix="shared-chrome-"))
    try:
        # Whatever its name, the copy holds the profile as Default, and _launch opens Default.
        shutil.copytree(root / name, copy / "Default", ignore=SKIP_IN_PROFILE, ignore_dangling_symlinks=True)
        if (root / "Local State").is_file():
            shutil.copy2(root / "Local State", copy / "Local State")
    # Any exception, Ctrl-C included, deletes the half-made copy before it goes on up: nothing
    # else knows of it yet, and it may already hold the profile's logins.
    except BaseException:
        shutil.rmtree(copy, ignore_errors=True)
        raise
    return copy


class Browser:
    """A running browser: open tabs in it, and read or add its cookies. `raw` is Playwright's `BrowserContext`.

        with browser.open() as chrome:
            tab = chrome.open("https://example.com/")
            chrome.add_cookies(cookies.load("example_cookies.txt")); chrome.cookies.header; chrome.pages

    Made by `browser.open`, not directly. Use it only on the thread that opened it.
    """

    def __init__(self, context: BrowserContext, *, close_steps: list[Callable[[], Any]], headless: bool,
                 page_viewport: tuple[int, int] | None = None):
        self.raw = context
        self._close_steps = close_steps
        self._headless = headless
        self._page_viewport = page_viewport
        self._wrappers: dict[RawPage, Page] = {}
        self._opened: list[Page] = []
        self._closed = False

    def __enter__(self) -> Browser:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    def open(self, url: str | None = None, *, idle: bool = False, timeout: float = 30.0) -> Page:
        """Open a new tab, and go to `url` when given.

            tab = chrome.open("https://example.com/", idle=True)

        Args:
            url: where the tab goes, as `Page.goto`; None leaves it on `about:blank`.
            idle: also wait until the network has been quiet for a moment, not only for `load`.
            timeout: seconds before the navigation counts as failed.

        Returns:
            The tab, which `close()` closes; the browser's close closes it too. A navigation error
            is logged, not raised, and leaves the tab open on `about:blank`.
        """
        raw = self.raw.new_page()
        if self._page_viewport is not None:
            raw.set_viewport_size({"width": self._page_viewport[0], "height": self._page_viewport[1]})
        page = self._wrap(raw)
        self._opened.append(page)
        if url is not None:
            page.goto(url, idle=idle, timeout=timeout)
        return page

    @property
    def pages(self) -> list[Page]:
        """Every open tab, including ones the site or a person opened."""
        return [self._wrap(raw) for raw in self.raw.pages]

    @property
    def cookies(self) -> Jar:
        """Every cookie in the browser, as a `shared.cookies.Jar`: a snapshot, which doesn't follow later changes."""
        return Jar(dict(cookie) for cookie in self.raw.cookies())

    def add_cookies(self, jar: Jar | Iterable[dict]) -> None:
        """Add each cookie to the browser unchanged, except that `path` defaults to `/`.

            chrome.add_cookies(cookies.load("example_cookies.txt"))
            chrome.add_cookies([{"name": "sid", "value": "1", "domain": ".example.com"}])

        Args:
            jar: a `Jar`, or any iterable of cookie dicts in Playwright's shape.

        They go in one at a time, so a rejected cookie is logged by name and the rest still go
        in. The log line never holds the cookie's value, even when Playwright's error quotes it.
        """
        for cookie in jar:
            try:
                self.raw.add_cookies([{**cookie, "path": cookie.get("path") or "/"}])  # type: ignore[list-item]
            # Playwright's Error is what a cookie it won't take (no domain, a bad expiry) raises;
            # anything else is a real failure and goes on up.
            except PlaywrightError as error:
                log.warn("cookie {name} was rejected: {error}", name=cookie.get("name", ""),
                         error=_without(_first_line(error), str(cookie.get("value", ""))))

    def clear_cookies(self) -> None:
        """Delete every cookie in the browser."""
        self.raw.clear_cookies()

    def close(self) -> None:
        """Close the tabs, the context, the browser and Playwright, in order: the same as leaving `with`.

            chrome.close()

        An attached (`cdp=`) browser keeps running, and a copied profile is deleted once the
        browser has stopped. A failure while closing is logged once, and never raised, so it
        can't hide the error that made a `with` block exit. A second call does nothing.
        """
        if self._closed:
            return
        self._closed = True
        failures = []
        for step in [page.raw.close for page in self._opened] + self._close_steps:
            try:
                step()
            # Every step runs even when one fails, so Playwright's driver is always stopped.
            except Exception as error:
                failures.append(error)
        if failures:
            log.warn("closing the browser failed: {error}", error=_first_line(failures[0]))

    def _wrap(self, raw: RawPage) -> Page:
        if raw not in self._wrappers:
            self._wrappers[raw] = Page(raw, self)
        return self._wrappers[raw]


def _without(text: str, secret: str) -> str:
    """`text` with every occurrence of `secret` replaced by `…`, so a log line can't leak it."""
    return text.replace(secret, "…") if secret else text


class Subscription:
    """A handler added by `Page.on_response` or `Page.on_request`, until `stop()` or the end of `with`.

        with tab.on_response("graphql", handler):
            tab.scroll()
        watching = tab.on_request("/api/", handler); ...; watching.stop()

    Stop it on the browser's thread, as everything else here.
    """

    def __init__(self, stop: Callable[[], None]):
        self._stop = stop
        self._stopped = False

    def __enter__(self) -> Subscription:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.stop()

    def stop(self) -> None:
        """Remove the handler, so it isn't called again; a second call does nothing."""
        if not self._stopped:
            self._stopped = True
            self._stop()


class Page:
    """One tab. `raw` is Playwright's `Page`, for `click`, `fill`, `evaluate` and everything else.

        tab = chrome.open("https://example.com/")
        with tab.on_response("graphql", handler):
            tab.scroll(count=lambda: len(posts))
        title = tab.document.css("h1").text

    Made by `Browser.open` or `Browser.pages`, not directly. Use it only on the browser's thread.
    """

    def __init__(self, raw: RawPage, browser: Browser):
        self.raw = raw
        self.browser = browser
        self._responses_seen = 0

    def goto(self, url: str, *, idle: bool = False, timeout: float = 30.0) -> int | None:
        """Go to `url`, and return the HTTP status of its main response.

            status = tab.goto("https://example.com/", idle=True)
            if status is None or status >= 400: ...

        Args:
            url: the address to load in this tab.
            idle: also wait until the network has been quiet for a moment, not only for the `load` event.
            timeout: seconds before the navigation counts as failed.

        Returns:
            The main response's status, such as 200 or 429, so a caller can tell a rate limit or an
            error page from a real load. None when it didn't load (a navigation error, which is
            logged, not raised), and also when no response came: Playwright reports none for a
            move within the same document, such as a new `#fragment`.
        """
        try:
            response = self.raw.goto(url, wait_until="networkidle" if idle else "load", timeout=timeout * 1000)
        # Playwright's Error covers DNS failures, aborted requests and timeouts: each means "not loaded".
        except PlaywrightError as error:
            log.warn("could not open {url}: {error}", url=url, error=_first_line(error))
            return None
        return response.status if response is not None else None

    def wait(self, css: str | None = None, *, url: str | re.Pattern[str] | Callable[[str], bool] | None = None,
             idle: bool = False, timeout: float = 30.0) -> None:
        """Wait until an element matches `css`, the URL matches `url`, and with `idle` the network is quiet.

            tab.wait(".grid")
            tab.wait(url="**/reels/")
            tab.wait(idle=True)
            tab.wait()                     # the `load` event

        Args:
            css: a CSS selector; waits until a visible element matches it. None doesn't wait for one.
            url: a glob ("**/feed/"), a compiled regex or a predicate over the URL; waits until the
                tab's URL matches it. None doesn't wait for one.
            idle: also wait until the network has been quiet for 500 ms. With neither `css` nor
                `url`, False waits for the `load` event instead.
            timeout: seconds for every condition together, not for each.

        Raises:
            WaitError: `timeout` seconds passed first. Its message names what was waited for.

        The conditions are waited for in the order above. Handlers keep running during the wait.
        """
        deadline = time.monotonic() + timeout

        def left() -> float:
            # Playwright reads a timeout of 0 as "no timeout", so the last moment is 1 ms, not 0.
            return max(1.0, (deadline - time.monotonic()) * 1000)

        what = "the page to load"
        try:
            if css is not None:
                what = f"an element to match {css!r}"
                self.raw.wait_for_selector(css, timeout=left())
            if url is not None:
                what = f"the URL to match {url!r}" if not callable(url) else "the URL to match a predicate"
                self.raw.wait_for_url(url, timeout=left())
            if idle or (css is None and url is None):
                what = "the network to go quiet" if idle else "the page to load"
                self.raw.wait_for_load_state("networkidle" if idle else "load", timeout=left())
        # Only Playwright's TimeoutError is "too slow"; any other error (a closed tab) goes on up as it is.
        except PlaywrightTimeout:
            raise WaitError(f"waited {timeout:g} s for {what}") from None

    def on_response(self, match: Match, handler: Callable[[Any, Response], Any], *,
                    lines: bool = False) -> Subscription:
        """Call `handler(data, response)` with each matching response's JSON, until the subscription stops.

            with tab.on_response("graphql", lambda data, response: posts.extend(data["items"])):
                tab.scroll()

        Args:
            match: a substring of the response's URL, a compiled regex searched in it, or a
                predicate over it (`lambda url: url.endswith("/feed")`).
            handler: called with the parsed body and Playwright's `Response`.
            lines: parse the body as newline-delimited JSON, with one call per line.

        Returns:
            A `Subscription`: `stop()` it, or use it in a `with` block.

        A body that isn't JSON is skipped, and an exception in `handler` is logged while later
        responses keep arriving. Each matching response also counts as progress for `scroll`.
        """
        matches = _matcher(match)

        def listener(response: Response) -> None:
            if not matches(response.url):
                return
            self._responses_seen += 1
            for parsed in _json_bodies(response, lines):
                try:
                    handler(parsed, response)
                # A handler is a source's code, called from inside Playwright's event loop: an
                # exception that went on up would be lost there, and stop every later response.
                except Exception as error:
                    log.warn("on_response handler failed for {url}: {error}", url=response.url,
                             error=f"{type(error).__name__} {error}".strip())

        self.raw.on("response", listener)
        return Subscription(lambda: self.raw.remove_listener("response", listener))

    def on_request(self, match: Match, handler: Callable[[Request], Any]) -> Subscription:
        """Call `handler(request)` for each matching request, until the subscription stops.

            with tab.on_request("/api/", lambda request: urls.append(request.url)): ...

        Args:
            match: a substring of the request's URL, a compiled regex searched in it, or a predicate over it.
            handler: called with Playwright's `Request`.

        Returns:
            A `Subscription`: `stop()` it, or use it in a `with` block.
        """
        matches = _matcher(match)

        def listener(request: Request) -> None:
            if matches(request.url):
                handler(request)

        self.raw.on("request", listener)
        return Subscription(lambda: self.raw.remove_listener("request", listener))

    @overload
    def fetch(self, url: str, *, method: str = ..., headers: Mapping[str, str] | None = ..., data: Any = ...,
              json: Any = ..., soft: Literal[False] = ..., timeout: float = ...) -> http.Response: ...
    @overload
    def fetch(self, url: str, *, method: str = ..., headers: Mapping[str, str] | None = ..., data: Any = ...,
              json: Any = ..., soft: Literal[True], timeout: float = ...) -> http.Response | None: ...
    @overload
    def fetch(self, url: str, *, method: str = ..., headers: Mapping[str, str] | None = ..., data: Any = ...,
              json: Any = ..., soft: bool, timeout: float = ...) -> http.Response | None: ...
    def fetch(self, url: str, *, method: str = "GET", headers: Mapping[str, str] | None = None, data: Any = None,
              json: Any = None, soft: bool = False, timeout: float = 30.0) -> http.Response | None:
        """Send one request from the page itself, by its own `fetch()`, and return its `http.Response`.

            items = tab.fetch("/api/items?page=2", headers={"x-csrf": token}).json
            tab.fetch("https://example.com/api/like", method="POST", json={"id": 1})
            tab.fetch("/example_user/", soft=True)                  # None for 401, 403, 404

        Args:
            url: the URL to request; a relative one is resolved against the page's, as the page's
                own scripts would.
            method: the HTTP method, such as "GET" or "POST".
            headers: headers for this request, on top of the ones the browser adds; None adds none.
                The browser refuses to set some itself (`Cookie`, `Host`, `User-Agent`, …).
            data: a form body (a dict, sent URL-encoded), text, or raw bytes; None sends none.
            json: a value to send as a JSON body, with `Content-Type: application/json` unless
                `headers` names another; None sends none.
            soft: True returns None for a 401, 403 or 404 instead of raising `http.RefusedError`.
            timeout: seconds for the whole request, body included; the page aborts it then.

        Returns:
            The response of a 1xx, 2xx or 3xx with its whole body (redirects are followed), or None
            for a 401, 403 or 404 when `soft` is True. Its `headers` are what the page's `fetch()`
            may read: never `Set-Cookie`, and for another origin only those its CORS headers expose.

        Raises:
            http.RateLimitedError: a 429, with the `Retry-After` seconds when the page may read them.
            http.RefusedError: any other 4xx, or a 401, 403 or 404 without `soft`.
            http.ServerError: a 5xx. There is one attempt: the caller decides whether to try again.
            http.Error: no answer within `timeout`, or the browser refused or couldn't send the
                request (a dead connection, an origin whose CORS headers don't allow it).
            browser.Error: the page couldn't run it, such as when it navigated away meanwhile.
            ValueError: both `data` and `json` were given.

        Because the page sends it, the request carries everything the page's own would: its
        cookies (for another origin too, when that origin's CORS headers allow it), its `Origin`
        and `Referer`, and the browser's TLS fingerprint. No message names a header value, a
        cookie or the URL's query. It needs the `http` extra for `http.Response`. Like every
        `Page` call, use it only on the browser's thread: Playwright's sync API is single-threaded,
        and handlers keep running while it waits.
        """
        from shared import _http

        if data is not None and json is not None:
            raise ValueError("pass data or json, not both")
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
        request = {"url": url, "method": method, "headers": sent, "form": form, "ms": timeout * 1000,
                   "body": None if body is None else base64.b64encode(body).decode("ascii")}
        try:
            result = self.raw.evaluate(IN_PAGE_FETCH, request)
        # A tab that navigated, closed or crashed meanwhile: the page is gone, not the request refused.
        except PlaywrightError as error:
            raise Error(_without_values(f"{method} {_http._where(url)}: the page couldn't send it: "
                                        f"{_first_line(error)}", sent)) from None
        where = f"{method} {_http._where(result['resolved'])}"
        if "error" in result:
            if result["error"] == "timeout":
                raise _http.Error(f"{where}: no answer within {timeout:g} s")
            raise _http.Error(_without_values(f"{where}: {result['error']}", sent))
        response = _http.Response(status=result["status"], url=result["url"], headers=dict(result["headers"]),
                                  content=base64.b64decode(result["body"]))
        return _http._checked(response, method, soft=soft)

    def scroll(self, *, within: str | Locator | None = None, until: Callable[[], bool] | None = None,
               count: Callable[[], int] | None = None, idle: float = 10.0, wait: float = 3.0, pause: float = 0.3,
               on_step: Callable[[], Any] | None = None) -> int:
        """Scroll like a person until the feed ends, as fast as it loads, and return the number of scrolls.

            tab.scroll(count=lambda: len(posts), until=lambda: len(posts) >= 500)
            tab.scroll(within="[role=dialog] .list")      # a list that scrolls inside the page

        Each scroll moves the mouse and turns the wheel in uneven steps. It then waits for the
        page's progress, checked every 50 ms: the page (or `within`) grew, `count()` rose, or an
        `on_response` handler's response arrived. Then it pauses `pause` seconds plus 0 to 40%,
        so two scrolls never come back to back, and scrolls again. A fast page is scrolled two or
        three times a second; a slow one is waited out.

        Args:
            within: the element that scrolls, as a CSS selector (its first match) or a Playwright
                `Locator`: the mouse goes over it, and its own height and end are watched. None
                scrolls the page.
            until: called before each scroll; True stops. None goes on until the feed ends.
            count: a number whose rise is progress, such as the items found so far, or any traffic
                the caller watches. None leaves it out.
            idle: seconds at the end with no progress after which the feed has ended, however
                many scrolls that takes.
            wait: the longest wait for progress after one scroll, in seconds.
            pause: the least seconds between the end of that wait and the next scroll: the floor,
                which a caller raises when the site has asked it to slow down.
            on_step: called after each scroll's pause, such as a spinner's advance.

        Returns:
            The number of scrolls done.

        Raises:
            Error: `within` matches no visible element.

        Waits run on Playwright's clock, so `on_*` handlers keep running during them.
        """
        target = self.raw.locator(within).first if isinstance(within, str) else within
        # Checked at once: a locator's reads would otherwise wait 30 s for an element that never comes.
        if target is not None and not target.is_visible():
            raise Error("scroll(within=…) found no visible element to scroll")
        marker = self._progress_marker(target, count)
        quiet_since: float | None = None                # when the scrolls with no progress at the end began
        scrolls = 0
        while until is None or not until():
            started = _clock()
            self._move(target)
            scrolls += 1
            # Away from the end there is nothing to wait for: feeds load more only when their end
            # comes into view. At the end, the wait never runs past the end of the quiet spell.
            at_bottom = self._at_bottom(target)
            if not at_bottom:
                longest = 0.0
            elif quiet_since is None:
                longest = wait
            else:
                longest = min(wait, quiet_since + idle - _clock())
            previous, marker = marker, self._wait_for_progress(target, marker, count, longest)
            self._pause(pause)
            if on_step is not None:
                on_step()
            if any(now > before for now, before in zip(marker, previous)) or not at_bottom:
                quiet_since = None
                continue
            if quiet_since is None:
                quiet_since = started
            if _clock() - quiet_since >= idle:
                break
            self._jiggle(target, pause)
        return scrolls

    def handoff(self, *, until: Callable[[], bool], message: str, poll: float = 1.0,
                timeout: float | None = None) -> bool:
        """Wait for a person to do a step, such as a captcha or a login, and return whether it got done.

            tab.handoff(until=lambda: not tab.raw.locator("#captcha").count(), message="Solve the captcha")

        It shows `message` and a spinner, and polls `until()` every `poll` seconds. False means
        `timeout` passed first. A headless browser raises `RuntimeError`, because nobody can see it.
        """
        if until():
            return True
        if self.browser._headless:
            raise RuntimeError(f"cannot hand off to a person in a headless browser: {message}")
        log.warn(message)
        deadline = None if timeout is None else time.monotonic() + timeout
        with log.progress(message) as bar:
            while not until():
                if deadline is not None and time.monotonic() >= deadline:
                    bar.fail()
                    return False
                self.raw.wait_for_timeout(poll * 1000)
        return True

    @property
    def document(self) -> Selection:
        """The live page as a `selector.Selection`, read fresh each time.

            tab.document.css("h1").text; tab.document.script(id="__NEXT_DATA__").jmespath("props").first

        It is a snapshot: a later change to the page needs another read. The HTML is `.raw.get()`.
        Needs the `selector` extra, and raises `selector.UnsafeError` when the page is over 64 MB.
        """
        # Imported here, so a script that only drives the browser doesn't need parsel.
        from shared._selector import parse

        return parse(self.raw.content())

    @property
    def url(self) -> str:
        """The tab's current URL."""
        return self.raw.url

    @property
    def title(self) -> str:
        """The page's `<title>`, or "" without one."""
        return self.raw.title()

    def close(self) -> None:
        """Close the tab."""
        self.raw.close()

    def _progress_marker(self, target: Locator | None, count: Callable[[], int] | None) -> tuple[int, int, int]:
        """The numbers whose rise is progress: the scrolled height, `count()` and the responses seen."""
        height = self.raw.evaluate(PAGE_HEIGHT) if target is None else target.evaluate(ELEMENT_HEIGHT)
        return height, count() if count is not None else 0, self._responses_seen

    def _at_bottom(self, target: Locator | None) -> bool:
        return self.raw.evaluate(NEAR_BOTTOM) if target is None else target.evaluate(ELEMENT_NEAR_BOTTOM)

    def _wait_for_progress(self, target: Locator | None, before: tuple[int, ...], count: Callable[[], int] | None,
                           longest: float) -> tuple[int, int, int]:
        """Poll every `POLL` seconds until a number of `before` rose or `longest` seconds passed; return the marker."""
        deadline = _clock() + longest
        while True:
            marker = self._progress_marker(target, count)
            if any(now > then for now, then in zip(marker, before)) or _clock() >= deadline:
                return marker
            self.raw.wait_for_timeout(POLL * 1000)

    def _area(self, target: Locator | None) -> tuple[float, float, float, float]:
        """The visible part of what scrolls, as (x, y, width, height) in the viewport's pixels."""
        width, height = self.raw.evaluate(VIEWPORT)
        if target is None:
            return 0.0, 0.0, width, height
        try:
            # A short timeout: the element was there a moment ago, so one that is gone now won't come back.
            box = target.bounding_box(timeout=1000)
        # Only Playwright's TimeoutError means "gone"; it is reported as the element missing, below.
        except PlaywrightTimeout:
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
        """One scroll: the mouse over the area, then the wheel down most of its height, now and then a little back up."""
        left, top, width, height = self._area(target)
        self.raw.mouse.move(left + width * _random.uniform(0.25, 0.75), top + height * _random.uniform(0.3, 0.7),
                            steps=_random.randint(2, 6))
        self._wheel(height * _random.uniform(0.6, 0.9))
        if _random.random() < 1 / 8:
            self._wheel(-height * _random.uniform(0.1, 0.25))

    def _wheel(self, distance: float) -> None:
        weights = [_random.uniform(0.5, 1.5) for _ in range(_random.randint(3, 6))]
        total = sum(weights)
        for weight in weights:
            self.raw.mouse.wheel(0, distance * weight / total)
            self.raw.wait_for_timeout(_random.uniform(20, 60))

    def _pause(self, floor: float) -> None:
        # Jitter only adds: a floor is the least a site is given, so it is never cut short.
        self.raw.wait_for_timeout(floor * _random.uniform(1.0, 1.4) * 1000)

    def _jiggle(self, target: Locator | None, floor: float) -> None:
        # Most feeds load the next page when their end comes back into view, so up a screen and down
        # wakes them. The mouse is still over the area from the scroll before.
        height = self._area(target)[3]
        self._wheel(-height)
        self._pause(floor)
        self._wheel(height)


def _start_playwright() -> Playwright:
    """Start Playwright's driver, and point `PlaywrightError` and `PlaywrightTimeout` at its error classes."""
    global PlaywrightError, PlaywrightTimeout
    try:
        from playwright.sync_api import Error, TimeoutError, sync_playwright
    except ImportError:
        raise ImportError("shared.browser needs playwright: pip install 'flntools[browser]'") from None
    PlaywrightError, PlaywrightTimeout = Error, TimeoutError
    return sync_playwright().start()


def _launch(playwright: Playwright, profile: str | Path | None, *, copied: bool, headless: bool,
            viewport: tuple[int, int], locale: str | None, timezone: str | None, args: Sequence[str],
            stealth: bool) -> Browser:
    """Launch a browser for `open`; `copied` says `profile` is a copy of an installed Chrome profile.

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
        launch_options["args"].append("--profile-directory=Default")
    context, close_steps = _open_context(playwright, profile, channel, launch_options, context_options)
    if stealth:
        context.add_init_script(HIDE_WEBDRIVER)
    if copied:
        copy = Path(str(profile))
        # Removed after Chrome has closed, or Chrome would still be writing into it.
        close_steps.append(lambda: shutil.rmtree(copy, ignore_errors=True))
    return Browser(context, close_steps=close_steps, headless=headless)


def _channel(playwright: Playwright) -> str:
    """The channel `open` launches: `chrome` when Google Chrome is installed, `chromium` otherwise.

    Without Chrome or a downloaded Chromium, it asks to download Chromium, and raises
    `RuntimeError(NEEDS_CHROME)` on no or without a terminal. A failed download raises its
    `RuntimeError`, and the same one again, without asking, for the rest of the process.
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
            raise RuntimeError(NEEDS_CHROME)
        try:
            _install_chromium()
        except RuntimeError as error:
            # A failed download (no space, no network) would most likely fail again, after asking again.
            _chromium_failed = error
            raise
    return "chromium"


def _chrome_installed() -> bool:
    """Whether Google Chrome is where Playwright's `chrome` channel looks for it."""
    platform = sys.platform if sys.platform in CHROME_PATHS else "linux"
    if platform != "win32":
        return Path(CHROME_PATHS[platform]).is_file()
    roots = [os.environ[name] for name in WINDOWS_ROOTS if os.environ.get(name)]
    return any((Path(root) / CHROME_PATHS["win32"]).is_file() for root in roots)


def _install_chromium() -> None:
    """Download Chromium with Playwright's own installer, under a progress row, into Playwright's user cache.

    `--no-shell` skips the separate headless shell: `open` runs Chromium itself in Chrome's
    new headless mode (the `chromium` channel), so the shell would never be used.

    Raises:
        RuntimeError: the installer failed, with its last line of output.
    """
    # A bundled flnd has no `python -m playwright`, so this runs the driver Playwright ships, as that command does.
    from playwright._impl._driver import compute_driver_executable, get_driver_env

    node, cli_js = compute_driver_executable()
    lines: list[str] = []
    with log.progress("Downloading Chromium", total=100) as bar:
        # The installer's output is read here, not shown: its own bars would tear the live area.
        with subprocess.Popen([node, cli_js, "install", "--no-shell", "chromium"], env=get_driver_env(),
                              stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                              encoding="utf-8", errors="replace") as installer:
            for line in installer.stdout or ():
                lines.append(line.strip())
                if found := INSTALL_PERCENT.search(line):
                    bar.count = int(found[1])
        if installer.returncode:
            last = next((line for line in reversed(lines) if line), "no output")
            raise RuntimeError(f"Chromium was not downloaded: Playwright's installer exited with "
                               f"{installer.returncode}: {last}")


def _open_context(playwright: Playwright, profile: str | Path | None, channel: str | None,
                  launch_options: dict[str, Any], context_options: dict[str, Any]) -> tuple[BrowserContext, list]:
    headless = launch_options["headless"]
    if profile is None:
        remote = playwright.chromium.launch(channel=channel, **launch_options)
        options = {**context_options, "user_agent": _headful_user_agent(remote)} if headless else context_options
        context = remote.new_context(**options)
        return context, [context.close, remote.close, playwright.stop]
    args = launch_options["args"]
    if headless:
        args = [*args, f"--user-agent={_headful_user_agent_of(playwright, channel)}"]
    context = playwright.chromium.launch_persistent_context(
        str(profile), channel=channel, **{**launch_options, "args": args}, **context_options)
    return context, [context.close, playwright.stop]


def _headful_user_agent(remote: Any) -> str:
    # Headless Chrome names itself "HeadlessChrome", which any site can read; everything else stays its own.
    probe = remote.new_context()
    try:
        agent = probe.new_page().evaluate("navigator.userAgent")
    finally:
        probe.close()
    return agent.replace("HeadlessChrome", "Chrome")


def _headful_user_agent_of(playwright: Playwright, channel: str | None) -> str:
    # A persistent profile can't be relaunched with a new user agent, so a throwaway browser reads it first.
    remote = playwright.chromium.launch(channel=channel, headless=True)
    try:
        return _headful_user_agent(remote)
    finally:
        remote.close()


def _without_values(text: str, headers: Mapping[str, str]) -> str:
    """`text` with every header value of `headers` replaced by `***`, so no token reaches a message."""
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
    raise TypeError(f"match must be a str, a compiled regex, or a function, not {type(match).__name__}")


def _json_bodies(response: Response, lines: bool) -> list[Any]:
    try:
        body = response.text()
    except PlaywrightError as error:
        log.debug("on_response: no body for {url}: {error}", url=response.url, error=_first_line(error))
        return []
    pieces = [line for line in body.splitlines() if line.strip()] if lines else [body]
    parsed = []
    for piece in pieces:
        try:
            parsed.append(jsonlib.loads(piece))
        except ValueError:
            log.debug("on_response: skipped a body that is not JSON from {url}", url=response.url)
    return parsed
