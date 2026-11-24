"""A browser library: async Playwright, shared tabs, response capture, scrolling.

SPEC.md#for-developers explains it."""

from __future__ import annotations

import asyncio
import base64
import contextlib
import functools
import inspect
import json
import logging
import math
import os
import random
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import weakref
from collections import Counter, deque
from collections.abc import AsyncIterator, Awaitable, Callable, Iterable, Iterator, Mapping
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import TYPE_CHECKING, Any, Union
from urllib.parse import urlsplit

import msgspec

from flnkit import clock, net
from flnkit.errors import Blocked, Changed, NetworkError, Stopped, ToolError, UsageError
from flnkit.text import redact

_log = logging.getLogger(__name__)

NO_PROFILES = "FLND_TEST_NO_PROFILES"


class Screen:
    """What the browser asks of a person and shows them: a yes-or-no question and a progress row.

    This default asks on a plain terminal, says no without one, and draws no rows. flnd passes
    its own (`flnd.engine.ctx`), so its questions and rows look like the rest of its screen.
    """

    def confirm(self, question: str) -> bool:
        """Ask `question`; True only for a yes. No terminal is a no, at once.

        Args:
            question: the yes-or-no question, shown with a `[y/N]` suffix.

        Returns:
            True when the answer is `y` or `yes` in any case, with spaces around it ignored.
            False for anything else, and for no terminal.
        """
        if not (sys.stdin.isatty() and sys.stdout.isatty()):
            return False
        try:
            return input(f"{question} [y/N] ").strip().lower() in ("y", "yes")
        except EOFError:
            return False

    def progress(self, label: str, *, total: float | None = None) -> Any:
        """A row for `label`, as a context manager giving a bar with `count` and `fail()`."""
        return contextlib.nullcontext(_Bar())


class _Bar:
    """The bar of the default `Screen`: it counts, and draws nothing."""

    count: float = 0

    def fail(self) -> None:
        """Mark the row failed; the default screen has nothing to mark."""


def _real_home() -> Path:
    """The home folder of the account running flnd, which a changed `$HOME` does not move."""
    if sys.platform == "win32":
        return Path(os.environ.get("USERPROFILE") or os.path.expanduser("~"))
    try:
        import pwd
        return Path(pwd.getpwuid(os.getuid()).pw_dir)
    except (ImportError, AttributeError):
        return Path.home()


def refuse_profile(folder: Path | str | None = None) -> None:
    """Raise when a test would read the real browser profile under the home folder.

    Does nothing unless `FLND_TEST_NO_PROFILES` is set. A folder inside the temp directory
    is always allowed.

    Args:
        folder: the folder a test is about to read or copy; None lets it pass.

    Raises:
        RuntimeError: the folder is inside the real home folder, outside the temp directory.
    """
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
    """An immutable collection of cookies: filter it, read it by name, send or save it.

    Args:
        cookies: the cookies, each a dict with at least a `name` and a `value`.
        error: the exception class a bad cookie raises; `UsageError` by default.
    """

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
        """A new Jar with the cookies that match `domains` and `names`.

        Args:
            domains: one domain or several. A cookie matches when its domain is one of them
                or a subdomain of one. None keeps every domain.
            names: the cookie names to keep; a single name counts as one. None keeps all.

        Returns:
            A new Jar of the matching cookies. This one is not changed.
        """
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
        """Write the cookies to `dest` as a Netscape cookie file, readable only by its owner.

        Args:
            dest: the file to write; its folder is made when it is missing.

        Returns:
            `dest` as a Path.

        Raises:
            UsageError: a cookie has a tab or newline in a field. The error class the jar was
                made with is raised instead when it is not `UsageError`.
        """
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
        from flnkit import files
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

__all__ = [
    "FAST", "HUMAN", "Browser", "Capture", "Feed", "Jar", "Match", "Page", "Reader",
    "Cursors", "RunBrowser", "Tabs", "Visit", "WaitError", "launch", "local_storage", "matcher",
    "run_on",
]

# ---------------------------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------------------------

