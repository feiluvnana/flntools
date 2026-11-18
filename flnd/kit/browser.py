"""A browser library: async Playwright on an asyncio loop, shared tabs, response capture, scrolling.

A standalone library: it imports the standard library, Playwright and flnd's other library
modules (`_loop`, `net`, `_cookies`, `forms`, `_ui`, `_text`, `clock`, `errors`), and nothing
of flnd's engine. In flnd, `flnd.engine.site.TargetSite` builds the `Site` browser verbs a source
calls
on it, each one call across to the run's loop (`flnd.kit.loop.Loop.run`, about 40 µs).

What it offers, from the bottom up:
- `launch` starts Chrome (or Playwright's Chromium, offering to download it) and gives a
  `Browser`. `Browser.tab` opens a `Page`, one tab.
- `Page` drives one tab: `goto`, `wait`, `fetch` (a request sent by the page itself), `content`,
  `evaluate`, `click`, `scroll`, and `capture`, which keeps the bodies of the responses the tab
  receives. Every `Page` method is a coroutine and runs on the loop.
- `RunBrowser` is one browser shared by many users on one loop, launched the first time one asks
  for a tab, with each user's cookies added once. (In flnd, one per run.)
- `Tabs` is one user's share of it (in flnd, a target's): the tab it works in, a stack of extra
  tabs for nested scrolling feeds, and up to `MAX_TABS` tabs pre-loading the pages it will open
  next ("warm" tabs). Its methods are plain functions, called from the user's own thread.
- `Feed` iterates the JSON of the matching responses a user's tab receives, in order, scrolling
  the tab for more when asked, and ending when the site's own cursor says there is no more.
- `local_storage` reads a page's local storage in a copy of a Chrome profile, on a loop of its own.

The main flow of a scrolled feed (`Feed(tabs, match, scroll=True, cursor=read)`):
1. the feed starts a `Capture` on the user's tab (one call to the loop), which keeps the bodies
   of the responses `match` takes: the feed's own API calls;
2. the user loads a page; its API calls arrive as responses, and the capture reads each matching
   body on the loop, keeping their order;
3. iterating the feed parses the bodies that arrived, on the user's thread, and yields them;
   `read` (the caller's cursor reader) says of each whether the site has another page;
4. when none is waiting, it scrolls one step at a time (one call to the loop each) until one is,
   the cursor said there is no more, or the scroller says the feed went quiet.

How a scroll step goes (`_Scroller`):
1. it scrolls: `fast` jumps to the bottom; `human` glides down 1.2 to 1.8 screens in uneven
   wheel steps, now and then after a small scroll back up;
2. it watches for progress, a new matching response or the page growing, and at the very bottom
   waits up to `WAIT` for it. `human` also pauses, as a reader looks at what came;
3. progress, or room left to scroll, means go on; a bottom that stays quiet for `IDLE` more
   seconds after a whole `WAIT` is the end of a feed whose site gave no cursor.

There are no fixed sleeps: a step costs what the site takes to answer, plus the human pause.

Rules a change must keep:
- Playwright objects are used only on the loop's thread. Everything another thread holds (a
  `Page`, a `Capture`) is touched there only through `RunBrowser.run`.
- `RunBrowser.run` must not be called on the loop itself: it would wait for itself; code on the
  loop awaits instead.
- Responses are kept in the order they arrived, even though their bodies are read at once.
- With `human`, at most `HUMAN_TABS` tabs of one login scroll at once (`Page.gate`), whatever
  holds them, since a person doesn't scroll many tabs of one account at the same moment.
"""

from __future__ import annotations

import asyncio
import base64
import functools
import json
import math
import os
import random
import re
import shutil
import subprocess
import sys
import tempfile
import threading
from collections import Counter, deque
from collections.abc import Awaitable, Callable, Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import TYPE_CHECKING, Any, Union
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from flnd.errors import Changed, NetworkError, Stopped, ToolError, UsageError
from flnd.kit import clock, net
from flnd.ui import forms
from flnd.ui.log import redact, ui

NO_PROFILES = "FLND_TEST_NO_PROFILES"


def _real_home() -> Path:
    if sys.platform == "win32":
        return Path(os.environ.get("USERPROFILE") or os.path.expanduser("~"))
    try:
        import pwd
        return Path(pwd.getpwuid(os.getuid()).pw_dir)
    except (ImportError, AttributeError):
        return Path.home()


def refuse_profile(folder: Path | str | None = None) -> None:
    if not os.environ.get(NO_PROFILES) or folder is None:
        return
    import tempfile

    resolved = Path(folder).resolve()
    try:
        candidates = (os.environ.get("TEMP"), os.environ.get("TMP"), tempfile.gettempdir())
        if any(c and resolved.is_relative_to(Path(c).resolve()) for c in candidates):
            return
    except (ValueError, OSError):
        pass
    real_home = _real_home().resolve()
    if resolved.is_relative_to(real_home):
        raise RuntimeError(f"{NO_PROFILES} is set: a test may not read or copy the real browser "
                           f"profile {folder}; fake the home folder or the browser instead")

def _belongs(domain: str, to: str) -> bool:
    domain, to = domain.lstrip(".").lower(), to.lstrip(".").lower()
    return domain == to or domain.endswith("." + to)


class Jar:
    """An immutable collection of cookies: filter it, read it by name, send or save it."""

    __slots__ = ("_cookies", "_error")

    def __init__(
        self, cookies: Iterable[Mapping[str, Any]] = (), *, error: type[Exception] = UsageError
    ):
        # The class a bad cookie raises: a caller that reads cookies from a file passes its own.
        self._error = error
        kept = []
        for index, cookie in enumerate(cookies):
            if not isinstance(cookie, dict) or "name" not in cookie or "value" not in cookie:
                raise self._error(f"cookie {index} needs a name and a value")
            kept.append(dict(cookie))
        self._cookies: tuple[Mapping[str, Any], ...] = tuple(kept)

    def filter(
        self, *, domains: str | Iterable[str] | None = None, names: Iterable[str] | None = None
    ) -> Jar:
        if domains is None:
            wanted_domains: tuple[str, ...] | None = None
        elif isinstance(domains, str):
            wanted_domains = (domains,)
        else:
            wanted_domains = tuple(domains)
        if names is None:
            wanted: set[str] | None = None
        elif isinstance(names, str):
            wanted = {names}
        else:
            wanted = set(names)
        return self.__class__(
            (
                cookie for cookie in self._cookies
                if (wanted_domains is None
                    or any(_belongs(str(cookie.get("domain", "")), dom) for dom in wanted_domains))
                and (wanted is None or cookie["name"] in wanted)
            ),
            error=self._error,
        )

    @property
    def header(self) -> str:
        return "; ".join(f"{cookie['name']}={cookie['value']}" for cookie in self._cookies)

    @property
    def list(self) -> list[Mapping[str, Any]]:
        return [dict(cookie) for cookie in self._cookies]

    def save(self, dest: Path | str) -> Path:
        path = Path(dest)
        lines = ["# Netscape HTTP Cookie File"]
        for cookie in self._cookies:
            domain = str(cookie.get("domain", ""))
            fields = [
                ("#HttpOnly_" if cookie.get("httpOnly") else "") + domain,
                "TRUE" if domain.startswith(".") else "FALSE",
                str(cookie.get("path") or "/"),
                "TRUE" if cookie.get("secure") else "FALSE",
                str(max(int(-1 if (exp := cookie.get("expires")) is None else exp), 0)),
                str(cookie["name"]),
                str(cookie["value"]),
            ]
            has_tab = any("\t" in field for field in fields[:-1])
            has_newline = any(m in "".join(fields) for m in "\r\n")
            if has_tab or has_newline:
                name = cookie["name"]
                raise self._error(
                    f"cookie {name!r} for {domain} has a tab or newline Netscape cannot hold"
                )
            lines.append("\t".join(fields))
        text = "\n".join(lines) + "\n"
        path.parent.mkdir(parents=True, exist_ok=True)
        from flnd.kit import files
        with files.part(path) as held:
            if os.name != "nt":
                os.chmod(held.fd, 0o600)
            held.write(text.encode("utf-8"))
        return path

    def __getitem__(self, name: str) -> str:
        for cookie in self._cookies:
            if cookie["name"] == name:
                return str(cookie["value"])
        raise KeyError(name)

    def __contains__(self, name: object) -> bool:
        return any(cookie["name"] == name for cookie in self._cookies)

    def __iter__(self) -> Iterator[Mapping[str, Any]]:
        return iter([dict(cookie) for cookie in self._cookies])

    def __len__(self) -> int:
        return len(self._cookies)

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, Jar):
            return NotImplemented
        return self._cookies == other._cookies

    def __repr__(self) -> str:
        names = list(dict.fromkeys(str(cookie["name"]) for cookie in self._cookies))
        return f"Jar({', '.join(names)})"

if TYPE_CHECKING:
    import threading as _threading

    from playwright.async_api import BrowserContext, Locator, Playwright, Response
    from playwright.async_api import Page as RawPage

    from flnd.kit.loop import Loop

__all__ = [
    "FAST", "HUMAN", "Browser", "Capture", "Feed", "Jar", "Match", "Page", "Reader",
    "RunBrowser", "Tabs", "Visit", "WaitError", "launch", "local_storage",
]

# ---------------------------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------------------------

# Switches that hide the automation Chrome would otherwise show (AutomationControlled sets
# `navigator.webdriver`; infobars shows the automated-software bar) and keep Chrome from
# throttling the background tabs of the one window every target shares.
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
DEFAULT_VIEWPORT = (1280, 900)
PAGE_HEIGHT = "(document.scrollingElement || document.documentElement).scrollHeight"
# Within a few pixels of the end counts as the bottom: a scroll position may be fractional.
AT_BOTTOM = ("(() => { const page = document.scrollingElement || document.documentElement;"
             " return page.scrollTop + innerHeight >= page.scrollHeight - 8; })()")
