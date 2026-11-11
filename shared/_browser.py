"""Playwright's sync API, minus the setup. Every function takes or returns a real Playwright `Page`."""

from __future__ import annotations

import json
import re
import time
import weakref
from contextlib import contextmanager
from typing import Any, Callable, Iterator, Union

try:
    from playwright.sync_api import Error as PlaywrightError
    from playwright.sync_api import Page, Response, sync_playwright
except ImportError:
    raise ImportError("shared.browser needs playwright: pip install 'flntools[browser]'") from None

from shared._http import agents
from shared._log import log

LAUNCH_ARGS = ["--disable-blink-features=AutomationControlled", "--disable-infobars"]
HIDE_WEBDRIVER = "Object.defineProperty(navigator, 'webdriver', {get: () => undefined})"
SCROLL_TO_BOTTOM = "window.scrollTo(0, document.body ? document.body.scrollHeight : 0)"
PAGE_HEIGHT = "document.body ? document.body.scrollHeight : 0"
# Scrolling up a little and back down re-triggers feeds that load on an intersection observer.
NUDGE_UP, NUDGE_DOWN = "window.scrollBy(0, -350)", SCROLL_TO_BOTTOM + "; window.dispatchEvent(new Event('scroll'))"
MAX_NUDGE_WAIT = 0.8
Match = Union[str, "re.Pattern[str]", Callable[[Response], bool]]

# handoff reads time through this, so tests can use a fake clock.
_clock = time.monotonic
# Pages opened headless by `open`; a person cannot see them, so `handoff` refuses to wait on them.
_headless_pages: weakref.WeakSet = weakref.WeakSet()


@contextmanager
def open(*, headless: bool = True, user_agent: str | None = agents.chrome, cookies: list[dict] | None = None,
         viewport: tuple[int, int] = (1280, 900), stealth: bool = True, slow_mo: float = 0,
         args: list[str] | None = None, **context_kwargs: Any) -> Iterator[Page]:
    """Launch Chromium with one context and one page, and close all of it on exit.

    `cookies` takes the `list[dict]` from `load_cookies`. `slow_mo` is in seconds.
    `args` are extra Chromium flags, added after the anti-automation ones.
    `page.context` and `page.context.browser` are the real objects for anything else.
    """
    with sync_playwright() as playwright:
        try:
            browser = playwright.chromium.launch(headless=headless, slow_mo=slow_mo * 1000,
                                                 args=LAUNCH_ARGS + list(args or []))
        except PlaywrightError as error:
            if "Executable doesn't exist" in str(error):
                raise RuntimeError("Chromium is not installed: run 'playwright install chromium'") from None
            raise
        try:
            if user_agent is not None:
                context_kwargs["user_agent"] = user_agent
            context = browser.new_context(viewport={"width": viewport[0], "height": viewport[1]}, **context_kwargs)
            try:
                _add_cookies(context, cookies or [])
                if stealth:
                    context.add_init_script(HIDE_WEBDRIVER)
                page = context.new_page()
                if headless:
                    _headless_pages.add(page)
                yield page
            finally:
                context.close()
        finally:
            browser.close()


def capture(page: Page, match: Match, handler: Callable[[Response, Any], None], *,
            lines: bool = False) -> Callable[[], None]:
    """Call `handler(response, data)` for each matching JSON response. Returns a function that stops it.

    `match` is a URL substring, a compiled regex, or a function of the response. A body
    that is not JSON is skipped. `lines=True` reads newline-delimited JSON and calls the
    handler once per line. An exception in `handler` is logged and later responses still arrive.
    """
    matches = _matcher(match)

    def on_response(response: Response) -> None:
        if not matches(response):
            return
        for data in _json_bodies(response, lines):
            try:
                handler(response, data)
            except Exception as error:
                log.warn("capture handler failed for {url}: {error}", url=response.url,
                         error=f"{type(error).__name__} {error}".strip())

    page.on("response", on_response)
    return lambda: page.remove_listener("response", on_response)


def scroll(page: Page, *, count: Callable[[], int] | None = None, limit: int | None = None, idle: int = 4,
           delay: float = 2.0, max: int = 200, more: Callable[[], bool] | None = None,
           step: Callable[[Page], Any] | None = None, onstep: Callable[[], Any] | None = None) -> int:
    """Scroll until the feed ends, and return the number of scrolls.

    Stops at the first of: `count()` reaches `limit`; `more()` returns False; `idle` scrolls
    in a row change neither the page height nor `count()`; `max` scrolls.
    """
    previous_height, previous_count = 0, count() if count else 0
    idle_rounds = scrolls = 0
    while scrolls < max:
        if count and limit is not None and count() >= limit:
            break
        if more and not more():
            break
        if step:
            step(page)
        else:
            page.evaluate(SCROLL_TO_BOTTOM)
        scrolls += 1
        page.wait_for_timeout(delay * 1000)
        height, current = page.evaluate(PAGE_HEIGHT), count() if count else 0
        if onstep:
            onstep()
        if height > previous_height or current > previous_count:
            idle_rounds, previous_height, previous_count = 0, height, current
            continue
        idle_rounds += 1
        if idle_rounds >= idle:
            break
        page.evaluate(NUDGE_UP)
        page.wait_for_timeout(min(delay, MAX_NUDGE_WAIT) * 1000)
        page.evaluate(NUDGE_DOWN)
    return scrolls


def handoff(page: Page, *, done: Callable[[], bool], message: str, poll: float = 1.0,
            timeout: float | None = None) -> bool:
    """Wait for a person to do a step, such as a captcha or a login. False when `timeout` passes first.

    Polls with `page.wait_for_timeout`, so Playwright keeps handling events while it waits.
    A headless page raises RuntimeError, because nobody can see it.
    """
    if done():
        return True
    if page in _headless_pages:
        raise RuntimeError(f"cannot hand off to a person in a headless browser: {message}")
    log.warn(message)
    started = _clock()
    with log.progress(message) as bar:
        while not done():
            if timeout is not None and _clock() - started >= timeout:
                bar.fail()
                return False
            page.wait_for_timeout(poll * 1000)
    log.ok("{message}: done", message=message)
    return True


def _add_cookies(context: Any, cookies: list[dict]) -> None:
    """Add each cookie with a leading `.` on its domain and a default path of `/`, one at a time."""
    for cookie in cookies:
        domain = cookie.get("domain", "")
        fixed = {"name": cookie["name"], "value": cookie["value"], "path": cookie.get("path") or "/",
                 "domain": domain if domain.startswith(".") else "." + domain}
        try:
            context.add_cookies([fixed])
        except PlaywrightError as error:
            log.warn("cookie {name} was rejected: {error}", name=fixed["name"], error=str(error).splitlines()[0])


def _matcher(match: Match) -> Callable[[Response], bool]:
    if isinstance(match, str):
        return lambda response: match in response.url
    if isinstance(match, re.Pattern):
        return lambda response: match.search(response.url) is not None
    if callable(match):
        return match
    raise TypeError(f"match must be a str, a compiled regex, or a function, not {type(match).__name__}")


def _json_bodies(response: Response, lines: bool) -> list[Any]:
    try:
        body = response.text()
    except PlaywrightError as error:
        log.debug("capture: no body for {url}: {error}", url=response.url, error=str(error).splitlines()[0])
        return []
    pieces = [line for line in body.splitlines() if line.strip()] if lines else [body]
    bodies = []
    for piece in pieces:
        try:
            bodies.append(json.loads(piece))
        except ValueError:
            log.debug("capture: skipped a body that is not JSON from {url}", url=response.url)
    return bodies