# Switches that hide the automation Chrome would otherwise show (AutomationControlled sets
# `navigator.webdriver`; infobars shows the automated-software bar) and keep Chrome from
# throttling the background tabs of the one window every target shares.
LAUNCH_ARGS = [
    "--disable-blink-features=AutomationControlled", "--disable-infobars",
    "--disable-background-timer-throttling", "--disable-backgrounding-occluded-windows",
    "--disable-renderer-backgrounding", "--disable-gpu", "--renderer-process-limit=1",
    "--disable-site-isolation-trials", "--disable-extensions", "--disable-default-apps",
    "--disable-component-extensions-with-background-pages", "--disable-background-networking",
    "--disable-sync", "--disable-translate", "--disable-client-side-phishing-detection",
    "--disable-component-update", "--disable-domain-reliability", "--mute-audio",
    "--disable-renderer-accessibility", "--autoplay-policy=user-gesture-required",
    "--no-first-run", "--no-default-browser-check", "--metrics-recording-only",
    "--disable-breakpad", "--disable-hang-monitor", "--disable-ipc-flooding-protection",
    "--password-store=basic", "--use-mock-keychain", "--disable-dev-shm-usage",
    "--disk-cache-size=1", "--media-cache-size=1", "--js-flags=--max-old-space-size=1024",
    ("--disable-features=Translate,MediaRouter,OptimizationHints,BackForwardCache,"
     "AcceptCHFrame,InterestFeedContentSuggestions,ProcessPerSiteUpToMainFrameThreshold,"
     "SpareRendererForSitePerProcess,CalculateNativeWinOcclusion,IntensiveWakeUpThrottling,"
     "QuickIntensiveWakeUpThrottlingAfterLoading,Prerender2,HeavyAdIntervention,"
     "MediaSessionService,AutofillServerCommunication,CertificateTransparencyComponentUpdater"),
]
# Browser process name substrings and POSIX niceness used to keep background tabs polite.
BROWSER_PROCS = ("chrom", "headless", "firefox", "webkit", "msedge")
POSIX_NICE = 10
# `navigator.webdriver` is removed on `Navigator.prototype`, matching how an ordinary browser acts.
HIDE_WEBDRIVER = (
    "Object.defineProperty(Navigator.prototype, 'webdriver', "
    "{get: () => undefined, configurable: true})"
)
# Background tabs inside one shared window must still look visible and focused to site scripts.
KEEP_VISIBLE = (
    "Object.defineProperties(Document.prototype, {"
    "visibilityState: {get: () => 'visible', configurable: true}, "
    "hidden: {get: () => false, configurable: true}"
    "});"
    "Document.prototype.hasFocus = () => true;"
    "window.addEventListener('visibilitychange', e => e.stopImmediatePropagation(), true);"
    "document.addEventListener('visibilitychange', e => e.stopImmediatePropagation(), true);"
    "window.addEventListener('blur', e => e.stopImmediatePropagation(), true);"
)
# What a browser reports as its screen: a desktop one, larger than its window. Playwright would
# report the page's size, which no real screen is.
SCREEN = {"width": 1920, "height": 1080}
# What the WebGL vendor and renderer read when a hidden browser has no GPU (SwiftShader), by
# platform: a typical desktop's, so they match the user agent.
SOFTWARE_GL = {
    "darwin": ("Google Inc. (Apple)",
               "ANGLE (Apple, ANGLE Metal Renderer: Apple M1, Unspecified Version)"),
    "win32": ("Google Inc. (NVIDIA)",
              "ANGLE (NVIDIA, NVIDIA GeForce GTX 1650 Direct3D11 vs_5_0 ps_5_0, D3D11)"),
    "linux": ("Google Inc. (Intel)",
              "ANGLE (Intel, Mesa Intel(R) UHD Graphics 620 (KBL GT2), OpenGL 4.6)"),
}
GL_SCRIPT = """(() => {
  const named = %(named)s;
  const patch = (proto) => {
    proto.getParameter = new Proxy(proto.getParameter, {apply(target, self, args) {
      const value = Reflect.apply(target, self, args);
      if (args[0] === 0x9245 || args[0] === 0x9246) {
        const renderer = args[0] === 0x9246 ? value : Reflect.apply(target, self, [0x9246]);
        if (/SwiftShader|llvmpipe|Software/i.test(String(renderer))) {
          return named[args[0] === 0x9245 ? 0 : 1];
        }
      }
      return value;
    }});
  };
  patch(WebGLRenderingContext.prototype);
  if (self.WebGL2RenderingContext) patch(WebGL2RenderingContext.prototype);
})();"""
# The height of a visible window's toolbar, which a hidden one's `outerHeight` adds to its page.
TOOLBAR = 87
# What makes a hidden browser read as a visible one, run in every page before its own scripts:
# a window taller than its page, no software WebGL name, and a notification permission that
# agrees with `permissions.query`.
HIDDEN_SCRIPT = """(() => {
  const define = (target, name, get) =>
    Object.defineProperty(target, name, {get, configurable: true});
  define(window, 'outerWidth', () => innerWidth);
  define(window, 'outerHeight', () => innerHeight + %(toolbar)d);
  if (!window.chrome) window.chrome = {runtime: {}};
  else if (!window.chrome.runtime) window.chrome.runtime = {};
  const named = %(named)s;
  const patch = (proto) => {
    proto.getParameter = new Proxy(proto.getParameter, {apply(target, self, args) {
      const value = Reflect.apply(target, self, args);
      if (args[0] === 0x9245 || args[0] === 0x9246) {
        const renderer = args[0] === 0x9246 ? value : Reflect.apply(target, self, [0x9246]);
        if (/SwiftShader|llvmpipe|Software/i.test(String(renderer))) {
          return named[args[0] === 0x9245 ? 0 : 1];
        }
      }
      return value;
    }});
  };
  patch(WebGLRenderingContext.prototype);
  if (self.WebGL2RenderingContext) patch(WebGL2RenderingContext.prototype);
  if (self.Notification && navigator.permissions) {
    navigator.permissions.query({name: 'notifications'}).then((status) => {
      if (Notification.permission === 'denied' && status.state === 'prompt') {
        define(Notification, 'permission', () => 'default');
      }
    }).catch(() => {});
  }
})();"""
DEFAULT_VIEWPORT = (1024, 720)
GC_EVERY = 25
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
# The script of `Page.shown`: an element is shown when it is displayed, visible and sized.
SHOWN = """(css) => { const e = document.querySelector(css); if (!e) return false;
  const s = getComputedStyle(e), r = e.getBoundingClientRect();
  return s.display !== "none" && s.visibility !== "hidden" && r.width > 0 && r.height > 0; }"""
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
# The times one cursor may come back before a feed counts as stuck at its end. Once is allowed:
# a page may send one request twice, and both answers name the same next page.
REPEATS = 1
# Seconds between two checks that a person finished a handoff (a captcha).
HANDOFF_POLL = 1.0
# How long a warm tab's first answer may take, in milliseconds: it only has to start loading.
WARM_TIMEOUT = 15_000
# A warm tab keeps the responses of its load, except these, which no source reads.
ASSET_TYPES = frozenset({"image", "media", "font", "stylesheet", "script", "manifest"})
# What a tab does not load unless its plugin reads it (`Plugin.assets`): sites' JSON carries the
# data, and these are the bulk of a page's memory, CPU and bytes.
BLOCKED_TYPES = frozenset({"image", "media", "font"})
# The longest a feed that doesn't scroll waits for the network to go quiet, in seconds.
QUIET = 10.0
# Seconds a scrolling feed goes on with no new answer before it ends as stalled (`Feed.stalled`);
# one scroll step that takes this long ends it too.
STALL = 60.0
# Seconds one read may take: a response's body, the page's HTML, a script's value.
READ = 30.0
# Seconds a tab waits for the browser while another target launches or reopens it.
LAUNCH_WAIT = 300.0

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
CHROMIUM_QUESTION = "Google Chrome isn't installed. Download Chromium instead (about 200 MB)?"
NEEDS_CHROME = "Google Chrome is needed: install it from https://www.google.com/chrome/"
# What a missing browser's `ToolError` says to do.
INSTALL_CHROME = "install Google Chrome, or run in a terminal and accept Chromium's download"
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
# The client-hint data of each browser executable (its brands and platform), the same way.
_HEADFUL_HINTS: dict[str, Mapping[str, Any]] = {}


class _NeverRaised(Exception):
    """Stands for Playwright's error classes where Playwright isn't installed: nothing matches."""


@functools.cache
def _raw_error() -> type[Exception]:
    """Playwright's `Error`, imported once; a class nothing raises without Playwright."""
    try:
        from playwright.async_api import Error
    except ImportError:  # no `browser` extra: no Playwright call can have raised
        return _NeverRaised
    return Error


@functools.cache
def _raw_timeout() -> type[Exception]:
    """Playwright's `TimeoutError`, imported once, as `_raw_error`."""
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
                 stealth: bool = True, screen: Screen | None = None,
                 assets: bool = True) -> Browser:
    """Launch Chrome, or Playwright's Chromium, and return it as a `Browser`. Runs on the loop.

        chrome = await launch()
        tab = await chrome.tab()

    Args:
        profile: the name of an installed Chrome profile ("Default") to open a private copy of,
            for its logins; None opens a fresh incognito browser.
        headless: hide the window. A visible one (the default) lets a person solve a captcha.
        stealth: hide `navigator.webdriver`, which sites read to tell automation apart.
        screen: where the offer to download Chromium is asked and its progress shown; None
            for the plain `Screen`.
        assets: load images, media and fonts; False disables image loading natively.

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
            return await _launch(playwright, copy, headless=headless, stealth=stealth,
                                 screen=screen or Screen(), assets=assets)
        # Any exception, Ctrl-C included, stops Playwright's driver before it goes on up.
        except BaseException:
            await playwright.stop()
            raise
    except BaseException:
        if copy is not None:
            shutil.rmtree(copy, ignore_errors=True)
        raise


async def _start_playwright() -> Playwright:
    """Start Playwright's driver on the running loop."""
    try:
        from playwright.async_api import async_playwright
    except ImportError:
        raise ImportError("flnkit.browser needs playwright: pip install playwright") from None
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
                  stealth: bool, screen: Screen, assets: bool = True) -> Browser:
    """Launch a browser for `launch`, with or without a copied profile."""
    launch_options: dict[str, Any] = {"headless": headless, "args": list(LAUNCH_ARGS)}
    if stealth:
        launch_options["ignore_default_args"] = ["--enable-automation"]
    if not assets:
        launch_options["args"].append("--blink-settings=imagesEnabled=false")
    width, height = DEFAULT_VIEWPORT
    context_options: dict[str, Any] = {"viewport": {"width": width, "height": height}}
    if copy is not None:
        # A copy of Chrome's profile opens only in Chrome, so it never asks to download Chromium.
        channel = PROFILE_CHANNEL
        # `_copy_profile` always stores the copy as `Default`, whatever the profile was called.
        launch_options["args"].append("--profile-directory=Default")
    else:
        # It may ask a person and download Chromium: off the loop, so nothing else stalls.
        channel = await asyncio.to_thread(_channel, playwright, screen)
    context_options["screen"] = dict(SCREEN)
    context, close_steps = await _open_context(
        playwright, copy, channel, launch_options, context_options
    )
    if stealth:
        await context.add_init_script(HIDE_WEBDRIVER)
        await context.add_init_script(gl_script())
    await context.add_init_script(KEEP_VISIBLE)
    override = None
    if headless:
        await context.add_init_script(hidden_script())
        override = await _headful_override(playwright, channel)
    if copy is not None:
        # Removed after Chrome has closed, or Chrome would still be writing into it.
        close_steps.append(lambda: shutil.rmtree(copy, ignore_errors=True))
    return Browser(context, close_steps=close_steps, headless=headless, override=override,
                   assets=assets)