# The same two reads for `scroll(within=…)`, on the element that scrolls instead of the page.
ELEMENT_HEIGHT = "element => element.scrollHeight"
ELEMENT_AT_BOTTOM = (
    "element => element.scrollTop + element.clientHeight >= element.scrollHeight - 8"
)
VIEWPORT = "[innerWidth, innerHeight]"
# A target's tab scrolls by script: a background tab gets no mouse events, so a wheel would stall
# there. The wheel event is sent too, since some feeds load their next page on it.
SCROLL_BY = (
    "d => { window.scrollBy(0, d);"
    " window.dispatchEvent(new WheelEvent('wheel', {deltaY: d, bubbles: true})); }"
)
# `Page.fetch`'s request, run in the page by its own `fetch()`. The body comes back as base64, since
# `evaluate` carries only JSON and a binary body would not survive as text. An AbortController ends
# the request, body included, after `ms`; every failure is returned as `error`, never thrown, so
# Python tells a timeout ("timeout") from a refused or blocked request (fetch's own TypeError).
IN_PAGE_FETCH = """async ({url, method, headers, body, ms}) => {
  const resolved = new URL(url, location.href).href;
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), ms);
  try {
    const init = {method, headers, credentials: "include", signal: controller.signal};
    if (body !== null) init.body = Uint8Array.from(atob(body), c => c.charCodeAt(0));
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
# Seconds `Page.fetch` waits for an answer, body included.
FETCH_TIMEOUT = 30.0
# What Playwright says when what a call read is gone: the page navigated under an evaluation, or
# Chrome freed a response body (Network.getResponseBody) once its page moved on, seen 2026-11-13.
GONE = ("Execution context was destroyed", "page is navigating",
        "No resource with given identifier found")
# Seconds between two checks of a page's height while a scroll step watches for progress; a
# matching response wakes the step at once instead.
POLL = 0.05

# -- scrolling: the two styles, and the numbers a step goes by --------------------------------
# `human` (the default) scrolls as a person does, so the site flags it less as a bot (the user
# asked for this on 2026-11-11); `fast` jumps to the bottom and never pauses.
HUMAN = "human"
FAST = "fast"
SCROLLS = (HUMAN, FAST)
# The longest a step at the very bottom waits for a matching response or for the page to grow.
WAIT = 3.0
# After a step's whole `WAIT` brought nothing, the seconds the bottom may stay quiet before the
# feed counts as ended. Only a feed whose site gives no cursor ever ends this way.
IDLE = 2.0
# A human pause is lognormal: most are near the median, a few are much longer, never negative.
# Its median is `LOOK` when the step brought new media (a reader looks at it), `GLANCE` when not.
LOOK = 0.4
GLANCE = 0.25
# The lognormal's sigma: a pause is within about 0.7x and 1.4x its median two times in three.
SPREAD = 0.35
# No pause is longer than this many medians, so one unlucky draw can't stall a feed.
LONGEST = 4.0
# How a human step glides: one screen times a factor in this range, in this many wheel steps,
# each `GLIDE` seconds apart (about 60 frames a second, unevenly). More than one screen, so a
# feed that loads its next page near its end gets there in one step.
SCREENS = (1.2, 1.8)
WHEEL_STEPS = (3, 6)
GLIDE = (0.008, 0.016)
# The chance a human step first scrolls back up a little (part of a screen), as a reader goes
# back to look at something, and the hesitation's median before going on down.
BACK_UP = 0.07
BACK_UP_SCREENS = (0.2, 0.5)
HESITATE = 0.15
# At most this many tabs of one login scroll at once with `human`.
HUMAN_TABS = 2
# The times one cursor may come back before a feed counts as stuck at its end. Once is allowed:
# a page may send one request twice, and both answers name the same next page.
REPEATS = 1
# Seconds between two checks that a person finished a handoff (a captcha).
HANDOFF_POLL = 1.0
# Most tabs one target holds at once: its own, nested feeds' and the warm ones.
MAX_TABS = 6
# How long a warm tab's first answer may take, in milliseconds: it only has to start loading.
WARM_TIMEOUT = 15_000
# A warm tab keeps the responses of its load, except these, which no source reads.
ASSET_TYPES = frozenset({"image", "media", "font", "stylesheet", "script", "manifest"})
# The longest a feed that doesn't scroll waits for the network to go quiet, in seconds.
QUIET = 10.0

# Where Chrome keeps its profiles, relative to the home folder, on each platform.
PROFILE_ROOTS = {
    "darwin": "Library/Application Support/Google/Chrome",
    "linux": ".config/google-chrome",
    "win32": "AppData/Local/Google/Chrome/User Data",
}
# A copy of an installed profile opens only in Chrome: its cookies are sealed with Chrome's key.
PROFILE_CHANNEL = "chrome"
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
# What a missing browser's `ToolError` says to do.
INSTALL_CHROME = "install Google Chrome, or run flnd in a terminal and accept Chromium's download"
# Playwright's installer prints a line such as "|■■■■      |  20% of 167.3 MiB" every few
# percent when its output isn't a terminal; the number is the progress row's.
INSTALL_PERCENT = re.compile(r"(\d+)% of")
# Caches are big and Chrome rebuilds them, so a profile copy leaves them out.
SKIP_IN_PROFILE = shutil.ignore_patterns("Cache", "Code Cache", "GPUCache", "Service Worker")
# What a feed takes as its match: a substring of the URL, a regex searched in it, or a predicate.
Match = Union[str, "re.Pattern[str]", Callable[[str], bool]]
# What a feed takes as its cursor reader: given one parsed body, `(more, cursor)` when the body is
# one of the feed's pages (whether the site has another, and the next page's cursor if it names
# one), or None when the body says nothing about paging.
Reader = Callable[[Any], "tuple[bool, str | None] | None"]

# Set when the Chromium download was declined (or couldn't be asked), so it isn't asked again.
_chromium_declined = False
# The error the Chromium download failed with, re-raised without asking for the rest of the process.
_chromium_failed: ToolError | None = None
# The check, the question and the download are one step: the launch runs them on a worker thread
# (the question waits for a person), and a second run in the process mustn't ask at the same time.
_chromium_offer = threading.Lock()
# The headed user agent of each browser executable, by its path: measuring it costs a launch.
_HEADFUL_UA: dict[str, str] = {}


class _NeverRaised(Exception):
    """Stands for Playwright's error classes where Playwright isn't installed: nothing matches."""


@functools.cache
def _raw_error() -> type[Exception]:
    """Playwright's `Error`, imported once; a class nothing raises without Playwright.

    Code that catches Playwright's errors names this instead of importing Playwright, so the
    module still imports (and its `except` clauses still work) without the `browser` extra.

    Returns:
        `playwright.async_api.Error`, or `_NeverRaised` when Playwright isn't installed.
    """
    try:
        from playwright.async_api import Error
    except ImportError:  # no `browser` extra: no Playwright call can have raised
        return _NeverRaised
    return Error


@functools.cache
def _raw_timeout() -> type[Exception]:
    """Playwright's `TimeoutError`, imported once, as `_raw_error`.

    Returns:
        `playwright.async_api.TimeoutError`, or `_NeverRaised` when Playwright isn't installed.
    """
    try:
        from playwright.async_api import TimeoutError
    except ImportError:  # no `browser` extra: no Playwright call can have raised
        return _NeverRaised
    return TimeoutError


def _gone(error: BaseException) -> bool:
    """Whether a Playwright error says what it read is gone: the page moved, or a body was freed."""
    return any(text in str(error) for text in GONE)


class WaitError(NetworkError):
    """A wait for a page (`Page.wait`, `Page.click`) didn't come true within its `timeout`.

    Its own class because callers act on it (a feed that stopped growing, a page to skip); it is
    a retryable `NetworkError` to everyone else, since a slow page may load on the next try.

    Not a subclass of the builtin `TimeoutError`, which is an `OSError`: a page that is slow to
    show an element isn't a failure of the operating system, and `except OSError` mustn't catch it.
    """


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


# ---------------------------------------------------------------------------------------------
# Launching
# ---------------------------------------------------------------------------------------------

async def launch(*, profile: str | None = None, headless: bool = False,
                 stealth: bool = True) -> Browser:
    """Launch Chrome, or Playwright's Chromium, and return it as a `Browser`. Runs on the loop.

        chrome = await launch()
        tab = await chrome.tab()

    Args:
        profile: the name of an installed Chrome profile ("Default") to open a private copy of,
            for its logins; None opens a fresh incognito browser.
        headless: hide the window. A visible one (the default) lets a person solve a captcha.
        stealth: hide `navigator.webdriver`, which sites read to tell automation apart.

    Returns:
        The browser, which `Browser.close` closes, deleting the profile copy.

    Raises:
        FileNotFoundError: Chrome has no profile of that name.
        ToolError: no Chrome, and Chromium's download was declined or failed.
        ImportError: Playwright isn't installed.
    """
    # Copied before Playwright starts, so a misspelt name fails at once. From here the copy holds
    # the profile's logins, so it is deleted however this ends without a launched browser.
    copy = await asyncio.to_thread(_copy_profile, profile) if profile is not None else None
    try:
        playwright = await _start_playwright()
        try:
            return await _launch(playwright, copy, headless=headless, stealth=stealth)
        # Any exception, Ctrl-C included, stops Playwright's driver before it goes on up.
        except BaseException:
            await playwright.stop()
            raise
    except BaseException:
        if copy is not None:
            shutil.rmtree(copy, ignore_errors=True)
        raise


async def _start_playwright() -> Playwright:
    """Start Playwright's driver on the running loop.

    Playwright is imported here, on first use, so importing this module stays cheap. The driver
    is a Node process Playwright spawns; it lives until `playwright.stop()`, which the
    `Browser`'s close steps run last.

    Returns:
        The started driver, bound to the running loop.

    Raises:
        ImportError: Playwright isn't installed (flnd's `browser` extra), with the install line.
    """
    try:
        from playwright.async_api import async_playwright
    except ImportError:
        raise ImportError("flnd.kit.browser needs playwright: pip install playwright") from None
    return await async_playwright().start()


def _profile_root() -> Path:
    """The folder that holds Google Chrome's profiles on this platform."""
    # Any platform other than macOS and Windows gets the Linux path, the one Chrome uses there.
    key = sys.platform if sys.platform in ("darwin", "win32") else "linux"
    return Path.home() / PROFILE_ROOTS[key]


