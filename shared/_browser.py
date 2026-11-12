"""Playwright's sync API as two small wrappers: `connect()` gives a `Browser`, and `open_page()` a `Page`."""

from __future__ import annotations

import json
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
from typing import TYPE_CHECKING, Any, Callable, Sequence, Union

from shared import _prompt as prompt
from shared._log import log

if TYPE_CHECKING:
    from shared._selector import Selection
    from playwright.sync_api import BrowserContext, Playwright, Request, Response
    from playwright.sync_api import Page as RawPage

LAUNCH_ARGS = ["--disable-blink-features=AutomationControlled", "--disable-infobars"]
HIDE_WEBDRIVER = "Object.defineProperty(navigator, 'webdriver', {get: () => undefined})"
PAGE_HEIGHT = "(document.scrollingElement || document.documentElement).scrollHeight"
# Half a screen from the end counts as the bottom, so the odd small scroll up doesn't reset the end rule.
NEAR_BOTTOM = ("(() => { const page = document.scrollingElement || document.documentElement;"
               " return page.scrollTop + innerHeight * 1.5 >= page.scrollHeight; })()")
VIEWPORT = "[innerWidth, innerHeight]"
PROFILE_ROOTS = {"darwin": "Library/Application Support/Google/Chrome", "linux": ".config/google-chrome",
                 "win32": "AppData/Local/Google/Chrome/User Data"}
CHANNELS = {"chrome": "chrome"}
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
Match = Union[str, "re.Pattern[str]", Callable[[Any], bool]]

# connect sets this to Playwright's error class, so importing this module never loads Playwright.
PlaywrightError: type[Exception] = Exception
# Every random choice in scrolling comes from here, so a test can seed it.
_random = random.Random()
# Scrolling reads the time here, and waits on Playwright's clock, so a test can fake both.
_clock = time.monotonic
# Seconds between two checks for a scroll's progress: short next to a page's load, cheap for the page.
POLL = 0.05
# Copies made by installed_profile, and the browser each came from. connect deletes a copy on close.
_installed_copies: dict[Path, str] = {}
# Set when the Chromium download was declined (or couldn't be asked), so it isn't asked again.
_chromium_declined = False
# The error the Chromium download failed with, raised again without asking for the rest of the process.
_chromium_failed: RuntimeError | None = None
# Each source's session connects on its own thread, so two can find Chromium missing at once:
# the check, the question and the download are one step, or both would ask and both download.
_chromium_offer = threading.Lock()


def connect(*, profile: str | Path | None = None, cdp: str | None = None, headless: bool = True,
            viewport: tuple[int, int] = (1280, 900), locale: str | None = None, timezone: str | None = None,
            args: Sequence[str] = (), stealth: bool = True) -> Browser:
    """Start or attach to a browser, and return it as a `Browser` for a `with` block.

        with browser.connect(headless=False) as b:
            page = b.open_page("https://example.com/")

    `profile=None` is incognito: nothing is kept. A folder is a persistent profile, and
    `installed_profile()` a copy of your Chrome's, opened with real Chrome. `cdp=` attaches to a
    running browser's DevTools endpoint (`http://127.0.0.1:9222`, or a cloud browser's `wss://` URL)
    and leaves it running. A local Chrome only allows that when started with `--remote-debugging-port`
    and its own `--user-data-dir`, not your default profile. With `cdp=`, `profile`, `headless`,
    `locale`, `timezone`, `args` and `stealth` raise `ValueError`, and `viewport` is set on each page opened.

    `locale` ("fr-FR") and `timezone` ("Europe/Paris") set the browser's language and clock;
    `None` keeps the system's.

    It launches Google Chrome, installed the usual way, through Playwright's `chrome` channel:
    Playwright's own Chromium differs in codecs and fonts, which fingerprinting sees. Without
    Chrome, it uses the Chromium it downloaded before, in Chrome's new headless mode. With neither,
    it asks once, in a terminal, whether to download Chromium (about 200 MB) with Playwright's own
    installer, under a progress row; that question holds the live rows while it's open. On no,
    or without a terminal, it raises `RuntimeError` saying to install Chrome. The browser keeps
    its own user agent, except that headless mode's "HeadlessChrome" is reported as "Chrome".
    """
    # A copy from installed_profile is claimed at once, so it is deleted however connect ends
    # without a launched browser: the copy holds the profile's logins (Telegram Web's among them).
    copy = Path(profile) if profile is not None and Path(profile) in _installed_copies else None
    copied_from = _installed_copies.pop(copy) if copy is not None else None
    launched = False
    try:
        launch_only = profile is not None or not headless or args or not stealth or locale or timezone
        if cdp is not None and launch_only:
            raise ValueError("connect(cdp=...) uses the running browser as it is: "
                             "profile, headless, locale, timezone, args and stealth don't apply")
        playwright = _start_playwright()
        try:
            if cdp is None:
                started = _launch(playwright, profile, copied_from, headless=headless, viewport=viewport,
                                  locale=locale, timezone=timezone, args=args, stealth=stealth)
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


