"""`flnkit.browser`: `launch`, the profile copy, the Chromium offer, `Page`, `Capture`, scrolling.

Every coroutine runs on one `flnkit.loop.Loop` for the module (`run`). Profile and Chromium tests
fake Playwright, the home and the temp folder. Page tests run Playwright's own headless Chromium
on pages served from memory (`fulfill`), or from 127.0.0.1 for `Page.fetch`, and skip without it.
"""

import asyncio
import contextlib
import importlib
import json
import os
import random
import re
import shutil
import stat
import sys
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from io import StringIO
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from unittest import mock

import no_profiles  # noqa: F401  (refuses real browser profiles)

from flnd.errors import Blocked, Changed, Error, NetworkError, SiteError, ToolError
from flnd.ui.log import ui
from flnkit import browser as _browser
from flnkit import clock as _time
from flnkit import cookies as _cookies
from flnkit import net as _net
from flnkit.loop import Loop

# The real lookup, before BrowserCase patches it out.
CHROME_INSTALLED = _browser._chrome_installed
LOOP = Loop()
run = LOOP.run


# The one browser the page tests share, launched by the first of them.
SHARED: list[_browser.Browser] = []


def tearDownModule():
    for browser in SHARED:
        run(browser.close())
    LOOP.close()


def chromium_installed() -> bool:
    """Whether Playwright's Chromium is downloaded, checked without a launch that could fetch it."""
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        return False
    with sync_playwright() as playwright:
        return Path(playwright.chromium.executable_path).is_file()


HAS_CHROMIUM = chromium_installed()
needs_chromium = unittest.skipUnless(HAS_CHROMIUM, "Chromium is not installed")
PLATFORM = sys.platform if sys.platform in ("darwin", "win32") else "linux"

# Items are 300px tall and come five at a time when the page is scrolled to its end.
FEED_PAGE = """<!doctype html><html><body style="margin:0"><div id="feed"></div><script>
  const cap = Number(new URLSearchParams(location.search).get("cap") || 40);
  const feed = document.getElementById("feed");
  window.wheels = [];
  function add(n) {
    for (let i = 0; i < n && feed.children.length < cap; i++) {
      const item = document.createElement("div");
      item.className = "item";
      item.style.height = "300px";
      feed.appendChild(item);
    }
  }
  add(5);
  addEventListener("wheel", event => wheels.push(Math.round(event.deltaY)));
  addEventListener("scroll", () => {
    if (scrollY + innerHeight >= document.body.scrollHeight - 10) add(5);
  });
</script></body></html>"""

# The same feed inside a 500px box that scrolls on its own; the page itself never scrolls.
BOX_PAGE = """<!doctype html><html><body style="margin:0">
<div id="box" style="height:500px;width:600px;overflow-y:auto;margin-top:100px"></div><script>
  const box = document.getElementById("box");
  function add(n) {
    for (let i = 0; i < n && box.children.length < 20; i++) {
      const item = document.createElement("div");
      item.className = "item";
      item.style.height = "200px";
      box.appendChild(item);
    }
  }
  add(5);
  box.addEventListener("scroll", () => {
    if (box.scrollTop + box.clientHeight >= box.scrollHeight - 10) add(5);
  });
</script></body></html>"""

HOME = "<html><head><title>Home</title></head><body>home</body></html>"
ROUTES = {
    "/feed": (200, "text/html", FEED_PAGE), "/box": (200, "text/html", BOX_PAGE),
    "/missing": (404, "text/html", "<p>not here</p>"),
    "/api/graphql": (200, "application/json", json.dumps({"data": {"n": 1}})),
    "/api/other": (200, "application/json", json.dumps({"other": True})),
}


async def fulfill(route):
    """Serves https://site.test/ from memory and aborts https://bad.test/."""
    url = route.request.url
    if url.startswith("https://bad.test/"):
        return await route.abort()
    path = url.removeprefix("https://site.test").split("?")[0]
    status, content_type, body = ROUTES.get(path, (200, "text/html", HOME))
    await route.fulfill(status=status, content_type=content_type, body=body)


class BrowserCase(unittest.TestCase):
    def setUp(self):
        self.dest = StringIO()
        self.enterContext(ui.scope(dest=self.dest, color=False, width=300))
        previous = ui.level
        self.addCleanup(setattr, ui, "level", previous)
        # The bundled build whether or not Chrome is installed, with no earlier test's answer to
        # the Chromium download remembered.
        self.enterContext(mock.patch.object(_browser, "_chrome_installed", return_value=False))
        self.enterContext(mock.patch.object(_browser, "_chromium_declined", False))
        self.enterContext(mock.patch.object(_browser, "_chromium_failed", None))

    def lines(self) -> list[str]:
        return self.dest.getvalue().splitlines()

    def patch_launch(self, start, open_context):
        self.enterContext(mock.patch.object(_browser, "_start_playwright", start))
        self.enterContext(mock.patch.object(_browser, "_open_context", open_context))


class FakePlaywright:
    """Playwright's driver as `launch` uses it before `_open_context`: it only stops."""

    def __init__(self, executable="/fake/chromium"):
        self.chromium = SimpleNamespace(executable_path=executable)
        self.stops = 0

    async def stop(self):
        self.stops += 1


class FakeContext:
    """A `BrowserContext` that keeps cookies, and rejects one without a domain, quoting it whole."""

    def __init__(self):
        self.stored = []

    async def add_init_script(self, script):
        pass

    async def add_cookies(self, cookies):
        for cookie in cookies:
            if "domain" not in cookie:
                raise _browser._raw_error()(f"Invalid cookie {cookie}")
            self.stored.append(cookie)

    async def cookies(self):
        return [dict(cookie) for cookie in self.stored]


def starts(playwright):
    async def start():
        return playwright

    return start