def gl_script() -> str:
    key = sys.platform if sys.platform in SOFTWARE_GL else "linux"
    return GL_SCRIPT % {"named": json.dumps(SOFTWARE_GL[key])}


def hidden_script() -> str:
    """`HIDDEN_SCRIPT` for this platform's WebGL names."""
    key = sys.platform if sys.platform in SOFTWARE_GL else "linux"
    return HIDDEN_SCRIPT % {"toolbar": TOOLBAR, "named": json.dumps(SOFTWARE_GL[key])}


def _channel(playwright: Playwright, screen: Screen) -> str:
    """The channel `launch` uses: `chrome` when Google Chrome is installed, `chromium` otherwise."""
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
        # Without a terminal `confirm` says no at once. A no is kept for the process, so it is
        # asked only once.
        if _chromium_declined or not screen.confirm(CHROMIUM_QUESTION):
            _chromium_declined = True
            raise ToolError(NEEDS_CHROME, hint=INSTALL_CHROME)
        try:
            _install_chromium(screen)
        except ToolError as error:
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


def _install_chromium(screen: Screen) -> None:
    """Download Chromium with Playwright's installer under a progress row into the user cache."""
    # A bundled flnd has no `python -m playwright`, so this runs the driver Playwright ships.
    from playwright._impl._driver import compute_driver_executable, get_driver_env

    node, cli_js = compute_driver_executable()
    lines: list[str] = []
    with screen.progress("Downloading Chromium", total=100) as bar:
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


def _headless_shell_installed(playwright: Playwright) -> bool:
    """Whether Playwright's lighter `chromium-headless-shell` binary is present on disk."""
    exe = Path(playwright.chromium.executable_path)
    for parent in exe.parents:
        if parent.name.startswith("chromium-"):
            rev = parent.name.removeprefix("chromium-")
            shell_dir = parent.parent / f"chromium_headless_shell-{rev}"
            try:
                return shell_dir.is_dir() and any(shell_dir.iterdir())
            except OSError:
                return False
    return False


def _lower_priority() -> None:
    """Lower priority of browser child processes on Windows, macOS and Linux."""
    try:
        import psutil

        children = psutil.Process().children(recursive=True)
    except Exception:
        return
    for proc in children:
        try:
            name = proc.name().lower()
            if not any(part in name for part in BROWSER_PROCS):
                continue
            if sys.platform == "win32":
                below = getattr(psutil, "BELOW_NORMAL_PRIORITY_CLASS", None)
                above = (
                    getattr(psutil, "NORMAL_PRIORITY_CLASS", None),
                    getattr(psutil, "ABOVE_NORMAL_PRIORITY_CLASS", None),
                    getattr(psutil, "HIGH_PRIORITY_CLASS", None),
                    getattr(psutil, "REALTIME_PRIORITY_CLASS", None),
                )
                if below is not None and proc.nice() in above:
                    proc.nice(below)
            elif proc.nice() < POSIX_NICE:
                proc.nice(POSIX_NICE)
        except Exception:
            continue


async def _open_context(playwright: Playwright, profile: Path | None, channel: str,
                        launch_options: dict[str, Any],
                        context_options: dict[str, Any]) -> tuple[BrowserContext, list]:
    """Launch the browser with or without a profile folder, returning `(context, close_steps)`."""
    headless = launch_options["headless"]
    if profile is not None:
        refuse_profile(profile)
    launch_channel: str | None = channel
    if (headless and profile is None and channel == "chromium"
            and _headless_shell_installed(playwright)):
        launch_channel = None
    if profile is None:
        remote = await playwright.chromium.launch(channel=launch_channel, **launch_options)
        await asyncio.to_thread(_lower_priority)
        options = context_options
        if headless:
            options = {**options, "user_agent": await _headful_ua(playwright, channel, remote)}
        context = await remote.new_context(**options)
        return context, [context.close, remote.close, playwright.stop]
    args = launch_options["args"]
    if headless:
        remote = await playwright.chromium.launch(channel=launch_channel, headless=True)
        try:
            agent = await _headful_ua(playwright, channel, remote)
        finally:
            await remote.close()
        args = [*args, f"--user-agent={agent}"]
    context = await playwright.chromium.launch_persistent_context(
        str(profile), channel=launch_channel, **{**launch_options, "args": args}, **context_options)
    await asyncio.to_thread(_lower_priority)
    return context, [context.close, playwright.stop]


def _executable_key(playwright: Playwright, channel: str) -> str | None:
    """What names the browser executable `channel` runs, for the caches above."""
    if channel == "chrome":
        path = _chrome_executable()
        return str(path) if path is not None else None
    return playwright.chromium.executable_path


# Read in a probe page on a secure origin (client hints are given to no other).
_HINTS_SCRIPT = """async () => {
  const data = navigator.userAgentData;
  if (!data) return null;
  const high = await data.getHighEntropyValues(
    ['fullVersionList', 'platformVersion', 'architecture', 'model', 'bitness', 'wow64']);
  return {brands: data.brands, mobile: data.mobile, platform: data.platform, ...high};
}"""
PROBE_URL = "https://probe.invalid/"


def _cache_file() -> Path:
    if os.environ.get(NO_PROFILES):
        return Path(tempfile.gettempdir()) / "flnd-cache" / "headful.json"
    root = os.environ.get("XDG_CACHE_HOME")
    return (Path(root) if root else Path.home() / ".cache") / "flnd" / "headful.json"


def _read_disk_cache(key: str) -> tuple[str, dict[str, Any]] | None:
    try:
        data = json.loads(_cache_file().read_text(encoding="utf-8")).get(key)
        if not data:
            return None
        st = os.stat(key)
        if data.get("mtime_ns") == st.st_mtime_ns and data.get("size") == st.st_size:
            return data["agent"], data["hints"]
    except Exception:
        pass
    return None


def _write_disk_cache(key: str, agent: str, hints: dict[str, Any]) -> None:
    try:
        path = _cache_file()
        data = json.loads(path.read_text(encoding="utf-8")) if path.is_file() else {}
        st = os.stat(key)
        data[key] = {
            "mtime_ns": st.st_mtime_ns, "size": st.st_size, "agent": agent, "hints": hints,
        }
        path.parent.mkdir(parents=True, exist_ok=True)
        from flnkit import files
        with files.part(path) as held:
            held.write(json.dumps(data).encode("utf-8"))
    except Exception:
        pass


async def _headful_override(playwright: Playwright, channel: str) -> dict[str, Any] | None:
    """The `Emulation.setUserAgentOverride` parameters that give a hidden tab the user agent and
    client hints a visible Chrome sends, or None where the browser has no client hints.

    The browser's own values, read once per executable in a throwaway headless browser, minus
    any brand that says "Headless".
    """
    key = _executable_key(playwright, channel)
    if key is not None and key not in _HEADFUL_HINTS and (cached := _read_disk_cache(key)):
        _HEADFUL_UA[key], _HEADFUL_HINTS[key] = cached
    if key is None or key not in _HEADFUL_HINTS:
        remote = await playwright.chromium.launch(channel=channel, headless=True)
        try:
            agent = await _headful_ua(playwright, channel, remote)
            probe = await remote.new_context()
            try:
                page = await probe.new_page()

                async def answer(route: Any) -> None:
                    await route.fulfill(body="<html></html>", content_type="text/html")

                await page.route(PROBE_URL, answer)
                await page.goto(PROBE_URL)
                hints = await page.evaluate(_HINTS_SCRIPT)
            finally:
                await probe.close()
        finally:
            await remote.close()
        if key is None:
            return _override(agent, hints)
        _HEADFUL_HINTS[key] = hints or {}
        _write_disk_cache(key, _HEADFUL_UA.get(key, agent), _HEADFUL_HINTS[key])
    return _override(_HEADFUL_UA[key], _HEADFUL_HINTS[key])


