"""`flnkit.browser`: `launch` and its profile copy, the Chromium offer, `Browser`, `Page`, `Capture`, scrolling.

Every coroutine runs on one `flnkit.loop.Loop` for the module (`run`), as the run's loop runs them.
The profile and Chromium tests fake Playwright, the home folder and the temp folder, so no real
profile or browser is touched. The page tests run Playwright's own Chromium, headless, on pages
served from memory (`fulfill`), and skip without it. `Page.fetch`'s tests serve their page from a
real HTTP server on 127.0.0.1 (`LocalServer`), since a request the page sends itself must leave it.
The scroll timing tests fake the page and the clock. `TargetSite`'s use of all this (tabs, warm
tabs, feeds) is in `test_site.py`.
"""

import asyncio
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
# The loop every coroutine of this module runs on.
LOOP = Loop()
run = LOOP.run


def tearDownModule():
    LOOP.close()


def chromium_installed() -> bool:
    """Whether Playwright's own Chromium is downloaded; checked without launch, which could ask to download it."""
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        return False
    with sync_playwright() as playwright:
        return Path(playwright.chromium.executable_path).is_file()


HAS_CHROMIUM = chromium_installed()
needs_chromium = unittest.skipUnless(HAS_CHROMIUM, "Chromium is not installed")

# Items are 300px tall and come five at a time when the page is scrolled to its end.
FEED_PAGE = """<!doctype html><html><body style="margin:0">
<div id="feed"></div>
<script>
  const params = new URLSearchParams(location.search);
  const cap = Number(params.get("cap") || 40);
  const feed = document.getElementById("feed");
  window.wheels = [];
  function add(n) {
    for (let i = 0; i < n && feed.children.length < cap; i++) {
      const item = document.createElement("div");
      item.className = "item";
      item.style.height = "300px";
      item.textContent = "item " + feed.children.length;
      feed.appendChild(item);
    }
  }
  add(5);
  addEventListener("wheel", event => wheels.push(Math.round(event.deltaY)));
  addEventListener("scroll", () => {
    if (scrollY + innerHeight < document.body.scrollHeight - 10) return;
    add(5);
  });
</script></body></html>"""

# The same feed inside a 500px box that scrolls on its own; the page itself never scrolls.
BOX_PAGE = """<!doctype html><html><body style="margin:0">
<div id="box" style="height:500px;width:600px;overflow-y:auto;margin-top:100px"></div>
<script>
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

API_BODIES = {
    "/api/graphql": ("application/json", json.dumps({"data": {"n": 1}})),
    "/api/other": ("application/json", json.dumps({"other": True})),
}


async def fulfill(route):
    """Serves https://site.test/ from memory, so no test ever touches the network."""
    url = route.request.url
    if url.startswith("https://bad.test/"):
        await route.abort()
        return
    path = url.removeprefix("https://site.test").split("?")[0]
    if path == "/feed":
        await route.fulfill(status=200, content_type="text/html", body=FEED_PAGE)
        return
    if path == "/box":
        await route.fulfill(status=200, content_type="text/html", body=BOX_PAGE)
        return
    if path == "/missing":
        await route.fulfill(status=404, content_type="text/html", body="<p>not here</p>")
        return
    content_type, body = API_BODIES.get(path, ("text/html", "<html><head><title>Home</title></head><body>home</body></html>"))
    await route.fulfill(status=200, content_type=content_type, body=body)


class BrowserCase(unittest.TestCase):
    def setUp(self):
        self.dest = StringIO()
        self.scope = ui.scope(dest=self.dest, color=False, width=300)
        self.scope.__enter__()
        self.previous_level = ui.level
        # The bundled build, so results don't depend on whether this machine has Chrome installed,
        # and no earlier test's "no" to the Chromium download, or failed download, is remembered.
        for patcher in (mock.patch.object(_browser, "_chrome_installed", return_value=False),
                        mock.patch.object(_browser, "_chromium_declined", False),
                        mock.patch.object(_browser, "_chromium_failed", None)):
            patcher.start()
            self.addCleanup(patcher.stop)

    def tearDown(self):
        ui.level = cast(Any, self.previous_level)
        self.scope.__exit__(None, None, None)

    def lines(self) -> list[str]:
        return self.dest.getvalue().splitlines()


class ImportTest(BrowserCase):
    def test_without_playwright(self):
        blocked = {"playwright": None, "playwright.async_api": None}
        with mock.patch.dict(sys.modules, blocked):
            sys.modules.pop("flnkit.browser", None)
            try:
                fresh = importlib.import_module("flnkit.browser")
                with self.assertRaisesRegex(ImportError, re.escape("pip install playwright")):
                    run(fresh.launch())
            finally:
                sys.modules["flnkit.browser"] = _browser


class FakePlaywright:
    """Playwright's driver as `launch` uses it before `_open_context`: it only stops."""

    def __init__(self, executable="/fake/chromium"):
        self.chromium = SimpleNamespace(executable_path=executable)
        self.stops = 0

    async def stop(self):
        self.stops += 1


class FakeContext:
    """A `BrowserContext` that keeps cookies, and rejects any without a domain, quoting it whole."""

    def __init__(self):
        self.stored = []
        self.scripts = []

    async def add_init_script(self, script):
        self.scripts.append(script)

    async def add_cookies(self, cookies):
        for cookie in cookies:
            if "domain" not in cookie:
                raise _browser._raw_error()(f"Invalid cookie {cookie}")
            self.stored.append(cookie)

    async def cookies(self):
        return [dict(cookie) for cookie in self.stored]