class ProfileCopyTest(BrowserCase):
    """`launch(profile=<name>)`: the copy, which holds the logins, never stays in the
    temp folder."""

    def setUp(self):
        super().setUp()
        folder = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.home, self.temp = folder / "home", folder / "temp"
        root = self.home / _browser.PROFILE_ROOTS[PLATFORM]
        (root / "Default").mkdir(parents=True)
        (root / "Default" / "Preferences").write_text("{}")
        self.temp.mkdir()
        self.enterContext(mock.patch.object(Path, "home", return_value=self.home))
        self.enterContext(mock.patch.object(tempfile, "tempdir", str(self.temp)))

    def copies(self) -> list[Path]:
        return list(self.temp.glob("flnd-chrome-*"))

    def test_without_playwright_launch_names_its_install(self):
        with mock.patch.dict(sys.modules, {"playwright": None, "playwright.async_api": None}):
            sys.modules.pop("flnkit.browser", None)
            try:
                fresh = importlib.import_module("flnkit.browser")
                with self.assertRaisesRegex(ImportError, re.escape("pip install playwright")):
                    run(fresh.launch())
            finally:
                sys.modules["flnkit.browser"] = _browser

    def test_a_name_is_copied_privately_into_chrome_and_deleted_on_close(self):
        launches = []

        async def open_context(playwright, profile, channel, launch_options, context_options):
            profile = Path(profile)
            launches.append((profile, channel, launch_options["args"][-1],
                             stat.S_IMODE(profile.stat().st_mode),
                             (profile / "Default" / "Preferences").is_file()))
            return FakeContext(), [playwright.stop]

        self.patch_launch(starts(FakePlaywright()), open_context)
        chrome = run(_browser.launch(profile="Default"))
        (copy, channel, last_arg, mode, has_preferences), = launches
        self.assertEqual((channel, last_arg, has_preferences),
                         ("chrome", "--profile-directory=Default", True))
        self.assertTrue(copy.is_relative_to(self.temp))
        if sys.platform != "win32":
            self.assertEqual(mode, 0o700)  # no other user of the machine can read its logins
        self.assertEqual(self.copies(), [copy])
        run(chrome.close())
        self.assertEqual(self.copies(), [])

    def test_a_bad_or_unknown_name_raises_before_playwright_starts(self):
        with mock.patch.object(_browser, "_start_playwright") as start:
            for profile in ("", "./Default", "profiles/Default", "/tmp/profile", ".", ".."):
                with self.subTest(profile=profile), self.assertRaises(ValueError):
                    run(_browser.launch(profile=profile))
            with self.assertRaisesRegex(FileNotFoundError, "no Chrome profile named 'Profile 9'"):
                run(_browser.launch(profile="Profile 9"))
        start.assert_not_called()
        self.assertEqual(self.copies(), [])

    def test_the_copy_is_deleted_whatever_fails(self):
        def raising(error):
            async def fail(*args):
                raise error

            return fail

        errors = [("start", ImportError("no playwright")), ("start", KeyboardInterrupt()),
                  ("open", KeyboardInterrupt()), ("open", RuntimeError(_browser.NEEDS_CHROME)),
                  ("copy", OSError("disk full")), ("copy", KeyboardInterrupt())]
        for where, error in errors:
            with self.subTest(where=where, error=error), contextlib.ExitStack() as stack:
                playwright = FakePlaywright()
                stack.enter_context(mock.patch.object(
                    _browser, "_start_playwright",
                    raising(error) if where == "start" else starts(playwright)))
                stack.enter_context(mock.patch.object(_browser, "_open_context", raising(error)))
                if where == "copy":
                    copytree = stack.enter_context(
                        mock.patch.object(shutil, "copytree", side_effect=error))
                with self.assertRaises(type(error)):
                    run(_browser.launch(profile="Default"))
                self.assertEqual(self.copies(), [])
                if where == "open":
                    self.assertEqual(playwright.stops, 1)
                if where == "copy":
                    # Started inside the private temp folder, so the empty glob means deleted.
                    self.assertTrue(Path(copytree.call_args.args[1]).is_relative_to(self.temp))


class FakeInstaller:
    """A finished `subprocess.Popen` of Playwright's installer, with its output and exit code."""

    def __init__(self, output: str, returncode: int):
        self.stdout, self.returncode = StringIO(output), returncode

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        return None


def drawn(screen, label, *, total=None):
    """A `Screen.progress` as flnd's (`flnd.engine.site`): a line with the label, and no bar."""
    ui.info("{label}", label=label)
    return contextlib.nullcontext(_browser._Bar())