def _override(agent: str, hints: Mapping[str, Any] | None) -> dict[str, Any] | None:
    """The parameters of `Emulation.setUserAgentOverride` for `agent` and the browser's `hints`."""
    if not hints:
        return None

    def listed(brands: Iterable[Mapping[str, str]]) -> list[dict[str, str]]:
        return [{"brand": entry["brand"], "version": entry["version"]} for entry in brands
                if "headless" not in entry["brand"].lower()]

    return {"userAgent": agent, "userAgentMetadata": {
        "brands": listed(hints["brands"]),
        "fullVersionList": listed(hints.get("fullVersionList", hints["brands"])),
        "platform": hints["platform"], "platformVersion": hints.get("platformVersion", ""),
        "architecture": hints.get("architecture", ""), "model": hints.get("model", ""),
        "mobile": bool(hints.get("mobile")), "bitness": hints.get("bitness", ""),
        "wow64": bool(hints.get("wow64"))}}


async def _headful_ua(playwright: Playwright, channel: str, remote: Any) -> str:
    """The user agent `remote` reports, with "HeadlessChrome" replaced by "Chrome", cached."""
    key = _executable_key(playwright, channel)
    if key is not None:
        if key not in _HEADFUL_UA and (cached := _read_disk_cache(key)):
            _HEADFUL_UA[key], _HEADFUL_HINTS[key] = cached
        if key in _HEADFUL_UA:
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
        headless: whether the window is hidden, which a handoff to a person reopens visible.
        override: for a hidden browser, the parameters of `Emulation.setUserAgentOverride` that
            each new tab is given (see `_headful_override`).
        assets: whether images, media and fonts are enabled for this browser.
    """

    def __init__(self, context: BrowserContext, *, close_steps: list[Callable[[], Any]],
                 headless: bool, override: dict[str, Any] | None = None, assets: bool = True):
        self.raw = context
        self.headless = headless
        self.assets = assets
        self._override = override
        # Its tabs, so `RunBrowser.reveal` can carry them to a window that replaces it.
        self.pages: weakref.WeakSet[Page] = weakref.WeakSet()
        self._close_steps = close_steps
        self._closed = False
        self._prioritized = False

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
        raw = await self.raw.new_page()
        if not self._prioritized:
            self._prioritized = True
            await asyncio.to_thread(_lower_priority)
        if self._override is not None:
            # The session stays open: an override lasts only as long as the session that set it.
            session = await self.raw.new_cdp_session(raw)
            await session.send("Emulation.setUserAgentOverride", self._override)
        page = Page(raw, self, by_script=by_script)
        self.pages.add(page)
        return page

    async def cookies(self) -> Jar:
        """Every cookie in the browser, as a `Jar` snapshot."""
        return Jar(dict(cookie) for cookie in await self.raw.cookies())

    async def add_cookies(self, jar: Iterable[Mapping[str, Any]]) -> None:
        """Add each cookie of `jar`, with `path` defaulting to `/`; one it refuses is logged.

        Args:
            jar: the cookies, as a login's `Jar` holds them.

        They go in with one call; when Playwright refuses that, each goes in alone, so a cookie
        it refuses (no domain, a bad expiry) is logged without its value, and the others still
        go in.
        """
        cookies = [{**cookie, "path": cookie.get("path") or "/"} for cookie in jar]
        try:
            await self.raw.add_cookies(cookies)
            return
        except _raw_error():
            pass
        for cookie in cookies:
            try:
                await self.raw.add_cookies([cookie])
            # Playwright's Error is what a cookie it won't take raises; anything else goes up.
            except _raw_error() as error:
                value = str(cookie.get("value", ""))
                message = _first_line(error)
                _log.warning(
                    "cookie {name} was rejected: {error}",
                    extra={"fields": {
                        "name": cookie.get("name", ""),
                        "error": message.replace(value, "…") if value else message,
                    }},
                )

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
            _log.warning(
                "closing the browser failed: {error}",
                extra={"fields": {
                    "error": _first_line(failures[0]),
                }},
            )


async def _block(route: Any) -> None:
    """A route's handler: abort a request of a `BLOCKED_TYPES` kind, let any other go on."""
    if route.request.resource_type in BLOCKED_TYPES:
        await route.abort()
    else:
        await route.continue_()


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
    seeds its own), and `turn`, which runs a `human` scroll step once it is the tab's turn
    (None: at once). `Tabs` sets it from its budget.
    """

    def __init__(self, raw: RawPage, browser: Browser, *, by_script: bool = False):
        self.raw = raw
        self.browser = browser
        self.by_script = by_script
        self.random = random.Random()
        self.turn: Callable[[Callable[[], Awaitable[bool]]], Awaitable[bool]] | None = None
        self._blocking = False
        self._cdp: Any | None = None
        # The captures still listening, which a move to another window gives its new tab again.
        self.captures: set[Capture] = set()

    async def _nudge_memory(self) -> None:
        """Periodically nudge Blink and V8 to free layers, caches, and garbage."""
        try:
            raw = self.raw
            context = getattr(raw, "context", None)
            if context is not None and hasattr(context, "new_cdp_session"):
                cdp = self._cdp or await context.new_cdp_session(raw)
                self._cdp = cdp
                await cdp.send("Memory.simulatePressureNotification", {"level": "critical"})
                await cdp.send("HeapProfiler.collectGarbage")
        except Exception:
            pass

    async def move(self, browser: Browser, url: str) -> None:
        """Carry this tab to a new tab of `browser`: the same `Page`, its captures and its
        blocking kept, the page loaded again from `url` (a failed load is logged).

        Args:
            browser: the browser that replaces the one this tab was in.
            url: the address the old tab was at; "" or a blank page loads nothing.
        """
        fresh = await browser.tab()
        browser.pages.discard(fresh)
        browser.pages.add(self)
        blocking = self._blocking
        self.raw, self.browser, self._blocking, self._cdp = fresh.raw, browser, False, None
        for capture in self.captures:
            self.raw.on("response", capture.listener)
        if blocking:
            await self.assets(False)
        if url and url != "about:blank":
            try:
                await self.raw.goto(url)
            except _raw_error() as error:
                _log.warning("the window was reopened, but its page was not loaded again: {error}",
                             extra={"fields": {"error": _first_line(error)}})

    async def assets(self, load: bool) -> None:
        """Load the images, media and fonts of this tab's pages (`load`), or abort their requests.

        Args:
            load: True to load them; False aborts every `BLOCKED_TYPES` request. The responses
                of the page's other requests are not touched.
        """
        if load == (not self._blocking):
            return
        if load:
            await self.raw.unroute("**/*", _block)
        else:
            await self.raw.route("**/*", _block)
        self._blocking = not load

    @property
    def url(self) -> str:
        """The tab's URL now; Playwright keeps it, so reading it asks the browser nothing."""
        return self.raw.url

    async def goto(self, url: str, *, idle: bool = False,
                   timeout: float = 30.0) -> Visit | None:  # noqa: ASYNC109 (Playwright's own)
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
        except (_raw_error(), TimeoutError) as error:
            if not idle and (isinstance(error, (_raw_timeout(), TimeoutError))
                             or "timeout" in str(error).lower()):
                if (url_now := self.raw.url) not in ("", "about:blank"):
                    with contextlib.suppress(_raw_error(), TimeoutError):
                        await self.raw.wait_for_load_state("domcontentloaded", timeout=5000)
                    return Visit(200, url_now, {})
            clean = redact(url)
            _log.warning(
                "could not open {url}: {error}",
                extra={"fields": {
                    "url": clean,
                    "error": _first_line(error).replace(url, clean),
                }},
            )
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
                   idle: bool = False,
                   timeout: float = 30.0) -> None:  # noqa: ASYNC109 (Playwright's own)
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

    async def shown(self, css: str) -> bool:
        """Whether an element `css` matches is displayed, visible and not zero-sized now."""
        return bool(await self.evaluate(SHOWN, css))

    async def click(self, css: str, *,
                    timeout: float = 10.0) -> None:  # noqa: ASYNC109 (Playwright's own)
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

    async def capture(self, accept: Callable[[Response], bool], *,
                      lazy: bool = False, requests: bool = False) -> Capture:
        """Start keeping the bodies of the responses this tab receives that `accept` takes.

        Args:
            accept: says, on the loop, whether a response is kept; it must not block.
            lazy: keep the responses and read no body until `Capture.close` is told which
                ones it wants (a warm tab's, which a feed may never take).
            requests: record full request metadata (URL, method, headers, post_data) in `requests`.

        Returns:
            The `Capture`, which `Capture.close` stops.
        """
        capture = Capture(self, accept, lazy=lazy, requests=requests)
        self.raw.on("response", capture.listener)
        self.captures.add(capture)
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
            check: raise for a status of 400 or more, as `flnkit.net` does.

        Returns:
            The answer, as `flnkit.net` gives one.

        Raises:
            NetworkError: the page couldn't send it or got no answer within `FETCH_TIMEOUT`.
            SiteError: `check` and a refusal (`Blocked` for a 429, `NotFound` for a 404).
        """
        method = method.upper()
        sent = dict(headers or {})
        if isinstance(body, Mapping) and method in net.QUERY_METHODS:
            url, body = net.with_query(url, body), None
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
        """Run a page read; if the page navigates meanwhile, wait for its load and read again."""
        for attempt in range(tries):
            try:
                return await asyncio.wait_for(read(), READ)
            except TimeoutError:
                raise WaitError(f"the page didn't answer in {READ:g} s: {what}") from None
            except _raw_error() as error:
                if not _gone(error):
                    raise
                if attempt + 1 == tries:
                    _log.warning(
                        "could not {what}, the page kept navigating: {error}",
                        extra={"fields": {
                            "what": what,
                            "error": _first_line(error),
                        }},
                    )
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
        """The visible part of what scrolls, as (x, y, width, height) in the viewport's pixels."""
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
        """Scroll by `distance` pixels in `steps` uneven wheel steps, about `GLIDE` apart."""
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
        """Wait `seconds`, or less when `news` is set meanwhile."""
        if news is None:
            await asyncio.sleep(seconds)
            return
        try:
            await asyncio.wait_for(news.wait(), seconds)
        except TimeoutError:
            pass


