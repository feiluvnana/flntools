"""`shared.browser`: `open` and its profiles, the Chromium offer, `Browser`, `Page` and `Subscription`.

The profile and Chromium tests fake Playwright, the home folder and the temp folder, so no real
profile or browser is touched. The page tests run Playwright's own Chromium, headless, on pages
served from memory (`fulfill`), and skip without it. `Page.fetch`'s tests serve their page from a
real HTTP server on 127.0.0.1 (`LocalServer`), since a request the page sends itself must leave it. The scroll timing tests fake the page and the clock.
"""

import importlib
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from io import StringIO
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from shared import cookies, http, log, theme
from shared import _browser as browser
from shared._theme import Screen

# The real lookup, before BrowserCase patches it out.
CHROME_INSTALLED = browser._chrome_installed


def chromium_installed() -> bool:
    """Whether Playwright's own Chromium is downloaded; checked without open, which could ask to download it."""
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        return False
    with sync_playwright() as playwright:
        return Path(playwright.chromium.executable_path).is_file()


HAS_CHROMIUM = chromium_installed()
needs_chromium = unittest.skipUnless(HAS_CHROMIUM, "Chromium is not installed")

# Items are 300px tall and come five at a time when the page is scrolled near its end. With
# ?jiggle=1, more only come after the reader went up at least half a screen since the last load.
FEED_PAGE = """<!doctype html><html><body style="margin:0">
<div id="feed"></div>
<script>
  const params = new URLSearchParams(location.search);
  const cap = Number(params.get("cap") || 40), jiggle = params.get("jiggle") === "1";
  const feed = document.getElementById("feed");
  window.wheels = [];
  let lowest = 0, wentUp = false;
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
  if (params.get("fetch") === "1") addEventListener("wheel", () => fetch("/api/other"));
  addEventListener("scroll", () => {
    lowest = Math.max(lowest, scrollY);
    if (scrollY < lowest - innerHeight / 2) wentUp = true;
    if (scrollY + innerHeight < document.body.scrollHeight - 10) return;
    if (jiggle && !wentUp) return;
    add(5);
    wentUp = false;
    lowest = scrollY;
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
    "/api/html": ("text/html", "<p>not json</p>"),
    "/api/stream": ("application/x-ndjson", '{"line": 1}\n\n{"line": 2}\n'),
}


def fulfill(route):
    """Serves https://site.test/ from memory, so no test ever touches the network."""
    url = route.request.url
    if url.startswith("https://bad.test/"):
        route.abort()
        return
    path = url.removeprefix("https://site.test").split("?")[0]
    if path == "/feed":
        route.fulfill(status=200, content_type="text/html", body=FEED_PAGE)
        return
    if path == "/box":
        route.fulfill(status=200, content_type="text/html", body=BOX_PAGE)
        return
    if path == "/missing":
        route.fulfill(status=404, content_type="text/html", body="<p>not here</p>")
        return
    content_type, body = API_BODIES.get(path, ("text/html", "<html><head><title>Home</title></head><body>home</body></html>"))
    route.fulfill(status=200, content_type=content_type, body=body)


class BrowserCase(unittest.TestCase):
    def setUp(self):
        self.screen = Screen(dest=StringIO(), inp=StringIO(""), width=300, tty=False, interactive=False)
        self.scope = theme.scope(screen=self.screen, color="none", unicode=True)
        self.scope.__enter__()
        self.previous_level = log.level
        # The bundled build, so results don't depend on whether this machine has Chrome installed,
        # and no earlier test's "no" to the Chromium download, or failed download, is remembered.
        for patcher in (mock.patch.object(browser, "_chrome_installed", return_value=False),
                        mock.patch.object(browser, "_chromium_declined", False),
                        mock.patch.object(browser, "_chromium_failed", None)):
            patcher.start()
            self.addCleanup(patcher.stop)

    def tearDown(self):
        log.level = self.previous_level
        self.scope.__exit__(None, None, None)

    def lines(self) -> list[str]:
        return self.screen.dest.getvalue().splitlines()


class ImportTest(BrowserCase):
    def test_without_playwright(self):
        blocked = {"playwright": None, "playwright.sync_api": None}
        with mock.patch.dict(sys.modules, blocked):
            sys.modules.pop("shared._browser", None)
            fresh = importlib.import_module("shared._browser")
            self.assertFalse(hasattr(fresh, "chrome_user_agent"))
            with self.assertRaisesRegex(ImportError, re.escape("pip install 'flntools[browser]'")):
                fresh.open()

    def test_cdp_refuses_launch_options(self):
        for options in ({"headless": True}, {"profile": "x"}, {"args": ["--x"]},
                        {"stealth": False}, {"locale": "fr-FR"}, {"timezone": "Europe/Paris"}):
            with self.assertRaises(ValueError, msg=options):
                browser.open(cdp="http://127.0.0.1:9", **options)