class ChromeOnlyTest(BrowserCase):
    """Which browser launch starts, and the Chromium download offered without Chrome."""

    def setUp(self):
        super().setUp()
        folder = Path(self.enterContext(tempfile.TemporaryDirectory()))
        # Where Playwright would keep its Chromium: nothing there until the fake installer runs.
        self.chromium = folder / "chromium-1" / "chrome"
        self.playwright = FakePlaywright(str(self.chromium))
        self.installs, self.launched = [], []

        async def open_context(playwright, profile, channel, launch_options, context_options):
            self.launched.append(channel)
            return FakeContext(), [playwright.stop]

        self.patch_launch(starts(self.playwright), open_context)
        self.enterContext(mock.patch.object(_browser.subprocess, "Popen", self.popen))
        # The rows show as flnd's screen shows them (`flnd.engine.site`).
        self.enterContext(mock.patch.object(_browser.Screen, "progress", drawn))
        self.enterContext(mock.patch("playwright._impl._driver.compute_driver_executable",
                                     return_value=("/driver/node", "/driver/cli.js")))

    def popen(self, argv, **options):
        """Playwright's installer: it prints progress lines, then the browser is there."""
        self.installs.append((argv, options))
        self.place()
        return FakeInstaller("|■■■■      |  40% of 167.3 MiB\n|■■■■■■■■■■| 100% of 167.3 MiB\n"
                             "Chromium downloaded\n", 0)

    def place(self):
        self.chromium.parent.mkdir(parents=True)
        self.chromium.write_text("")

    def answer(self, *keys):
        answers = iter(keys)
        self.enterContext(mock.patch.object(_browser.Screen, "confirm",
                                            side_effect=lambda *a: next(answers) == "y"))

    def launch_and_close(self):
        async def go():
            await (await _browser.launch()).close()

        run(go())

    def test_chrome_or_a_downloaded_chromium_is_used_without_asking(self):
        self.answer()  # any question would read a key, and there is none
        with mock.patch.object(_browser, "_chrome_installed", return_value=True):
            self.launch_and_close()
        self.place()
        self.launch_and_close()
        self.assertEqual((self.launched, self.installs), (["chrome", "chromium"], []))

    def test_yes_downloads_chromium_then_launches_it(self):
        self.answer("y")
        self.launch_and_close()
        self.assertEqual(self.launched, ["chromium"])
        (argv, options), = self.installs
        self.assertEqual(argv, ["/driver/node", "/driver/cli.js", "install", "--no-shell",
                                "chromium"])
        self.assertEqual(options["env"]["PW_LANG_NAME"], "python")
        # Into Playwright's user cache: the installer's folder is left to Playwright.
        added = {key for key in options["env"] if key not in os.environ}
        self.assertNotIn("PLAYWRIGHT_BROWSERS_PATH", added)
        self.assertIn("Downloading Chromium", self.dest.getvalue())
        # A later run finds it and doesn't ask: there is no key left to answer with.
        self.launch_and_close()
        self.assertEqual((self.launched, len(self.installs)), (["chromium", "chromium"], 1))

    def test_no_or_no_terminal_raises_naming_chrome_and_asks_once(self):
        with mock.patch.object(_browser.Screen, "confirm", side_effect=[False]):
            for _ in range(2):
                with self.assertRaises(ToolError) as caught:
                    run(_browser.launch())
                self.assertEqual(str(caught.exception), _browser.NEEDS_CHROME)
                self.assertEqual(caught.exception.hint, _browser.INSTALL_CHROME)
        self.assertEqual(self.playwright.stops, 2)
        self.dest.seek(0)
        self.dest.truncate()
        with mock.patch.object(_browser, "_chromium_declined", False), \
                mock.patch.object(sys.stdin, "isatty", return_value=False), \
                self.assertRaisesRegex(ToolError, "^Google Chrome is needed: install it"):
            run(_browser.launch())
        self.assertNotIn(_browser.CHROMIUM_QUESTION, self.dest.getvalue())
        self.assertEqual((self.launched, self.installs), ([], []))

    def test_a_failed_download_is_raised_and_raised_again_without_asking(self):
        self.answer("y")
        failed = FakeInstaller("Error: ENOSPC\n", 1)
        raised = []
        with mock.patch.object(_browser.subprocess, "Popen", return_value=failed) as popen:
            for _ in range(2):
                with self.assertRaisesRegex(ToolError, "exited with 1: Error: ENOSPC") as caught:
                    run(_browser.launch())
                raised.append(caught.exception)
        self.assertIs(raised[0], raised[1])
        self.assertEqual((popen.call_count, self.launched), (1, []))
        self.assertIn("Downloading Chromium", self.dest.getvalue())

    def test_two_threads_at_once_ask_once_and_download_once(self):
        asked, installs, channels = [], [], []
        start = threading.Barrier(2)

        def confirm(*args):
            asked.append(args)
            time.sleep(0.05)  # time for the other thread to reach the question, were it free to
            return True

        def install(screen):
            installs.append(1)
            self.place()

        def open_one():
            start.wait()
            channels.append(_browser._channel(self.playwright, _browser.Screen()))

        with mock.patch.object(_browser.Screen, "confirm", confirm), \
                mock.patch.object(_browser, "_install_chromium", install):
            threads = [threading.Thread(target=open_one) for _ in range(2)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()
        self.assertEqual((len(asked), len(installs), channels), (1, 1, ["chromium", "chromium"]))

    def test_chrome_is_looked_for_where_playwright_looks(self):
        found = []
        windows = {"LOCALAPPDATA": "C:/Users/a/AppData/Local", "PROGRAMFILES": "C:/PF"}
        with mock.patch.object(Path, "is_file", lambda path: found.append(str(path)) or False):
            for platform, env in (("darwin", {}), ("linux", {}), ("win32", windows)):
                with mock.patch.object(_browser.sys, "platform", platform), \
                        mock.patch.dict(os.environ, env, clear=True):
                    self.assertFalse(CHROME_INSTALLED())
        self.assertEqual([Path(path) for path in found], [
            Path("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"),
            Path("/opt/google/chrome/chrome"),
            Path("C:/Users/a/AppData/Local/Google/Chrome/Application/chrome.exe"),
            Path("C:/PF/Google/Chrome/Application/chrome.exe")])

    def test_the_headed_name_is_measured_once_per_executable(self):
        _browser._HEADFUL_UA.clear()
        self.addCleanup(_browser._HEADFUL_UA.clear)
        probes = []

        class Remote:
            async def new_context(self):
                probes.append(1)
                page = SimpleNamespace(
                    evaluate=mock.AsyncMock(return_value="Mozilla HeadlessChrome/120"))
                return SimpleNamespace(new_page=mock.AsyncMock(return_value=page),
                                       close=mock.AsyncMock())

        playwright = cast(Any, FakePlaywright("/fake/chrome/bin"))
        names = [run(_browser._headful_ua(playwright, "chromium", Remote())) for _ in range(2)]
        self.assertEqual((names, len(probes)), (["Mozilla Chrome/120"] * 2, 1))


@needs_chromium
class LaunchTest(BrowserCase):
    def test_incognito_stealth_and_close_after_an_exception(self):
        async def first(seen):
            async with await _browser.launch(headless=True) as chrome:
                seen.append(chrome.raw.browser)
                await chrome.add_cookies([{"name": "sid", "value": "1", "domain": "site.test",
                                           "expires": time.time() + 3600}])
                seen.append(len(await chrome.cookies()))
                tab = await chrome.tab()
                seen.append(await tab.evaluate("[navigator.userAgent, navigator.webdriver]"))
                raise ValueError("boom")

        async def second():
            async with await _browser.launch(headless=True, stealth=False) as chrome:
                tab = await chrome.tab()
                return len(await chrome.cookies()), await tab.evaluate("navigator.webdriver")

        seen = []
        with self.assertRaises(ValueError):
            run(first(seen))
        browser, cookies, (agent, webdriver) = seen
        self.assertFalse(browser.is_connected())
        self.assertEqual(cookies, 1)
        self.assertIn("Chrome/", agent)
        self.assertNotIn("Headless", agent)
        self.assertIsNone(webdriver)
        # Incognito: the next browser starts with no cookies; without stealth `webdriver` shows.
        self.assertEqual(run(second()), (0, False))

    def test_an_installed_profile_is_copied_and_deleted(self):
        home = Path(self.enterContext(tempfile.TemporaryDirectory()))
        root = home / _browser.PROFILE_ROOTS[PLATFORM]
        (root / "Profile 1" / "Cache").mkdir(parents=True)
        (root / "Profile 1" / "Preferences").write_text("{}")
        (root / "Profile 1" / "Cache" / "big").write_text("cache")
        (root / "Local State").write_text("{}")
        temp = home / "temp"
        temp.mkdir()

        async def check():
            async with await _browser.launch(headless=True, profile="Profile 1") as chrome:
                copy, = temp.glob("flnd-chrome-*")
                self.assertTrue((copy / "Default" / "Preferences").is_file())
                self.assertTrue((copy / "Local State").is_file())
                # Chromium makes its own Cache once it runs; the profile's wasn't copied into it.
                self.assertFalse((copy / "Default" / "Cache" / "big").exists())
                self.assertEqual((await chrome.tab()).url, "about:blank")
            return copy

        # The bundled Chromium stands in for real Chrome.
        with mock.patch.object(Path, "home", return_value=home), \
                mock.patch.object(tempfile, "tempdir", str(temp)), \
                mock.patch.object(_browser, "PROFILE_CHANNEL", "chromium"):
            self.assertFalse(run(check()).exists())


@needs_chromium
class PageCase(BrowserCase):
    """One browser for every page test; each test gets a fresh tab on `start`."""

    start = "https://site.test/"

    @classmethod
    def setUpClass(cls):
        if not SHARED:
            SHARED.append(run(_browser.launch(headless=True)))
            for site in ("https://site.test/**", "https://bad.test/**"):
                run(SHARED[0].raw.route(site, fulfill))
        cls.browser = SHARED[0]

    def setUp(self):
        super().setUp()
        self.page = run(self.browser.tab())
        self.addCleanup(lambda: run(self.browser.raw.clear_cookies()))
        self.addCleanup(lambda: run(self.page.close()))
        run(self.page.goto(self.start))


class PageTest(PageCase):
    def fetch(self, path):
        run(self.page.raw.evaluate("path => fetch(path).then(response => response.text())", path))

    def test_cookies_go_in_and_come_out_as_a_jar(self):
        run(self.browser.add_cookies(_cookies.Jar([
            {"name": "sid", "value": "1", "domain": ".site.test", "secure": True},
            {"name": "csrf", "value": "2", "domain": "site.test", "path": "/api"},
        ])))
        jar = run(self.browser.cookies())
        self.assertIsInstance(jar, _cookies.Jar)
        stored = {cookie["name"]: (cookie["domain"], cookie["path"]) for cookie in jar}
        self.assertEqual(stored, {"sid": (".site.test", "/"), "csrf": ("site.test", "/api")})
        self.assertEqual(jar.filter(names="sid").header, "sid=1")

    def test_goto_returns_the_status(self):
        visit = run(self.page.goto("https://site.test/other", idle=True))
        assert visit is not None
        self.assertEqual((visit.status, visit.url), (200, "https://site.test/other"))
        self.assertIn("text/html", visit.headers["content-type"])
        missing = run(self.page.goto("https://site.test/missing"))
        self.assertEqual(None if missing is None else missing.status, 404)
        self.assertIsNone(run(self.page.goto("https://bad.test/?token=secret")))
        line, = self.lines()
        self.assertIn("https://bad.test/", line)
        self.assertNotIn("token=secret", line)

    def test_a_capture_keeps_the_bodies_it_accepts_in_order(self):
        capture = run(self.page.capture(lambda response: "/api/" in response.url))
        for path in ("/api/graphql", "/home", "/api/other"):
            self.fetch(path)
        run(capture.settle())
        self.assertEqual([(url.removeprefix("https://site.test"), json.loads(body))
                          for url, body in capture.items],
                         [("/api/graphql", {"data": {"n": 1}}), ("/api/other", {"other": True})])
        self.assertEqual(len(run(capture.close())), 2)
        self.fetch("/api/graphql")
        run(capture.settle())
        self.assertEqual(list(capture.items), [])

    def test_wait(self):
        run(self.page.evaluate(
            "setTimeout(() => document.body.innerHTML = '<article>hi</article>', 20)"))
        run(self.page.wait("article", timeout=5))
        run(self.page.evaluate("setTimeout(() => history.pushState({}, '', '/reels/'), 20)"))
        for url in ("**/reels/", re.compile(r"/reels/$"), lambda url: url.endswith("/reels/")):
            run(self.page.wait(url=url, timeout=5))
        run(self.page.goto("https://site.test/"))
        run(self.page.wait(timeout=5))
        run(self.page.wait(idle=True, timeout=5))
        run(self.page.wait("body", url="**/site.test/", idle=True, timeout=5))

    def test_a_wait_that_times_out_names_what_it_waited_for(self):
        cases = [({"css": "#never"}, "an element to match '#never'"),
                 ({"url": "**/never/"}, "the URL to match '**/never/'"),
                 ({"url": lambda url: False}, "the URL to match a predicate"),
                 # The element comes at once; the URL wait has what is left of the one timeout.
                 ({"css": "body", "url": "**/never/"}, "the URL to match '**/never/'")]
        for options, what in cases:
            with self.subTest(what=what):
                started = time.monotonic()
                with self.assertRaises(_browser.WaitError) as caught:
                    run(self.page.wait(**cast(dict[str, Any], options), timeout=0.1))
                self.assertEqual(str(caught.exception), f"waited 0.1 s for {what}")
                self.assertIsInstance(caught.exception, Error)
                self.assertNotIsInstance(caught.exception, _browser._raw_error())
                self.assertLess(time.monotonic() - started, 2)

    def test_content_evaluate_click_url_close(self):
        self.assertIn("home", run(self.page.content()))
        run(self.page.evaluate("document.body.innerHTML = "
                               "'<button onclick=\"this.textContent = 1\">0</button>'"))
        self.assertIn("<button", run(self.page.content()))
        run(self.page.click("button"))
        self.assertEqual(run(self.page.evaluate("document.querySelector('button').textContent")),
                         "1")
        with self.assertRaisesRegex(_browser.WaitError, "to click '#never'"):
            run(self.page.click("#never", timeout=0.1))
        self.assertEqual(run(self.page.evaluate("x => x + 1", 41)), 42)
        self.assertEqual(self.page.url, "https://site.test/")
        run(self.page.close())
        self.assertTrue(self.page.raw.is_closed())


def upto(scrolls: int):
    """An `until` that lets `scrolls` scrolls through: it is asked once before each."""
    asked = iter(range(scrolls + 1))
    return lambda: next(asked, scrolls) >= scrolls


class ScrollTest(PageCase):
    """The scroller on a real page, with its waits made short."""

    def setUp(self):
        super().setUp()
        # A quiet bottom ends a feed after 0.2 s plus 0.2 s here, not 3 s plus 2 s.
        self.enterContext(mock.patch.object(_browser, "WAIT", 0.2))
        for name, value in (("GLANCE", 0.01), ("LOOK", 0.01), ("HESITATE", 0.01),
                            ("GLIDE", (0.001, 0.002))):
            self.enterContext(mock.patch.object(_browser, name, value))
        self.page.random = random.Random(7)

    def count(self) -> int:
        return run(self.page.evaluate("document.querySelectorAll('.item').length"))

    def test_fast_reaches_the_end_of_the_feed_by_wheel_or_by_script(self):
        for by_script in (False, True):
            with self.subTest(by_script=by_script):
                self.page.by_script = by_script
                run(self.page.goto("https://site.test/feed?cap=20"))
                scrolls = run(self.page.scroll(idle=0.2, style="fast"))
                self.assertEqual(self.count(), 20)
                self.assertLess(scrolls, 20)

    def test_within_scrolls_an_element_to_its_end(self):
        run(self.page.goto("https://site.test/box"))
        self.assertGreater(run(self.page.scroll(within="#box", idle=0.2, style="fast")), 3)
        self.assertEqual(self.count(), 20)
        # The box scrolled, and the page around it didn't.
        self.assertGreater(run(self.page.evaluate("document.getElementById('box').scrollTop")),
                           0)
        self.assertEqual(run(self.page.evaluate("scrollY")), 0)

    def test_until_is_asked_before_each_scroll_and_bad_options_raise(self):
        run(self.page.goto("https://site.test/feed"))
        self.assertEqual(run(self.page.scroll(until=upto(3), style="fast")), 3)
        self.assertEqual(run(self.page.scroll(until=lambda: True)), 0)
        with self.assertRaisesRegex(Changed, "no visible element"):
            run(self.page.scroll(within="#missing"))
        with self.assertRaisesRegex(ValueError, "human, fast, not 'slow'"):
            run(self.page.scroll(style="slow"))

    def test_human_uses_the_wheel_in_uneven_steps(self):
        run(self.page.goto("https://site.test/feed"))
        run(self.page.scroll(until=upto(8)))
        down = [delta for delta in run(self.page.evaluate("wheels")) if delta > 0]
        self.assertGreaterEqual(len(down), 8 * _browser.WHEEL_STEPS[0])
        self.assertGreater(len(set(down)), 5)
        # No one wheel step scrolls much more than a screen: a glide is several of them.
        self.assertTrue(all(delta < 900 * 1.25 for delta in down))


class FakeBrowserTest(BrowserCase):
    """`Browser`, `Page` and `Capture` over fake Playwright objects: no browser."""

    def test_a_jar_goes_in_and_a_rejected_cookie_is_logged_without_its_value(self):
        live = _browser.Browser(cast(Any, FakeContext()), close_steps=[], headless=True)
        run(live.add_cookies(_cookies.Jar([
            {"name": "sid", "value": "1", "domain": ".example.com"},
            {"name": "bad", "value": "s3cr3t-value"}])))
        self.assertEqual(run(live.cookies()), _cookies.Jar([
            {"name": "sid", "value": "1", "domain": ".example.com", "path": "/"}]))
        self.assertEqual(run(live.cookies()).header, "sid=1")
        line, = self.lines()
        self.assertTrue(line.startswith("! cookie bad was rejected: Invalid cookie"))
        self.assertNotIn("s3cr3t-value", line)

    def test_close_runs_every_step_once_and_a_failure_is_one_line(self):
        ran = []

        async def fails():
            ran.append("context")
            raise RuntimeError("Target closed")

        chrome = _browser.Browser(cast(Any, FakeContext()),
                                  close_steps=[fails, lambda: ran.append("copy")], headless=True)
        run(chrome.close())
        run(chrome.close())
        self.assertEqual(ran, ["context", "copy"])
        self.assertEqual(self.lines(), ["! closing the browser failed: Target closed"])

    def test_an_unreadable_body_is_skipped_and_only_an_unexpected_one_is_a_debug_line(self):
        ui.level = "debug"
        freed = ("Response.text: Protocol error (Network.getResponseBody): "
                 "No resource with given identifier found")
        for error, lines in ((freed, []), ("Target closed", [
                "responses: no body for https://site.test/api/graphql: Target closed"])):
            with self.subTest(error=error):
                self.dest.truncate(0)
                self.dest.seek(0)
                raw = SimpleNamespace(on=lambda *args: None, remove_listener=lambda *args: None)
                page = _browser.Page(cast(Any, raw), cast(Any, None))
                capture = run(page.capture(lambda response: True))

                async def hear(capture=capture, error=error):
                    capture.listener(SimpleNamespace(
                        url="https://site.test/api/graphql",
                        text=mock.AsyncMock(side_effect=_browser._raw_error()(error))))
                    await capture.settle()

                run(hear())
                self.assertEqual(list(capture.items), [])
                # The debug mark is a glyph of the theme; the text after it is what matters.
                self.assertEqual([line[2:] for line in self.lines()], lines)

    def test_a_read_survives_a_navigation_or_two_and_no_other_error(self):
        navigating = _browser._raw_error()("Page.content: Execution context was destroyed, "
                                           "most likely because of a navigation")
        for failures, content, loads in ((1, "<p>hi</p>", 1), (10, "<html></html>", 2)):
            with self.subTest(failures=failures):
                errors, waited = [navigating] * failures, []

                async def read(errors=errors):
                    if errors:
                        raise errors.pop()
                    return "<p>hi</p>"

                async def wait_for_load_state(state, timeout, waited=waited):  # noqa: ASYNC109
                    waited.append(state)

                raw = SimpleNamespace(content=read, wait_for_load_state=wait_for_load_state)
                page = _browser.Page(cast(Any, raw), cast(Any, None))
                self.assertEqual(run(page.content()), content)
                self.assertEqual(waited, ["load"] * loads)
        self.assertTrue(any("kept navigating" in line for line in self.lines()))
        raw = SimpleNamespace(content=mock.AsyncMock(side_effect=_browser._raw_error()("Target")))
        with self.assertRaises(_browser._raw_error()):
            run(_browser.Page(cast(Any, raw), cast(Any, None)).content())

    def test_each_logins_cookies_are_added_once_and_a_released_tab_is_reused(self):
        added = []

        class Fake:
            async def tab(self, *, by_script=False):
                return _browser.Page(cast(Any, SimpleNamespace()), cast(Any, self),
                                     by_script=by_script)

            async def add_cookies(self, jar):
                added.append(jar)

        shared = _browser.RunBrowser(LOOP)
        shared._browser = cast(Any, Fake())
        one, other = _cookies.Jar([]), _cookies.Jar([])
        first, second, third = (run(shared.tab(jar)) for jar in (one, one, other))
        self.assertEqual([jar is one for jar in added], [True, False])
        run(shared.release([third]))
        self.assertIs(run(shared.tab(one)), third)


class FakeFeed:
    """A raw page on a fake clock: each scroll brings a page `after` seconds later, `grows` times.

    `dom` says whether that page makes the page taller or only arrives as a response (`capture`);
    `bottom` whether the tab is at the very end. `nap` advances the clock: nothing waits for real.
    """

    def __init__(self, *, after=0.1, grows=10**9, bottom=True, dom=True):
        self.clock = _time.Fake()
        self.after, self.grows, self.bottom, self.dom = after, grows, bottom, dom
        self.height, self.heard = 1000, 0
        self.due, self.scrolled_at = [], []
        self.mouse = SimpleNamespace(move=mock.AsyncMock(), wheel=mock.AsyncMock())
        self.capture = SimpleNamespace(news=asyncio.Event())

    def scroll(self):
        self.scrolled_at.append(self.clock.now)
        if self.grows:
            self.grows -= 1
            self.due.append(self.clock.now + self.after)

    def arrive(self):
        arrived = [when for when in self.due if when <= self.clock.now]
        self.due = [when for when in self.due if when > self.clock.now]
        if self.dom:
            self.height += 900 * len(arrived)
        else:
            self.heard += len(arrived)
        self.capture.count = self.heard

    async def evaluate(self, expression, arg=None):
        self.arrive()
        answers = {_browser.PAGE_HEIGHT: self.height, _browser.AT_BOTTOM: self.bottom,
                   _browser.VIEWPORT: [800, 900], _browser.SCROLL_BY: None}
        return answers[expression]

    async def nap(self, seconds, news=None):
        self.clock.advance(seconds)
        self.arrive()

    def gaps(self):
        return [later - earlier for earlier, later in zip(self.scrolled_at, self.scrolled_at[1:])]


class ScrollTimingTest(BrowserCase):
    """The scroller's waits on a fake page and clock, so the timing rules are exact and instant."""

    def page(self, feed, seed=7):
        self.enterContext(_time.use(feed.clock))
        page = _browser.Page(cast(Any, feed), cast(Any, None))
        page.random = random.Random(seed)
        self.enterContext(mock.patch.object(page, "_nap", feed.nap))
        return page

    def scroll(self, until=None, *, style="fast", watch=False, **options):
        """Scrolls a fake feed, each scroll one instant `FakeFeed.scroll`; returns both."""
        feed = FakeFeed(dom=not watch, **options)
        feed.arrive()
        page = self.page(feed)
        scroller = run(page.scroller(style=style, watch=cast(Any, feed.capture) if watch else None))

        async def move():
            feed.scroll()

        async def steps():
            while not (until is not None and until()) and await scroller.step():
                pass

        with mock.patch.object(scroller, "_move", move):
            run(steps())
        return feed, scroller

    def assertGaps(self, feed, low, high):
        self.assertTrue(all(low - 1e-9 <= gap <= high + 1e-9 for gap in feed.gaps()), feed.gaps())

    def test_fast_scrolls_again_as_soon_as_the_page_grows_or_a_response_comes(self):
        for watch in (False, True):
            with self.subTest(watch=watch):
                feed, _ = self.scroll(upto(20), after=0.1, grows=20, watch=watch)
                # 0.1 s for the page, give or take one check of its height.
                self.assertGaps(feed, 0.1, 0.1 + _browser.POLL)
                self.assertEqual(feed.height, 1000 if watch else 1000 + 900 * 20)
        feed, _ = self.scroll(upto(5), after=2.0, grows=5)  # a slow page is waited for
        self.assertGaps(feed, 2.0, 2.0 + 2 * _browser.POLL)
        self.assertEqual(feed.height, 1000 + 900 * 5)

    def test_a_quiet_bottom_ends_after_the_wait_and_the_idle_window(self):
        for style in ("fast", "human"):
            with self.subTest(style=style):
                feed, _ = self.scroll(after=0.1, grows=5, style=style)
                # The quiet starts with the sixth scroll, the first that brought nothing.
                quiet = feed.clock.now - feed.scrolled_at[5]
                self.assertGreaterEqual(quiet, _browser.WAIT + _browser.IDLE)
                self.assertLess(quiet, _browser.WAIT + _browser.IDLE + 1.5)

    def test_300_growing_scrolls_are_not_cut_off_and_off_the_bottom_none_waits(self):
        feed, scroller = self.scroll(after=0.1, grows=300)
        self.assertGreater(scroller.scrolls, 300)
        self.assertEqual(feed.height, 1000 + 900 * 300)
        feed, scroller = self.scroll(upto(10), grows=0, bottom=False)
        self.assertEqual(scroller.scrolls, 10)
        self.assertGaps(feed, 0.0, 0.0)

    def test_a_human_pause_is_a_look_after_new_media_and_a_glance_otherwise(self):
        looks, _ = self.scroll(upto(200), after=0.05, grows=200, style="human")
        glances, _ = self.scroll(upto(200), grows=0, bottom=False, style="human")
        for feed, median in ((looks, _browser.LOOK), (glances, _browser.GLANCE)):
            gaps = sorted(feed.gaps())
            # Lognormal around its median: the middle gap is near it, some well off either way.
            self.assertAlmostEqual(gaps[len(gaps) // 2], median, delta=median * 0.15)
            self.assertLess(gaps[0], median * 0.7)
            self.assertGreater(gaps[-1], median * 1.4)
            self.assertLessEqual(gaps[-1], median * _browser.LONGEST + 1e-9)

    def test_a_seeded_run_is_repeatable(self):
        def wheels():
            feed = FakeFeed(grows=0, bottom=False)
            run(self.page(feed, seed=42).scroll(until=upto(60)))
            return [call.args for call in feed.mouse.wheel.call_args_list]

        first = wheels()
        self.assertEqual(first, wheels())
        self.assertGreater(len(first), 60 * 2)
        self.assertTrue(any(delta < 0 for _, delta in first))  # back-ups included

    def test_a_human_step_waits_for_its_pages_turn_and_a_fast_one_never(self):
        turns = []

        async def turn(step):
            turns.append(step)
            return await step()

        async def step(self):
            return True

        page = _browser.Page(cast(Any, SimpleNamespace()), cast(Any, None))
        page.turn = turn
        with mock.patch.object(_browser._Scroller, "_step", step):
            for style, waited in (("human", 1), ("fast", 1)):
                scroller = _browser._Scroller(page, None, idle=2.0, style=style, watch=None,
                                              height=0)
                self.assertTrue(run(scroller.step()))
                self.assertEqual(len(turns), waited)


class NoProfilesTest(unittest.TestCase):
    """The suite-wide guard (`tests/no_profiles.py`): no test reads or copies a real profile.

    The real home is left as it is, so each call would reach real profiles; the reads are also
    faked to fail, so even a broken guard reads nothing.
    """

    def setUp(self):
        self.assertTrue(os.environ.get(_cookies.NO_PROFILES), "the guard is off under the suite")
        never = AssertionError("a real profile was about to be read")
        for target, name in ((shutil, "copytree"), (_cookies, "_open_copy"),
                             (_browser, "_start_playwright")):
            self.enterContext(mock.patch.object(target, name, side_effect=never))

    def test_a_real_profile_is_neither_copied_launched_nor_read(self):
        with self.assertRaisesRegex(RuntimeError, _cookies.NO_PROFILES):
            run(_browser.launch(profile="Default", headless=True))
        with self.assertRaisesRegex(RuntimeError, _cookies.NO_PROFILES):
            run(_browser._open_context(cast(Any, None), _cookies.home() / "profile",
                                       "chrome", {"headless": True, "args": []}, {}))
        for browser in ("chrome", "firefox"):
            with self.subTest(browser=browser), \
                    self.assertRaisesRegex(RuntimeError, _cookies.NO_PROFILES):
                _cookies.load(browser)

    def test_a_temporary_home_is_allowed_and_the_guard_is_off_without_the_variable(self):
        with tempfile.TemporaryDirectory() as folder:
            _cookies.refuse_profile(Path(folder) / "Default")
        with mock.patch.dict(os.environ, {_cookies.NO_PROFILES: ""}):
            _cookies.refuse_profile(_cookies.home() / "Default")


class LoopTest(unittest.TestCase):
    def test_run_from_the_loop_itself_raises_and_a_closed_loop_refuses(self):
        loop = Loop()

        async def inner():
            return loop.run(asyncio.sleep(0))

        with self.assertRaisesRegex(RuntimeError, "own thread"):
            loop.run(inner())
        self.assertEqual(loop.run(asyncio.sleep(0, "done")), "done")
        loop.close()
        with self.assertRaisesRegex(RuntimeError, "closed"):
            loop.run(asyncio.sleep(0))

    def test_the_callers_clock_is_the_coroutines(self):
        async def now():
            return _time.now()

        with _time.use(_time.Fake(start=5.0)):
            self.assertEqual(run(now()), 5.0)


# Every byte value, so a body that went through text anywhere would come back changed.
BINARY_BODY = bytes(range(256)) * 300
# The header a test sends, and checks no error message ever shows.
SECRET = "Bearer s3cr3t-token-value"


class LocalHandler(BaseHTTPRequestHandler):
    """A page under /app/, bodies of each kind, statuses, an echo and a slow answer."""

    def do_GET(self):
        self.answer()

    do_POST = do_GET

    def answer(self):
        path, _, query = self.path.partition("?")
        bodies = {
            "/app/": ("text/html", b"<!doctype html><title>Local</title>"),
            "/text": ("text/plain; charset=utf-8", "héllo\r\nwörld".encode()),
            "/binary": ("application/octet-stream", BINARY_BODY),
        }
        if path in bodies:
            return self.send(200, *bodies[path])
        if path in ("/json", "/app/data/json"):
            return self.send(200, "application/json",
                             json.dumps({"path": path, "query": query}).encode())
        if path == "/echo":
            length = int(self.headers.get("content-length") or 0)
            return self.send(200, "application/json", json.dumps({
                "method": self.command, "body": self.rfile.read(length).decode("latin-1"),
                "headers": {name.lower(): value for name, value in self.headers.items()}}).encode())
        if path.startswith("/status/"):
            status = int(path.removeprefix("/status/"))
            return self.send(status, "text/plain", b"no",
                             {"Retry-After": "7"} if status == 429 else {})
        if path == "/slow":
            # Held until the test ends (or 10 s), so the page's abort is what ends the request.
            cast(Any, self.server).release.wait(10)
            return self.send(200, "text/plain", b"late")
        self.send(404, "text/plain", b"not here")

    def send(self, status, content_type, body, headers=None):
        try:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            for name, value in (headers or {}).items():
                self.send_header(name, value)
            self.end_headers()
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            pass  # the page aborted (the timeout test): nobody is reading any more

    def log_message(self, format, *args):
        """Quiet: the default writes every request to stderr."""


class FetchTest(PageCase):
    """`Page.fetch`: a request the page sends itself, to 127.0.0.1, as an `_net.Response`."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), LocalHandler)
        cls.server.daemon_threads = True
        cast(Any, cls.server).release = threading.Event()
        cls.url = f"http://127.0.0.1:{cls.server.server_address[1]}"
        cls.start = f"{cls.url}/app/"
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cast(Any, cls.server).release.set()
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join()

    def fetch(self, url, **options):
        return run(self.page.fetch(url, **options))

    def test_bodies_of_each_kind_and_a_relative_url(self):
        response = self.fetch(f"{self.url}/json?page=2")
        self.assertIsInstance(response, _net.Response)
        self.assertEqual((response.status, response.ok, response.url),
                         (200, True, f"{self.url}/json?page=2"))
        self.assertEqual(response.headers["Content-Type"], "application/json")
        self.assertEqual(response.json, {"path": "/json", "query": "page=2"})
        text = self.fetch("/text")
        self.assertEqual((text.content, text.text), ("héllo\r\nwörld".encode(), "héllo\r\nwörld"))
        self.assertEqual(self.fetch("/binary").content, BINARY_BODY)
        relative = self.fetch("data/json")
        self.assertEqual((relative.url, relative.json["path"]),
                         (f"{self.url}/app/data/json", "/app/data/json"))

    def test_bodies_sent_headers_and_cookies(self):
        echo = self.fetch("/echo", method="post", body={"id": 1, "name": "example_user"}).json
        self.assertEqual((echo["method"], json.loads(echo["body"])),
                         ("POST", {"id": 1, "name": "example_user"}))
        self.assertEqual(echo["headers"]["content-type"], "application/json")
        self.assertEqual(self.fetch("/echo", method="POST", body=b"\x00\xff\n").json["body"],
                         "\x00\xff\n")
        form = self.fetch("/echo", method="POST", body="q=a+b&n=2",
                          headers={"Content-Type": "application/x-www-form-urlencoded"}).json
        self.assertEqual(form["body"], "q=a+b&n=2")
        # A GET takes a dict body as its query.
        self.assertEqual(self.fetch("/json?page=2", body={"n": 3}).json["query"], "page=2&n=3")
        run(self.page.evaluate("document.cookie = 'sid=example-session; path=/'"))
        headers = self.fetch("/echo", headers={"X-Csrf": "token-1",
                                               "Accept": "application/json"}).json["headers"]
        self.assertEqual((headers["x-csrf"], headers["accept"], headers["referer"],
                          headers["cookie"]),
                         ("token-1", "application/json", f"{self.url}/app/", "sid=example-session"))

    def test_a_bad_status_raises_and_unchecked_fetch_returns_it(self):
        with self.assertRaises(SiteError) as caught:
            self.fetch("/status/404?token=abc", headers={"Authorization": SECRET})
        self.assertEqual(caught.exception.status, 404)
        self.assertEqual(str(caught.exception), f"GET {self.url}/status/404: HTTP 404")
        with self.assertRaises(Blocked) as caught:
            self.fetch("/status/429")
        self.assertEqual((caught.exception.wait, caught.exception.status), (7.0, 429))
        with self.assertRaisesRegex(SiteError, "500"):
            self.fetch("/status/500")
        for status in (404, 429):
            self.assertEqual(self.fetch(f"/status/{status}", check=False).status, status)

    def test_the_timeout_aborts_the_request_in_the_page(self):
        started = time.monotonic()
        with self.assertRaises(NetworkError) as caught, \
                mock.patch.object(_browser, "FETCH_TIMEOUT", 0.3):
            self.fetch("/slow?token=abc", headers={"Authorization": SECRET})
        self.assertLess(time.monotonic() - started, 5)
        self.assertEqual(str(caught.exception), f"GET {self.url}/slow: no answer within 0.3 s")
        self.assertEqual(self.fetch("/json").status, 200)  # the page is still usable

    def test_a_request_the_browser_refuses_names_no_header_value(self):
        with self.assertRaises(Error) as caught:
            # A NUL byte makes the value invalid: the browser's TypeError must not show it.
            self.fetch("/json", headers={"X-Token": SECRET + "\x00"})
        self.assertNotIn("s3cr3t", str(caught.exception))
        self.assertTrue(str(caught.exception).startswith(f"GET {self.url}/json: TypeError"),
                        caught.exception)


if __name__ == "__main__":
    unittest.main()