def _copy_profile(name: str) -> Path:
    """Copy the installed Chrome profile `name` to a new private temporary folder, as its `Default`.

    Args:
        name: the profile's folder name under Chrome's profile root, such as "Default".

    Returns:
        The new folder, which holds the copy as `Default` and Chrome's `Local State` beside it.

    Raises:
        FileNotFoundError: Chrome has no profile of that name.
        ValueError: `name` is empty or a path, not a name.

    A copy that fails half way is deleted before the error goes on.
    """
    if not name or any(sep in name for sep in {"/", os.sep, os.altsep} - {None}) \
            or name in (".", ".."):
        raise ValueError(
            f"profile must be a Chrome profile's name, such as 'Default', not {name!r}"
        )
    root = _profile_root()
    # Under the test suite a real profile, which holds logins, is refused before anything reads it.
    refuse_profile(root / name)
    if not (root / name).is_dir():
        raise FileNotFoundError(f"no Chrome profile named {name!r} in {root}")
    # mkdtemp makes the folder with mode 0700, so on a shared machine no other user can read the
    # copy, which holds logins. Windows ignores the mode; the user's temp folder is private there.
    copy = Path(tempfile.mkdtemp(prefix="flnd-chrome-"))
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


async def _launch(playwright: Playwright, copy: Path | None, *, headless: bool,
                  stealth: bool) -> Browser:
    """Launch a browser for `launch`, with or without a copied profile.

    Args:
        playwright: the started driver.
        copy: a copied profile's folder, or None for an incognito browser.
        headless: hide the window.
        stealth: hide `navigator.webdriver`.

    Returns:
        The browser. `launch` deletes the copy when this raises; on success, the Browser's
        close deletes it.
    """
    launch_options: dict[str, Any] = {"headless": headless, "args": list(LAUNCH_ARGS)}
    width, height = DEFAULT_VIEWPORT
    context_options: dict[str, Any] = {"viewport": {"width": width, "height": height}}
    if copy is not None:
        # A copy of Chrome's profile opens only in Chrome, so it never asks to download Chromium.
        channel = PROFILE_CHANNEL
        # `_copy_profile` always stores the copy as `Default`, whatever the profile was called.
        launch_options["args"].append("--profile-directory=Default")
    else:
        # It may ask a person and download Chromium: off the loop, so nothing else stalls.
        channel = await asyncio.to_thread(_channel, playwright)
    context, close_steps = await _open_context(
        playwright, copy, channel, launch_options, context_options
    )
    if stealth:
        await context.add_init_script(HIDE_WEBDRIVER)
    await context.add_init_script(KEEP_VISIBLE)
    if copy is not None:
        # Removed after Chrome has closed, or Chrome would still be writing into it.
        close_steps.append(lambda: shutil.rmtree(copy, ignore_errors=True))
    return Browser(context, close_steps=close_steps, headless=headless)


