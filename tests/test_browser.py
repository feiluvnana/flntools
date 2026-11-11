import importlib
import json
import re
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.request
from io import StringIO
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from shared import log, theme
from shared import _browser as browser
from shared._theme import Screen


def chromium_installed() -> bool:
    try:
        with browser.connect():
            return True
    except (ImportError, RuntimeError):
        return False


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
    content_type, body = API_BODIES.get(path, ("text/html", "<html><head><title>Home</title></head><body>home</body></html>"))
    route.fulfill(status=200, content_type=content_type, body=body)


class BrowserCase(unittest.TestCase):
    def setUp(self):
        self.screen = Screen(out=StringIO(), inp=StringIO(""), width=300, tty=False, interactive=False)
        self.scope = theme.scope(screen=self.screen, color="none", unicode=True)
        self.scope.__enter__()
        self.previous_level = log.level
        # The bundled build, so results don't depend on whether this machine has Chrome installed.
        channels = mock.patch.object(browser, "LAUNCH_CHANNELS", ("chromium",))
        channels.start()
        self.addCleanup(channels.stop)

    def tearDown(self):
        log.level = self.previous_level
        self.scope.__exit__(None, None, None)

    def lines(self) -> list[str]:
        return self.screen.out.getvalue().splitlines()


class ImportTest(BrowserCase):
    def test_without_playwright(self):
        blocked = {"playwright": None, "playwright.sync_api": None}
        with mock.patch.dict(sys.modules, blocked):
            sys.modules.pop("shared._browser", None)
            fresh = importlib.import_module("shared._browser")
            self.assertFalse(hasattr(fresh, "chrome_user_agent"))
            with self.assertRaisesRegex(ImportError, re.escape("pip install 'flntools[browser]'")):
                fresh.connect()

    def test_cdp_refuses_launch_options(self):
        for options in ({"headless": False}, {"profile": "x"}, {"args": ["--x"]},
                        {"stealth": False}):
            with self.assertRaises(ValueError, msg=options):
                browser.connect(cdp="http://127.0.0.1:9", **options)