def installed_profile(browser: str = "chrome", profile: str = "Default") -> Path:
    """Copy your installed Chrome's profile to a temp folder, for `connect(profile=…)`.

        with browser.connect(profile=browser.installed_profile("chrome", "Profile 1"), headless=False) as b: ...

    The copy avoids Chrome's profile lock, so Chrome can stay open. Changes aren't written back,
    and `connect` deletes the copy on close.
    """
    if browser not in CHANNELS:
        raise ValueError(f"browser must be one of {', '.join(CHANNELS)}, not {browser!r}")
    root = Path.home() / PROFILE_ROOTS[sys.platform if sys.platform in ("darwin", "win32") else "linux"]
    if not (root / profile).is_dir():
        raise FileNotFoundError(f"no {browser} profile named {profile!r} in {root}")
    # mkdtemp makes the folder with mode 0700, so on a shared machine no other user can read the
    # copy, which holds the profile's logins. Windows ignores the mode; the user's temp folder is private there.
    copy = Path(tempfile.mkdtemp(prefix=f"shared-{browser}-"))
    try:
        # Whatever its name, the copy holds the profile as Default, and connect opens Default.
        shutil.copytree(root / profile, copy / "Default", ignore=SKIP_IN_PROFILE, ignore_dangling_symlinks=True)
        if (root / "Local State").is_file():
            shutil.copy2(root / "Local State", copy / "Local State")
    # Any exception, Ctrl-C included, deletes the half-made copy before it goes on up: it isn't in
    # _installed_copies yet, so nothing else would delete it, and it may already hold the profile's logins.
    except BaseException:
        shutil.rmtree(copy, ignore_errors=True)
        raise
    _installed_copies[copy] = browser
    return copy