def _channel(playwright: Playwright) -> str:
    """The channel `launch` uses: `chrome` when Google Chrome is installed, `chromium` otherwise.

    Without Chrome or a downloaded Chromium, it asks to download Chromium, and raises
    `ToolError(NEEDS_CHROME)` on no or without a terminal. A failed download raises its
    `ToolError`, and the same one again, without asking, for the rest of the process. It runs on
    a worker thread, since the question waits for a person.

    `_chromium_offer` serialises the question: two runs in one process that both find no
    browser ask once, and the second one reads what the first one decided.

    Args:
        playwright: the started driver, which says where its downloaded Chromium would be.

    Returns:
        `"chrome"` or `"chromium"`, Playwright's channel name.

    Raises:
        ToolError: no browser, and the download was declined, impossible without a terminal,
            or failed.
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
        # Without a terminal `confirm` answers its default, no, at once. A no is kept for the
        # process, so it is asked only once.
        if _chromium_declined or not forms.confirm(CHROMIUM_QUESTION, default=False):
            _chromium_declined = True
            raise ToolError(NEEDS_CHROME, hint=INSTALL_CHROME)
        try:
            _install_chromium()
        except ToolError as error:
            # A failed download (no space, no network) would likely fail again after asking again.
            _chromium_failed = error
            raise
    return "chromium"


def _chrome_executable() -> Path | None:
    """The installed Google Chrome executable path, or None if not found.

    It looks only where Playwright's `chrome` channel looks: one fixed path on macOS and Linux,
    and on Windows the same relative path under each of `WINDOWS_ROOTS` in order (the user's
    local app data, Program Files, Program Files (x86)): Chrome installs per user or per machine.

    Returns:
        The executable's path, or None when Chrome isn't installed in a place Playwright finds.
    """
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

    `--no-shell` skips the separate headless shell: `launch` runs Chromium itself in Chrome's
    new headless mode (the `chromium` channel), so the shell would never be used.

    Raises:
        ToolError: the installer failed, with its last line of output.
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
            raise ToolError(f"Chromium was not downloaded: Playwright's installer exited with "
                            f"{installer.returncode}: {last}", hint=INSTALL_CHROME)


async def _open_context(playwright: Playwright, profile: Path | None, channel: str,
                        launch_options: dict[str, Any],
                        context_options: dict[str, Any]) -> tuple[BrowserContext, list]:
    """Launch the browser with or without a profile folder, returning `(context, close_steps)`.

    With no `profile` the browser is an incognito context of its own; with one, Chrome's persistent
    profile in that folder. The returned steps run in order when the `Browser` closes.

    Headless, the browser sends the headed user agent (`_headful_ua`). A persistent context
    can't set a user agent per context, so a short-lived headless launch reads it first, and
    it goes in as a `--user-agent` launch argument.

    Args:
        playwright: the started driver.
        profile: a profile folder to open as Chrome's persistent profile, or None for incognito.
            A real (not copied) profile is refused under the test suite (`refuse_profile`).
        channel: "chrome" or "chromium", from `_channel`.
        launch_options: Playwright's launch options: `headless`, `args`, and the like.
        context_options: Playwright's context options: the viewport, and the like.

    Returns:
        `(context, close_steps)`: the context tabs open in, and the steps that close it, the
        browser and the driver, in that order.
    """
    headless = launch_options["headless"]
    if profile is not None:
        refuse_profile(profile)
    if profile is None:
        remote = await playwright.chromium.launch(channel=channel, **launch_options)
        options = context_options
        if headless:
            options = {**options, "user_agent": await _headful_ua(playwright, channel, remote)}
        context = await remote.new_context(**options)
        return context, [context.close, remote.close, playwright.stop]
    args = launch_options["args"]
    if headless:
        remote = await playwright.chromium.launch(channel=channel, headless=True)
        try:
            agent = await _headful_ua(playwright, channel, remote)
        finally:
            await remote.close()
        args = [*args, f"--user-agent={agent}"]
    context = await playwright.chromium.launch_persistent_context(
        str(profile), channel=channel, **{**launch_options, "args": args}, **context_options)
    return context, [context.close, playwright.stop]


async def _headful_ua(playwright: Playwright, channel: str, remote: Any) -> str:
    """The user agent `remote` reports, with "HeadlessChrome" replaced by "Chrome", cached.

    Headless Chrome names itself "HeadlessChrome", which any site can read, so a headless launch
    sends the headed name instead. Reading it costs a context, so it is kept per executable.

    Args:
        playwright: the started driver, which says where its Chromium is.
        channel: "chrome" or "chromium", which says which executable it is.
        remote: a launched browser to read the name from, the first time.

    Returns:
        The user agent with the headed name.
    """
    if channel == "chrome":
        path = _chrome_executable()
        key = str(path) if path is not None else None
    else:
        key = playwright.chromium.executable_path
    if key is not None and key in _HEADFUL_UA:
        return _HEADFUL_UA[key]
    probe = await remote.new_context()
    try:
        agent = await (await probe.new_page()).evaluate("navigator.userAgent")
    finally:
        await probe.close()
    agent = agent.replace("HeadlessChrome", "Chrome")
    if key is not None:
        _HEADFUL_UA[key] = agent
    return agent


# ---------------------------------------------------------------------------------------------
# One browser and its tabs
# ---------------------------------------------------------------------------------------------

class Browser:
    """A launched browser: open tabs in it and manage its cookies. Used on the loop only.

        chrome = await launch()
        tab = await chrome.tab()
        await chrome.add_cookies(jar)
        await chrome.close()

    Made by `launch`, not directly.

    Args:
        context: Playwright's browser context, which holds the tabs and the cookies.
        close_steps: what `close` runs, in order: the context, the browser, Playwright's driver,
            and the deletion of a profile copy. Each may be a coroutine function.
        headless: whether the window is hidden, which a handoff to a person can't use.
    """

    def __init__(self, context: BrowserContext, *, close_steps: list[Callable[[], Any]],
                 headless: bool):
        self.raw = context
        self.headless = headless
        self._close_steps = close_steps
        self._closed = False

    async def __aenter__(self) -> Browser:
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        await self.close()

    async def tab(self, *, by_script: bool = False) -> Page:
        """Open a new, blank tab.

        Args:
            by_script: scroll it by script, not with the mouse wheel (see `SCROLL_BY`).

        Returns:
            The tab.
        """
        return Page(await self.raw.new_page(), self, by_script=by_script)

    async def cookies(self) -> Jar:
        """Every cookie in the browser, as a `Jar` snapshot."""
        return Jar(dict(cookie) for cookie in await self.raw.cookies())

    async def add_cookies(self, jar: Iterable[Mapping[str, Any]]) -> None:
        """Add each cookie of `jar`, with `path` defaulting to `/`; one it refuses is logged.

        Args:
            jar: the cookies, as a login's `Jar` holds them.

        A cookie Playwright refuses (no domain, a bad expiry) is logged without its value, and
        the others still go in.
        """
        for cookie in jar:
            try:
                await self.raw.add_cookies([{**cookie, "path": cookie.get("path") or "/"}])
            # Playwright's Error is what a cookie it won't take raises; anything else goes up.
            except _raw_error() as error:
                value = str(cookie.get("value", ""))
                message = _first_line(error)
                ui.warn("cookie {name} was rejected: {error}", name=cookie.get("name", ""),
                        error=message.replace(value, "…") if value else message)

    async def close(self) -> None:
        """Close the tabs, the browser and Playwright in order; a second call does nothing.

        A failure while closing is logged once and never raised, so it can't hide the error that
        made the run end; every step runs even when one fails, so the driver is always stopped.
        """
        if self._closed:
            return
        self._closed = True
        failures = []
        for step in self._close_steps:
            try:
                result = step()
                if asyncio.iscoroutine(result):
                    await result
            except Exception as error:
                failures.append(error)
        if failures:
            ui.warn("closing the browser failed: {error}", error=_first_line(failures[0]))


class Page:
    """One tab. Every method is a coroutine for the loop; `raw` is Playwright's `Page`.

        tab = await chrome.tab()
        visit = await tab.goto("https://example.com/")
        html = await tab.content()

    Made by `Browser.tab`, not directly.

    Args:
        raw: Playwright's page.
        browser: the browser it belongs to.
        by_script: scroll the page by script instead of the mouse wheel (see `SCROLL_BY`); an
            element (`scroll(within=…)`) is always scrolled with the wheel.

    Two attributes shape its scrolling: `random`, the source of every scroll's jitter (a test
    seeds its own), and `gate`, the semaphore its login's tabs share so that at most
    `HUMAN_TABS` of them scroll at once with `human` (None: no limit). `RunBrowser.tab` sets it.
    """

    def __init__(self, raw: RawPage, browser: Browser, *, by_script: bool = False):
        self.raw = raw
        self.browser = browser
        self.by_script = by_script
        self.random = random.Random()
        self.gate: asyncio.Semaphore | None = None

    @property
    def url(self) -> str:
        """The tab's URL now; Playwright keeps it, so reading it asks the browser nothing."""
        return self.raw.url

    async def goto(self, url: str, *, idle: bool = False, timeout: float = 30.0) -> Visit | None:
        """Go to `url`, and return its main document's status, final URL and headers.

        Args:
            url: the address to load.
            idle: also wait until the network has been quiet for a moment, not only for `load`.
            timeout: seconds before the navigation counts as failed.

        Returns:
            The `Visit`, or None when it didn't load (a navigation error, which is logged, not
            raised), and also when no response came: Playwright reports none for a move within
            the same document, such as a new `#fragment`.
        """
        try:
            response = await self.raw.goto(
                url, wait_until="networkidle" if idle else "load", timeout=timeout * 1000
            )
        # Playwright's Error covers DNS failures, aborted requests and timeouts ("not loaded").
        except _raw_error() as error:
            clean = redact(url)
            ui.warn("could not open {url}: {error}", url=clean,
                    error=_first_line(error).replace(url, clean))
            return None
        if response is None:
            return None
        return Visit(response.status, response.url, dict(response.headers))

    async def commit(self, url: str) -> Visit | None:
        """Start loading `url` and return once its first answer came, for a warm tab.

        Args:
            url: the address to load.

        Returns:
            Its first answer's `Visit`, or None when there was none.

        Raises:
            Playwright's `Error`: it didn't start loading within `WARM_TIMEOUT`.
        """
        response = await self.raw.goto(url, wait_until="commit", timeout=WARM_TIMEOUT)
        if response is None:
            return None
        return Visit(response.status, response.url, dict(response.headers))

    async def wait(self, css: str | None = None, *, url: Match | None = None,
                   idle: bool = False, timeout: float = 30.0) -> None:
        """Wait until `css` matches an element, `url` matches the URL, and (`idle`) network stops.

        Args:
            css: a CSS selector; waits until a visible element matches it. None doesn't wait.
            url: a glob ("**/feed/"), a compiled regex or a predicate over the URL; waits until the
                tab's URL matches it. None doesn't wait for one.
            idle: also wait until the network has been quiet for 500 ms. With neither `css` nor
                `url`, False waits for the `load` event instead.
            timeout: seconds for every condition together, not for each.

        Raises:
            WaitError: `timeout` seconds passed first. Its message names what was waited for.

        The conditions are waited for in the order above.
        """
        deadline = clock.now() + timeout

        def left() -> float:
            # Playwright reads a timeout of 0 as "no timeout", so the last moment is 1 ms, not 0.
            return max(1.0, (deadline - clock.now()) * 1000)

        what = "the page to load"
        try:
            if css is not None:
                what = f"an element to match {css!r}"
                await self.raw.wait_for_selector(css, timeout=left())
            if url is not None:
                what = f"the URL to match {'a predicate' if callable(url) else repr(url)}"
                await self.raw.wait_for_url(url, timeout=left())
            if idle or (css is None and url is None):
                what = "the network to go quiet" if idle else "the page to load"
                await self.raw.wait_for_load_state("networkidle" if idle else "load",
                                                   timeout=left())
        # Only Playwright's TimeoutError is "too slow"; any other error (closed tab) goes on up.
        except _raw_timeout():
            raise WaitError(f"waited {timeout:g} s for {what}") from None

    async def content(self) -> str:
        """The page's HTML now; reading a page that keeps navigating gives an empty page."""
        return await self._steady(self.raw.content, "<html></html>", "read the page")

    async def evaluate(self, js: str, arg: Any = None) -> Any:
        """Run `js` in the page and return its value, as JSON carries it.

        Args:
            js: an expression, or a function taking `arg`.
            arg: what the function gets; JSON values only.

        Returns:
            The value, or None when the page kept navigating under it (logged).
        """
        return await self._steady(lambda: self.raw.evaluate(js, arg), None, "run a script")

    async def click(self, css: str, *, timeout: float = 10.0) -> None:
        """Click the first element `css` matches, waiting for it to be there and clickable.

        Args:
            css: a CSS selector.
            timeout: seconds before giving up.

        Raises:
            WaitError: no clickable element within `timeout`.
            NetworkError: the tab closed or crashed meanwhile.
        """
        try:
            await self.raw.locator(css).first.click(timeout=timeout * 1000)
        except _raw_timeout():
            raise WaitError(f"waited {timeout:g} s to click {css!r}") from None
        except _raw_error() as error:
            raise NetworkError(f"could not click {css!r}: {_first_line(error)}") from None

    async def capture(self, accept: Callable[[Response], bool]) -> Capture:
        """Start keeping the bodies of the responses this tab receives that `accept` takes.

        Args:
            accept: says, on the loop, whether a response is kept; it must not block.

        Returns:
            The `Capture`, which `Capture.close` stops.
        """
        capture = Capture(self, accept)
        self.raw.on("response", capture.listener)
        return capture

    async def fetch(self, url: str, *, method: str = "GET",
                    headers: Mapping[str, str] | None = None, body: Any = None,
                    check: bool = True) -> net.Response:
        """Send one request from the page, with its cookies, origin and TLS.

        Args:
            url: where to; relative to the page's own URL when relative.
            method: the HTTP method.
            headers: extra headers.
            body: a dict (the query for a GET, JSON otherwise), a list (JSON), text or bytes.
            check: raise for a status of 400 or more, as `flnd.kit.net` does.

        Returns:
            The answer, as `flnd.kit.net` gives one.

        Raises:
            NetworkError: the page couldn't send it or got no answer within `FETCH_TIMEOUT`.
            SiteError: `check` and a refusal (`Blocked` for a 429, `NotFound` for a 404).
        """
        method = method.upper()
        sent = dict(headers or {})
        if isinstance(body, Mapping) and method in net.QUERY_METHODS:
            url, body = _with_query(url, body), None
        elif isinstance(body, (Mapping, list)):
            body = json.dumps(body).encode("utf-8")
            if not any(name.lower() == "content-type" for name in sent):
                sent["Content-Type"] = "application/json"
        elif isinstance(body, str):
            body = body.encode("utf-8")
        encoded = None if body is None else base64.b64encode(bytes(body)).decode("ascii")
        request = {"url": url, "method": method, "headers": sent, "body": encoded,
                   "ms": FETCH_TIMEOUT * 1000}
        where = f"{method} {redact(url)}"
        try:
            result = await self.raw.evaluate(IN_PAGE_FETCH, request)
        # A tab that navigated, closed or crashed meanwhile: the page is gone, not refused.
        except _raw_error() as error:
            raise NetworkError(_without_values(f"{where}: the page couldn't send it: "
                                               f"{_first_line(error)}", sent),
                               retryable=True) from None
        where = f"{method} {redact(result['resolved'])}"
        if "error" in result:
            if result["error"] == "timeout":
                raise NetworkError(f"{where}: no answer within {FETCH_TIMEOUT:g} s",
                                   retryable=True)
            raise NetworkError(_without_values(f"{where}: {result['error']}", sent))
        response = net.Response(
            status=result["status"], url=result["url"], headers=dict(result["headers"]),
            content=base64.b64decode(result["body"]),
        )
        return net._checked(response, method) if check else response

    async def scroll(self, *, until: Callable[[], bool] | None = None,
                     within: str | None = None, idle: float = IDLE, style: str = HUMAN) -> int:
        """Scroll until the feed ends or `until()`, and return the number of scrolls.

        Here a step's progress is the page growing; `Feed` scrolls with its capture watched
        (`scroller(watch=…)`), so a matching response counts too.

        Args:
            until: asked before each scroll, on the loop; True stops. It must not block.
            within: a CSS selector of an element that scrolls on its own; None scrolls the page.
            idle: seconds the bottom may stay quiet, after a whole `WAIT` brought nothing, before
                the feed counts as ended.
            style: `HUMAN` or `FAST` (see `_Scroller`).

        Returns:
            How many scrolls it made.

        Raises:
            Changed: `within` matches no visible element.
            ValueError: `style` is neither.
        """
        scroller = await self.scroller(within=within, idle=idle, style=style)
        while not (until is not None and until()) and await scroller.step():
            pass
        return scroller.scrolls

    async def scroller(self, *, within: str | None = None, idle: float = IDLE,
                       style: str = HUMAN, watch: Capture | None = None) -> _Scroller:
        """A scroller for this tab, which `_Scroller.step` moves one scroll at a time.

        Args:
            within: as `scroll`'s.
            idle: as `scroll`'s.
            style: as `scroll`'s.
            watch: a capture on this tab whose new bodies count as progress; None watches only
                the page's height.

        Returns:
            The scroller, with the page's height measured.

        Raises:
            Changed: `within` matches no visible element.
            ValueError: `style` is neither `HUMAN` nor `FAST`.
        """
        if style not in SCROLLS:
            raise ValueError(f"scroll must be one of {', '.join(SCROLLS)}, not {style!r}")
        target = self.raw.locator(within).first if within is not None else None
        if target is not None and not await target.is_visible():
            raise Changed("scroll(within=…) found no visible element to scroll")
        return _Scroller(self, target, idle=idle, style=style, watch=watch,
                         height=await self._height(target))

    async def close(self) -> None:
        """Close the tab."""
        await self.raw.close()

    async def _steady(self, read: Callable[[], Awaitable[Any]], fallback: Any, what: str, *,
                      tries: int = 3) -> Any:
        """Run a page read; if the page navigates meanwhile, wait for its load and read again.

        Args:
            read: makes the read's coroutine, once per try.
            fallback: what to return when the page kept navigating through every try.
            what: the read, for the warning ("read the page").
            tries: reads in all.

        Returns:
            What `read` gave, or `fallback`.

        Playwright fails a read with "Execution context was destroyed" (or says the page is
        navigating) when the page moves on under it. Other errors go up.
        """
        for attempt in range(tries):
            try:
                return await read()
            except _raw_error() as error:
                if not _gone(error):
                    raise
                if attempt + 1 == tries:
                    ui.warn("could not {what}, the page kept navigating: {error}", what=what,
                            error=_first_line(error))
                    return fallback
                try:
                    # Up to 10 s for the navigation to finish, in milliseconds.
                    await self.raw.wait_for_load_state("load", timeout=10000)
                except _raw_error():
                    # Still moving, or closed; the next read decides.
                    pass
        return fallback

    # -- scrolling: what `_Scroller` asks of the tab -------------------------------------------

    async def _height(self, target: Locator | None) -> int:
        """The scrolled height of the page, or of `target`: its rise is progress."""
        return await self._steady(
            lambda: self.raw.evaluate(PAGE_HEIGHT) if target is None
            else target.evaluate(ELEMENT_HEIGHT), 0, "measure the page")

    async def _at_bottom(self, target: Locator | None) -> bool:
        """Whether the page, or `target`, is scrolled to its very end."""
        return await self._steady(lambda: self.raw.evaluate(AT_BOTTOM) if target is None
                                  else target.evaluate(ELEMENT_AT_BOTTOM),
                                  False, "measure the page")

    async def _area(self, target: Locator | None) -> tuple[float, float, float, float]:
        """The visible part of what scrolls, as (x, y, width, height) in the viewport's pixels.

        Args:
            target: the element that scrolls, or None for the page.

        Returns:
            The area; the whole viewport for the page.

        Raises:
            Changed: the element is gone or outside the viewport.
        """
        width, height = await self._steady(
            lambda: self.raw.evaluate(VIEWPORT), (1280, 720), "measure the page"
        )
        if target is None:
            return 0.0, 0.0, width, height
        try:
            # Short timeout: the element was there a moment ago, so if gone now it won't come back.
            box = await target.bounding_box(timeout=1000)
        # Only Playwright's TimeoutError means "gone"; it is reported as the element missing.
        except _raw_timeout():
            box = None
        if box is None:
            raise Changed("scroll(within=…) found no visible element to scroll")
        # Only the part inside the viewport can be under the mouse.
        top, bottom = max(box["y"], 0.0), min(box["y"] + box["height"], height)
        left, right = max(box["x"], 0.0), min(box["x"] + box["width"], width)
        if bottom <= top or right <= left:
            raise Changed("scroll(within=…) found its element outside the viewport")
        return left, top, right - left, bottom - top

    async def _roll(self, distance: float, target: Locator | None,
                    area: tuple[float, float, float, float], *, steps: int) -> None:
        """Scroll by `distance` pixels in `steps` uneven wheel steps, about `GLIDE` apart.

        The page of a target's tab (`by_script`) scrolls by script, each step with its wheel
        event (see `SCROLL_BY`); anything else gets the mouse over `area`, somewhere near its
        middle, and real wheel events (`page.mouse.wheel`).

        Args:
            distance: pixels down; negative goes up.
            target: the element that scrolls, or None for the page.
            area: the visible part of what scrolls, as `_area` gives it.
            steps: wheel steps; 1 scrolls at once, with no wait.
        """
        weights = [self.random.uniform(0.6, 1.4) for _ in range(steps)]
        total = sum(weights)
        by_script = target is None and self.by_script
        if not by_script:
            left, top, width, height = area
            # The mouse lands somewhere in the middle of the area, never on one pixel each time.
            await self.raw.mouse.move(left + width * self.random.uniform(0.25, 0.75),
                                      top + height * self.random.uniform(0.3, 0.7),
                                      steps=self.random.randint(1, 3))
        for weight in weights:
            part = distance * weight / total
            if by_script:
                await self._steady(lambda part=part: self.raw.evaluate(SCROLL_BY, part), None,
                                   "scroll the page")
            else:
                await self.raw.mouse.wheel(0, part)
            if steps > 1:
                await self._nap(self.random.uniform(*GLIDE))

    async def _nap(self, seconds: float, news: asyncio.Event | None = None) -> None:
        """Wait `seconds`, or less when `news` is set meanwhile.

        Every wait of a scroll goes through here, so a test can make them take no time.

        Args:
            seconds: the longest to wait.
            news: an event that ends the wait early when set; None waits the whole time.
        """
        if news is None:
            await asyncio.sleep(seconds)
            return
        try:
            await asyncio.wait_for(news.wait(), seconds)
        except TimeoutError:
            pass