@needs_chromium
class ConnectTest(BrowserCase):
    def setUp(self):
        super().setUp()
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)

    def test_incognito_leaves_nothing_behind(self):
        with browser.connect() as b:
            b.cookies.add([{"name": "sid", "value": "1", "domain": "site.test", "expires": time.time() + 3600}])
            self.assertEqual(len(b.cookies.all), 1)
        with browser.connect() as b:
            self.assertEqual(b.cookies.all, [])

    def test_headless_reports_its_own_user_agent(self):
        with browser.connect() as b:
            agent = b.open_page().raw.evaluate("navigator.userAgent")
        self.assertIn("Chrome/", agent)
        self.assertNotIn("Headless", agent)
        with browser.connect(profile=Path(self.folder.name) / "profile") as b:
            self.assertEqual(b.open_page().raw.evaluate("navigator.userAgent"), agent)

    def test_falls_back_when_a_channel_is_missing(self):
        with mock.patch.object(browser, "LAUNCH_CHANNELS", ("no-such-browser", "chromium")):
            with browser.connect() as b:
                self.assertEqual(b.open_page().raw.evaluate("1 + 1"), 2)

    def test_profile_keeps_a_cookie(self):
        profile = Path(self.folder.name) / "profile"
        with browser.connect(profile=profile) as b:
            b.cookies.add([{"name": "sid", "value": "1", "domain": "site.test", "expires": time.time() + 3600}])
        with browser.connect(profile=profile) as b:
            self.assertEqual([cookie["name"] for cookie in b.cookies.all], ["sid"])

    def test_installed_profile_is_copied_and_deleted(self):
        home = Path(self.folder.name)
        platform = sys.platform if sys.platform in ("darwin", "win32") else "linux"
        root = home / browser.PROFILE_ROOTS[platform]
        (root / "Profile 1" / "Cache").mkdir(parents=True)
        (root / "Profile 1" / "Preferences").write_text("{}")
        (root / "Profile 1" / "Cache" / "big").write_text("cache")
        (root / "Local State").write_text("{}")
        with mock.patch.object(Path, "home", return_value=home):
            copy = browser.installed_profile("chrome", "Profile 1")
            with self.assertRaises(FileNotFoundError):
                browser.installed_profile("chrome", "Profile 9")
        self.assertTrue((copy / "Default" / "Preferences").is_file())
        self.assertTrue((copy / "Local State").is_file())
        self.assertFalse((copy / "Default" / "Cache").exists())
        # The bundled Chromium stands in for real Chrome.
        with mock.patch.dict(browser.CHANNELS, {"chrome": None}):
            with browser.connect(profile=copy) as b:
                self.assertEqual(b.open_page().url, "about:blank")
        self.assertFalse(copy.exists())

    def test_cdp_attaches_and_leaves_it_running(self):
        user_data = Path(self.folder.name) / "chromium"
        chromium = subprocess.Popen([chromium_path(), "--headless=new", "--remote-debugging-port=0",
                                     f"--user-data-dir={user_data}", "about:blank"],
                                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.addCleanup(chromium.wait)
        self.addCleanup(chromium.terminate)
        endpoint = f"http://127.0.0.1:{debugging_port(user_data)}"
        with browser.connect(cdp=endpoint, viewport=(700, 500)) as b:
            page = b.open_page()
            self.assertEqual(page.raw.evaluate("[innerWidth, innerHeight]"), [700, 500])
            b.cookies.add([{"name": "sid", "value": "1", "domain": "site.test"}])
            self.assertEqual([cookie["name"] for cookie in b.cookies.all], ["sid"])
        self.assertIsNone(chromium.poll())
        with urllib.request.urlopen(f"{endpoint}/json/version") as response:
            self.assertEqual(response.status, 200)

    def test_args_are_added(self):
        error = browser.PlaywrightError("stop here")
        with mock.patch("playwright.sync_api._generated.BrowserType.launch", side_effect=error) as launch:
            with self.assertRaises(browser.PlaywrightError):
                browser.connect(args=["--no-sandbox"])
        self.assertEqual(launch.call_args.kwargs["args"], [*browser.LAUNCH_ARGS, "--no-sandbox"])

    def test_missing_chromium_message(self):
        error = browser.PlaywrightError("BrowserType.launch: Executable doesn't exist at /x/chrome")
        with mock.patch("playwright.sync_api._generated.BrowserType.launch", side_effect=error):
            with self.assertRaisesRegex(RuntimeError, "^Chromium is not installed: run 'playwright install chromium'$"):
                browser.connect()

    def test_closed_after_an_exception(self):
        with self.assertRaises(ValueError):
            with browser.connect() as b:
                chromium = b.raw.browser
                page = b.open_page()
                raise ValueError("boom")
        self.assertTrue(page.raw.is_closed())
        self.assertFalse(chromium.is_connected())

    def test_webdriver_hidden(self):
        with browser.connect() as b:
            self.assertIsNone(b.open_page().raw.evaluate("navigator.webdriver"))
        with browser.connect(stealth=False) as b:
            self.assertIs(b.open_page().raw.evaluate("navigator.webdriver"), False)


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
        cls.browser = browser.connect(viewport=(800, 900))
        cls.browser.raw.route("https://site.test/**", fulfill)
        cls.browser.raw.route("https://bad.test/**", fulfill)

    @classmethod
    def tearDownClass(cls):
        cls.browser.close()

    def setUp(self):
        super().setUp()
        self.page = self.browser.open_page("https://site.test/")
        self.addCleanup(self.page.close)


class BrowserTest(PageCase):
    def test_open_page_with_and_without_url(self):
        self.assertEqual(self.browser.open_page().url, "about:blank")
        self.assertEqual(self.page.url, "https://site.test/")
        failed = self.browser.open_page("https://bad.test/")
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
        self.browser.cookies.add([{"name": "sid", "value": "1", "domain": ".site.test", "secure": True},
                                  {"name": "csrf", "value": "2", "domain": "site.test", "path": "/api"}])
        stored = {cookie["name"]: (cookie["domain"], cookie["path"]) for cookie in self.browser.cookies.all}
        self.assertEqual(stored, {"sid": (".site.test", "/"), "csrf": ("site.test", "/api")})
        self.browser.cookies.clear_all()
        self.assertEqual(self.browser.cookies.all, [])

    def test_one_bad_cookie_among_good_ones(self):
        self.browser.cookies.add([{"name": "bad", "value": "x"}, {"name": "good", "value": "1", "domain": "site.test"}])
        self.assertEqual([cookie["name"] for cookie in self.browser.cookies.all], ["good"])
        self.assertEqual(len(self.lines()), 1)
        self.assertTrue(self.lines()[0].startswith("! cookie bad was rejected: "))
        self.browser.cookies.clear_all()


class PageTest(PageCase):
    def fetch(self, path):
        with self.page.raw.expect_response(lambda response: response.url.endswith(path)):
            self.page.raw.evaluate("path => fetch(path).then(response => response.text())", path)

    def test_goto(self):
        self.assertTrue(self.page.goto("https://site.test/other", idle=True))
        self.assertFalse(self.page.goto("https://bad.test/"))
        self.assertEqual(len(self.lines()), 1)

    def test_on_response_each_match_form(self):
        for match in ("graphql", re.compile(r"/api/graph"), lambda response: response.url.endswith("graphql")):
            seen = []
            stop = self.page.on_response(match, lambda parsed, response: seen.append((parsed, response.status)))
            self.fetch("/api/graphql")
            self.fetch("/api/other")
            stop()
            self.assertEqual(seen, [({"data": {"n": 1}}, 200)], match)

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
        stop = self.page.on_request("/api/", lambda request: urls.append(request.url))
        self.fetch("/api/other")
        stop()
        self.fetch("/api/graphql")
        self.assertEqual(urls, ["https://site.test/api/other"])

    def test_wait_for(self):
        self.page.raw.evaluate("setTimeout(() => document.body.innerHTML = '<article>hi</article>', 100)")
        self.page.wait_for_selector("article", timeout=5)
        self.page.raw.evaluate("setTimeout(() => history.pushState({}, '', '/reels/'), 100)")
        self.page.wait_for_url("**/reels/", timeout=5)
        self.page.goto("https://site.test/")
        self.page.wait_for_load(timeout=5)
        self.page.wait_for_load(idle=True, timeout=5)
        with self.assertRaises(browser.PlaywrightError):
            self.page.wait_for_selector("#never", timeout=0.1)

    def test_content_url_title_close(self):
        self.assertIn("home", self.page.selector.raw.get())
        self.page.raw.evaluate("document.body.innerHTML = '<p>changed</p>'")
        self.assertIn("changed", self.page.selector.css("body").text)
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
        scrolls = self.page.scroll(idle=2, delay=0.02)
        self.assertEqual(self.count(), 20)
        self.assertLess(scrolls, 20)

    def test_feed_that_only_loads_after_the_jiggle(self):
        self.load(cap=20, jiggle=True)
        self.page.scroll(count=self.count, idle=3, delay=0.02)
        self.assertEqual(self.count(), 20)

    def test_stops_at_limit(self):
        self.load()
        self.page.scroll(count=self.count, limit=15, delay=0.02)
        self.assertGreaterEqual(self.count(), 15)
        self.assertLess(self.count(), 25)

    def test_stops_when_more_is_false(self):
        self.load()
        calls = []
        self.assertEqual(self.page.scroll(more=lambda: calls.append(1) or len(calls) <= 3, delay=0.02), 3)

    def test_stops_at_max(self):
        self.load()
        self.assertEqual(self.page.scroll(max=2, delay=0.02), 2)

    def test_new_responses_count_as_progress(self):
        self.load(cap=5)
        self.page.on_response("/api/", lambda parsed, response: None)
        fetch = "fetch('/api/other'); scrollBy(0, 100000)"
        # Every scroll brings a response, so the feed never looks finished until max.
        self.assertEqual(self.page.scroll(by=fetch, idle=1, delay=0.05, max=4), 4)
        self.assertEqual(self.page.scroll(by="scrollBy(0, 100000)", idle=1, delay=0.02, max=4), 1)

    def test_custom_by_and_onscroll(self):
        self.load(cap=20)
        moves, ticks = [], []

        def by(raw_page):
            moves.append(1)
            raw_page.evaluate("scrollBy(0, 100000)")

        scrolls = self.page.scroll(by=by, idle=1, delay=0.02, onscroll=lambda: ticks.append(1))
        self.assertEqual(self.count(), 20)
        self.assertEqual((len(moves), len(ticks)), (scrolls, scrolls))
        scrolls = self.page.scroll(by="scrollBy(0, 100000)", idle=1, delay=0.02)
        self.assertEqual(scrolls, 1)

    def test_uses_the_wheel_in_uneven_steps(self):
        self.load()
        self.page.scroll(max=8, delay=0.02)
        down = [delta for delta in self.wheels() if delta > 0]
        self.assertGreaterEqual(len(down), 8 * 3)
        self.assertGreater(len(set(down)), 5)
        self.assertTrue(all(delta < 900 * 0.9 for delta in down))

    def test_seeded_run_is_repeatable(self):
        runs = []
        for _ in range(2):
            browser._random.seed(42)
            self.load(cap=5)
            self.page.scroll(idle=100, max=4, delay=0.02)
            runs.append(self.wheels())
        self.assertEqual(runs[0], runs[1])
        self.assertTrue(any(delta < 0 for delta in runs[0]))


class FakeRawPage:
    def wait_for_timeout(self, ms):
        time.sleep(ms / 1000)


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
        self.assertEqual(self.lines(), ["! Solve the captcha", "✓ Solve the captcha"])

    def test_timeout(self):
        self.assertFalse(self.page.handoff(until=lambda: False, message="Log in", poll=0.02, timeout=0.1))
        self.assertEqual(self.lines(), ["! Log in", "✗ Log in"])


if __name__ == "__main__":
    unittest.main()