class ProfileCopyTest(BrowserCase):
    """`launch(profile=<name>)`: the copy, which holds Telegram Web's login, never staying in the temp
    folder (fix #14). A fake home holds the profiles and a private temp folder the copies, so no real
    profile is touched. No browser."""

    def setUp(self):
        super().setUp()
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.home = Path(folder.name) / "home"
        platform = sys.platform if sys.platform in ("darwin", "win32") else "linux"
        self.root = self.home / _browser.PROFILE_ROOTS[platform]
        (self.root / "Default").mkdir(parents=True)
        (self.root / "Default" / "Preferences").write_text("{}")
        self.temp = Path(folder.name) / "temp"
        self.temp.mkdir()
        for patcher in (mock.patch.object(Path, "home", return_value=self.home),
                        mock.patch.object(tempfile, "tempdir", str(self.temp))):
            patcher.start()
            self.addCleanup(patcher.stop)
        self.playwright = FakePlaywright()
        self.launches = []

    def copies(self) -> list[Path]:
        return list(self.temp.glob("flnd-chrome-*"))

    async def open_context(self, playwright, profile, channel, launch_options, context_options):
        """Records what _launch asked for; the copy is checked while it exists."""
        profile = Path(profile)
        self.launches.append((profile, channel, launch_options["args"][-1],
                              stat.S_IMODE(profile.stat().st_mode), (profile / "Default" / "Preferences").is_file()))
        return FakeContext(), [playwright.stop]

    def launch(self, **options):
        async def start():
            return self.playwright

        with mock.patch.object(_browser, "_start_playwright", start), \
                mock.patch.object(_browser, "_open_context", self.open_context):
            return run(_browser.launch(**options))

    def test_a_name_is_copied_privately_into_chrome_and_deleted_on_close(self):
        chrome = self.launch(profile="Default")
        (copy, channel, last_arg, mode, has_preferences), = self.launches
        self.assertEqual((channel, last_arg, has_preferences), ("chrome", "--profile-directory=Default", True))
        self.assertTrue(copy.is_relative_to(self.temp))
        if sys.platform != "win32":
            # mkdtemp's mode: no other user of the machine can read the copy's logins.
            self.assertEqual(mode, 0o700)
        self.assertEqual(self.copies(), [copy])
        run(chrome.close())
        self.assertEqual(self.copies(), [])

    def test_a_path_or_nothing_is_no_profile_name(self):
        for profile in ("", "./Default", "profiles/Default", "/tmp/profile", ".", ".."):
            with self.subTest(profile=profile), self.assertRaises(ValueError):
                self.launch(profile=profile)
        self.assertEqual(self.copies(), [])

    def test_an_unknown_name_raises_before_playwright_starts(self):
        with mock.patch.object(_browser, "_start_playwright") as start:
            with self.assertRaisesRegex(FileNotFoundError, "no Chrome profile named 'Profile 9'"):
                run(_browser.launch(profile="Profile 9"))
        start.assert_not_called()
        self.assertEqual(self.copies(), [])

    def test_copy_deleted_without_playwright_or_on_ctrl_c_before_launch(self):
        for error in (ImportError("no playwright"), KeyboardInterrupt()):
            with self.subTest(error=error):
                async def start(error=error):
                    raise error

                with mock.patch.object(_browser, "_start_playwright", start), self.assertRaises(type(error)):
                    run(_browser.launch(profile="Default"))
                self.assertEqual(self.copies(), [])

    def test_copy_deleted_when_the_launch_fails(self):
        for error in (KeyboardInterrupt(), RuntimeError(_browser.NEEDS_CHROME)):
            with self.subTest(error=error):
                playwright = FakePlaywright()

                async def start(playwright=playwright):
                    return playwright

                async def fail(*args, error=error):
                    raise error

                with mock.patch.object(_browser, "_start_playwright", start), \
                        mock.patch.object(_browser, "_open_context", fail):
                    with self.assertRaises(type(error)):
                        run(_browser.launch(profile="Default"))
                self.assertEqual(self.copies(), [])
                self.assertEqual(playwright.stops, 1)

    def test_copy_deleted_when_the_copy_fails(self):
        """A copy that fails half way, Ctrl-C included, must not leave the profile's logins in the temp folder."""
        for error in (OSError("disk full"), KeyboardInterrupt()):
            with self.subTest(error=error):
                with mock.patch.object(shutil, "copytree", side_effect=error) as copytree:
                    with self.assertRaises(type(error)):
                        run(_browser.launch(profile="Default"))
                # The copy was started inside the private temp folder, so an empty glob means it was deleted.
                self.assertTrue(Path(copytree.call_args.args[1]).is_relative_to(self.temp))
                self.assertEqual(self.copies(), [])


class FakeInstaller:
    """A finished `subprocess.Popen` of Playwright's installer, with its output and exit code."""

    def __init__(self, output: str, returncode: int):
        self.stdout, self.returncode = StringIO(output), returncode

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        return None


def drawn(screen, label, *, total=None):
    """A `Screen.progress` that draws its row as flnd's screen does."""
    return ui.progress(label, total=total)