class _Scroller:
    """Scrolls a tab one step at a time, driven by what the page does, and says when it ended.

    Args:
        page: the tab.
        target: the element that scrolls, or None for the page.
        idle: seconds the bottom may stay quiet, after a whole `WAIT` brought nothing, before the
            feed counts as ended.
        style: `HUMAN` or `FAST`.
        watch: a capture whose new bodies count as progress, or None.
        height: the height measured before the first step.

    A step (`step`):
    1. with `HUMAN`, waits for its turn: at most `HUMAN_TABS` tabs of one login scroll at once;
    2. scrolls. `FAST` jumps past the bottom in one go. `HUMAN` glides down `SCREENS` screens in
       `WHEEL_STEPS` uneven wheel steps, and now and then (`BACK_UP`) first goes up a little and
       hesitates, as a reader going back to something does;
    3. watches for progress: a body the capture read, or the page growing. With `HUMAN` it
       pauses meanwhile, a lognormal time counted from the scroll: `GLANCE` at the median, or
       `LOOK` once new media came. At the very bottom with nothing yet, it waits up to `WAIT`;
    4. progress, or room left to scroll, means go on. A bottom where nothing came in a whole
       `WAIT` starts the idle window: once the bottom stays quiet `idle` seconds more, the feed
       has ended. A feed whose site says when it ends (`Feed`'s cursor) never gets that far.
    """

    def __init__(self, page: Page, target: Locator | None, *, idle: float, style: str,
                 watch: Capture | None, height: int):
        self._page = page
        self._target = target
        self._idle = idle
        self._style = style
        self._watch = watch
        self._height = height
        self._quiet_since: float | None = None
        self.scrolls = 0

    async def step(self) -> bool:
        """Scroll once and watch, as the class's docstring says.

        Returns:
            False when the feed ended, so there is nothing more to scroll for; True otherwise.
        """
        gate = self._page.gate
        if self._style == HUMAN and gate is not None:
            async with gate:
                return await self._step()
        return await self._step()

    async def _step(self) -> bool:
        """`step`'s work, once it is this tab's turn: scroll, watch, and say whether to go on.

        Returns:
            As `step`'s.
        """
        page, target, human = self._page, self._target, self._style == HUMAN
        heard = self._watch.count if self._watch is not None else 0
        before = self._height
        # 2. Scroll.
        await self._move()
        self.scrolls += 1
        moved = clock.now()
        # 3. Watch, pausing meanwhile. One draw sets both pauses, so a step takes as many numbers
        # from the random source whatever the page does, and a seeded run repeats.
        scale = min(math.exp(page.random.gauss(0.0, SPREAD)), LONGEST) if human else 0.0
        glance, look = GLANCE * scale, LOOK * scale
        progressed = await self._progress(heard, before, moved + glance)
        at_bottom = False
        if not progressed:
            at_bottom = await page._at_bottom(target)
        if at_bottom:
            deadline = moved + WAIT
            if self._quiet_since is not None:
                deadline = min(deadline, self._quiet_since + self._idle)
            progressed = await self._progress(heard, before, deadline)
        if human:
            left = moved + (look if progressed else glance) - clock.now()
            if left > 0:
                await page._nap(left)
        # 4. Go on, or count the quiet.
        if progressed or not at_bottom:
            self._quiet_since = None
            return True
        if self._quiet_since is None:
            self._quiet_since = clock.now()
            return True
        # The same sum as the deadline above, so a wait that reached it is always the end.
        return clock.now() < self._quiet_since + self._idle

    async def _move(self) -> None:
        """Scroll once, as the style says: a jump to the bottom, or a human glide."""
        page, target = self._page, self._target
        area = await page._area(target)
        if self._style == FAST:
            # Past the end: the browser stops at the bottom, wherever that is now.
            await page._roll(self._height + area[3], target, area, steps=1)
            return
        rng = page.random
        distance = area[3] * rng.uniform(*SCREENS)
        if rng.random() < BACK_UP:
            up = area[3] * rng.uniform(*BACK_UP_SCREENS)
            await page._roll(-up, target, area, steps=rng.randint(*WHEEL_STEPS))
            await page._nap(_lognormal(rng, HESITATE))
            distance += up
        await page._roll(distance, target, area, steps=rng.randint(*WHEEL_STEPS))

    async def _progress(self, heard: int, before: int, deadline: float) -> bool:
        """Watch for progress until `deadline`, a time on `flnd.kit.clock`'s clock.

        Args:
            heard: the capture's `count` before the scroll; a count past it is progress.
            before: the height before the scroll; a height past it is progress.
            deadline: when to give up.

        Returns:
            Whether progress came. The height last measured is kept for the next step.
        """
        page, watch = self._page, self._watch
        news = watch.news if watch is not None else None
        while True:
            # Cleared before the count is read: a body read after that sets it again, so the nap
            # below ends at once instead of missing it.
            if news is not None:
                news.clear()
            if watch is not None and watch.count > heard:
                return True
            height = await page._height(self._target)
            self._height = max(self._height, height)
            if height > before:
                return True
            left = deadline - clock.now()
            if left <= 0:
                return False
            await page._nap(min(POLL, left), news)