class ProfileCopyTest(BrowserCase):
    """`open(profile=<name>)`: which profiles are names, and the copy, which holds Telegram Web's login,
    never staying in the temp folder (fix #14). A fake home holds the profiles and a private temp
    folder the copies, so no real profile is touched. No browser."""

    def setUp(self):
        super().setUp()
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.home = Path(folder.name) / "home"
        platform = sys.platform if sys.platform in ("darwin", "win32") else "linux"
        self.root = self.home / browser.PROFILE_ROOTS[platform]
        (self.root / "Default").mkdir(parents=True)
        (self.root / "Default" / "Preferences").write_text("{}")
        self.temp = Path(folder.name) / "temp"
        self.temp.mkdir()
        for patcher in (mock.patch.object(Path, "home", return_value=self.home),
                        mock.patch.object(tempfile, "tempdir", str(self.temp))):
            patcher.start()
            self.addCleanup(patcher.stop)
        self.playwright = mock.Mock()
        self.launches = []

    def copies(self) -> list[Path]:
        return list(self.temp.glob("shared-chrome-*"))

    def open_context(self, playwright, profile, channel, launch_options, context_options):
        """Records what _launch asked for; the copy is checked while it exists."""
        profile = Path(profile)
        self.launches.append((profile, channel, launch_options["args"][-1],
                              stat.S_IMODE(profile.stat().st_mode), (profile / "Default" / "Preferences").is_file()))
        return mock.Mock(), [playwright.stop]

    def launch(self, **options):
        with mock.patch.object(browser, "_start_playwright", return_value=self.playwright), \
                mock.patch.object(browser, "_open_context", self.open_context):
            return browser.open(**options)

    def test_names_and_folders(self):
        for profile in ("Default", "Profile 1", "Person 2"):
            self.assertEqual(browser._profile_name(profile), profile)
        for profile in (None, Path("Default"), "./Default", "profiles/Default", "/tmp/profile", ".", ".."):
            self.assertIsNone(browser._profile_name(profile), profile)
        with self.assertRaises(ValueError):
            browser._profile_name("")

    def test_a_name_is_copied_privately_into_chrome_and_deleted_on_close(self):
        chrome = self.launch(profile="Default")
        (copy, channel, last_arg, mode, has_preferences), = self.launches
        self.assertEqual((channel, last_arg, has_preferences), ("chrome", "--profile-directory=Default", True))
        self.assertTrue(copy.is_relative_to(self.temp))
        if sys.platform != "win32":
            # mkdtemp's mode: no other user of the machine can read the copy's logins.
            self.assertEqual(mode, 0o700)
        self.assertEqual(self.copies(), [copy])
        chrome.close()
        self.assertEqual(self.copies(), [])

    def test_a_folder_is_used_as_it_is_and_never_deleted(self):
        folder = self.home / "kept"
        folder.mkdir()
        with mock.patch.object(browser, "_channel", return_value="chromium"):
            for profile in (folder, str(folder)):
                self.launch(profile=profile).close()
        self.assertEqual([(path, channel) for path, channel, *_ in self.launches],
                         [(folder, "chromium"), (folder, "chromium")])
        self.assertEqual(self.copies(), [])

    def test_an_unknown_name_raises_before_playwright_starts(self):
        with mock.patch.object(browser, "_start_playwright") as start:
            with self.assertRaisesRegex(FileNotFoundError, "no Chrome profile named 'Profile 9'"):
                browser.open(profile="Profile 9")
        start.assert_not_called()
        self.assertEqual(self.copies(), [])

    def test_copy_deleted_without_playwright(self):
        with mock.patch.object(browser, "_start_playwright", side_effect=ImportError("no playwright")):
            with self.assertRaises(ImportError):
                browser.open(profile="Default")
        self.assertEqual(self.copies(), [])

    def test_copy_deleted_on_ctrl_c_before_launch(self):
        with mock.patch.object(browser, "_start_playwright", side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                browser.open(profile="Default")
        self.assertEqual(self.copies(), [])

    def test_copy_deleted_when_the_launch_fails(self):
        for error in (KeyboardInterrupt(), RuntimeError(browser.NEEDS_CHROME)):
            with self.subTest(error=error):
                playwright = mock.Mock()
                with mock.patch.object(browser, "_start_playwright", return_value=playwright), \
                        mock.patch.object(browser, "_open_context", side_effect=error):
                    with self.assertRaises(type(error)):
                        browser.open(profile="Default")
                self.assertEqual(self.copies(), [])
                playwright.stop.assert_called_once()

    def test_copy_deleted_when_the_copy_fails(self):
        """A copy that fails half way, Ctrl-C included, must not leave the profile's logins in the temp folder."""
        for error in (OSError("disk full"), KeyboardInterrupt()):
            with self.subTest(error=error):
                with mock.patch.object(shutil, "copytree", side_effect=error) as copytree:
                    with self.assertRaises(type(error)):
                        browser.open(profile="Default")
                # The copy was started inside the private temp folder, so an empty glob means it was deleted.
                self.assertTrue(Path(copytree.call_args.args[1]).is_relative_to(self.temp))
                self.assertEqual(self.copies(), [])

    def test_no_copy_when_cdp_is_misused(self):
        with self.assertRaises(ValueError):
            browser.open(profile="Default", cdp="http://127.0.0.1:9")
        self.assertEqual(self.copies(), [])


class FakeInstaller:
    """A finished `subprocess.Popen` of Playwright's installer, with its output and exit code."""

    def __init__(self, output: str, returncode: int):
        self.stdout, self.returncode = StringIO(output), returncode

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        return None


class ChromeOnlyTest(BrowserCase):
    """Which browser open launches, and the Chromium download offered without Chrome. No browser starts."""

    def setUp(self):
        super().setUp()
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        # Where Playwright would keep its Chromium: nothing there until the fake installer runs.
        self.chromium = Path(folder.name) / "chromium-1" / "chrome"
        self.playwright = mock.Mock()
        self.playwright.chromium.executable_path = str(self.chromium)
        self.installs = []
        self.launched = []
        self.context = mock.Mock()
        for patcher in (mock.patch.object(browser, "_start_playwright", return_value=self.playwright),
                        mock.patch.object(browser, "_open_context", self.open_context),
                        mock.patch.object(browser.subprocess, "Popen", self.popen),
                        mock.patch("playwright._impl._driver.compute_driver_executable",
                                   return_value=("/driver/node", "/driver/cli.js"))):
            patcher.start()
            self.addCleanup(patcher.stop)

    def open_context(self, playwright, profile, channel, launch_options, context_options):
        self.launched.append(channel)
        return self.context, [playwright.stop]

    def popen(self, argv, **options):
        """Playwright's installer: it prints progress lines, then the browser is there."""
        self.installs.append((argv, options))
        self.chromium.parent.mkdir(parents=True)
        self.chromium.write_text("")
        output = "|■■■■      |  40% of 167.3 MiB\n|■■■■■■■■■■| 100% of 167.3 MiB\nChromium downloaded\n"
        return FakeInstaller(output, 0)

    def terminal(self, *keys):
        self.screen._interactive = True
        self.screen.readkey = iter(keys).__next__

    def test_chrome_is_launched_when_installed(self):
        with mock.patch.object(browser, "_chrome_installed", return_value=True):
            browser.open().close()
        self.assertEqual((self.launched, self.installs), (["chrome"], []))

    def test_a_downloaded_chromium_is_used_without_asking(self):
        self.chromium.parent.mkdir(parents=True)
        self.chromium.write_text("")
        self.terminal()   # Any question would read a key, and there is none.
        browser.open().close()
        self.assertEqual((self.launched, self.installs), (["chromium"], []))

    def test_yes_downloads_chromium_then_launches_it(self):
        self.terminal("y")
        browser.open().close()
        self.assertEqual(self.launched, ["chromium"])
        (argv, options), = self.installs
        self.assertEqual(argv, ["/driver/node", "/driver/cli.js", "install", "--no-shell", "chromium"])
        self.assertEqual(options["env"]["PW_LANG_NAME"], "python")
        # Into Playwright's user cache: the installer's folder is left to Playwright.
        self.assertNotIn("PLAYWRIGHT_BROWSERS_PATH", {key: value for key, value in options["env"].items()
                                                      if key not in os.environ})
        out = self.screen.dest.getvalue()
        self.assertIn(f"✓ {browser.CHROMIUM_QUESTION} Yes", out)
        self.assertIn("✓ " + "━" * 20 + " Downloading Chromium " + "100", out)
        # A later run finds it and doesn't ask: the screen has no key left to answer with.
        browser.open().close()
        self.assertEqual((self.launched, len(self.installs)), (["chromium", "chromium"], 1))

    def test_no_raises_naming_chrome_and_is_asked_once(self):
        self.terminal("n")
        for _ in range(2):
            with self.assertRaises(RuntimeError) as caught:
                browser.open()
            self.assertEqual(str(caught.exception), "flnd needs Google Chrome: install it from https://www.google.com/chrome/")
        self.assertEqual(self.screen.dest.getvalue().count(f"✓ {browser.CHROMIUM_QUESTION} No"), 1)
        self.assertEqual((self.launched, self.installs), ([], []))
        self.assertEqual(self.playwright.stop.call_count, 2)

    def test_without_a_terminal_it_raises_naming_chrome(self):
        with self.assertRaisesRegex(RuntimeError, "^flnd needs Google Chrome: install it from"):
            browser.open()
        self.assertEqual((self.launched, self.installs), ([], []))
        self.assertNotIn(browser.CHROMIUM_QUESTION, self.screen.dest.getvalue())

    def test_check_asks_up_front_and_open_then_launches_without_asking(self):
        self.terminal("y")
        browser.check()
        self.assertEqual((self.launched, len(self.installs)), ([], 1))
        self.playwright.stop.assert_called_once()
        # The screen has no key left: open finds the Chromium check downloaded, and asks nothing.
        browser.open().close()
        self.assertEqual(self.launched, ["chromium"])

    def test_check_with_chrome_starts_nothing(self):
        with mock.patch.object(browser, "_chrome_installed", return_value=True):
            browser.check()
        self.assertEqual((self.playwright.stop.call_count, self.installs), (0, []))

    def test_a_no_to_check_is_kept_by_open(self):
        self.terminal("n")
        with self.assertRaisesRegex(RuntimeError, "^flnd needs Google Chrome"):
            browser.check()
        with self.assertRaisesRegex(RuntimeError, "^flnd needs Google Chrome"):
            browser.open()
        self.assertEqual(self.screen.dest.getvalue().count(f"✓ {browser.CHROMIUM_QUESTION} No"), 1)

    def test_a_failed_download(self):
        self.terminal("y")
        with mock.patch.object(browser.subprocess, "Popen", return_value=FakeInstaller("Error: ENOSPC\n", 1)), \
                self.assertRaisesRegex(RuntimeError, "installer exited with 1: Error: ENOSPC"):
            browser.open()
        self.assertEqual(self.launched, [])
        self.assertIn("✗ " + "─" * 20 + " Downloading Chromium " + "0/100", self.screen.dest.getvalue())

    def test_a_failed_download_is_raised_again_without_asking(self):
        asked = []
        failure = RuntimeError("Chromium was not downloaded: Playwright's installer exited with 1: Error: ENOSPC")
        with mock.patch.object(browser.prompt, "confirm", lambda *args, **options: asked.append(args) or True), \
                mock.patch.object(browser, "_install_chromium", side_effect=failure) as install:
            for _ in range(2):
                with self.assertRaises(RuntimeError) as caught:
                    browser.open()
                self.assertIs(caught.exception, failure)
        self.assertEqual((len(asked), install.call_count, self.launched), (1, 1, []))

    def test_two_threads_at_once_ask_once_and_download_once(self):
        # Each source's session opens its browser on its own thread; both find Chromium missing together.
        asked, installs, channels = [], [], []
        start = threading.Barrier(2)

        def confirm(*args, **options):
            asked.append(args)
            # Long enough for the other thread to reach the question too, were nothing holding it back.
            time.sleep(0.1)
            return True

        def install():
            installs.append(1)
            self.chromium.parent.mkdir(parents=True)
            self.chromium.write_text("")

        def open_one():
            start.wait()
            channels.append(browser._channel(self.playwright))

        with mock.patch.object(browser.prompt, "confirm", confirm), \
                mock.patch.object(browser, "_install_chromium", install):
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
                with mock.patch.object(browser.sys, "platform", platform), mock.patch.dict(os.environ, env):
                    self.assertFalse(CHROME_INSTALLED())
        self.assertEqual([Path(path) for path in found], [
            Path("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"), Path("/opt/google/chrome/chrome"),
            Path("C:/Users/a/AppData/Local/Google/Chrome/Application/chrome.exe"),
            Path("C:/PF/Google/Chrome/Application/chrome.exe")])


@needs_chromium
class ChannelTest(unittest.TestCase):
    """The channel a headless launch passes, pinned without a real browser."""

    def launched(self, channel, *, profile):
        playwright = mock.Mock()
        options = {"headless": True, "args": []}
        with mock.patch.object(browser, "_headful_user_agent", return_value="Chrome/1"):
            browser._open_context(playwright, profile, channel, options, {})
        chromium = playwright.chromium
        return [call.kwargs["channel"] for call in chromium.launch.call_args_list + chromium.launch_persistent_context.call_args_list]

    def test_bundled_chromium_is_launched_through_the_chromium_channel(self):
        # With no channel, Playwright would run the headless shell, which flnd doesn't install.
        for profile in (None, "profile"):
            self.assertEqual(self.launched(None, profile=profile)[-1], "chromium")
            self.assertEqual(set(self.launched("chromium", profile=profile)), {"chromium"})

    def test_installed_chrome_keeps_its_channel(self):
        for profile in (None, "profile"):
            self.assertEqual(set(self.launched("chrome", profile=profile)), {"chrome"})


class OpenTest(BrowserCase):
    def setUp(self):
        super().setUp()
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)

    def test_incognito_leaves_nothing_behind(self):
        with browser.open(headless=True) as b:
            b.add_cookies([{"name": "sid", "value": "1", "domain": "site.test", "expires": time.time() + 3600}])
            self.assertEqual(len(b.cookies), 1)
        with browser.open(headless=True) as b:
            self.assertEqual(len(b.cookies), 0)

    def test_headless_reports_its_own_user_agent(self):
        with browser.open(headless=True) as b:
            agent = b.open().raw.evaluate("navigator.userAgent")
        self.assertIn("Chrome/", agent)
        self.assertNotIn("Headless", agent)
        with browser.open(headless=True, profile=Path(self.folder.name) / "profile") as b:
            self.assertEqual(b.open().raw.evaluate("navigator.userAgent"), agent)

    def test_locale_and_timezone(self):
        read = "[navigator.language, Intl.DateTimeFormat().resolvedOptions().timeZone]"
        with browser.open(headless=True, locale="fr-FR", timezone="Europe/Paris") as b:
            self.assertEqual(b.open().raw.evaluate(read), ["fr-FR", "Europe/Paris"])
        with browser.open(headless=True, profile=Path(self.folder.name) / "profile", locale="de-DE", timezone="Asia/Tokyo") as b:
            self.assertEqual(b.open().raw.evaluate(read), ["de-DE", "Asia/Tokyo"])

    def test_profile_keeps_a_cookie(self):
        profile = Path(self.folder.name) / "profile"
        with browser.open(headless=True, profile=profile) as b:
            b.add_cookies([{"name": "sid", "value": "1", "domain": "site.test", "expires": time.time() + 3600}])
        with browser.open(headless=True, profile=profile) as b:
            self.assertEqual([cookie["name"] for cookie in b.cookies], ["sid"])

    def test_an_installed_profile_is_copied_and_deleted(self):
        home = Path(self.folder.name)
        platform = sys.platform if sys.platform in ("darwin", "win32") else "linux"
        root = home / browser.PROFILE_ROOTS[platform]
        (root / "Profile 1" / "Cache").mkdir(parents=True)
        (root / "Profile 1" / "Preferences").write_text("{}")
        (root / "Profile 1" / "Cache" / "big").write_text("cache")
        (root / "Local State").write_text("{}")
        temp = home / "temp"
        temp.mkdir()
        # The bundled Chromium stands in for real Chrome.
        with mock.patch.object(Path, "home", return_value=home), mock.patch.object(tempfile, "tempdir", str(temp)), \
                mock.patch.object(browser, "PROFILE_CHANNEL", None):
            with browser.open(headless=True, profile="Profile 1") as b:
                copy, = temp.glob("shared-chrome-*")
                self.assertTrue((copy / "Default" / "Preferences").is_file())
                self.assertTrue((copy / "Local State").is_file())
                # Chromium makes its own Cache once it runs; the profile's wasn't copied into it.
                self.assertFalse((copy / "Default" / "Cache" / "big").exists())
                self.assertEqual(b.open().url, "about:blank")
        self.assertFalse(copy.exists())

    def test_cdp_attaches_and_leaves_it_running(self):
        user_data = Path(self.folder.name) / "chromium"
        chromium = subprocess.Popen([chromium_path(), "--headless=new", "--remote-debugging-port=0",
                                     f"--user-data-dir={user_data}", "about:blank"],
                                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.addCleanup(chromium.wait)
        self.addCleanup(chromium.terminate)
        endpoint = f"http://127.0.0.1:{debugging_port(user_data)}"
        with browser.open(cdp=endpoint, viewport=(700, 500)) as b:
            page = b.open()
            self.assertEqual(page.raw.evaluate("[innerWidth, innerHeight]"), [700, 500])
            b.add_cookies([{"name": "sid", "value": "1", "domain": "site.test"}])
            self.assertEqual([cookie["name"] for cookie in b.cookies], ["sid"])
        self.assertIsNone(chromium.poll())
        with urllib.request.urlopen(f"{endpoint}/json/version") as response:
            self.assertEqual(response.status, 200)

    def test_args_are_added(self):
        error = browser.PlaywrightError("stop here")
        with mock.patch("playwright.sync_api._generated.BrowserType.launch", side_effect=error) as launch:
            with self.assertRaises(browser.PlaywrightError):
                browser.open(headless=True, args=["--no-sandbox"])
        self.assertEqual(launch.call_args.kwargs["args"], [*browser.LAUNCH_ARGS, "--no-sandbox"])

    def test_closed_after_an_exception(self):
        with self.assertRaises(ValueError):
            with browser.open(headless=True) as b:
                chromium = b.raw.browser
                page = b.open()
                raise ValueError("boom")
        self.assertTrue(page.raw.is_closed())
        self.assertFalse(chromium.is_connected())

    def test_webdriver_hidden(self):
        with browser.open(headless=True) as b:
            self.assertIsNone(b.open().raw.evaluate("navigator.webdriver"))
        with browser.open(headless=True, stealth=False) as b:
            self.assertIs(b.open().raw.evaluate("navigator.webdriver"), False)


def chromium_path() -> str:
    from playwright.sync_api import sync_playwright
    with sync_playwright() as playwright:
        return playwright.chromium.executable_path


def debugging_port(user_data: Path) -> str:
    """Chromium writes the port it picked for --remote-debugging-port=0 into its profile folder."""
    port_file = user_data / "DevToolsActivePort"
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        if port_file.is_file() and port_file.read_text().strip():
            return port_file.read_text().split()[0]
        time.sleep(0.05)
    raise TimeoutError("Chromium did not open its debugging port")


@needs_chromium
class PageCase(BrowserCase):
    """One browser for the whole class; each test gets a fresh tab."""

    @classmethod
    def setUpClass(cls):
        cls.browser = browser.open(headless=True, viewport=(800, 900))
        cls.browser.raw.route("https://site.test/**", fulfill)
        cls.browser.raw.route("https://bad.test/**", fulfill)

    @classmethod
    def tearDownClass(cls):
        cls.browser.close()

    def setUp(self):
        super().setUp()
        self.page = self.browser.open("https://site.test/")
        self.addCleanup(self.page.close)


class BrowserTest(PageCase):
    def test_open_page_with_and_without_url(self):
        self.assertEqual(self.browser.open().url, "about:blank")
        self.assertEqual(self.page.url, "https://site.test/")
        failed = self.browser.open("https://bad.test/")
        self.assertEqual(failed.url, "about:blank")
        self.assertTrue(self.lines()[0].startswith("! could not open https://bad.test/: "))
        self.assertIs(failed.browser, self.browser)

    def test_pages_lists_every_tab(self):
        before = len(self.browser.pages)
        with self.page.raw.context.expect_page():
            self.page.raw.evaluate("window.open('about:blank')")
        pages = self.browser.pages
        self.assertEqual(len(pages), before + 1)
        self.assertIn(self.page, pages)
        self.assertIs(self.browser.pages[-1], pages[-1])
        pages[-1].close()

    def test_cookies(self):
        self.browser.add_cookies(cookies.Jar([{"name": "sid", "value": "1", "domain": ".site.test", "secure": True},
                                              {"name": "csrf", "value": "2", "domain": "site.test", "path": "/api"}]))
        jar = self.browser.cookies
        self.assertIsInstance(jar, cookies.Jar)
        stored = {cookie["name"]: (cookie["domain"], cookie["path"]) for cookie in jar}
        self.assertEqual(stored, {"sid": (".site.test", "/"), "csrf": ("site.test", "/api")})
        self.assertEqual(jar.filter(names="sid").header, "sid=1")
        self.browser.clear_cookies()
        self.assertEqual(len(self.browser.cookies), 0)

    def test_one_bad_cookie_among_good_ones(self):
        self.browser.add_cookies([{"name": "bad", "value": "s3cr3t-value"},
                                  {"name": "good", "value": "1", "domain": "site.test"}])
        self.assertEqual([cookie["name"] for cookie in self.browser.cookies], ["good"])
        self.assertEqual(len(self.lines()), 1)
        self.assertTrue(self.lines()[0].startswith("! cookie bad was rejected: "))
        self.assertNotIn("s3cr3t-value", self.lines()[0])
        self.browser.clear_cookies()


class PageTest(PageCase):
    def fetch(self, path):
        with self.page.raw.expect_response(lambda response: response.url.endswith(path)):
            self.page.raw.evaluate("path => fetch(path).then(response => response.text())", path)

    def test_goto_returns_the_status(self):
        self.assertEqual(self.page.goto("https://site.test/other", idle=True), 200)
        self.assertEqual(self.page.goto("https://site.test/missing"), 404)
        self.assertIsNone(self.page.goto("https://bad.test/"))
        self.assertEqual(len(self.lines()), 1)

    def test_on_response_each_match_form(self):
        for match in ("graphql", re.compile(r"/api/graph"), lambda url: url.endswith("graphql")):
            seen = []
            with self.page.on_response(match, lambda parsed, response: seen.append((parsed, response.status))):
                self.fetch("/api/graphql")
                self.fetch("/api/other")
            self.fetch("/api/graphql")
            self.assertEqual(seen, [({"data": {"n": 1}}, 200)], match)

    def test_a_match_of_another_type_raises(self):
        with self.assertRaises(TypeError):
            self.page.on_response(3, lambda parsed, response: None)  # type: ignore[arg-type]

    def test_on_response_skips_bodies_that_are_not_json(self):
        log.level = "debug"
        seen = []
        self.page.on_response("/api/", lambda parsed, response: seen.append(parsed))
        self.fetch("/api/html")
        self.fetch("/api/other")
        self.assertEqual(seen, [{"other": True}])
        self.assertEqual(self.lines(), ["· on_response: skipped a body that is not JSON from https://site.test/api/html"])

    def test_on_response_lines(self):
        seen = []
        self.page.on_response("stream", lambda parsed, response: seen.append(parsed), lines=True)
        self.fetch("/api/stream")
        self.assertEqual(seen, [{"line": 1}, {"line": 2}])

    def test_on_response_handler_exception_keeps_later_responses(self):
        seen = []

        def handler(parsed, response):
            if "data" in parsed:
                raise KeyError("edges")
            seen.append(parsed)

        self.page.on_response("/api/", handler)
        self.fetch("/api/graphql")
        self.fetch("/api/other")
        self.assertEqual(seen, [{"other": True}])
        self.assertEqual(self.lines(), ["! on_response handler failed for https://site.test/api/graphql: KeyError 'edges'"])

    def test_on_request_and_stop(self):
        urls = []
        watching = self.page.on_request(lambda url: "/api/" in url, lambda request: urls.append(request.url))
        self.assertIsInstance(watching, browser.Subscription)
        self.fetch("/api/other")
        watching.stop()
        watching.stop()   # a second stop does nothing
        self.fetch("/api/graphql")
        self.assertEqual(urls, ["https://site.test/api/other"])

    def test_wait(self):
        self.page.raw.evaluate("setTimeout(() => document.body.innerHTML = '<article>hi</article>', 100)")
        self.page.wait("article", timeout=5)
        self.page.raw.evaluate("setTimeout(() => history.pushState({}, '', '/reels/'), 100)")
        self.page.wait(url="**/reels/", timeout=5)
        self.page.wait(url=re.compile(r"/reels/$"), timeout=5)
        self.page.wait(url=lambda url: url.endswith("/reels/"), timeout=5)
        self.page.goto("https://site.test/")
        self.page.wait(timeout=5)
        self.page.wait(idle=True, timeout=5)
        self.page.wait("body", url="**/site.test/", idle=True, timeout=5)

    def test_wait_raises_wait_error_naming_what_it_waited_for(self):
        for options, what in (({"css": "#never"}, "an element to match '#never'"),
                              ({"url": "**/never/"}, "the URL to match '**/never/'"),
                              ({"url": lambda url: False}, "the URL to match a predicate")):
            with self.subTest(what=what):
                started = time.monotonic()
                with self.assertRaises(browser.WaitError) as caught:
                    self.page.wait(**options, timeout=0.2)
                self.assertEqual(str(caught.exception), f"waited 0.2 s for {what}")
                self.assertIsInstance(caught.exception, browser.Error)
                self.assertNotIsInstance(caught.exception, browser.PlaywrightError)
                self.assertLess(time.monotonic() - started, 3)

    def test_wait_timeout_is_for_every_condition_together(self):
        # The element never comes, so the URL wait has only what the first one left: none.
        started = time.monotonic()
        with self.assertRaises(browser.WaitError):
            self.page.wait("body", url="**/never/", timeout=0.5)
        self.assertLess(time.monotonic() - started, 2)

    def test_content_url_title_close(self):
        self.assertIn("home", self.page.document.raw.get())
        self.page.raw.evaluate("document.body.innerHTML = '<p>changed</p>'")
        self.assertIn("changed", self.page.document.css("body").text)
        self.assertEqual((self.page.url, self.page.title), ("https://site.test/", "Home"))
        self.page.close()
        self.assertNotIn(self.page, self.browser.pages)

    def test_handoff_headless_raises(self):
        with self.assertRaises(RuntimeError):
            self.page.handoff(until=lambda: False, message="Solve the captcha")
        self.assertTrue(self.page.handoff(until=lambda: True, message="Solve the captcha"))


class ScrollTest(PageCase):
    def setUp(self):
        super().setUp()
        browser._random.seed(7)

    def load(self, cap=40, jiggle=False):
        self.page.goto(f"https://site.test/feed?cap={cap}&jiggle={int(jiggle)}")

    def count(self) -> int:
        return self.page.raw.locator(".item").count()

    def wheels(self) -> list[int]:
        return self.page.raw.evaluate("wheels")

    def test_reaches_the_end_of_the_feed(self):
        self.load(cap=20)
        scrolls = self.page.scroll(idle=1.5, wait=0.3, pause=0.01)
        self.assertEqual(self.count(), 20)
        self.assertLess(scrolls, 20)

    def test_feed_that_only_loads_after_the_jiggle(self):
        self.load(cap=20, jiggle=True)
        self.page.scroll(count=self.count, idle=2.0, wait=0.3, pause=0.01)
        self.assertEqual(self.count(), 20)

    def test_until_stops_it(self):
        self.load()
        self.page.scroll(count=self.count, until=lambda: self.count() >= 15, wait=0.3, pause=0.01)
        self.assertGreaterEqual(self.count(), 15)
        self.assertLess(self.count(), 25)

    def test_until_is_asked_before_each_scroll(self):
        self.load()
        self.assertEqual(self.page.scroll(until=upto(3), wait=0.02, pause=0.01), 3)
        self.assertEqual(self.page.scroll(until=lambda: True), 0)

    def test_new_responses_count_as_progress(self):
        # Every scroll fetches /api/other, so while it is watched the feed never looks finished.
        self.page.goto("https://site.test/feed?cap=5&fetch=1")
        with self.page.on_response("/api/", lambda parsed, response: None):
            self.assertEqual(self.page.scroll(until=upto(4), idle=0.01, wait=2.0, pause=0.01), 4)
        # Unwatched, the same responses are no progress: the first scroll at the bottom ends it.
        self.assertEqual(self.page.scroll(until=upto(4), idle=0.01, wait=0.02, pause=0.01), 1)

    def test_on_step_is_called_after_each_scroll(self):
        self.load(cap=20)
        ticks = []
        scrolls = self.page.scroll(idle=1.5, wait=0.3, pause=0.01, on_step=lambda: ticks.append(1))
        self.assertEqual(self.count(), 20)
        self.assertEqual(len(ticks), scrolls)

    def test_within_scrolls_an_element_to_its_end(self):
        for within in ("#box", lambda: self.page.raw.locator("#box")):
            with self.subTest(within=within):
                self.page.goto("https://site.test/box")
                target = within() if callable(within) else within
                scrolls = self.page.scroll(within=target, count=self.count, idle=1.0, wait=0.3, pause=0.01)
                self.assertEqual(self.count(), 20)
                self.assertGreater(scrolls, 3)
                # The box scrolled, and the page around it didn't.
                self.assertGreater(self.page.raw.evaluate("document.getElementById('box').scrollTop"), 0)
                self.assertEqual(self.page.raw.evaluate("scrollY"), 0)

    def test_within_nothing_visible_raises(self):
        self.load()
        with self.assertRaisesRegex(browser.Error, "no visible element"):
            self.page.scroll(within="#missing", wait=0.02, pause=0.01)

    def test_uses_the_wheel_in_uneven_steps(self):
        self.load()
        self.page.scroll(until=upto(8), wait=0.02, pause=0.01)
        down = [delta for delta in self.wheels() if delta > 0]
        self.assertGreaterEqual(len(down), 8 * 3)
        self.assertGreater(len(set(down)), 5)
        self.assertTrue(all(delta < 900 * 0.9 for delta in down))

    def test_seeded_run_is_repeatable(self):
        runs = []
        for _ in range(2):
            browser._random.seed(42)
            self.load(cap=5)
            self.page.scroll(idle=100, until=upto(4), wait=0.02, pause=0.01)
            runs.append(self.wheels())
        self.assertEqual(runs[0], runs[1])
        self.assertTrue(any(delta < 0 for delta in runs[0]))


def upto(scrolls: int):
    """An `until` that lets `scrolls` scrolls through: it is asked once before each."""
    asked = iter(range(scrolls + 1))
    return lambda: next(asked, scrolls) >= scrolls


class FakeContext:
    """A `BrowserContext` that keeps cookies, and rejects any without a domain, quoting it whole."""

    def __init__(self):
        self.stored = []

    def add_cookies(self, cookies):
        for cookie in cookies:
            if "domain" not in cookie:
                raise browser.PlaywrightError(f"Invalid cookie {cookie}")
            self.stored.append(cookie)

    def cookies(self):
        return [dict(cookie) for cookie in self.stored]

    def clear_cookies(self):
        self.stored.clear()


class CookiesTest(BrowserCase):
    def setUp(self):
        super().setUp()
        self.live = browser.Browser(FakeContext(), close_steps=[], headless=True)  # type: ignore[arg-type]

    def test_a_jar_goes_in_and_comes_out(self):
        jar = cookies.Jar([{"name": "sid", "value": "1", "domain": ".example.com"}])
        self.live.add_cookies(jar)
        self.assertEqual(self.live.cookies,
                         cookies.Jar([{"name": "sid", "value": "1", "domain": ".example.com", "path": "/"}]))
        self.assertEqual(self.live.cookies.header, "sid=1")
        self.live.clear_cookies()
        self.assertEqual(len(self.live.cookies), 0)

    def test_a_rejected_cookie_is_logged_without_its_value(self):
        self.live.add_cookies([{"name": "bad", "value": "s3cr3t-value"},
                               {"name": "good", "value": "1", "domain": "example.com"}])
        self.assertEqual(self.live.cookies.names, ["good"])
        self.assertEqual(len(self.lines()), 1)
        self.assertTrue(self.lines()[0].startswith("! cookie bad was rejected: Invalid cookie"))
        self.assertNotIn("s3cr3t-value", self.lines()[0])


class PageContextTest(unittest.TestCase):
    def test_with_closes_the_tab_even_when_the_block_raises(self):
        for fails in (False, True):
            with self.subTest(fails=fails):
                closed = []
                tab = browser.Page(SimpleNamespace(close=lambda: closed.append(1)), None)  # type: ignore[arg-type]
                try:
                    with tab as inside:
                        self.assertIs(inside, tab)
                        if fails:
                            raise KeyError("x")
                except KeyError:
                    self.assertTrue(fails)
                self.assertEqual(closed, [1])


class GoneBodyTest(BrowserCase):
    def response(self, error):
        return SimpleNamespace(url="https://site.test/api/graphql", text=mock.Mock(side_effect=error))

    def test_a_body_freed_after_the_page_moved_on_is_skipped_without_a_line(self):
        log.level = "debug"
        error = browser.PlaywrightError("Response.text: Protocol error (Network.getResponseBody): "
                                        "No resource with given identifier found")
        self.assertEqual(browser._json_bodies(self.response(error), False), [])
        self.assertEqual(self.lines(), [])

    def test_another_unreadable_body_is_a_debug_line(self):
        log.level = "debug"
        self.assertEqual(browser._json_bodies(self.response(browser.PlaywrightError("Target closed")), False), [])
        self.assertEqual(self.lines(), ["· on_response: no body for https://site.test/api/graphql: Target closed"])


class SubscriptionTest(unittest.TestCase):
    def test_stop_runs_once_and_with_stops(self):
        stops = []
        watching = browser.Subscription(lambda: stops.append(1))
        watching.stop()
        watching.stop()
        with browser.Subscription(lambda: stops.append(2)) as other:
            self.assertIsInstance(other, browser.Subscription)
            self.assertEqual(stops, [1])
        self.assertEqual(stops, [1, 2])


class FakeRawPage:
    def wait_for_timeout(self, ms):
        time.sleep(ms / 1000)


class FakeFeed:
    """A raw page on a fake clock: each scroll makes it grow `after` seconds later, for `grows` scrolls.

    It is always at the bottom, as a feed waiting for its next page is. Waits advance the clock.
    """

    def __init__(self, *, after=0.1, grows=10**9, bottom=True):
        self.now = 0.0
        self.after, self.grows, self.bottom = after, grows, bottom
        self.height = 1000
        self.due = []                                    # when the next pages arrive
        self.scrolled_at = []
        self.mouse = SimpleNamespace(move=lambda *args, **kw: None, wheel=lambda *args: None)

    def scroll(self, raw_page):
        self.scrolled_at.append(self.now)
        if self.grows:
            self.grows -= 1
            self.due.append(self.now + self.after)

    def evaluate(self, expression):
        if expression == browser.PAGE_HEIGHT:
            arrived = [when for when in self.due if when <= self.now]
            self.due = [when for when in self.due if when > self.now]
            self.height += 900 * len(arrived)
            return self.height
        if expression == browser.NEAR_BOTTOM:
            return self.bottom
        if expression == browser.VIEWPORT:
            return [800, 900]
        raise AssertionError(f"unexpected script {expression!r}")

    def wait_for_timeout(self, ms):
        self.now += ms / 1000


class ScrollTimingTest(BrowserCase):
    """Page.scroll's waits on a fake page and clock, so the timing rules are exact and take no time."""

    def feed(self, **options):
        feed = FakeFeed(**options)
        clock = mock.patch.object(browser, "_clock", lambda: feed.now)
        clock.start()
        self.addCleanup(clock.stop)
        browser._random.seed(5)
        page = browser.Page(feed, SimpleNamespace(_headless=True))  # type: ignore[arg-type]
        # The fake page's scroll stands in for the mouse and the wheel, which would advance the clock.
        move = mock.patch.object(page, "_move", lambda target: feed.scroll(None))
        move.start()
        self.addCleanup(move.stop)
        return feed, page

    def gaps(self, feed):
        return [later - earlier for earlier, later in zip(feed.scrolled_at, feed.scrolled_at[1:])]

    def test_a_page_that_grows_quickly_is_scrolled_again_after_the_floor_not_the_wait(self):
        feed, page = self.feed(after=0.1, grows=20)
        page.scroll(wait=3.0, until=upto(20))
        # 0.1 s for the page, then the 0.3 s floor plus at most 40%, give or take one 50 ms check.
        gaps = self.gaps(feed)
        self.assertGreaterEqual(min(gaps), 0.1 + 0.3)
        self.assertLessEqual(max(gaps), 0.1 + 0.05 + 0.3 * 1.4 + 1e-9)

    def test_a_slow_page_is_waited_for(self):
        feed, page = self.feed(after=2.0, grows=5)
        page.scroll(wait=3.0, until=upto(5))
        self.assertTrue(all(2.0 + 0.3 <= gap < 3.0 for gap in self.gaps(feed)))
        self.assertEqual(feed.height, 1000 + 900 * 5)

    def test_a_page_that_stops_growing_ends_after_10_s_whatever_the_wait(self):
        for wait in (0.05, 0.5, 3.0):
            with self.subTest(wait=wait):
                feed, page = self.feed(after=0.1, grows=5)
                page.scroll(wait=wait)
                # The quiet spell starts with the sixth scroll, the first that brought nothing.
                quiet = feed.scrolled_at[5]
                self.assertGreaterEqual(feed.now - quiet, 10.0)
                self.assertLess(feed.now - quiet, 11.5)

    def test_300_growing_scrolls_are_not_cut_off(self):
        feed, page = self.feed(after=0.1, grows=300)
        scrolls = page.scroll()
        self.assertGreater(scrolls, 300)
        self.assertEqual(feed.height, 1000 + 900 * 300)

    def test_count_rising_is_progress(self):
        feed, page = self.feed(grows=0)

        def counter():
            # A number that rises 0.2 s after each scroll, while the page's height stays the same.
            return sum(1 for when in feed.scrolled_at if when + 0.2 <= feed.now)

        page.scroll(wait=3.0, until=upto(6), count=counter)
        self.assertTrue(all(0.2 + 0.3 <= gap < 1.0 for gap in self.gaps(feed)))

    def test_away_from_the_bottom_there_is_nothing_to_wait_for(self):
        feed, page = self.feed(grows=0, bottom=False)
        self.assertEqual(page.scroll(wait=3.0, until=upto(10)), 10)
        self.assertTrue(all(0.3 <= gap <= 0.3 * 1.4 + 1e-9 for gap in self.gaps(feed)))

    def test_the_floor_is_the_pause(self):
        feed, page = self.feed(after=0.0, grows=10)
        page.scroll(pause=2.4, until=upto(10))
        self.assertTrue(all(2.4 <= gap <= 2.4 * 1.4 + 1e-9 for gap in self.gaps(feed)))


class NavigationRaceTest(BrowserCase):
    """Reads that evaluate in the page survive "Execution context was destroyed" once or twice."""

    def page(self, failures):
        errors = [browser.PlaywrightError("Page.content: Execution context was destroyed, most likely "
                                          "because of a navigation")] * failures
        loads = []

        def content():
            if errors:
                raise errors.pop()
            return "<html><body><p id='x'>hi</p></body></html>"

        raw = SimpleNamespace(content=content, wait_for_load_state=lambda state, timeout: loads.append(state))
        return browser.Page(raw, SimpleNamespace(_headless=True)), loads  # type: ignore[arg-type]

    def test_a_read_that_hits_a_navigation_waits_for_the_load_and_reads_again(self):
        page, loads = self.page(1)
        self.assertEqual(page.document.css("#x::text").first, "hi")
        self.assertEqual(loads, ["load"])

    def test_a_page_that_keeps_navigating_warns_and_reads_as_empty(self):
        page, loads = self.page(10)
        self.assertEqual(page.document.css("#x::text").all, [])
        self.assertEqual(len(loads), 2)
        self.assertTrue(any("kept navigating" in line for line in self.lines()))

    def test_another_error_is_not_swallowed(self):
        raw = SimpleNamespace(content=mock.Mock(side_effect=browser.PlaywrightError("Target closed")))
        page = browser.Page(raw, SimpleNamespace(_headless=True))  # type: ignore[arg-type]
        with self.assertRaises(browser.PlaywrightError):
            page.document


class HandoffTest(BrowserCase):
    def setUp(self):
        super().setUp()
        self.page = browser.Page(FakeRawPage(), SimpleNamespace(_headless=False))  # type: ignore[arg-type]

    def test_done_at_once(self):
        self.assertTrue(self.page.handoff(until=lambda: True, message="Solve the captcha"))
        self.assertEqual(self.lines(), [])

    def test_done_later(self):
        ready = time.monotonic() + 0.1
        self.assertTrue(self.page.handoff(until=lambda: time.monotonic() >= ready, message="Solve the captcha",
                                          poll=0.02))
        self.assertEqual(self.lines(), ["! Solve the captcha", "✓ " + "━" * 20 + " Solve the captcha"])

    def test_timeout(self):
        self.assertFalse(self.page.handoff(until=lambda: False, message="Log in", poll=0.02, timeout=0.1))
        self.assertEqual(self.lines(), ["! Log in", "✗ " + "─" * 20 + " Log in"])


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
            self.server.release.wait(10)
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
        except (BrokenPipeError, ConnectionResetError):
            pass

    def log_message(self, format, *args):
        """Quiet: the default writes every request to stderr."""


class LocalServer:
    """An HTTP server on 127.0.0.1 and a free port, serving `LocalHandler` from a thread until `close`."""

    def __init__(self):
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), LocalHandler)
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
    """`Page.fetch`: a request sent by the page itself, to a local server, returned as an `http.Response`."""

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
        self.page = self.browser.open(f"{self.server.url}/app/")
        self.addCleanup(self.page.close)

    def test_a_json_body(self):
        response = self.page.fetch(f"{self.server.url}/json?page=2")
        self.assertIsInstance(response, http.Response)
        self.assertEqual((response.status, response.ok, response.url), (200, True, f"{self.server.url}/json?page=2"))
        self.assertEqual(response.headers["Content-Type"], "application/json")
        self.assertEqual(response.json, {"path": "/json", "query": "page=2"})

    def test_a_text_body_keeps_its_bytes(self):
        response = self.page.fetch("/text")
        self.assertEqual(response.content, "héllo\r\nwörld".encode())
        self.assertEqual(response.text, "héllo\r\nwörld")

    def test_a_binary_body_comes_back_byte_for_byte(self):
        self.assertEqual(self.page.fetch("/binary").content, BINARY_BODY)

    def test_a_relative_url_is_resolved_against_the_page(self):
        response = self.page.fetch("data/json")
        self.assertEqual(response.url, f"{self.server.url}/app/data/json")
        self.assertEqual(response.json["path"], "/app/data/json")

    def test_post_with_json(self):
        echo = self.page.fetch("/echo", method="post", json={"id": 1, "name": "example_user"}).json
        self.assertEqual(echo["method"], "POST")
        self.assertEqual(json.loads(echo["body"]), {"id": 1, "name": "example_user"})
        self.assertEqual(echo["headers"]["content-type"], "application/json")

    def test_post_with_a_form_and_with_bytes(self):
        form = self.page.fetch("/echo", method="POST", data={"q": "a b", "n": 2}).json
        self.assertEqual(form["body"], "q=a+b&n=2")
        self.assertTrue(form["headers"]["content-type"].startswith("application/x-www-form-urlencoded"))
        raw = self.page.fetch("/echo", method="POST", data=b"\x00\xff\n").json
        self.assertEqual(raw["body"], "\x00\xff\n")

    def test_data_and_json_together_are_refused(self):
        with self.assertRaises(ValueError):
            self.page.fetch("/echo", method="POST", data="a", json={"a": 1})

    def test_headers_are_passed_through_with_the_pages_origin(self):
        echo = self.page.fetch("/echo", headers={"X-Csrf": "token-1", "Accept": "application/json"}).json
        self.assertEqual(echo["headers"]["x-csrf"], "token-1")
        self.assertEqual(echo["headers"]["accept"], "application/json")
        self.assertEqual(echo["headers"]["referer"], f"{self.server.url}/app/")

    def test_the_pages_cookies_are_sent(self):
        self.page.raw.evaluate("document.cookie = 'sid=example-session; path=/'")
        self.assertEqual(self.page.fetch("/echo").json["headers"]["cookie"], "sid=example-session")

    def test_a_404_is_refused_and_soft_returns_none(self):
        with self.assertRaises(http.RefusedError) as caught:
            self.page.fetch("/status/404?token=abc", headers={"Authorization": SECRET})
        self.assertEqual(caught.exception.status, 404)
        self.assertEqual(str(caught.exception), f"GET {self.server.url}/status/404: HTTP 404")
        self.assertIsNone(self.page.fetch("/status/404", soft=True))

    def test_a_429_is_rate_limited_with_retry_after(self):
        with self.assertRaises(http.RateLimitedError) as caught:
            self.page.fetch("/status/429", soft=True)
        self.assertEqual(caught.exception.retry_after, 7.0)
        self.assertNotIsInstance(caught.exception, http.RefusedError)

    def test_a_500_is_a_server_error(self):
        with self.assertRaises(http.ServerError) as caught:
            self.page.fetch("/status/500")
        self.assertEqual(caught.exception.status, 500)

    def test_the_timeout_aborts_the_request_in_the_page(self):
        started = time.monotonic()
        with self.assertRaises(http.Error) as caught:
            self.page.fetch("/slow?token=abc", headers={"Authorization": SECRET}, timeout=0.5)
        self.assertLess(time.monotonic() - started, 5)
        self.assertEqual(str(caught.exception), f"GET {self.server.url}/slow: no answer within 0.5 s")
        # The page is still usable afterwards.
        self.assertEqual(self.page.fetch("/json").status, 200)

    def test_a_request_the_browser_refuses_names_no_header_value(self):
        with self.assertRaises(http.Error) as caught:
            # A NUL byte makes the value invalid: the browser's TypeError must not show it, whatever its wording.
            self.page.fetch("/json", headers={"X-Token": SECRET + "\x00"})
        self.assertNotIn("s3cr3t", str(caught.exception))
        self.assertTrue(str(caught.exception).startswith(f"GET {self.server.url}/json: TypeError"), caught.exception)


if __name__ == "__main__":
    unittest.main()