class ChromeOnlyTest(BrowserCase):
    """Which browser launch starts, and the Chromium download offered without Chrome. No browser starts."""

    def setUp(self):
        super().setUp()
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        # Where Playwright would keep its Chromium: nothing there until the fake installer runs.
        self.chromium = Path(folder.name) / "chromium-1" / "chrome"
        self.playwright = FakePlaywright(str(self.chromium))
        self.installs = []
        self.launched = []

        async def start():
            return self.playwright

        for patcher in (mock.patch.object(_browser, "_start_playwright", start),
                        mock.patch.object(_browser, "_open_context", self.open_context),
                        mock.patch.object(_browser.subprocess, "Popen", self.popen),
                        # The rows draw as flnd's screen draws them (`flnd.engine.site`).
                        mock.patch.object(_browser.Screen, "progress", drawn),
                        mock.patch("playwright._impl._driver.compute_driver_executable",
                                   return_value=("/driver/node", "/driver/cli.js"))):
            patcher.start()
            self.addCleanup(patcher.stop)

    async def open_context(self, playwright, profile, channel, launch_options, context_options):
        self.launched.append(channel)
        return FakeContext(), [playwright.stop]

    def popen(self, argv, **options):
        """Playwright's installer: it prints progress lines, then the browser is there."""
        self.installs.append((argv, options))
        self.chromium.parent.mkdir(parents=True)
        self.chromium.write_text("")
        output = "|■■■■      |  40% of 167.3 MiB\n|■■■■■■■■■■| 100% of 167.3 MiB\nChromium downloaded\n"
        return FakeInstaller(output, 0)

    def terminal(self, *keys):
        answers = iter(keys)
        p = mock.patch.object(_browser.Screen, "confirm",
                              side_effect=lambda *a: next(answers) == "y")
        p.start()
        self.addCleanup(p.stop)

    def launch_and_close(self):
        run(self._launch_and_close())

    async def _launch_and_close(self):
        await (await _browser.launch()).close()

    def test_chrome_is_launched_when_installed(self):
        with mock.patch.object(_browser, "_chrome_installed", return_value=True):
            self.launch_and_close()
        self.assertEqual((self.launched, self.installs), (["chrome"], []))

    def test_a_downloaded_chromium_is_used_without_asking(self):
        self.chromium.parent.mkdir(parents=True)
        self.chromium.write_text("")
        self.terminal()   # Any question would read a key, and there is none.
        self.launch_and_close()
        self.assertEqual((self.launched, self.installs), (["chromium"], []))

    def test_yes_downloads_chromium_then_launches_it(self):
        self.terminal("y")
        self.launch_and_close()
        self.assertEqual(self.launched, ["chromium"])
        (argv, options), = self.installs
        self.assertEqual(argv, ["/driver/node", "/driver/cli.js", "install", "--no-shell", "chromium"])
        self.assertEqual(options["env"]["PW_LANG_NAME"], "python")
        # Into Playwright's user cache: the installer's folder is left to Playwright.
        self.assertNotIn("PLAYWRIGHT_BROWSERS_PATH", {key: value for key, value in options["env"].items()
                                                      if key not in os.environ})
        self.assertIn("Downloading Chromium", self.dest.getvalue())
        # A later run finds it and doesn't ask: the screen has no key left to answer with.
        self.launch_and_close()
        self.assertEqual((self.launched, len(self.installs)), (["chromium", "chromium"], 1))

    def test_no_raises_naming_chrome_and_is_asked_once(self):
        self.terminal("n")
        for _ in range(2):
            with self.assertRaises(ToolError) as caught:
                run(_browser.launch())
            self.assertEqual(str(caught.exception), "flnd needs Google Chrome: install it from https://www.google.com/chrome/")
            self.assertEqual(caught.exception.hint, _browser.INSTALL_CHROME)
        self.assertEqual((self.launched, self.installs), ([], []))
        self.assertEqual(self.playwright.stops, 2)

    def test_without_a_terminal_it_raises_naming_chrome(self):
        with mock.patch.object(sys.stdin, "isatty", return_value=False), \
                self.assertRaisesRegex(ToolError, "^flnd needs Google Chrome: install it"):
            run(_browser.launch())
        self.assertEqual((self.launched, self.installs), ([], []))
        self.assertNotIn(_browser.CHROMIUM_QUESTION, self.dest.getvalue())

    def test_a_failed_download(self):
        self.terminal("y")
        with mock.patch.object(_browser.subprocess, "Popen", return_value=FakeInstaller("Error: ENOSPC\n", 1)), \
                self.assertRaisesRegex(ToolError, "exited with 1: Error: ENOSPC"):
            run(_browser.launch())
        self.assertEqual(self.launched, [])
        self.assertIn("Downloading Chromium", self.dest.getvalue())

    def test_a_failed_download_is_raised_again_without_asking(self):
        asked = []
        failure = ToolError("Chromium was not downloaded: Playwright's installer exited with 1: Error: ENOSPC")
        with mock.patch.object(_browser.Screen, "confirm",
                               lambda *args: asked.append(args) or True), \
                mock.patch.object(_browser, "_install_chromium", side_effect=failure) as install:
            for _ in range(2):
                with self.assertRaises(ToolError) as caught:
                    run(_browser.launch())
                self.assertIs(caught.exception, failure)
        self.assertEqual((len(asked), install.call_count, self.launched), (1, 1, []))

    def test_two_threads_at_once_ask_once_and_download_once(self):
        # Two runs in one process may find Chromium missing together.
        asked, installs, channels = [], [], []
        start = threading.Barrier(2)

        def confirm(*args):
            asked.append(args)
            # Long enough for the other thread to reach the question too, were nothing holding it back.
            time.sleep(0.1)
            return True

        def install(screen):
            installs.append(1)
            self.chromium.parent.mkdir(parents=True)
            self.chromium.write_text("")

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
        with mock.patch.object(Path, "is_file", lambda path: found.append(str(path)) or False):
            for platform, env in (("darwin", {}), ("linux", {}),
                                  ("win32", {"LOCALAPPDATA": "C:/Users/a/AppData/Local", "PROGRAMFILES": "C:/PF"})):
                with mock.patch.object(_browser.sys, "platform", platform), mock.patch.dict(os.environ, env, clear=True):
                    self.assertFalse(CHROME_INSTALLED())
        self.assertEqual([Path(path) for path in found], [
            Path("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"), Path("/opt/google/chrome/chrome"),
            Path("C:/Users/a/AppData/Local/Google/Chrome/Application/chrome.exe"),
            Path("C:/PF/Google/Chrome/Application/chrome.exe")])


class HeadfulAgentTest(unittest.TestCase):
    def test_the_headed_name_is_measured_once_per_executable(self):
        _browser._HEADFUL_UA.clear()
        self.addCleanup(_browser._HEADFUL_UA.clear)
        probes = []

        class Remote:
            async def new_context(self):
                probes.append(1)
                page = SimpleNamespace(evaluate=mock.AsyncMock(return_value="Mozilla HeadlessChrome/120"))
                return SimpleNamespace(new_page=mock.AsyncMock(return_value=page), close=mock.AsyncMock())

        playwright = FakePlaywright("/fake/chrome/bin")
        first = run(_browser._headful_ua(cast(Any, playwright), "chromium", Remote()))
        second = run(_browser._headful_ua(cast(Any, playwright), "chromium", Remote()))
        self.assertEqual((first, second, len(probes)), ("Mozilla Chrome/120", "Mozilla Chrome/120", 1))