def _lognormal(rng: random.Random, median: float) -> float:
    """A lognormal pause around `median` seconds, `SPREAD` wide, at most `LONGEST` medians."""
    return median * min(math.exp(rng.gauss(0.0, SPREAD)), LONGEST)


class Capture:
    """The bodies of the responses a tab receives that `accept` takes, in the order they came.

        capture = await tab.capture(lambda response: "graphql" in response.url)
        ...
        while capture.items:
            url, body = capture.items.popleft()      # on any thread
        await capture.close()

    Made by `Page.capture`. Playwright calls `listener` on the loop for each response; the body is
    read at once in a task of its own, and `items` gets `(url, body)` in arrival order however the
    reads finish. A body Chrome already freed (its page moved on) is skipped.

    `items` is a deque: the loop appends and a listing thread pops, which a deque allows without a
    lock. Everything else runs on the loop. `count` and `news` are what a scroll step watches:
    how many responses have had their turn so far, and an event set each time more have.

    Args:
        page: the tab it listens on.
        accept: whether a response is kept; called on the loop.
    """

    def __init__(self, page: Page, accept: Callable[[Response], bool]):
        self.items: deque[tuple[str, str]] = deque()
        self._page = page
        self._accept = accept
        # Each accepted response gets the next number; `_ready` holds the reads done out of turn.
        self._numbered = 0
        self._flushed = 0
        self._ready: dict[int, tuple[str, str] | None] = {}
        self._reads: set[asyncio.Task[None]] = set()
        self._stopped = False
        # Set on the loop each time responses have their turn; a scroll step clears it and waits.
        self.news = asyncio.Event()
        # One bound method, kept, so `remove_listener` gets the very object `on` was given.
        self.listener = self._heard

    @property
    def count(self) -> int:
        """How many accepted responses have had their turn, an unreadable body included."""
        return self._flushed

    def _heard(self, response: Response) -> None:
        """Playwright's call for each response the tab gets: number it and start reading it.

        Args:
            response: Playwright's response; one `accept` refuses is left alone.
        """
        if self._stopped or not self._accept(response):
            return
        number, self._numbered = self._numbered, self._numbered + 1
        task = asyncio.ensure_future(self._read(number, response))
        self._reads.add(task)
        task.add_done_callback(self._reads.discard)

    async def _read(self, number: int, response: Response) -> None:
        """Read one body, then move every body now in turn into `items`.

        Args:
            number: the response's place in arrival order.
            response: Playwright's response.
        """
        entry: tuple[str, str] | None = None
        try:
            entry = (response.url, await response.text())
        except _raw_error() as error:
            # Chrome frees a page's response bodies when the page moves on, and a read that comes
            # after that finds nothing: expected, so it is skipped without a line. Any other
            # unreadable body is still worth a debug line.
            if not _gone(error):
                ui.debug("responses: no body for {url}: {error}",
                         url=redact(response.url), error=_first_line(error))
        finally:
            self._ready[number] = entry
            while self._flushed in self._ready:
                done = self._ready.pop(self._flushed)
                self._flushed += 1
                if done is not None:
                    self.items.append(done)
                self.news.set()

    async def settle(self) -> None:
        """Wait for the bodies still being read, so `items` holds every response heard so far."""
        if self._reads:
            await asyncio.gather(*list(self._reads), return_exceptions=True)

    async def close(self) -> list[tuple[str, str]]:
        """Stop listening, wait for the reads under way, and empty `items`.

        Returns:
            The `(url, body)` pairs `items` still held, oldest first.
        """
        if not self._stopped:
            self._stopped = True
            self._page.raw.remove_listener("response", self.listener)
        await self.settle()
        left = list(self.items)
        self.items.clear()
        return left


# ---------------------------------------------------------------------------------------------
# The run's browser, and one target's tabs
# ---------------------------------------------------------------------------------------------

class RunBrowser:
    """One browser shared by many users on one loop, launched the first time one asks for a tab.

        shared = RunBrowser(loop)
        tab = shared.run(shared.tab(jar))
        shared.close()

    Every user shares it, so one window serves them all; each user's cookies are added the first
    time it asks. (In flnd there is one per run, and its users are the targets.) Its coroutines
    run on the loop; `run` hands one to the loop from another thread.

    The tabs of one login (one `jar`) share one `Page.gate`, so at most `HUMAN_TABS` of them
    scroll at once with `human`, however many users hold them.

    Args:
        loop: the loop the browser lives on.
        stealth: hide `navigator.webdriver` (see `launch`).
        headless: hide the window. A visible one (the default) lets a person solve a captcha.
    """

    def __init__(self, loop: Loop, *, stealth: bool = True, headless: bool = False):
        self.loop = loop
        self._stealth = stealth
        self._headless = headless
        self._browser: Browser | None = None
        # Made on the loop, the first time a tab is asked for: a lock belongs to its loop.
        self._launching: asyncio.Lock | None = None
        # One tab a finished target left, for the next target to reuse, so the window stays open.
        self._idle: list[Page] = []
        # Each login seen so far, by identity, with the gate its tabs share.
        self._jars: list[tuple[Any, asyncio.Semaphore]] = []

    @property
    def headless(self) -> bool:
        """Whether the window is hidden; False before the browser is launched."""
        return self._browser is not None and self._browser.headless

    @property
    def launched(self) -> bool:
        """Whether a target has asked for a tab yet, so the browser is running."""
        return self._browser is not None

    @property
    def tabs(self) -> int:
        """How many tabs are open now, warm ones and the idle one included; 0 before launch.

        Read on the loop, where the browser lives: Playwright's list of the context's pages.
        A user holds at most `MAX_TABS` of them.
        """
        return 0 if self._browser is None else len(self._browser.raw.pages)

    def run(self, awaitable: Awaitable[Any]) -> Any:
        """Run `awaitable` on the loop from another thread, and return its value.

        Args:
            awaitable: one of this module's coroutines.

        Returns:
            Its value.

        Raises:
            RuntimeError: called on the loop's own thread, which would wait for itself; code
                there awaits instead.
        """
        return self.loop.run(awaitable)

    async def tab(self, jar: Any) -> Page:
        """A tab for a target whose login is `jar`: the idle one, or a new one.

        Args:
            jar: the source's login cookies, added to the browser the first time they come.

        Returns:
            The tab, which scrolls by script, with the gate of `jar`'s tabs.
        """
        if self._launching is None:
            self._launching = asyncio.Lock()
        async with self._launching:
            if self._browser is None:
                self._browser = await launch(stealth=self._stealth, headless=self._headless)
            gate = next((gate for seen, gate in self._jars if seen is jar), None)
            if gate is None:
                await self._browser.add_cookies(jar)
                gate = asyncio.Semaphore(HUMAN_TABS)
                self._jars.append((jar, gate))
        page = self._idle.pop() if self._idle else await self._browser.tab(by_script=True)
        # A tab another login left behind takes this login's gate.
        page.gate = gate
        return page

    async def release(self, pages: Iterable[Page]) -> None:
        """Take back a finished target's tabs: keep one open for the next target, close the rest.

        Args:
            pages: the tabs; one given twice counts once.
        """
        seen: set[int] = set()
        for page in pages:
            if id(page) in seen or any(page is idle for idle in self._idle):
                continue
            seen.add(id(page))
            if not self._idle:
                self._idle.append(page)
                continue
            try:
                await page.close()
            # A tab that crashed or closed already needs no closing.
            except Exception:
                pass

    async def cookies(self) -> Jar:
        """Every cookie in the browser; none before it is launched."""
        if self._browser is None:
            return Jar([])
        return await self._browser.cookies()

    def close(self) -> None:
        """Close the browser, from the thread that made the run; a second call does nothing."""
        browser, self._browser = self._browser, None
        self._idle.clear()
        self._jars.clear()
        if browser is not None:
            self.loop.run(browser.close())