class _Scroller:
    """Scrolls a tab one step at a time, driven by what the page does, and says when it ended."""

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
        turn = self._page.turn
        if self._style == HUMAN and turn is not None:
            return await turn(self._step)
        return await self._step()

    async def nudge(self) -> None:
        """Scroll up slightly and pause so a bottom-pinned feed can re-trigger loading."""
        turn = self._page.turn
        if self._style == HUMAN and turn is not None:
            await turn(self._nudge)
            return
        await self._nudge()

    async def _nudge(self) -> None:
        self._quiet_since = None
        area = await self._page._area(self._target)
        await self._page._roll(-area[3] * 0.6, self._target, area, steps=1)
        news = self._watch.news if self._watch is not None else None
        await self._page._nap(min(1.0, self._idle), news)

    async def _step(self) -> bool:
        """`step`'s work, once it is this tab's turn: scroll, watch, and say whether to go on."""
        page, target, human = self._page, self._target, self._style == HUMAN
        heard = self._watch.count if self._watch is not None else 0
        before = self._height
        # 2. Scroll.
        await self._move()
        self.scrolls += 1
        if self.scrolls % GC_EVERY == 0:
            await page._nudge_memory()
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
        """Watch for progress until `deadline`, a time on `flnkit.clock`'s clock."""
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


RATE_HEADERS = frozenset({
    "x-rate-limit-limit", "x-rate-limit-remaining", "x-rate-limit-reset", "retry-after",
})


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

    `items` is a deque: the loop appends and the caller's thread pops, which a deque allows
    without a lock. Everything else runs on the loop. `count` and `news` are what a scroll step
    watches: how many responses have had their turn so far, and an event set each time more
    have.

    A lazy capture keeps the responses only, and reads no body until `close` is told which
    URLs it wants.

    Args:
        page: the tab it listens on.
        accept: whether a response is kept; called on the loop.
        lazy: keep the responses, and read their bodies only at `close`.
        requests: record full request metadata (URL, method, headers, post_data) in `requests`.
    """

    def __init__(self, page: Page, accept: Callable[[Response], bool], *, lazy: bool = False,
                 requests: bool = False):
        self.items: deque[tuple[str, str]] = deque()
        self.requests: deque[tuple[str, str, dict[str, str], str | None] | None] = deque()
        self._metas: deque[tuple[int, dict[str, str]]] = deque()
        self._page = page
        self._accept = accept
        self._requests = requests
        # A lazy capture's responses, in the order they came.
        self._lazy = lazy
        self._kept: list[Response] = []
        self._closed_metas: list[tuple[int, dict[str, str]]] = []
        # Each accepted response gets the next number; `_ready` holds the reads done out of turn.
        self._numbered = 0
        self._flushed = 0
        self._ready: dict[int, tuple[tuple[str, str],
                          tuple[str, str, dict[str, str], str | None] | None,
                          tuple[int, dict[str, str]]] | None] = {}
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
        """Playwright's call for each response the tab gets: number it and start reading it."""
        if self._stopped or not self._accept(response):
            return
        if self._lazy:
            self._kept.append(response)
            return
        number, self._numbered = self._numbered, self._numbered + 1
        task = asyncio.ensure_future(self._read(number, response))
        self._reads.add(task)
        task.add_done_callback(self._reads.discard)

    async def _read(self, number: int, response: Response) -> None:
        """Read one body, then move every body now in turn into `items`."""
        entry: tuple[str, str] | None = None
        raw_status = getattr(response, "status", 200)
        status = raw_status if isinstance(raw_status, int) else 200
        raw_headers = getattr(response, "headers", None) or {}
        headers = (
            {str(k).lower(): str(v) for k, v in raw_headers.items()}
            if status >= 400
            else {low: str(v) for k, v in raw_headers.items()
                  if (low := str(k).lower()) in RATE_HEADERS}
        )
        sent = None
        if self._requests:
            req = getattr(response, "request", None)
            if req is not None and isinstance(getattr(req, "method", None), str):
                sent = (
                    response.url, req.method, dict(getattr(req, "headers", None) or {}),
                    getattr(req, "post_data", None)
                )
        try:
            entry = (response.url, await asyncio.wait_for(response.text(), READ))
        except TimeoutError:
            _log.debug("responses: no body for {url} in {seconds:g} s", extra={"fields": {
                "url": redact(response.url), "seconds": READ}})
        except _raw_error() as error:
            if status == 429 or (status >= 400 and not _gone(error)):
                entry = (response.url, "")
            # Chrome frees a page's response bodies when the page moves on, and a read that comes
            # after that finds nothing: expected, so it is skipped without a line. Any other
            # unreadable body is still worth a debug line.
            elif not _gone(error):
                _log.debug(
                    "responses: no body for {url}: {error}",
                    extra={"fields": {
                        "url": redact(response.url),
                        "error": _first_line(error),
                    }},
                )
        finally:
            self._ready[number] = (entry, sent, (status, headers)) if entry is not None else None
            while self._flushed in self._ready:
                done = self._ready.pop(self._flushed)
                self._flushed += 1
                if done is not None:
                    self.items.append(done[0])
                    if self._requests:
                        self.requests.append(done[1])
                    self._metas.append(done[2])
                self.news.set()

    async def settle(self) -> None:
        """Wait for the bodies still being read, so `items` holds every response heard so far."""
        if self._reads:
            await asyncio.gather(*list(self._reads), return_exceptions=True)

    async def close(self, wanted: Callable[[str], bool] | None = None) -> list[tuple[str, str]]:
        """Stop listening, wait for the reads under way, and empty `items`.

        Args:
            wanted: for a lazy capture, which responses' bodies to read, by URL; None reads
                none of them.

        Returns:
            The `(url, body)` pairs `items` still held, oldest first.
        """
        if not self._stopped:
            self._stopped = True
            self._page.raw.remove_listener("response", self.listener)
            self._page.captures.discard(self)
        kept, self._kept = self._kept, []
        if wanted is not None:
            for response in kept:
                if wanted(response.url):
                    number, self._numbered = self._numbered, self._numbered + 1
                    self._reads.add(asyncio.ensure_future(self._read(number, response)))
        await self.settle()
        left = list(self.items)
        self._closed_metas = list(self._metas)
        self.items.clear()
        self.requests.clear()
        self._metas.clear()
        return left

    async def _close_meta(
        self, wanted: Callable[[str], bool] | None = None
    ) -> list[tuple[str, str, int, dict[str, str]]]:
        """`close`, paired with each response's `(status, headers)`."""
        left = await self.close(wanted)
        metas = self._closed_metas
        return [
            (url, body, *(metas[i] if i < len(metas) else (200, {})))
            for i, (url, body) in enumerate(left)
        ]


