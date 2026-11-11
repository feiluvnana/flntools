import json
import re
import tempfile
import unittest
from io import StringIO
from pathlib import Path
from unittest import mock

from shared import log, theme
from shared._theme import Screen

try:
    from shared import browser, http
    from shared import _browser
except ImportError:  # the browser extra is missing
    browser = None


def chromium_installed() -> bool:
    if browser is None:
        return False
    try:
        with browser.open():
            return True
    except RuntimeError:
        return False


HAS_CHROMIUM = chromium_installed()

FEED_PAGE = """<!doctype html><html><body style="margin:0">
<div id="feed"></div>
<script>
  const cap = Number(new URLSearchParams(location.search).get("cap") || 40);
  const feed = document.getElementById("feed");
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
  window.addEventListener("scroll", () => {
    if (window.scrollY + window.innerHeight >= document.body.scrollHeight - 10) add(5);
  });
</script></body></html>"""


class BrowserCase(unittest.TestCase):
    def setUp(self):
        self.screen = Screen(out=StringIO(), inp=StringIO(""), width=300, tty=False, interactive=False)
        self.scope = theme.scope(screen=self.screen, color="none", unicode=True)
        self.scope.__enter__()
        self.previous_level = log.level

    def tearDown(self):
        log.level = self.previous_level
        self.scope.__exit__(None, None, None)

    def lines(self) -> list[str]:
        return self.screen.out.getvalue().splitlines()


@unittest.skipUnless(HAS_CHROMIUM, "Chromium is not installed")
class OpenTest(BrowserCase):
    def setUp(self):
        super().setUp()
        self.tmp = tempfile.TemporaryDirectory()
        self.page_file = Path(self.tmp.name) / "page.html"
        self.page_file.write_text("<html><body><p>hi</p></body></html>")

    def tearDown(self):
        self.tmp.cleanup()
        super().tearDown()

    def test_user_agent_and_viewport(self):
        with browser.open(user_agent=http.agents.safari, viewport=(800, 600)) as page:
            page.goto(self.page_file.as_uri())
            self.assertEqual(page.evaluate("navigator.userAgent"), http.agents.safari)
            self.assertEqual(page.evaluate("[window.innerWidth, window.innerHeight]"), [800, 600])
            self.assertIs(page.context.browser.contexts[0], page.context)

    def test_cookie_domain_fix_up(self):
        cookies = [{"name": "sid", "value": "1", "domain": "example.com", "expires": 0, "httpOnly": False},
                   {"name": "csrf", "value": "2", "domain": ".example.com", "path": "/api"}]
        with browser.open(cookies=cookies) as page:
            stored = {c["name"]: (c["domain"], c["path"]) for c in page.context.cookies()}
        self.assertEqual(stored, {"sid": (".example.com", "/"), "csrf": (".example.com", "/api")})

    def test_bad_cookie_logs_a_warning(self):
        cookies = [{"name": "", "value": "x", "domain": ""}, {"name": "good", "value": "1", "domain": "a.com"}]
        with browser.open(cookies=cookies) as page:
            page.goto(self.page_file.as_uri())
            self.assertEqual([c["name"] for c in page.context.cookies()], ["good"])
        self.assertEqual(len(self.lines()), 1)
        self.assertTrue(self.lines()[0].startswith("! cookie  was rejected: "))

    def test_webdriver_hidden(self):
        with browser.open() as page:
            page.goto(self.page_file.as_uri())
            self.assertIsNone(page.evaluate("navigator.webdriver"))
        with browser.open(stealth=False) as page:
            page.goto(self.page_file.as_uri())
            # The launch flag already makes it false; only the init script makes it undefined.
            self.assertIs(page.evaluate("navigator.webdriver"), False)

    def test_closed_after_an_exception(self):
        with self.assertRaises(ValueError):
            with browser.open() as page:
                chromium = page.context.browser
                raise ValueError("boom")
        self.assertTrue(page.is_closed())
        self.assertFalse(chromium.is_connected())

    def test_missing_chromium_message(self):
        error = _browser.PlaywrightError("BrowserType.launch: Executable doesn't exist at /x/chrome")
        with mock.patch("playwright.sync_api._generated.BrowserType.launch", side_effect=error):
            with self.assertRaisesRegex(RuntimeError, "^Chromium is not installed: run 'playwright install chromium'$"):
                with browser.open():
                    pass


