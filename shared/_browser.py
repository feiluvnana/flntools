"""Playwright's sync API as two small wrappers: `connect()` gives a `Browser`, and `open_page()` a `Page`."""

from __future__ import annotations

import json
import random
import re
import shutil
import sys
import tempfile
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable, Sequence, Union

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
# Real Chrome first: Playwright's own build differs in codecs and fonts, which fingerprinting sees.
# "chromium" is the bundled build in Chrome's new headless mode, not the old, easily spotted one.
LAUNCH_CHANNELS = ("chrome", "chromium")
# Caches are big and Chrome rebuilds them, so a profile copy leaves them out.
SKIP_IN_PROFILE = shutil.ignore_patterns("Cache", "Code Cache", "GPUCache", "Service Worker")
Match = Union[str, "re.Pattern[str]", Callable[[Any], bool]]

# connect sets this to Playwright's error class, so importing this module never loads Playwright.
PlaywrightError: type[Exception] = Exception
# Every random choice in scrolling comes from here, so a test can seed it.
_random = random.Random()
# Copies made by installed_profile, and the browser each came from. connect deletes a copy on close.
_installed_copies: dict[Path, str] = {}


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

    It launches your installed Chrome when there is one, and Playwright's Chromium otherwise; a
    missing Chromium raises `RuntimeError`. The browser keeps its own user agent, except that
    headless mode's "HeadlessChrome" is reported as "Chrome".
    """
    launch_only = profile is not None or not headless or args or not stealth or locale or timezone
    if cdp is not None and launch_only:
        raise ValueError("connect(cdp=...) uses the running browser as it is: "
                         "profile, headless, locale, timezone, args and stealth don't apply")
    playwright = _start_playwright()
    try:
        if cdp is None:
            return _launch(playwright, profile, headless=headless, viewport=viewport, locale=locale,
                           timezone=timezone, args=args, stealth=stealth)
        remote = playwright.chromium.connect_over_cdp(cdp)
        context = remote.contexts[0] if remote.contexts else remote.new_context()
        # Closing a browser reached over CDP only disconnects; the context is the user's, so it stays open.
        return Browser(context, close_steps=[remote.close, playwright.stop], headless=False, page_viewport=viewport)
    except BaseException:
        playwright.stop()
        raise


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
    copy = Path(tempfile.mkdtemp(prefix=f"shared-{browser}-"))
    # Whatever its name, the copy holds the profile as Default, and connect opens Default.
    shutil.copytree(root / profile, copy / "Default", ignore=SKIP_IN_PROFILE, ignore_dangling_symlinks=True)
    if (root / "Local State").is_file():
        shutil.copy2(root / "Local State", copy / "Local State")
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

    def goto(self, url: str, *, idle: bool = False, timeout: float = 30.0) -> bool:
        """Go to `url`, and return whether it loaded.

            if not page.goto("https://example.com/", idle=True): ...

        `idle=True` also waits until the network is quiet. A navigation error is logged, not raised.
        """
        try:
            self.raw.goto(url, wait_until="networkidle" if idle else "load", timeout=timeout * 1000)
        except PlaywrightError as error:
            log.warn("could not open {url}: {error}", url=url, error=_first_line(error))
            return False
        return True

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
               limit: int | None = None, more: Callable[[], bool] | None = None, idle: int = 4, delay: float = 2.0,
               max: int = 200, onscroll: Callable[[], Any] | None = None) -> int:
        """Scroll like a person until the feed ends, and return the number of scrolls.

            page.scroll(count=lambda: len(posts), limit=500)

        Each scroll moves the mouse, turns the wheel in uneven steps, and pauses about `delay`
        seconds. It stops when `count()` reaches `limit`, `more()` returns False, `max` scrolls are
        done, or `idle` scrolls in a row at the bottom made no progress: the page, `count()` and the
        `on_response` traffic stayed the same. `by` replaces the wheel with a JS expression or a
        `(raw_page) -> None` function. `onscroll()` runs after each pause. Waits run on
        Playwright's clock, so `on_*` handlers keep running.
        """
        marker = self._progress_marker(count)
        idle_scrolls = scrolls = 0
        while scrolls < max:
            if count is not None and limit is not None and count() >= limit:
                break
            if more is not None and not more():
                break
            self._move(by)
            scrolls += 1
            self._pause(delay)
            if onscroll is not None:
                onscroll()
            previous, marker = marker, self._progress_marker(count)
            if any(now > before for now, before in zip(marker, previous)) or not self.raw.evaluate(NEAR_BOTTOM):
                idle_scrolls = 0
                continue
            idle_scrolls += 1
            if idle_scrolls >= idle:
                break
            self._jiggle(delay)
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

    def _progress_marker(self, count: Callable[[], int] | None) -> tuple[int, int, int]:
        return self.raw.evaluate(PAGE_HEIGHT), count() if count is not None else 0, self._responses_seen

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

    def _pause(self, delay: float) -> None:
        seconds = delay * _random.uniform(0.6, 1.4)
        if _random.random() < 0.1:
            seconds *= 2
        self.raw.wait_for_timeout(seconds * 1000)

    def _jiggle(self, delay: float) -> None:
        # Most feeds load the next page when their end comes back into view, so up a screen and down wakes them.
        self._pause(delay)
        height = self.raw.evaluate(VIEWPORT)[1]
        self._wheel(-height)
        self._pause(delay / 2)
        self._wheel(height)


def _start_playwright() -> Playwright:
    global PlaywrightError
    try:
        from playwright.sync_api import Error, sync_playwright
    except ImportError:
        raise ImportError("shared.browser needs playwright: pip install 'flntools[browser]'") from None
    PlaywrightError = Error
    return sync_playwright().start()


def _launch(playwright: Playwright, profile: str | Path | None, *, headless: bool, viewport: tuple[int, int],
            locale: str | None, timezone: str | None, args: Sequence[str], stealth: bool) -> Browser:
    launch_options: dict[str, Any] = {"headless": headless, "args": [*LAUNCH_ARGS, *args]}
    context_options: dict[str, Any] = {"viewport": {"width": viewport[0], "height": viewport[1]}}
    if locale is not None:
        context_options["locale"] = locale
    if timezone is not None:
        context_options["timezone_id"] = timezone
    copy = Path(profile) if profile is not None and Path(profile) in _installed_copies else None
    channels = (CHANNELS[_installed_copies.pop(copy)],) if copy is not None else LAUNCH_CHANNELS
    if copy is not None:
        launch_options["args"].append("--profile-directory=Default")
    try:
        context, close_steps = _start_context(playwright, profile, channels, launch_options, context_options)
    except BaseException:
        if copy is not None:
            shutil.rmtree(copy, ignore_errors=True)
        raise
    if stealth:
        context.add_init_script(HIDE_WEBDRIVER)
    if copy is not None:
        # Removed after Chrome has closed, or Chrome would still be writing into it.
        close_steps.append(lambda: shutil.rmtree(copy, ignore_errors=True))
    return Browser(context, close_steps=close_steps, headless=headless)


def _start_context(playwright: Playwright, profile: str | Path | None, channels: Sequence[str],
                   launch_options: dict[str, Any], context_options: dict[str, Any]) -> tuple[BrowserContext, list]:
    for channel in channels[:-1]:
        try:
            return _open_context(playwright, profile, channel, launch_options, context_options)
        except PlaywrightError as error:
            log.debug("connect: {channel} did not start: {error}", channel=channel, error=_first_line(error))
    try:
        return _open_context(playwright, profile, channels[-1], launch_options, context_options)
    except PlaywrightError as error:
        if "Executable doesn't exist" in str(error) or "is not found" in str(error):
            raise RuntimeError("Chromium is not installed: run 'playwright install chromium'") from None
        raise


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