# ---------------------------------------------------------------------------------------------
# The run's browser, and one target's tabs
# ---------------------------------------------------------------------------------------------

def run_on(loop: asyncio.AbstractEventLoop, awaitable: Awaitable[Any]) -> Any:
    """Run `awaitable` on `loop` from another thread, wait for it, and return its value.

    The coroutine runs in a copy of the caller's context, so a test's fake clock reaches it. Its
    error, a `KeyboardInterrupt` included, is raised on the caller's thread, never let out of
    the loop. A Ctrl-C while waiting cancels it on the loop before it goes on up.

    Raises:
        RuntimeError: called on `loop`'s own thread, which would wait for itself.
    """
    try:
        here = asyncio.get_running_loop() is loop
    except RuntimeError:
        here = False
    if here:
        if inspect.iscoroutine(awaitable):
            awaitable.close()
        raise RuntimeError("run_on was called on the loop's own thread; await it instead")
    future = asyncio.run_coroutine_threadsafe(_carried(awaitable), loop)
    try:
        ok, value = future.result()
    except BaseException:
        future.cancel()
        raise
    if not ok:
        raise value
    return value


async def _carried(awaitable: Awaitable[Any]) -> tuple[bool, Any]:
    """`(True, value)`, or `(False, error)` for any error but a cancel, for `run_on` to raise."""
    try:
        return True, await awaitable
    except asyncio.CancelledError:
        raise
    except BaseException as error:
        return False, error


class RunBrowser:
    """The run's browsers on one loop, launched the first time a login asks for a tab.

        shared = RunBrowser(asyncio.get_running_loop())   # on the loop
        tab = shared.run(shared.tab(jar))                 # from another thread
        shared.close()

    One window serves every login whose cookies are on other sites than its logins' (two
    sites' logins, or no login at all). A login with cookies on a site a login in a window has
    already (a second account of that site) gets a window of its own, with its own cookies and
    its own idle tab, so two accounts never see each other's cookies. (In flnd there is one
    per run, and its users are the targets.) Its coroutines run on the loop; `run` hands one to
    the loop from another thread.

    Args:
        loop: the asyncio loop the browsers live on, running on its own thread.
        stealth: hide `navigator.webdriver` (see `launch`).
        headless: hide a window when a tab is asked for without saying (see `tab`); a visible
            one (the default) lets a person solve a captcha.
        screen: what asks a person and shows them progress (see `Screen`); None for the plain
            one.
    """

    def __init__(self, loop: asyncio.AbstractEventLoop, *, stealth: bool = True,
                 headless: bool = False, screen: Screen | None = None):
        self.loop = loop
        self.screen = screen or Screen()
        self._stealth = stealth
        self._headless = headless
        # Made on the loop, the first time a tab is asked for: a lock belongs to its loop.
        self._launching: asyncio.Lock | None = None
        # Each window: the logins (jars, by identity) whose cookies it has, the browser, and the
        # one tab a finished target left in it, for the next target to reuse.
        self._logins: list[tuple[list[Any], Browser, list[Page]]] = []

    @property
    def headless(self) -> bool:
        """Whether the windows are hidden; False before a browser is launched."""
        return bool(self._logins) and self._logins[0][1].headless

    @property
    def launched(self) -> bool:
        """Whether a target has asked for a tab yet, so a browser is running."""
        return bool(self._logins)

    @property
    def tabs(self) -> int:
        """How many tabs are open now, warm and idle ones included, in every browser.

        Read on the loop, where the browsers live: Playwright's lists of the contexts' pages.
        """
        return sum(len(browser.raw.pages) for _, browser, _ in self._logins)

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
        return run_on(self.loop, awaitable)

    def _login(self, jar: Any) -> tuple[list[Any], Browser, list[Page]] | None:
        return next((found for found in self._logins
                     if any(seen is jar for seen in found[0])), None)

    async def tab(self, jar: Any, *, assets: bool = True, hidden: bool | None = None) -> Page:
        """A tab for a target whose login is `jar`: its window's idle one, or a new one.

        Args:
            jar: the login's cookies, given to its window the first time they come.
            assets: load images, media and fonts (`Page.assets`, the default); False aborts them.
            hidden: whether the asker works in a hidden window; None takes the `headless` this
                browser was made with. A window opens hidden only for such an asker; an asker
                that is not hidden makes every window visible (`reveal`), so the browser is
                hidden only while all its askers accept it.

        Returns:
            The tab, which scrolls by script.
        """
        async with self._launch():
            wanted = self._headless if hidden is None else hidden
            if not wanted and any(window[1].headless for window in self._logins):
                await self._reveal()
            found = self._login(jar)
            if found is None:
                found = next((window for window in self._logins
                              if not any(_overlap(seen, jar) for seen in window[0])), None)
                if found is None:
                    browser = await launch(stealth=self._stealth, headless=wanted,
                                           screen=self.screen, assets=assets)
                    found = ([], browser, [])
                    self._logins.append(found)
                await found[1].add_cookies(jar)
                found[0].append(jar)
            if assets and not getattr(found[1], "assets", True):
                index = self._logins.index(found)
                await self._reopen(index, headless=found[1].headless, assets=True)
                found = self._logins[index]
            # Opened under the lock, so a reveal can't close the window under the new tab.
            _, browser, idle = found
            page = idle.pop() if idle else await browser.tab(by_script=True)
        await page.assets(assets)
        return page

    @contextlib.asynccontextmanager
    async def _launch(self) -> AsyncIterator[None]:
        """Hold the launch lock, waiting at most `LAUNCH_WAIT` for it.

        Raises:
            NetworkError: another target's launch or reveal held it that long.
        """
        if self._launching is None:
            self._launching = asyncio.Lock()
        try:
            await asyncio.wait_for(self._launching.acquire(), LAUNCH_WAIT)
        except TimeoutError:
            raise NetworkError(f"the browser didn't start in {LAUNCH_WAIT:g} s",
                               retryable=True) from None
        try:
            yield
        finally:
            self._launching.release()

    async def reveal(self) -> None:
        """Close each hidden window and open it again visible, with its cookies and its tabs
        (each loaded again from its URL), so a person can act in it; a visible one stays.

        Later windows open visible too. A tab's page state (a form, a scroll position) is lost;
        a tab another target is using fails the call it is in the middle of.
        """
        async with self._launch():
            await self._reveal()

    async def _reopen(self, index: int, *, headless: bool, assets: bool) -> None:
        """Replace window `index` with a fresh one carrying its cookies and tabs."""
        logins, old, idle = self._logins[index]
        jar = await old.cookies()
        tabs = [(page, page.raw.url) for page in list(old.pages)
                if not page.raw.is_closed()]
        await old.close()
        new = await launch(stealth=self._stealth, headless=headless, screen=self.screen,
                           assets=assets)
        await new.add_cookies(jar)
        for page, url in tabs:
            await page.move(new, url)
        self._logins[index] = (logins, new, idle)

    async def _reveal(self) -> None:
        """`reveal`, with the launch lock already held."""
        self._headless = False
        for index, (_, old, _) in enumerate(self._logins):
            if not old.headless:
                continue
            await self._reopen(index, headless=False, assets=getattr(old, "assets", True))

    async def release(self, pages: Iterable[Page]) -> None:
        """Take back a finished target's tabs: keep one open per window for its next target,
        close the rest.

        Args:
            pages: the tabs; one given twice counts once.
        """
        seen: set[int] = set()
        for page in pages:
            found = next((login for login in self._logins if login[1] is page.browser), None)
            if id(page) in seen or (found is not None and any(page is idle
                                                               for idle in found[2])):
                continue
            seen.add(id(page))
            if found is not None and not found[2]:
                if hasattr(page.raw, "context") and hasattr(page.raw, "goto"):
                    try:
                        if getattr(page.raw, "url", "") != "about:blank":
                            is_closed = getattr(page.raw, "is_closed", None)
                            if is_closed is None or not is_closed():
                                await page.raw.goto("about:blank")
                    except Exception:
                        pass
                found[2].append(page)
                continue
            try:
                await page.close()
            # A tab that crashed or closed already needs no closing.
            except Exception:
                pass

    async def cookies(self, jar: Any) -> Jar:
        """The cookies of the window of the login `jar`; none before it has one."""
        found = self._login(jar)
        return Jar([]) if found is None else await found[1].cookies()

    def close(self) -> None:
        """Close the browsers, from a thread other than the loop's; a second call does
        nothing."""
        logins, self._logins = self._logins, []
        for _, browser, _ in logins:
            self.run(browser.close())

    async def aclose(self) -> None:
        """Close the browsers, on the loop; a second call does nothing."""
        logins, self._logins = self._logins, []
        for _, browser, _ in logins:
            await browser.close()