@unittest.skipUnless(HAS_CHROMIUM, "Chromium is not installed")
class CaptureTest(BrowserCase):
    """Responses come from routed https URLs, so fetch works without a server."""

    BODIES = {
        "/api/graphql": ("application/json", json.dumps({"data": {"n": 1}})),
        "/api/other": ("application/json", json.dumps({"other": True})),
        "/api/html": ("text/html", "<p>not json</p>"),
        "/api/stream": ("application/x-ndjson", '{"line": 1}\n\n{"line": 2}\n'),
    }

    def setUp(self):
        super().setUp()
        self.opened = browser.open()
        self.page = self.opened.__enter__()
        self.page.route("https://site.test/**", self.fulfill)
        self.page.goto("https://site.test/")

    def tearDown(self):
        self.opened.__exit__(None, None, None)
        super().tearDown()

    def fulfill(self, route):
        path = route.request.url.removeprefix("https://site.test")
        content_type, body = self.BODIES.get(path, ("text/html", "<html><body>home</body></html>"))
        route.fulfill(status=200, content_type=content_type, body=body)

    def fetch(self, path):
        with self.page.expect_response(lambda response: response.url.endswith(path)):
            self.page.evaluate("path => fetch(path).then(r => r.text())", path)

    def test_each_match_form(self):
        for match in ("graphql", re.compile(r"/api/graph"), lambda response: response.url.endswith("graphql")):
            seen = []
            stop = browser.capture(self.page, match, lambda response, data: seen.append(data))
            self.fetch("/api/graphql")
            self.fetch("/api/other")
            stop()
            self.assertEqual(seen, [{"data": {"n": 1}}], match)

    def test_body_that_is_not_json_is_skipped(self):
        log.level = "debug"
        seen = []
        browser.capture(self.page, "/api/", lambda response, data: seen.append(data))
        self.fetch("/api/html")
        self.fetch("/api/other")
        self.assertEqual(seen, [{"other": True}])
        self.assertEqual(self.lines(), ["· capture: skipped a body that is not JSON from https://site.test/api/html"])

    def test_lines(self):
        seen = []
        browser.capture(self.page, "stream", lambda response, data: seen.append(data), lines=True)
        self.fetch("/api/stream")
        self.assertEqual(seen, [{"line": 1}, {"line": 2}])

    def test_handler_exception_is_logged_and_later_responses_arrive(self):
        seen = []

        def handler(response, data):
            if "data" in data:
                raise KeyError("edges")
            seen.append(data)

        browser.capture(self.page, "/api/", handler)
        self.fetch("/api/graphql")
        self.fetch("/api/other")
        self.assertEqual(seen, [{"other": True}])
        self.assertEqual(self.lines(), ["! capture handler failed for https://site.test/api/graphql: KeyError 'edges'"])

    def test_stop_removes_the_listener(self):
        seen = []
        stop = browser.capture(self.page, "/api/", lambda response, data: seen.append(data))
        stop()
        self.fetch("/api/other")
        self.assertEqual(seen, [])


@unittest.skipUnless(HAS_CHROMIUM, "Chromium is not installed")
class ScrollTest(BrowserCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.feed = Path(cls.tmp.name) / "feed.html"
        cls.feed.write_text(FEED_PAGE)

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def setUp(self):
        super().setUp()
        self.opened = browser.open(viewport=(800, 900))
        self.page = self.opened.__enter__()

    def tearDown(self):
        self.opened.__exit__(None, None, None)
        super().tearDown()

    def load(self, cap=40):
        self.page.goto(f"{self.feed.as_uri()}?cap={cap}")

    def count(self) -> int:
        return self.page.locator(".item").count()

    def test_stops_at_limit(self):
        self.load()
        scrolls = browser.scroll(self.page, count=self.count, limit=15, delay=0.05)
        self.assertGreaterEqual(self.count(), 15)
        self.assertLess(self.count(), 40)
        self.assertEqual(scrolls, 2)

    def test_stops_when_more_is_false(self):
        self.load()
        calls = []
        scrolls = browser.scroll(self.page, count=self.count, more=lambda: calls.append(1) or len(calls) <= 3,
                                 delay=0.05)
        self.assertEqual(scrolls, 3)

    def test_stops_when_idle(self):
        self.load(cap=10)
        scrolls = browser.scroll(self.page, count=self.count, idle=2, delay=0.05)
        self.assertEqual(self.count(), 10)
        # One scroll loads the last items, then two scrolls change nothing.
        self.assertEqual(scrolls, 3)

    def test_stops_at_max(self):
        self.load()
        self.assertEqual(browser.scroll(self.page, count=self.count, max=2, delay=0.05), 2)

    def test_custom_step_and_onstep(self):
        self.load(cap=20)
        steps, ticks = [], []

        def step(page):
            steps.append(1)
            page.evaluate("window.scrollBy(0, 100000)")

        scrolls = browser.scroll(self.page, count=self.count, idle=1, delay=0.05, step=step,
                                 onstep=lambda: ticks.append(1))
        self.assertEqual(self.count(), 20)
        self.assertEqual(len(steps), scrolls)
        self.assertEqual(len(ticks), scrolls)


class FakePage:
    """Stands in for a headed page: each wait moves the fake clock forward."""

    def __init__(self, clock):
        self.clock = clock
        self.waits = []

    def wait_for_timeout(self, ms):
        self.waits.append(ms)
        self.clock[0] += ms / 1000


@unittest.skipIf(browser is None, "the browser extra is not installed")
class HandoffTest(BrowserCase):
    def setUp(self):
        super().setUp()
        self.clock = [0.0]
        patcher = mock.patch.object(_browser, "_clock", lambda: self.clock[0])
        patcher.start()
        self.addCleanup(patcher.stop)
        self.page = FakePage(self.clock)

    def test_already_done(self):
        self.assertTrue(browser.handoff(self.page, done=lambda: True, message="Solve the captcha"))
        self.assertEqual((self.page.waits, self.lines()), ([], []))

    def test_done_after_polling(self):
        done = lambda: self.clock[0] >= 3
        self.assertTrue(browser.handoff(self.page, done=done, message="Solve the captcha", poll=1.0))
        self.assertEqual(self.page.waits, [1000.0, 1000.0, 1000.0])
        self.assertEqual(self.lines(), ["! Solve the captcha", "✓ Solve the captcha", "✓ Solve the captcha: done"])

    def test_timeout(self):
        self.assertFalse(browser.handoff(self.page, done=lambda: False, message="Log in", poll=0.5, timeout=2))
        self.assertEqual(len(self.page.waits), 4)
        self.assertEqual(self.lines(), ["! Log in", "✗ Log in"])

    @unittest.skipUnless(HAS_CHROMIUM, "Chromium is not installed")
    def test_headless_raises(self):
        with browser.open(headless=True) as page:
            with self.assertRaises(RuntimeError):
                browser.handoff(page, done=lambda: False, message="Solve the captcha")
            self.assertTrue(browser.handoff(page, done=lambda: True, message="Solve the captcha"))



if __name__ == "__main__":
    unittest.main()