class Browser:
    """A connected browser: open pages in it, and read or add its cookies. `raw` is the `BrowserContext`.

        with browser.connect() as b:
            page = b.open_page("https://example.com/"); b.cookies.all; b.pages
    """

    def __init__(self, context: BrowserContext, *, close_steps: list[Callable[[], Any]], headless: bool,
                 page_viewport: tuple[int, int] | None = None):
        self.raw = context
        self.cookies = Cookies(context)
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

    def open_page(self, url: str | None = None, *, idle: bool = False, timeout: float = 30.0) -> Page:
        """Open a new tab, and go to `url` when given.

            page = b.open_page("https://example.com/", idle=True)

        A navigation error is logged and leaves the page open on `about:blank`.
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

    def close(self) -> None:
        """Close the pages, the context, the browser and Playwright, in order: the same as leaving `with`.

            b.close()

        An attached (`cdp=`) browser keeps running. A failure while closing is logged once, and
        never raised, so it can't hide the error that made a `with` block exit.
        """
        if self._closed:
            return
        self._closed = True
        failures = []
        for step in [page.raw.close for page in self._opened] + self._close_steps:
            try:
                step()
            except Exception as error:
                failures.append(error)
        if failures:
            log.warn("closing the browser failed: {error}", error=_first_line(failures[0]))

    def _wrap(self, raw: RawPage) -> Page:
        if raw not in self._wrappers:
            self._wrappers[raw] = Page(raw, self)
        return self._wrappers[raw]


class Cookies:
    """The live browser's cookie jar, in the `cookies` module's shape.

        b.cookies.add(cookies.load_from_file("ins.txt")); b.cookies.all; b.cookies.clear_all()
    """

    def __init__(self, context: BrowserContext):
        self._context = context

    @property
    def all(self) -> list[dict]:
        """A snapshot of every cookie."""
        return [dict(cookie) for cookie in self._context.cookies()]

    def add(self, jar: list[dict]) -> None:
        """Add each cookie unchanged, except that `path` defaults to `/`.

            b.cookies.add([{"name": "sid", "value": "1", "domain": ".example.com"}])

        One at a time, so a rejected cookie is logged and the rest still go in.
        """
        for cookie in jar:
            try:
                self._context.add_cookies([{**cookie, "path": cookie.get("path") or "/"}])  # type: ignore[list-item]
            except PlaywrightError as error:
                log.warn("cookie {name} was rejected: {error}", name=cookie.get("name", ""), error=_first_line(error))

    def clear_all(self) -> None:
        """Delete every cookie in the browser."""
        self._context.clear_cookies()


class Page:
    """One tab. `raw` is the Playwright `Page`, for `click`, `fill`, `evaluate` and everything else.

        page = b.open_page("https://example.com/")
        page.on_response("graphql", handler); page.scroll(count=lambda: len(posts)); title = page.selector.css("h1").text
    """

    def __init__(self, raw: RawPage, browser: Browser):
        self.raw = raw
        self.browser = browser
        self._responses_seen = 0

    def goto(self, url: str, *, idle: bool = False, timeout: float = 30.0) -> int | None:
        """Go to `url`, and return the HTTP status of its main response.

            status = page.goto("https://example.com/", idle=True)
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

    def on_response(self, match: Match, handler: Callable[[Any, Response], Any], *,
                    lines: bool = False) -> Callable[[], None]:
        """Call `handler(parsed, response)` for each matching JSON response, and return a function that stops it.

            stop = page.on_response("graphql", lambda parsed, response: posts.extend(parsed["items"]))

        `match` is a URL substring, a compiled regex, or a function of the response. `lines=True`
        parses newline-delimited JSON, one call per line. A body that isn't JSON is skipped, and an
        exception in `handler` is logged while later responses keep arriving.
        """
        matches = _matcher(match)

        def listener(response: Response) -> None:
            if not matches(response):
                return
            self._responses_seen += 1
            for parsed in _json_bodies(response, lines):
                try:
                    handler(parsed, response)
                except Exception as error:
                    log.warn("on_response handler failed for {url}: {error}", url=response.url,
                             error=f"{type(error).__name__} {error}".strip())

        self.raw.on("response", listener)
        return lambda: self.raw.remove_listener("response", listener)

    def on_request(self, match: Match, handler: Callable[[Request], Any]) -> Callable[[], None]:
        """Call `handler(request)` for each matching request, and return a function that stops it.

            stop = page.on_request("/api/", lambda request: urls.append(request.url))
        """
        matches = _matcher(match)

        def listener(request: Request) -> None:
            if matches(request):
                handler(request)

        self.raw.on("request", listener)
        return lambda: self.raw.remove_listener("request", listener)

    def wait_for_selector(self, css: str, *, timeout: float = 30.0) -> None:
        """Wait until an element matches `css`, as in `page.wait_for_selector("article")`.

        Each `wait_for_*` raises Playwright's `TimeoutError` when `timeout` seconds pass first.
        """
        self.raw.wait_for_selector(css, timeout=timeout * 1000)

    def wait_for_url(self, pattern: str | re.Pattern[str] | Callable[[str], bool], *, timeout: float = 30.0) -> None:
        """Wait until the URL matches a glob, a regex or a function, as in `page.wait_for_url("**/reels/")`."""
        self.raw.wait_for_url(pattern, timeout=timeout * 1000)

    def wait_for_load(self, *, idle: bool = False, timeout: float = 30.0) -> None:
        """Wait until the page has loaded, and with `idle=True` the network is quiet: `page.wait_for_load(idle=True)`."""
        self.raw.wait_for_load_state("networkidle" if idle else "load", timeout=timeout * 1000)

    def scroll(self, *, by: str | Callable[[RawPage], Any] | None = None, count: Callable[[], int] | None = None,
               limit: int | None = None, more: Callable[[], bool] | None = None, idle: float = 10.0,
               delay: float = 3.0, pause: float = 0.3, max: int | None = None,
               progress: Callable[[], int] | None = None, onscroll: Callable[[], Any] | None = None) -> int:
        """Scroll like a person until the feed ends, as fast as the page loads, and return the number of scrolls.

            page.scroll(count=lambda: len(posts), limit=500)

        Each scroll moves the mouse and turns the wheel in uneven steps. It then waits for the
        page's progress, checked every 50 ms: the page grew, `count()` rose, `progress()` rose, or
        an `on_response` handler's response arrived. Then it pauses `pause` seconds plus 0 to 40%,
        so two scrolls never come back to back, and scrolls again. A fast page is scrolled two or
        three times a second; a slow one is waited out.

        Args:
            by: replaces the wheel: a JS expression to evaluate, or a `(raw_page) -> None` function.
            count: the number of items found so far; a rise is progress. None leaves it out.
            limit: stop once `count()` reaches it. None, or no `count`, has no limit.
            more: called before each scroll; False stops. None always goes on.
            idle: seconds at the bottom of the page with no progress after which the feed has
                ended, however many scrolls that takes.
            delay: the longest wait for progress after one scroll, in seconds.
            pause: the least seconds between the end of that wait and the next scroll: the floor,
                which a caller raises when the site has asked it to slow down.
            max: the most scrolls. None (the default) has no cap: the end rule, `limit` and
                `more` stop it.
            progress: a counter of other traffic the caller watches; a rise is progress.
            onscroll: called after each scroll's pause, such as a spinner's advance.

        Returns:
            The number of scrolls done.

        Waits run on Playwright's clock, so `on_*` handlers keep running during them.
        """
        marker = self._progress_marker(count, progress)
        quiet_since: float | None = None                # when the scrolls with no progress at the bottom began
        scrolls = 0
        while max is None or scrolls < max:
            if count is not None and limit is not None and count() >= limit:
                break
            if more is not None and not more():
                break
            started = _clock()
            self._move(by)
            scrolls += 1
            # Away from the bottom there is nothing to wait for: feeds load more only when their end
            # comes into view. At the bottom, the wait never runs past the end of the quiet spell.
            at_bottom = self.raw.evaluate(NEAR_BOTTOM)
            if not at_bottom:
                longest = 0.0
            elif quiet_since is None:
                longest = delay
            else:
                longest = min(delay, quiet_since + idle - _clock())
            previous, marker = marker, self._wait_for_progress(marker, count, progress, longest)
            self._pause(pause)
            if onscroll is not None:
                onscroll()
            if any(now > before for now, before in zip(marker, previous)) or not at_bottom:
                quiet_since = None
                continue
            if quiet_since is None:
                quiet_since = started
            if _clock() - quiet_since >= idle:
                break
            self._jiggle(pause)
        return scrolls

    def handoff(self, *, until: Callable[[], bool], message: str, poll: float = 1.0,
                timeout: float | None = None) -> bool:
        """Wait for a person to do a step, such as a captcha or a login, and return whether it got done.

            page.handoff(until=lambda: not page.raw.locator("#captcha").count(), message="Solve the captcha")

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
    def selector(self) -> Selection:
        """The live page as a `selector.Selection`, read fresh each time.

            page.selector.css("h1").text; page.selector.script("__NEXT_DATA__").jmespath("props").first

        It is a snapshot: a later change to the page needs another read. The HTML is `.raw.get()`.
        Needs the `selector` extra.
        """
        # Imported here, so a script that only drives the browser doesn't need parsel.
        from shared._selector import select

        return select(self.raw.content())

    @property
    def url(self) -> str:
        return self.raw.url

    @property
    def title(self) -> str:
        return self.raw.title()

    def close(self) -> None:
        """Close the tab."""
        self.raw.close()

    def _progress_marker(self, count: Callable[[], int] | None,
                         progress: Callable[[], int] | None) -> tuple[int, int, int, int]:
        """The numbers whose rise is progress: page height, `count()`, responses seen and `progress()`."""
        return (self.raw.evaluate(PAGE_HEIGHT), count() if count is not None else 0, self._responses_seen,
                progress() if progress is not None else 0)

    def _wait_for_progress(self, before: tuple[int, ...], count: Callable[[], int] | None,
                           progress: Callable[[], int] | None, longest: float) -> tuple[int, int, int, int]:
        """Poll every `POLL` seconds until a number of `before` rose or `longest` seconds passed; return the marker."""
        deadline = _clock() + longest
        while True:
            marker = self._progress_marker(count, progress)
            if any(now > then for now, then in zip(marker, before)) or _clock() >= deadline:
                return marker
            self.raw.wait_for_timeout(POLL * 1000)

    def _move(self, by: str | Callable[[RawPage], Any] | None) -> None:
        if isinstance(by, str):
            self.raw.evaluate(by)
        elif by is not None:
            by(self.raw)
        else:
            width, height = self.raw.evaluate(VIEWPORT)
            self.raw.mouse.move(width * _random.uniform(0.25, 0.75), height * _random.uniform(0.3, 0.7),
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

    def _jiggle(self, floor: float) -> None:
        # Most feeds load the next page when their end comes back into view, so up a screen and down wakes them.
        height = self.raw.evaluate(VIEWPORT)[1]
        self._wheel(-height)
        self._pause(floor)
        self._wheel(height)


def _start_playwright() -> Playwright:
    global PlaywrightError
    try:
        from playwright.sync_api import Error, sync_playwright
    except ImportError:
        raise ImportError("shared.browser needs playwright: pip install 'flntools[browser]'") from None
    PlaywrightError = Error
    return sync_playwright().start()


def _launch(playwright: Playwright, profile: str | Path | None, copied_from: str | None, *, headless: bool,
            viewport: tuple[int, int], locale: str | None, timezone: str | None, args: Sequence[str],
            stealth: bool) -> Browser:
    """Launch a browser for `connect`; `copied_from` is the browser an `installed_profile` copy came from.

    `connect` deletes the copy when this raises; on success, the Browser's close deletes it.
    """
    launch_options: dict[str, Any] = {"headless": headless, "args": [*LAUNCH_ARGS, *args]}
    context_options: dict[str, Any] = {"viewport": {"width": viewport[0], "height": viewport[1]}}
    if locale is not None:
        context_options["locale"] = locale
    if timezone is not None:
        context_options["timezone_id"] = timezone
    copy = Path(profile) if profile is not None and copied_from is not None else None
    # A copy of Chrome's profile opens only in Chrome: its cookies are sealed with Chrome's key.
    channel = CHANNELS[copied_from] if copied_from is not None else _channel(playwright)
    if copy is not None:
        launch_options["args"].append("--profile-directory=Default")
    context, close_steps = _open_context(playwright, profile, channel, launch_options, context_options)
    if stealth:
        context.add_init_script(HIDE_WEBDRIVER)
    if copy is not None:
        # Removed after Chrome has closed, or Chrome would still be writing into it.
        close_steps.append(lambda: shutil.rmtree(copy, ignore_errors=True))
    return Browser(context, close_steps=close_steps, headless=headless)


def _channel(playwright: Playwright) -> str:
    """The channel `connect` launches: `chrome` when Google Chrome is installed, `chromium` otherwise.

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
        # a run that connects once per source asks only once.
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

    `--no-shell` skips the separate headless shell: `connect` runs Chromium itself in Chrome's
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
                    bar.update(int(found[1]))
        if installer.returncode:
            last = next((line for line in reversed(lines) if line), "no output")
            raise RuntimeError(f"Chromium was not downloaded: Playwright's installer exited with "
                               f"{installer.returncode}: {last}")


def _open_context(playwright: Playwright, profile: str | Path | None, channel: str,
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


def _headful_user_agent_of(playwright: Playwright, channel: str) -> str:
    # A persistent profile can't be relaunched with a new user agent, so a throwaway browser reads it first.
    remote = playwright.chromium.launch(channel=channel, headless=True)
    try:
        return _headful_user_agent(remote)
    finally:
        remote.close()


def _first_line(error: BaseException) -> str:
    return (str(error).splitlines() or [""])[0]


def _matcher(match: Match) -> Callable[[Any], bool]:
    if isinstance(match, str):
        return lambda event: match in event.url
    if isinstance(match, re.Pattern):
        return lambda event: match.search(event.url) is not None
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
            parsed.append(json.loads(piece))
        except ValueError:
            log.debug("on_response: skipped a body that is not JSON from {url}", url=response.url)
    return parsed