def _overlap(one: Iterable[Mapping[str, Any]], other: Iterable[Mapping[str, Any]]) -> bool:
    """Whether two logins have cookies on one site: a domain of one is, or is under, the other's."""
    hosts = {str(cookie.get("domain", "")).lstrip(".").lower() for cookie in one}
    for cookie in other:
        host = str(cookie.get("domain", "")).lstrip(".").lower()
        if any(host == seen or host.endswith("." + seen) or seen.endswith("." + host)
               for seen in hosts):
            return True
    return False


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
    background tabs while the budget says one may open, keeping the responses they get;
    `take_warm` then switches to such a tab, and a feed that is open moves to it with those
    responses.

    Every Playwright call goes through `run`, one hop to the loop each.

    Args:
        shared: the run's browser.
        jar: the source's login cookies.
        scroll: how its feeds scroll: `HUMAN` (the default) or `FAST` (see `_Scroller`).
        budget: the caller's tabs: each tab held takes a place (`await take()`, `give()`),
            `warm` says whether a warm tab may open now, and `turn(step)` runs a `human` scroll
            step in its turn. None limits nothing and opens no warm tab.
        assets: load images, media and fonts (the default); False aborts their requests in its tabs.
        hidden: its tabs may be in a hidden window (see `RunBrowser.tab`).

    Raises:
        ValueError: `scroll` is neither.
    """

    def __init__(self, shared: RunBrowser, jar: Any, *, scroll: str = HUMAN,
                 budget: Any = None, assets: bool = True, hidden: bool = False):
        if scroll not in SCROLLS:
            raise ValueError(f"scroll must be one of {', '.join(SCROLLS)}, not {scroll!r}")
        self._shared = shared
        self._jar = jar
        self._assets = assets
        self._hidden = hidden
        self.scroll = scroll
        self._budget = budget
        # The places taken from the budget: one per tab the target holds.
        self._held = 0
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
            self._page = self._idle.popleft() if self._idle else await self._take()
        return self._page

    def handoff(self, *, until: Callable[[], bool], message: str, timeout: float | None,
                stop: _threading.Event | None) -> bool:
        """Show `message` and wait for a person to finish a step in the window; see `Site.handoff`.

        A hidden browser is closed and reopened visible first (`RunBrowser.reveal`).

        Args:
            until: True once the step is done; asked on this thread about once a second.
            message: what the person should do.
            timeout: seconds before giving up; None waits as long as it takes.
            stop: the run's stop event, which ends the wait.

        Returns:
            True when `until` came true; False when `timeout` passed first.

        Raises:
            Stopped: the run was told to stop.
        """
        if until():
            return True
        if self.headless:
            self.run(self._shared.reveal())
        _log.warning(message)
        deadline = None if timeout is None else clock.now() + timeout
        with self._shared.screen.progress(message) as bar:
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
        return self.run(self._take())

    async def _take(self) -> Page:
        """A tab from the run's browser, once the budget has a place for it."""
        if self._budget is not None:
            await self._budget.take()
            self._held += 1
        return await self._opened()

    async def _opened(self) -> Page:
        """A tab from the run's browser, its budget's place taken already; given back when the
        tab doesn't open."""
        budget = self._budget
        try:
            page = await self._shared.tab(self._jar, assets=self._assets, hidden=self._hidden)
        except BaseException:
            if budget is not None:
                self._held -= 1
                budget.give()
            raise
        page.turn = budget.turn if budget is not None else None
        return page

    def _recycle(self, tab: Page) -> None:
        if tab not in self._idle:
            self._idle.append(tab)

    def _switch(self, tab: Page) -> None:
        """Make `tab` the target's, recycle the old one, and move the innermost feed to it."""
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
        """Start loading `urls` in background tabs, while the budget allows; queue the rest.

        Args:
            urls: pages the target will open next, in order.
        """
        for url in urls:
            if url not in self._warmed and url not in self._pending:
                self._pending.append(url)
        self._fill()

    def _fill(self) -> None:
        """Start loading queued URLs in idle or new tabs, while the budget says one may open."""
        _ = self.page
        if self._pending:
            self.run(self._filling())

    async def _filling(self) -> None:
        """`_fill` on the loop: the budget's places are taken in turn, then every tab opens and
        starts its load at once.

        Raises:
            Exception: a tab couldn't be opened (the browser didn't launch); the others opened.
        """
        starts: list[tuple[str, Page | None]] = []
        while self._pending:
            if not self._idle and (self._budget is None or not self._budget.warm):
                break
            url = self._pending.popleft()
            if url in self._warmed or any(url == started for started, _ in starts):
                continue
            if self._idle:
                starts.append((url, self._idle.popleft()))
                continue
            if self._budget is not None:
                await self._budget.take()
                self._held += 1
            starts.append((url, None))
        opened = await asyncio.gather(*(self._start_warm(url, tab) for url, tab in starts),
                                      return_exceptions=True)
        failed: BaseException | None = None
        for (url, _), result in zip(starts, opened):
            if isinstance(result, BaseException):
                failed = failed or result
                continue
            tab, visit = result
            if visit is _NOT_STARTED:
                self._recycle(tab)
            else:
                self._warmed[url] = (tab, visit)
        if failed is not None:
            raise failed

    async def _start_warm(self, url: str, tab: Page | None) -> tuple[Page, Any]:
        """Start loading `url` in `tab` (a new one when None), keeping its responses unread."""
        if tab is None:
            tab = await self._opened()
        await self._closed_buffer(tab)
        self._buffers[tab] = await tab.capture(_not_an_asset, lazy=True)
        try:
            return tab, await tab.commit(url)
        # Any failure only means no warm tab: `Site.open` loads the page as usual.
        except Exception:
            await self._closed_buffer(tab)
            return tab, _NOT_STARTED

    async def _closed_buffer(self, tab: Page) -> None:
        capture = self._buffers.pop(tab, None)
        if capture is not None:
            await capture.close()

    def take_buffer(self, tab: Page,
                    wanted: Callable[[str], bool] | None = None) -> list[tuple[str, str]]:
        """Stop keeping `tab`'s responses, and return the `(url, body)` it kept.

        Args:
            tab: the tab.
            wanted: which kept responses' bodies a warm tab reads now, by URL: an open feed's;
                None reads none.
        """
        capture = self._buffers.pop(tab, None)
        return [] if capture is None else self.run(capture.close(wanted))

    def _take_buffer_meta(
        self, tab: Page, wanted: Callable[[str], bool] | None = None
    ) -> list[tuple[str, str, int, dict[str, str]]]:
        """`take_buffer`, with each response's `(status, headers)` preserved."""
        capture = self._buffers.pop(tab, None)
        return [] if capture is None else self.run(capture._close_meta(wanted))

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
            for _ in range(self._held):
                self._budget.give()
            self._held = 0
            self._page = None
            self._stack.clear()
            self._idle.clear()
            self._warmed.clear()
            self._pending.clear()
            self._feeds.clear()
            self._buffers.clear()