@needs_chromium
class LaunchTest(BrowserCase):
    def test_incognito_leaves_nothing_behind(self):
        async def check():
            async with await _browser.launch(headless=True) as chrome:
                await chrome.add_cookies([{"name": "sid", "value": "1", "domain": "site.test",
                                           "expires": time.time() + 3600}])
                first = len(await chrome.cookies())
            async with await _browser.launch(headless=True) as chrome:
                return first, len(await chrome.cookies())

        self.assertEqual(run(check()), (1, 0))

    def test_headless_reports_a_headed_user_agent_and_hides_webdriver(self):
        async def check(stealth):
            async with await _browser.launch(headless=True, stealth=stealth) as chrome:
                tab = await chrome.tab()
                return await tab.evaluate("navigator.userAgent"), await tab.evaluate("navigator.webdriver")

        agent, webdriver = run(check(True))
        self.assertIn("Chrome/", agent)
        self.assertNotIn("Headless", agent)
        self.assertIsNone(webdriver)
        self.assertIs(run(check(False))[1], False)

    def test_an_installed_profile_is_copied_and_deleted(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        home = Path(folder.name)
        platform = sys.platform if sys.platform in ("darwin", "win32") else "linux"
        root = home / _browser.PROFILE_ROOTS[platform]
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
        with mock.patch.object(Path, "home", return_value=home), mock.patch.object(tempfile, "tempdir", str(temp)), \
                mock.patch.object(_browser, "PROFILE_CHANNEL", "chromium"):
            copy = run(check())
        self.assertFalse(copy.exists())

    def test_closed_after_an_exception(self):
        async def fail(seen):
            async with await _browser.launch(headless=True) as chrome:
                seen.append(chrome.raw.browser)
                raise ValueError("boom")

        seen = []
        with self.assertRaises(ValueError):
            run(fail(seen))
        self.assertFalse(seen[0].is_connected())


@needs_chromium
class PageCase(BrowserCase):
    """One browser for the whole class; each test gets a fresh tab on https://site.test/."""

    @classmethod
    def setUpClass(cls):
        cls.browser = run(_browser.launch(headless=True))
        run(cls.browser.raw.route("https://site.test/**", fulfill))
        run(cls.browser.raw.route("https://bad.test/**", fulfill))

    @classmethod
    def tearDownClass(cls):
        run(cls.browser.close())

    def setUp(self):
        super().setUp()
        self.page = run(self.browser.tab())
        run(self.page.goto("https://site.test/"))
        self.addCleanup(lambda: run(self.page.close()))


class BrowserTest(PageCase):
    def test_cookies(self):
        run(self.browser.add_cookies(_cookies.Jar([
            {"name": "sid", "value": "1", "domain": ".site.test", "secure": True},
            {"name": "csrf", "value": "2", "domain": "site.test", "path": "/api"},
        ])))
        jar = run(self.browser.cookies())
        self.assertIsInstance(jar, _cookies.Jar)
        stored = {cookie["name"]: (cookie["domain"], cookie["path"]) for cookie in jar}
        self.assertEqual(stored, {"sid": (".site.test", "/"), "csrf": ("site.test", "/api")})
        self.assertEqual(jar.filter(names="sid").header, "sid=1")
        run(self.browser.raw.clear_cookies())

    def test_one_bad_cookie_among_good_ones(self):
        run(self.browser.add_cookies([{"name": "bad", "value": "s3cr3t-value"},
                                      {"name": "good", "value": "1", "domain": "site.test"}]))
        self.assertEqual([cookie["name"] for cookie in run(self.browser.cookies())], ["good"])
        self.assertEqual(len(self.lines()), 1)
        self.assertTrue(self.lines()[0].startswith("! cookie bad was rejected: "))
        self.assertNotIn("s3cr3t-value", self.lines()[0])
        run(self.browser.raw.clear_cookies())


class PageTest(PageCase):
    def fetch(self, path):
        run(self.page.raw.evaluate("path => fetch(path).then(response => response.text())", path))

    def test_goto_returns_the_status(self):
        visit = run(self.page.goto("https://site.test/other", idle=True))
        assert visit is not None
        self.assertEqual((visit.status, visit.url), (200, "https://site.test/other"))
        self.assertIn("text/html", visit.headers["content-type"])
        missing = run(self.page.goto("https://site.test/missing"))
        self.assertEqual(None if missing is None else missing.status, 404)
        self.assertIsNone(run(self.page.goto("https://bad.test/?token=secret")))
        self.assertEqual(len(self.lines()), 1)
        self.assertIn("https://bad.test/", self.lines()[0])
        self.assertNotIn("token=secret", self.lines()[0])

    def test_a_capture_keeps_the_bodies_it_accepts_in_order(self):
        capture = run(self.page.capture(lambda response: "/api/" in response.url))
        self.fetch("/api/graphql")
        self.fetch("/home")
        self.fetch("/api/other")
        run(capture.settle())
        self.assertEqual([(url.removeprefix("https://site.test"), json.loads(body)) for url, body in capture.items],
                         [("/api/graphql", {"data": {"n": 1}}), ("/api/other", {"other": True})])
        left = run(capture.close())
        self.assertEqual(len(left), 2)
        self.fetch("/api/graphql")
        run(capture.settle())
        self.assertEqual(list(capture.items), [])

    def test_wait(self):
        run(self.page.evaluate("setTimeout(() => document.body.innerHTML = '<article>hi</article>', 100)"))
        run(self.page.wait("article", timeout=5))
        run(self.page.evaluate("setTimeout(() => history.pushState({}, '', '/reels/'), 100)"))
        run(self.page.wait(url="**/reels/", timeout=5))
        run(self.page.wait(url=re.compile(r"/reels/$"), timeout=5))
        run(self.page.wait(url=lambda url: url.endswith("/reels/"), timeout=5))
        run(self.page.goto("https://site.test/"))
        run(self.page.wait(timeout=5))
        run(self.page.wait(idle=True, timeout=5))
        run(self.page.wait("body", url="**/site.test/", idle=True, timeout=5))

    def test_wait_raises_wait_error_naming_what_it_waited_for(self):
        for options, what in (({"css": "#never"}, "an element to match '#never'"),
                              ({"url": "**/never/"}, "the URL to match '**/never/'"),
                              ({"url": lambda url: False}, "the URL to match a predicate")):
            with self.subTest(what=what):
                started = time.monotonic()
                with self.assertRaises(_browser.WaitError) as caught:
                    run(self.page.wait(**cast(dict[str, Any], options), timeout=0.2))
                self.assertEqual(str(caught.exception), f"waited 0.2 s for {what}")
                self.assertIsInstance(caught.exception, Error)
                self.assertNotIsInstance(caught.exception, _browser._raw_error())
                self.assertLess(time.monotonic() - started, 3)

    def test_wait_timeout_is_for_every_condition_together(self):
        # The element never comes, so the URL wait has only what the first one left: none.
        started = time.monotonic()
        with self.assertRaises(_browser.WaitError):
            run(self.page.wait("body", url="**/never/", timeout=0.5))
        self.assertLess(time.monotonic() - started, 2)

    def test_content_evaluate_url_close(self):
        self.assertIn("home", run(self.page.content()))
        run(self.page.evaluate("document.body.innerHTML = '<p>changed</p>'"))
        self.assertIn("changed", run(self.page.content()))
        self.assertEqual(run(self.page.evaluate("x => x + 1", 41)), 42)
        self.assertEqual(self.page.url, "https://site.test/")
        run(self.page.close())
        self.assertTrue(self.page.raw.is_closed())

    def test_click(self):
        run(self.page.evaluate("document.body.innerHTML = "
                               "'<button onclick=\"this.textContent = 1\">0</button>'"))
        run(self.page.click("button"))
        self.assertEqual(run(self.page.evaluate("document.querySelector('button').textContent")), "1")
        with self.assertRaisesRegex(_browser.WaitError, "to click '#never'"):
            run(self.page.click("#never", timeout=0.2))


class ScrollTest(PageCase):
    """The scroller on a real page. Most use `fast`, which has no pauses; the human one makes its pauses short."""

    def setUp(self):
        super().setUp()
        # A quiet bottom ends a feed after 0.3 s plus 0.3 s here, not 3 s plus 2 s.
        self.enterContext(mock.patch.object(_browser, "WAIT", 0.3))
        self.page.random = random.Random(7)

    def load(self, cap=40):
        run(self.page.goto(f"https://site.test/feed?cap={cap}"))

    def count(self) -> int:
        return run(self.page.evaluate("document.querySelectorAll('.item').length"))

    def wheels(self) -> list[int]:
        return run(self.page.evaluate("wheels"))

    def test_reaches_the_end_of_the_feed(self):
        self.load(cap=20)
        scrolls = run(self.page.scroll(idle=0.3, style="fast"))
        self.assertEqual(self.count(), 20)
        self.assertLess(scrolls, 20)

    def test_by_script_reaches_the_end_too(self):
        self.page.by_script = True
        self.load(cap=20)
        run(self.page.scroll(idle=0.3, style="fast"))
        self.assertEqual(self.count(), 20)

    def test_until_is_asked_before_each_scroll(self):
        self.load()
        self.assertEqual(run(self.page.scroll(until=upto(3), style="fast")), 3)
        self.assertEqual(run(self.page.scroll(until=lambda: True)), 0)

    def test_within_scrolls_an_element_to_its_end(self):
        run(self.page.goto("https://site.test/box"))
        scrolls = run(self.page.scroll(within="#box", idle=0.3, style="fast"))
        self.assertEqual(self.count(), 20)
        self.assertGreater(scrolls, 3)
        # The box scrolled, and the page around it didn't.
        self.assertGreater(run(self.page.evaluate("document.getElementById('box').scrollTop")), 0)
        self.assertEqual(run(self.page.evaluate("scrollY")), 0)

    def test_within_nothing_visible_raises(self):
        self.load()
        with self.assertRaisesRegex(Changed, "no visible element"):
            run(self.page.scroll(within="#missing"))

    def test_human_uses_the_wheel_in_uneven_steps(self):
        self.load()
        with mock.patch.object(_browser, "GLANCE", 0.01), mock.patch.object(_browser, "LOOK", 0.01):
            run(self.page.scroll(until=upto(8)))
        down = [delta for delta in self.wheels() if delta > 0]
        self.assertGreaterEqual(len(down), 8 * _browser.WHEEL_STEPS[0])
        self.assertGreater(len(set(down)), 5)
        # No one wheel step scrolls much more than a screen: a glide is several of them.
        self.assertTrue(all(delta < 900 * 1.25 for delta in down))

    def test_an_unknown_style_is_refused(self):
        with self.assertRaisesRegex(ValueError, "human, fast, not 'slow'"):
            run(self.page.scroll(style="slow"))


def upto(scrolls: int):
    """An `until` that lets `scrolls` scrolls through: it is asked once before each."""
    asked = iter(range(scrolls + 1))
    return lambda: next(asked, scrolls) >= scrolls


class CookiesTest(BrowserCase):
    def setUp(self):
        super().setUp()
        self.live = _browser.Browser(cast(Any, FakeContext()), close_steps=[], headless=True)

    def test_a_jar_goes_in_and_comes_out(self):
        run(self.live.add_cookies(_cookies.Jar([{"name": "sid", "value": "1", "domain": ".example.com"}])))
        self.assertEqual(run(self.live.cookies()),
                         _cookies.Jar([{"name": "sid", "value": "1", "domain": ".example.com", "path": "/"}]))
        self.assertEqual(run(self.live.cookies()).header, "sid=1")

    def test_a_rejected_cookie_is_logged_without_its_value(self):
        run(self.live.add_cookies([{"name": "bad", "value": "s3cr3t-value"},
                                   {"name": "good", "value": "1", "domain": "example.com"}]))
        self.assertEqual([c["name"] for c in run(self.live.cookies()).list], ["good"])
        self.assertEqual(len(self.lines()), 1)
        self.assertTrue(self.lines()[0].startswith("! cookie bad was rejected: Invalid cookie"))
        self.assertNotIn("s3cr3t-value", self.lines()[0])


class CloseTest(BrowserCase):
    def test_every_step_runs_and_a_failure_is_one_line(self):
        ran = []

        async def fails():
            ran.append("context")
            raise RuntimeError("Target closed")

        chrome = _browser.Browser(cast(Any, FakeContext()), close_steps=[fails, lambda: ran.append("copy")],
                                  headless=True)
        run(chrome.close())
        run(chrome.close())
        self.assertEqual(ran, ["context", "copy"])
        self.assertEqual(self.lines(), ["! closing the browser failed: Target closed"])


class GoneTest(unittest.TestCase):
    def test_gone_matches_a_navigation_and_a_freed_body_only(self):
        self.assertTrue(_browser._gone(Exception("Execution context was destroyed, most likely")))
        self.assertTrue(_browser._gone(Exception("No resource with given identifier found")))
        self.assertFalse(_browser._gone(Exception("Target closed")))


class GoneBodyTest(BrowserCase):
    def capture(self, error):
        raw = SimpleNamespace(on=lambda *args: None, remove_listener=lambda *args: None)
        capture = run(_browser.Page(cast(Any, raw), cast(Any, None)).capture(lambda response: True))

        async def hear():
            capture.listener(SimpleNamespace(url="https://site.test/api/graphql", text=mock.AsyncMock(side_effect=error)))
            await capture.settle()

        run(hear())
        return capture

    def test_a_body_freed_after_the_page_moved_on_is_skipped_without_a_line(self):
        ui.level = "debug"
        error = _browser._raw_error()("Response.text: Protocol error (Network.getResponseBody): "
                                      "No resource with given identifier found")
        self.assertEqual(list(self.capture(error).items), [])
        self.assertEqual(self.lines(), [])

    def test_another_unreadable_body_is_a_debug_line(self):
        ui.level = "debug"
        self.assertEqual(list(self.capture(_browser._raw_error()("Target closed")).items), [])
        self.assertIn(self.lines(), ([". responses: no body for https://site.test/api/graphql: Target closed"],
                                     ["· responses: no body for https://site.test/api/graphql: Target closed"]))


class FakeFeed:
    """A raw page on a fake clock: each scroll brings the next page `after` seconds later, for `grows` scrolls.

    `grows` says whether that page makes the page taller (a DOM feed) or only arrives as a response (`capture`,
    which a scroller watches); `bottom` whether the tab is at the very end, as a feed waiting for its next page
    is. Nothing here waits for real: `nap` advances the clock.
    """

    def __init__(self, *, after=0.1, grows=10**9, bottom=True, dom=True):
        self.clock = _time.Fake()
        self.after, self.grows, self.bottom, self.dom = after, grows, bottom, dom
        self.height = 1000
        self.due = []                                    # when the next pages arrive
        self.scrolled_at = []
        self.mouse = SimpleNamespace(move=mock.AsyncMock(), wheel=mock.AsyncMock())
        self.capture = SimpleNamespace(news=asyncio.Event())
        self.heard = 0

    def scroll(self):
        self.scrolled_at.append(self.now)
        if self.grows:
            self.grows -= 1
            self.due.append(self.now + self.after)

    def arrive(self):
        """Move the pages whose time came: into the height for a DOM feed, else into the capture's count."""
        arrived = [when for when in self.due if when <= self.now]
        self.due = [when for when in self.due if when > self.now]
        if self.dom:
            self.height += 900 * len(arrived)
        else:
            self.heard += len(arrived)
        self.capture.count = self.heard

    async def evaluate(self, expression, arg=None):
        self.arrive()
        if expression == _browser.PAGE_HEIGHT:
            return self.height
        if expression == _browser.AT_BOTTOM:
            return self.bottom
        if expression == _browser.VIEWPORT:
            return [800, 900]
        if expression == _browser.SCROLL_BY:
            return None
        raise AssertionError(f"unexpected script {expression!r}")

    async def nap(self, seconds, news=None):
        self.clock.advance(seconds)
        self.arrive()

    @property
    def now(self):
        return self.clock.now


class ScrollTimingTest(BrowserCase):
    """The scroller's waits on a fake page and clock, so the timing rules are exact, repeat, and take no time."""

    def feed(self, *, style="fast", watch=False, **options):
        """A fake feed, and a scroller over it whose every scroll is one instant `FakeFeed.scroll`."""
        feed = FakeFeed(dom=not watch, **options)
        feed.arrive()
        self.enterContext(_time.use(feed.clock))
        page = _browser.Page(cast(Any, feed), cast(Any, None))
        page.random = random.Random(7)
        self.enterContext(mock.patch.object(page, "_nap", feed.nap))
        scroller = run(page.scroller(style=style, watch=cast(Any, feed.capture) if watch else None))

        async def move():
            feed.scroll()

        self.enterContext(mock.patch.object(scroller, "_move", move))
        return feed, scroller

    def scroll(self, scroller, until=None):
        async def steps():
            while not (until is not None and until()) and await scroller.step():
                pass

        run(steps())

    def gaps(self, feed):
        return [later - earlier for earlier, later in zip(feed.scrolled_at, feed.scrolled_at[1:])]

    def test_fast_scrolls_again_as_soon_as_the_page_grows(self):
        feed, scroller = self.feed(after=0.1, grows=20)
        self.scroll(scroller, upto(20))
        # 0.1 s for the page, give or take one check of its height.
        self.assertTrue(all(0.1 - 1e-9 <= gap <= 0.1 + _browser.POLL + 1e-9 for gap in self.gaps(feed)), self.gaps(feed))

    def test_a_matching_response_is_progress_before_the_page_grows(self):
        feed, scroller = self.feed(after=0.1, grows=20, watch=True)
        self.scroll(scroller, upto(20))
        self.assertTrue(all(0.1 - 1e-9 <= gap <= 0.1 + _browser.POLL + 1e-9 for gap in self.gaps(feed)), self.gaps(feed))
        self.assertEqual(feed.height, 1000)

    def test_a_slow_page_is_waited_for(self):
        feed, scroller = self.feed(after=2.0, grows=5)
        self.scroll(scroller, upto(5))
        self.assertTrue(all(2.0 - 1e-9 <= gap < 2.0 + 2 * _browser.POLL for gap in self.gaps(feed)), self.gaps(feed))
        self.assertEqual(feed.height, 1000 + 900 * 5)

    def test_a_quiet_bottom_ends_after_the_wait_and_the_idle_window(self):
        for style in ("fast", "human"):
            with self.subTest(style=style):
                feed, scroller = self.feed(after=0.1, grows=5, style=style)
                self.scroll(scroller)
                # The quiet starts with the sixth scroll, the first that brought nothing: a whole wait of 3 s,
                # then 2 s more, give or take a human pause.
                quiet = feed.now - feed.scrolled_at[5]
                self.assertGreaterEqual(quiet, _browser.WAIT + _browser.IDLE)
                self.assertLess(quiet, _browser.WAIT + _browser.IDLE + 1.5)

    def test_300_growing_scrolls_are_not_cut_off(self):
        feed, scroller = self.feed(after=0.1, grows=300)
        self.scroll(scroller)
        self.assertGreater(scroller.scrolls, 300)
        self.assertEqual(feed.height, 1000 + 900 * 300)

    def test_away_from_the_bottom_there_is_nothing_to_wait_for(self):
        feed, scroller = self.feed(grows=0, bottom=False)
        self.scroll(scroller, upto(10))
        self.assertEqual(scroller.scrolls, 10)
        self.assertTrue(all(gap == 0.0 for gap in self.gaps(feed)))

    def test_a_human_pause_is_a_look_after_new_media_and_a_glance_otherwise(self):
        looks, scroller = self.feed(after=0.05, grows=200, style="human")
        self.scroll(scroller, upto(200))
        glances, scroller = self.feed(grows=0, bottom=False, style="human")
        self.scroll(scroller, upto(200))
        for feed, median in ((looks, _browser.LOOK), (glances, _browser.GLANCE)):
            gaps = sorted(self.gaps(feed))
            # Lognormal around its median: the middle gap is near it, and some are well off it either way.
            self.assertAlmostEqual(gaps[len(gaps) // 2], median, delta=median * 0.15)
            self.assertLess(gaps[0], median * 0.7)
            self.assertGreater(gaps[-1], median * 1.4)
            self.assertLessEqual(gaps[-1], median * _browser.LONGEST + 1e-9)

    def test_seeded_run_is_repeatable(self):
        """Two runs from one seed scroll the same wheel steps, back-ups included, however busy the machine is."""
        def wheels():
            feed = FakeFeed(grows=0, bottom=False)
            self.enterContext(_time.use(feed.clock))
            page = _browser.Page(cast(Any, feed), cast(Any, None))
            page.random = random.Random(42)
            self.enterContext(mock.patch.object(page, "_nap", feed.nap))
            run(page.scroll(until=upto(60)))
            return [call.args for call in feed.mouse.wheel.call_args_list]

        first, second = wheels(), wheels()
        self.assertEqual(first, second)
        self.assertGreater(len(first), 60 * 2)
        self.assertTrue(any(delta < 0 for _, delta in first))

    def test_at_most_two_tabs_of_one_login_scroll_at_once_with_human(self):
        inside, most = 0, 0

        async def step(self):
            nonlocal inside, most
            inside += 1
            most = max(most, inside)
            await asyncio.sleep(0.01)
            inside -= 1
            return True

        async def four(style):
            gate = asyncio.Semaphore(_browser.HUMAN_TABS)
            scrollers = []
            for _ in range(4):
                page = _browser.Page(cast(Any, SimpleNamespace()), cast(Any, None))
                page.gate = gate
                scrollers.append(_browser._Scroller(page, None, idle=2.0, style=style, watch=None, height=0))
            await asyncio.gather(*(scroller.step() for scroller in scrollers))

        with mock.patch.object(_browser._Scroller, "_step", step):
            for style, allowed in (("human", 2), ("fast", 4)):
                most = 0
                run(four(style))
                self.assertEqual(most, allowed)


class GateTest(BrowserCase):
    """`RunBrowser.tab` gives the tabs of one login one gate, whoever held a tab before."""

    def test_one_gate_per_login(self):
        class Fake:
            async def tab(self, *, by_script=False):
                return _browser.Page(cast(Any, SimpleNamespace()), cast(Any, self), by_script=by_script)

            async def add_cookies(self, jar):
                pass

        shared = _browser.RunBrowser(LOOP)
        shared._browser = cast(Any, Fake())
        one, other = _cookies.Jar([]), _cookies.Jar([])
        first, second, third = (run(shared.tab(jar)) for jar in (one, one, other))
        self.assertIs(first.gate, second.gate)
        self.assertIsNot(first.gate, third.gate)
        run(shared.release([third]))
        self.assertIs(run(shared.tab(one)), third)
        self.assertIs(third.gate, first.gate)


class NavigationRaceTest(BrowserCase):
    """Reads that evaluate in the page survive "Execution context was destroyed" once or twice."""

    def page(self, failures):
        errors = [_browser._raw_error()("Page.content: Execution context was destroyed, most likely "
                                        "because of a navigation")] * failures
        loads = []

        async def content():
            if errors:
                raise errors.pop()
            return "<html><body><p id='x'>hi</p></body></html>"

        async def wait_for_load_state(state, timeout):  # noqa: ASYNC109 (mirrors Playwright's)
            loads.append(state)

        raw = SimpleNamespace(content=content, wait_for_load_state=wait_for_load_state)
        return _browser.Page(cast(Any, raw), cast(Any, None)), loads

    def test_a_read_that_hits_a_navigation_waits_for_the_load_and_reads_again(self):
        page, loads = self.page(1)
        self.assertIn("hi", run(page.content()))
        self.assertEqual(loads, ["load"])

    def test_a_page_that_keeps_navigating_warns_and_reads_as_empty(self):
        page, loads = self.page(10)
        self.assertEqual(run(page.content()), "<html></html>")
        self.assertEqual(len(loads), 2)
        self.assertTrue(any("kept navigating" in line for line in self.lines()))

    def test_another_error_is_not_swallowed(self):
        raw = SimpleNamespace(content=mock.AsyncMock(side_effect=_browser._raw_error()("Target closed")))
        page = _browser.Page(cast(Any, raw), cast(Any, None))
        with self.assertRaises(_browser._raw_error()):
            run(page.content())


class NoProfilesTest(unittest.TestCase):
    """The suite-wide guard (`tests/no_profiles.py`): no test may read or copy a real browser profile.

    The real home is left as it is here, so each call below would reach the account's real
    profiles; the reads are also faked to fail, so even a broken guard reads nothing.
    """

    def setUp(self):
        self.assertTrue(os.environ.get(_cookies.NO_PROFILES), "the guard is off under the suite")
        never = AssertionError("a real profile was about to be read")
        for patcher in (mock.patch.object(shutil, "copytree", side_effect=never),
                        mock.patch.object(_cookies, "_open_copy", side_effect=never),
                        mock.patch.object(_browser, "_start_playwright", side_effect=never)):
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_a_real_chrome_profile_is_neither_copied_nor_launched(self):
        with self.assertRaisesRegex(RuntimeError, _cookies.NO_PROFILES):
            run(_browser.launch(profile="Default", headless=True))
        with self.assertRaisesRegex(RuntimeError, _cookies.NO_PROFILES):
            run(_browser._open_context(cast(Any, None), _cookies._real_home() / "profile", "chrome",
                                       {"headless": True, "args": []}, {}))

    def test_real_browser_cookies_are_not_read(self):
        for browser in ("chrome", "firefox"):
            with self.subTest(browser=browser), self.assertRaisesRegex(RuntimeError, _cookies.NO_PROFILES):
                _cookies.load(browser)

    def test_a_fake_home_in_a_temporary_folder_is_allowed_and_the_guard_is_off_without_the_variable(self):
        with tempfile.TemporaryDirectory() as folder:
            _cookies.refuse_profile(Path(folder) / "Default")
        with mock.patch.dict(os.environ, {_cookies.NO_PROFILES: ""}):
            _cookies.refuse_profile(_cookies._real_home() / "Default")


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
        fake = _time.Fake(start=5.0)

        async def now():
            return _time.now()

        with _time.use(fake):
            self.assertEqual(run(now()), 5.0)


# Every byte value, so a body that went through text anywhere would come back changed.
BINARY_BODY = bytes(range(256)) * 300
# The header a test sends, and checks no error message ever shows.
SECRET = "Bearer s3cr3t-token-value"


class LocalHandler(BaseHTTPRequestHandler):
    """`LocalServer`'s routes: a page under /app/, bodies of each kind, statuses, an echo and a slow answer."""

    def do_GET(self):
        self.answer()

    def do_POST(self):
        self.answer()

    def answer(self):
        path, _, query = self.path.partition("?")
        if path == "/app/":
            return self.send(200, "text/html", b"<!doctype html><title>Local</title>")
        if path in ("/json", "/app/data/json"):
            return self.send(200, "application/json", json.dumps({"path": path, "query": query}).encode())
        if path == "/text":
            return self.send(200, "text/plain; charset=utf-8", "héllo\r\nwörld".encode())
        if path == "/binary":
            return self.send(200, "application/octet-stream", BINARY_BODY)
        if path == "/echo":
            length = int(self.headers.get("content-length") or 0)
            return self.send(200, "application/json", json.dumps({
                "method": self.command, "body": self.rfile.read(length).decode("latin-1"),
                "headers": {name.lower(): value for name, value in self.headers.items()}}).encode())
        if path.startswith("/status/"):
            status = int(path.removeprefix("/status/"))
            return self.send(status, "text/plain", b"no", {"Retry-After": "7"} if status == 429 else {})
        if path == "/slow":
            # Held until the test ends (or 10 s), so the page's abort is what ends the request.
            cast(_LocalHTTPServer, self.server).release.wait(10)
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
        # The page aborted (the timeout test): nobody is reading any more.
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            pass

    def log_message(self, format, *args):
        """Quiet: the default writes every request to stderr."""


class _LocalHTTPServer(ThreadingHTTPServer):
    release: threading.Event


class LocalServer:
    """An HTTP server on 127.0.0.1 and a free port, serving `LocalHandler` from a thread until `close`."""

    def __init__(self):
        self.server = _LocalHTTPServer(("127.0.0.1", 0), LocalHandler)
        self.server.daemon_threads = True
        self.server.release = threading.Event()
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def close(self):
        self.server.release.set()
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()


class FetchTest(PageCase):
    """`Page.fetch`: a request sent by the page itself, to a local server, returned as an `_net.Response`."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.server = LocalServer()

    @classmethod
    def tearDownClass(cls):
        cls.server.close()
        super().tearDownClass()

    def setUp(self):
        BrowserCase.setUp(self)
        self.page = run(self.browser.tab())
        run(self.page.goto(f"{self.server.url}/app/"))
        self.addCleanup(lambda: run(self.page.close()))

    def fetch(self, url, **options):
        return run(self.page.fetch(url, **options))

    def test_a_json_body(self):
        response = self.fetch(f"{self.server.url}/json?page=2")
        self.assertIsInstance(response, _net.Response)
        self.assertEqual((response.status, response.ok, response.url), (200, True, f"{self.server.url}/json?page=2"))
        self.assertEqual(response.headers["Content-Type"], "application/json")
        self.assertEqual(response.json, {"path": "/json", "query": "page=2"})

    def test_a_text_body_keeps_its_bytes(self):
        response = self.fetch("/text")
        self.assertEqual(response.content, "héllo\r\nwörld".encode())
        self.assertEqual(response.text, "héllo\r\nwörld")

    def test_a_binary_body_comes_back_byte_for_byte(self):
        self.assertEqual(self.fetch("/binary").content, BINARY_BODY)

    def test_a_relative_url_is_resolved_against_the_page(self):
        response = self.fetch("data/json")
        self.assertEqual(response.url, f"{self.server.url}/app/data/json")
        self.assertEqual(response.json["path"], "/app/data/json")

    def test_post_with_a_json_body(self):
        echo = self.fetch("/echo", method="post", body={"id": 1, "name": "example_user"}).json
        self.assertEqual(echo["method"], "POST")
        self.assertEqual(json.loads(echo["body"]), {"id": 1, "name": "example_user"})
        self.assertEqual(echo["headers"]["content-type"], "application/json")

    def test_post_with_bytes_or_a_form_as_text(self):
        raw = self.fetch("/echo", method="POST", body=b"\x00\xff\n").json
        self.assertEqual(raw["body"], "\x00\xff\n")
        form = self.fetch("/echo", method="POST", body="q=a+b&n=2",
                          headers={"Content-Type": "application/x-www-form-urlencoded"}).json
        self.assertEqual(form["body"], "q=a+b&n=2")

    def test_a_get_takes_a_dict_body_as_its_query(self):
        response = self.fetch("/json?page=2", body={"n": 3})
        self.assertEqual(response.json["query"], "page=2&n=3")

    def test_headers_are_passed_through_with_the_pages_origin(self):
        echo = self.fetch("/echo", headers={"X-Csrf": "token-1", "Accept": "application/json"}).json
        self.assertEqual(echo["headers"]["x-csrf"], "token-1")
        self.assertEqual(echo["headers"]["accept"], "application/json")
        self.assertEqual(echo["headers"]["referer"], f"{self.server.url}/app/")

    def test_the_pages_cookies_are_sent(self):
        run(self.page.evaluate("document.cookie = 'sid=example-session; path=/'"))
        self.assertEqual(self.fetch("/echo").json["headers"]["cookie"], "sid=example-session")

    def test_a_404_is_refused_and_unchecked_fetch_returns_it(self):
        with self.assertRaises(SiteError) as caught:
            self.fetch("/status/404?token=abc", headers={"Authorization": SECRET})
        self.assertEqual(caught.exception.status, 404)
        self.assertEqual(str(caught.exception), f"GET {self.server.url}/status/404: HTTP 404")
        self.assertEqual(self.fetch("/status/404", check=False).status, 404)

    def test_a_429_is_rate_limited_with_retry_after(self):
        with self.assertRaises(Blocked) as caught:
            self.fetch("/status/429")
        self.assertEqual(caught.exception.wait, 7.0)
        self.assertEqual(caught.exception.status, 429)
        self.assertEqual(self.fetch("/status/429", check=False).status, 429)

    def test_a_500_is_a_retryable_site_error(self):
        with self.assertRaises(SiteError) as caught:
            self.fetch("/status/500")
        self.assertIn("500", str(caught.exception))

    def test_the_timeout_aborts_the_request_in_the_page(self):
        started = time.monotonic()
        with self.assertRaises(NetworkError) as caught, mock.patch.object(_browser, "FETCH_TIMEOUT", 0.5):
            self.fetch("/slow?token=abc", headers={"Authorization": SECRET})
        self.assertLess(time.monotonic() - started, 5)
        self.assertEqual(str(caught.exception), f"GET {self.server.url}/slow: no answer within 0.5 s")
        # The page is still usable afterwards.
        self.assertEqual(self.fetch("/json").status, 200)

    def test_a_request_the_browser_refuses_names_no_header_value(self):
        with self.assertRaises(Error) as caught:
            # A NUL byte makes the value invalid: the browser's TypeError must not show it, whatever its wording.
            self.fetch("/json", headers={"X-Token": SECRET + "\x00"})
        self.assertNotIn("s3cr3t", str(caught.exception))
        self.assertTrue(str(caught.exception).startswith(f"GET {self.server.url}/json: TypeError"), caught.exception)


if __name__ == "__main__":
    unittest.main()