class Tabs:
    """One user's tabs in a shared browser (in flnd, a target's), used from that user's thread.

        tabs = Tabs(shared, jar, scroll="human")
        visit = tabs.run(tabs.page.goto(url))
        tabs.warm([next_url])          # starts loading it in another tab
        tabs.take_warm(next_url)       # makes that tab the target's own
        tabs.close()

    The target works in one tab, `page`, opened on first use. A scrolling feed opened while
    another scrolling feed is still open gets a tab of its own on a stack, so the outer feed's
    page stays where it was. `warm` loads the pages the source says it will open next in
    background tabs, up to `MAX_TABS` in all, keeping the responses they get; `take_warm` then
    switches to such a tab, and a feed that is open moves to it with those responses.

    Every Playwright call goes through `run`, one hop to the loop each.

    Args:
        shared: the run's browser.
        jar: the source's login cookies.
        scroll: how its feeds scroll: `HUMAN` (the default) or `FAST` (see `_Scroller`).

    Raises:
        ValueError: `scroll` is neither.
    """

    def __init__(self, shared: RunBrowser, jar: Any, *, scroll: str = HUMAN):
        if scroll not in SCROLLS:
            raise ValueError(f"scroll must be one of {', '.join(SCROLLS)}, not {scroll!r}")
        self._shared = shared
        self._jar = jar
        self.scroll = scroll
        self._page: Page | None = None
        self._stack: list[Page] = []
        self._idle: deque[Page] = deque()
        # Warm tabs by the URL they load, with their first answer when it came.
        self._warmed: dict[str, tuple[Page, Visit | None]] = {}
        self._pending: deque[str] = deque()
        # The open feeds, innermost last: (feed, whether it pushed a tab, whether it scrolls).
        self._feeds: list[tuple[Feed, bool, bool]] = []
        # What each warm tab's load brought, kept until a feed adopts the tab.
        self._buffers: dict[Page, Capture] = {}

    def run(self, awaitable: Awaitable[Any]) -> Any:
        """Run one of a tab's coroutines on the loop; see `RunBrowser.run`."""
        return self._shared.run(awaitable)

    @property
    def opened(self) -> bool:
        """Whether the target has a tab yet."""
        return self._page is not None or bool(self._stack)

    @property
    def headless(self) -> bool:
        """Whether the browser's window is hidden."""
        return self._shared.headless

    @property
    def page(self) -> Page:
        """The tab the target works in now, opened (and the browser launched) on first use."""
        if self._stack:
            return self._stack[-1]
        if self._page is None:
            self._page = self._new()
        return self._page

    async def current(self) -> Page:
        """`page`, for code on the loop: the tab the target works in, opened on first use.

        Returns:
            The tab.
        """
        if self._stack:
            return self._stack[-1]
        if self._page is None:
            self._page = self._idle.popleft() if self._idle else await self._shared.tab(self._jar)
        return self._page

    def handoff(self, *, until: Callable[[], bool], message: str, timeout: float | None,
                stop: _threading.Event | None) -> bool:
        """Show `message` and wait for a person to finish a step in the window; see `Site.handoff`.

        Args:
            until: True once the step is done; asked on this thread about once a second.
            message: what the person should do.
            timeout: seconds before giving up; None waits as long as it takes.
            stop: the run's stop event, which ends the wait.

        Returns:
            True when `until` came true; False when `timeout` passed first.

        Raises:
            UsageError: the window is hidden, so no person can see it.
            Stopped: the run was told to stop.
        """
        if until():
            return True
        if self.headless:
            raise UsageError(f"cannot hand off to a person in a headless browser: {message}",
                             hint="run it again with a visible browser")
        ui.warn(message)
        deadline = None if timeout is None else clock.now() + timeout
        with ui.progress(message) as bar:
            while not until():
                if deadline is not None and clock.now() >= deadline:
                    bar.fail()
                    return False
                if not clock.sleep(HANDOFF_POLL, stop=stop):
                    raise Stopped("stopped while waiting for a person")
        return True

    # -- tabs -----------------------------------------------------------------------------------

    def _new(self) -> Page:
        """An idle tab of this target, or a tab from the run's browser."""
        if self._idle:
            return self._idle.popleft()
        return self.run(self._shared.tab(self._jar))

    def _recycle(self, tab: Page) -> None:
        if tab not in self._idle:
            self._idle.append(tab)

    def _switch(self, tab: Page) -> None:
        """Make `tab` the target's, recycle the old one, and move the innermost feed to it.

        Args:
            tab: a warm tab, now the one the target works in. With no feed open, the responses
                its load brought are dropped.
        """
        if self._stack:
            old, self._stack[-1] = self._stack[-1], tab
        else:
            old, self._page = self._page, tab
        if old is not None and old is not tab:
            self._recycle(old)
        if self._feeds:
            self._feeds[-1][0].adopt(tab)
        else:
            self.take_buffer(tab)

    def push(self, feed: Feed, *, scroll: bool) -> None:
        """Register `feed`; a scrolling one inside another scrolling one gets a tab of its own.

        Args:
            feed: the feed being opened.
            scroll: whether it scrolls; one that only listens shares the tab it opens on.
        """
        pushed = scroll and any(scrolls for _, _, scrolls in self._feeds)
        if pushed:
            _ = self.page
            self._stack.append(self._new())
        self._feeds.append((feed, pushed, scroll))

    def pop(self, feed: Feed) -> None:
        """Unregister `feed`, and recycle the tab it pushed.

        Args:
            feed: the feed being closed.
        """
        for index in range(len(self._feeds) - 1, -1, -1):
            if self._feeds[index][0] is feed:
                _, pushed, _ = self._feeds.pop(index)
                if pushed and self._stack:
                    self._recycle(self._stack.pop())
                return

    # -- warm tabs ------------------------------------------------------------------------------

    def warm(self, urls: Iterable[str]) -> None:
        """Start loading `urls` in background tabs, as many as `MAX_TABS` allows; queue the rest.

        Args:
            urls: pages the target will open next, in order.
        """
        for url in urls:
            if url not in self._warmed and url not in self._pending:
                self._pending.append(url)
        self._fill()

    def _fill(self) -> None:
        """Start loading queued URLs in idle or new tabs, up to `MAX_TABS` held in all."""
        _ = self.page
        while self._pending:
            held = (1 if self._page is not None else 0) + len(self._stack) + len(self._warmed)
            if held >= MAX_TABS:
                return
            url = self._pending.popleft()
            if url in self._warmed:
                continue
            tab = self._new()
            started, visit = self._start_warm(tab, url)
            if not started:
                self._recycle(tab)
                continue
            self._warmed[url] = (tab, visit)

    def _start_warm(self, tab: Page, url: str) -> tuple[bool, Visit | None]:
        """Start loading `url` in `tab`, keeping its responses.

        Args:
            tab: an idle or new tab.
            url: the page to warm.

        Returns:
            `(started, first answer)`; not started when the load failed at once.
        """
        self.take_buffer(tab)
        self._buffers[tab] = self.run(tab.capture(_not_an_asset))
        try:
            return True, self.run(tab.commit(url))
        # Any failure only means no warm tab: `Site.open` loads the page as usual.
        except Exception:
            self.take_buffer(tab)
            return False, None

    def take_buffer(self, tab: Page) -> list[tuple[str, str]]:
        """Stop keeping `tab`'s responses, and return the `(url, body)` it kept."""
        capture = self._buffers.pop(tab, None)
        return [] if capture is None else self.run(capture.close())

    def take_warm(self, url: str) -> tuple[bool, Visit | None]:
        """Switch to the warm tab loading `url`, if there is one, and warm the next queued URL.

        Args:
            url: the page about to be opened.

        Returns:
            `(found, first answer)`: whether a warm tab was loading it, and its first answer.
        """
        if url in self._pending:
            self._pending.remove(url)
        warmed = self._warmed.pop(url, None)
        if warmed is None:
            return False, None
        tab, visit = warmed
        self._switch(tab)
        self._fill()
        return True, visit

    # -- the end --------------------------------------------------------------------------------

    def close(self) -> None:
        """Give the target's tabs back to the run's browser; a second call does nothing."""
        held = [*self._stack, *self._idle, *(tab for tab, _ in self._warmed.values())]
        if self._page is not None:
            held.append(self._page)
        try:
            for tab in list(self._buffers):
                self.take_buffer(tab)
            if held:
                self.run(self._shared.release(held))
        finally:
            self._page = None
            self._stack.clear()
            self._idle.clear()
            self._warmed.clear()
            self._pending.clear()
            self._feeds.clear()
            self._buffers.clear()


def _not_an_asset(response: Response) -> bool:
    """Whether a warm tab keeps `response`: anything but images, media, fonts, styles, scripts."""
    return response.request.resource_type not in ASSET_TYPES