# What `Tabs._start_warm` gives for a warm tab whose load failed at once.
_NOT_STARTED = object()


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
      (`WAIT`, then `IDLE`);
    - or no new answer came for `STALL` seconds of scrolling, or one step took that long: it
      ends `stalled`.
    Without `scroll`, it waits once for the network to go quiet (up to `QUIET`), yields what
    came, and ends. Leaving `with`, or the end of the iteration, closes it.

    Made on the caller's thread, used there only; the cursor reader runs there too.

    Args:
        tabs: the target's tabs.
        match: which responses: a substring of the URL, a regex searched in it, or a predicate.
        scroll: scroll for more when none is waiting.
        lines: a body holds one JSON value per line (NDJSON), not one in all.
        cursor: reads each parsed value (each line, with `lines`), as `Reader` says; None never
            ends the feed on the site's word.
        requests: record full request metadata on captured responses.
    """

    def __init__(self, tabs: Tabs, match: Match, *, scroll: bool = False, lines: bool = False,
                 cursor: Reader | None = None, requests: bool = False):
        self._tabs = tabs
        self._matches = matcher(match)
        self._scroll = scroll
        self._lines = lines
        self._cursors = Cursors(cursor)
        self._closed = False
        self._requests = requests
        self._raw: deque[tuple[str, str, tuple[str, str, dict[str, str], str | None] | None,
                               int, Mapping[str, str]]] = deque()
        self._waiting: deque[tuple[Any, tuple[str, str, dict[str, str], str | None] | None]] = (
            deque()
        )
        self.request: tuple[str, str, dict[str, str], str | None] | None = None
        # Whether it ended because no new answer came for `STALL` seconds.
        self.stalled = False
        self.headers: Mapping[str, str] = {}
        self._got_json = False
        self._last_error: Exception | None = None
        self._non_json_url: str | None = None
        tabs.push(self, scroll=scroll)
        self._page = tabs.page
        self._capture: Capture = tabs.run(self._page.capture(self._accepts, requests=requests))
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
        self._waiting.clear()
        self._raw.clear()
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
        for url, body, status, headers in self._tabs.run(self._capture._close_meta()):
            self._raw.append((url, body, None, status, headers))
        self._page = page
        for url, body, status, headers in self._tabs._take_buffer_meta(page, self._matches):
            self._raw.append((url, body, None, status, headers))
        self._capture = self._tabs.run(page.capture(self._accepts, requests=self._requests))
        self._scroller = None

    def _iterate(self) -> Iterator[Any]:
        """The bodies, scrolling or waiting once for more as the class's docstring says."""
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
                    self._check_end()
                    return
                if not self._scroll_until_one():
                    self._check_end()
                    return
        finally:
            self.close()

    def _scroll_until_one(self) -> bool:
        """Scroll step by step, one call to the loop each, until a body is waiting, for at
        most `STALL` seconds."""
        began = clock.now()
        while not self._fill():
            if self._cursors.ended:
                break
            if self._scroller is None:
                self._scroller = self._tabs.run(self._page.scroller(
                    style=self._tabs.scroll, watch=self._capture))
            left = STALL - (clock.now() - began)
            if left <= 0:
                self.stalled = True
                break
            try:
                went = self._tabs.run(asyncio.wait_for(self._scroller.step(), left))
            except (TimeoutError, WaitError):
                went = None
            if went is False:
                if not self._cursors.active or self._cursors.ended:
                    break
                if clock.now() - began >= STALL:
                    self.stalled = True
                    break
                try:
                    self._tabs.run(asyncio.wait_for(
                        self._scroller.nudge(), max(0.1, STALL - (clock.now() - began))))
                except (TimeoutError, WaitError):
                    self.stalled = True
                    break
                continue
            if went is None or clock.now() - began >= STALL:
                self.stalled = True
                break
        else:
            return True
        # The end: the site said so, the page went quiet, or it stalled. A body still being
        # read is waited for, since it may be the last page.
        self._tabs.run(self._capture.settle())
        if self._fill():
            self.stalled = False
            return True
        return False

    def _arrived(self) -> Iterator[Any]:
        self._fill()
        while self._waiting or self._raw:
            if not self._waiting:
                self._parse(*self._raw.popleft())
            while self._waiting:
                value, self.request, self.headers = self._waiting.popleft()
                if isinstance(value, Blocked):
                    raise value
                yield value

    def _fill(self) -> bool:
        """Move arrived bodies from the capture into `_raw`; whether any is waiting."""
        items, requests = self._capture.items, self._capture.requests
        metas = self._capture._metas
        while items:
            req = requests.popleft() if requests else None
            status, headers = metas.popleft() if metas else (200, {})
            self._raw.append((*items.popleft(), req, status, headers))
        return bool(self._waiting or self._raw)

    def _parse(self, url: str, body: str,
               request: tuple[str, str, dict[str, str], str | None] | None = None,
               status: int = 200, headers: Mapping[str, str] | None = None) -> None:
        """Parse `body`, or each line of it with `lines`, into `_waiting`, reading its cursor."""
        kept_headers = headers or {}
        if status == 429:
            method = request[1] if request is not None else "GET"
            error = net.refusal(net.Response(status, url, kept_headers, body.encode()),
                                method=method)
            assert error is not None
            self._waiting.append((error, request, kept_headers))
            return
        pieces = [line for line in body.splitlines() if line.strip()] if self._lines else [body]
        decoded = False
        for piece in pieces:
            try:
                value = msgspec.json.decode(piece)
            # A piece that isn't JSON (an HTML error page, say) is skipped; the others still count.
            except (ValueError, msgspec.DecodeError):
                redacted = redact(url)
                _log.debug(
                    "responses: skipped a body that is not JSON (HTTP {status}, {bytes} B) "
                    "from {url}",
                    extra={"fields": {
                        "url": redacted,
                        "status": status,
                        "bytes": len(piece),
                    }},
                )
                self._non_json_url = redacted
                continue
            decoded = True
            self._got_json = True
            self._last_error = None
            self._waiting.append((value, request, kept_headers))
            self._cursors.read(value)
        if not decoded:
            if status >= 400:
                method = request[1] if request is not None else "GET"
                self._last_error = net.refusal(
                    net.Response(status, url, headers or {}, body.encode()),
                    method=method,
                )
            elif not pieces:
                self._non_json_url = redact(url)

    def _check_end(self) -> None:
        """Raise when the feed ended without a final cursor after HTTP or non-JSON errors."""
        if self._cursors.ended:
            return
        if self._last_error is not None:
            raise self._last_error
        if not self._got_json and self._non_json_url is not None:
            raise Changed(f"responses from {self._non_json_url} were not JSON")


class Cursors:
    """What a feed's pages said about the next one, and whether the site said there is none.

        cursors = Cursors(lambda page: (page["more"], page.get("next")))
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
        self.active = False
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
        self.active = True
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

    For a login that reads a web app's saved session (Telegram's). It runs outside any run, on a
    thread with no loop of its own, so it starts a loop and closes it, with the browser and the
    profile copy, before returning.

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
    return asyncio.run(_local_storage(url, profile))


async def _local_storage(url: str, profile: str) -> dict[str, str] | None:
    """`local_storage`'s work, on its loop."""
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

def _without_values(text: str, headers: Mapping[str, str]) -> str:
    """`text` with every header value in `headers` replaced by `***`, hiding tokens."""
    for value in headers.values():
        if value:
            text = text.replace(value, "***")
    return text


def _first_line(error: BaseException) -> str:
    return (str(error).splitlines() or [""])[0]


def matcher(match: Match) -> Callable[[str], bool]:
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