# ---------------------------------------------------------------------------------------------
# Feeds
# ---------------------------------------------------------------------------------------------

class Feed:
    """The JSON of the matching responses a target's tab receives; what `site.feed` returns.

        with site.feed("/graphql", scroll=True, cursor=page_state) as pages:
            site.open(url)
            for page in pages: ...

    Iterating yields each body that arrived, oldest first. Then, with `scroll`, it scrolls the tab
    one step at a time until another arrives (`_Scroller`, in the tabs' `scroll` style), and ends
    when the feed ended:
    - the site said so: `cursor` read a page with no next one, or the same cursor came back more
      than `REPEATS` times (the site is answering the same page again);
    - or, for a feed without a cursor reader, or whose site never said, the bottom stayed quiet
      (`WAIT`, then `IDLE`).
    Without `scroll`, it waits once for the network to go quiet (up to `QUIET`), yields what
    came, and ends. Leaving `with`, or the end of the iteration, closes it.

    Made on the listing thread, used there only; the cursor reader runs there too.

    Args:
        tabs: the target's tabs.
        match: which responses: a substring of the URL, a regex searched in it, or a predicate.
        scroll: scroll for more when none is waiting.
        lines: a body holds one JSON value per line (NDJSON), not one in all.
        cursor: reads each parsed value (each line, with `lines`), as `Reader` says; None never
            ends the feed on the site's word.
    """

    def __init__(self, tabs: Tabs, match: Match, *, scroll: bool = False, lines: bool = False,
                 cursor: Reader | None = None):
        self._tabs = tabs
        self._matches = _matcher(match)
        self._scroll = scroll
        self._lines = lines
        self._cursors = _Cursors(cursor)
        self._closed = False
        self._waiting: deque[Any] = deque()
        tabs.push(self, scroll=scroll)
        self._page = tabs.page
        self._capture: Capture = tabs.run(self._page.capture(self._accepts))
        # Made at the first scroll, and again after `adopt` moves the feed to another tab.
        self._scroller: _Scroller | None = None
        self._items = self._iterate()

    def _accepts(self, response: Response) -> bool:
        return self._matches(response.url)

    def __iter__(self) -> Iterator[Any]:
        return self

    def __next__(self) -> Any:
        return next(self._items)

    def __enter__(self) -> Feed:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    @property
    def ended(self) -> bool:
        """Whether the site said the feed has no more pages (see the class's docstring)."""
        return self._cursors.ended

    def close(self) -> None:
        """Stop listening and give back a tab it pushed; a second call does nothing."""
        if self._closed:
            return
        self._closed = True
        try:
            self._tabs.run(self._capture.close())
        finally:
            self._tabs.pop(self)

    def adopt(self, page: Page) -> None:
        """Move to `page`, the target's tab now, taking the matching responses its load brought.

        Args:
            page: the warm tab the target switched to. What the old tab's capture still held is
                kept, ahead of what the new one brought.
        """
        if self._closed or page is self._page:
            return
        for url, body in self._tabs.run(self._capture.close()):
            self._parse(url, body)
        self._page = page
        for url, body in self._tabs.take_buffer(page):
            if self._matches(url):
                self._parse(url, body)
        self._capture = self._tabs.run(page.capture(self._accepts))
        self._scroller = None

    def _iterate(self) -> Iterator[Any]:
        """The bodies, scrolling or waiting once for more as the class's docstring says.

        Yields:
            Each parsed JSON value, oldest first.
        """
        try:
            while True:
                yield from self._arrived()
                if not self._scroll:
                    try:
                        self._tabs.run(self._page.wait(idle=True, timeout=QUIET))
                    except WaitError:
                        pass
                    self._tabs.run(self._capture.settle())
                    yield from self._arrived()
                    return
                if not self._scroll_until_one():
                    return
        finally:
            self.close()

    def _scroll_until_one(self) -> bool:
        """Scroll step by step, one call to the loop each, until a body is waiting.

        Returns:
            True when one is; False when the feed ended, the bodies still being read included.
        """
        while not self._fill():
            if not self._cursors.ended:
                if self._scroller is None:
                    self._scroller = self._tabs.run(self._page.scroller(
                        style=self._tabs.scroll, watch=self._capture))
                if self._tabs.run(self._scroller.step()):
                    continue
            # The end: the site said so, or the page went quiet. A body still being read is
            # waited for, since it may be the last page.
            self._tabs.run(self._capture.settle())
            return self._fill()
        return True

    def _arrived(self) -> Iterator[Any]:
        self._fill()
        while self._waiting:
            yield self._waiting.popleft()

    def _fill(self) -> bool:
        """Parse the bodies the capture holds into `_waiting`; whether any is waiting."""
        items = self._capture.items
        while items:
            self._parse(*items.popleft())
        return bool(self._waiting)

    def _parse(self, url: str, body: str) -> None:
        """Parse `body`, or each line of it with `lines`, into `_waiting`, reading its cursor.

        Args:
            url: where it came from, named in the debug line for a piece that isn't JSON.
            body: the response's text.
        """
        pieces = [line for line in body.splitlines() if line.strip()] if self._lines else [body]
        for piece in pieces:
            try:
                value = json.loads(piece)
            # A piece that isn't JSON (an HTML error page, say) is skipped; the others still count.
            except ValueError:
                ui.debug("responses: skipped a body that is not JSON from {url}",
                         url=redact(url))
                continue
            self._waiting.append(value)
            self._cursors.read(value)


class _Cursors:
    """What a feed's pages said about the next one, and whether the site said there is none.

        cursors = _Cursors(lambda page: (page["more"], page.get("next")))
        cursors.read({"more": True, "next": "c2"})
        cursors.ended                                # False
        cursors.read({"more": False})
        cursors.ended                                # True

    Used by `Feed`, and by test fakes that stand in for one, so both end the same way.

    Args:
        reader: the source's cursor reader (see `Reader`); None reads nothing and never ends.
    """

    def __init__(self, reader: Reader | None):
        self._reader = reader
        self._seen: Counter[str] = Counter()
        self.ended = False

    def read(self, value: Any) -> None:
        """Read one parsed value: a page with no next one, or a cursor seen too often, ends it.

        Args:
            value: one body, or one line of an NDJSON body.
        """
        if self._reader is None or self.ended:
            return
        state = self._reader(value)
        if state is None:
            return
        more, cursor = state
        if not more:
            self.ended = True
            return
        if cursor is not None:
            self._seen[cursor] += 1
            self.ended = self._seen[cursor] > 1 + REPEATS


# ---------------------------------------------------------------------------------------------
# A page's local storage, for a login
# ---------------------------------------------------------------------------------------------

def local_storage(url: str, *, profile: str = "Default") -> dict[str, str] | None:
    """Open `url` in a hidden copy of a Chrome profile, and read the page's local storage.

        storage = local_storage("https://web.telegram.org/k/")

    For a login that reads a web app's saved session (Telegram's). It runs outside any run, so it
    starts a loop of its own and closes it, with the browser and the profile copy, before
    returning.

    Args:
        url: the page.
        profile: the installed Chrome profile to copy, with its logins.

    Returns:
        `localStorage` as `{key: value}`, or None when the tab didn't land on `url`'s host (no
        network, or the site sent it elsewhere), which is not read then.

    Raises:
        NetworkError: the page couldn't be read (it navigated away or closed meanwhile).
        FileNotFoundError: Chrome has no such profile.
        ToolError: Chrome isn't installed.
    """
    from flnd.kit.loop import Loop

    with Loop() as loop:
        return loop.run(_local_storage(url, profile))


async def _local_storage(url: str, profile: str) -> dict[str, str] | None:
    """`local_storage`'s work, on its loop.

    Args:
        url: the page.
        profile: the Chrome profile to copy.

    Returns:
        The storage, or None when the tab landed elsewhere.

    Raises:
        NetworkError: the read failed.
    """
    async with await launch(profile=profile, headless=True) as chrome:
        page = await chrome.tab()
        await page.goto(url, idle=True)
        if urlsplit(page.url).hostname != urlsplit(url).hostname:
            return None
        try:
            return await page.raw.evaluate("Object.assign({}, localStorage)")
        except _raw_error() as error:
            where = redact(url)
            raise NetworkError(f"could not read {where}: {_first_line(error)}") from None


# ---------------------------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------------------------

def _with_query(url: str, params: Mapping[str, Any] | Sequence[tuple[str, Any]]) -> str:
    parts = urlsplit(url)
    pairs = parse_qsl(parts.query, keep_blank_values=True)
    items = params.items() if isinstance(params, Mapping) else params
    pairs.extend((str(key), str(value)) for key, value in items if value is not None)
    return urlunsplit(parts._replace(query=urlencode(pairs)))


def _without_values(text: str, headers: Mapping[str, str]) -> str:
    """`text` with every header value in `headers` replaced by `***`, hiding tokens."""
    for value in headers.values():
        if value:
            text = text.replace(value, "***")
    return text


def _first_line(error: BaseException) -> str:
    return (str(error).splitlines() or [""])[0]


def _matcher(match: Match) -> Callable[[str], bool]:
    """`match` as a predicate over a URL.

    Args:
        match: a substring the URL must contain, a compiled regex it must match somewhere
            (`search`, not `match`), or a function that takes the URL and says yes or no.

    Returns:
        A function from a URL to whether it matches.

    Raises:
        TypeError: `match` is none of the three.
    """
    if isinstance(match, str):
        return lambda url: match in url
    if isinstance(match, re.Pattern):
        return lambda url: match.search(url) is not None
    if callable(match):
        return match
    raise TypeError(
        f"match must be a str, a compiled regex, or a function, not {type(match).__name__}"
    )

