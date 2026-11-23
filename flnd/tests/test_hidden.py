"""A hidden browser reads as a visible one: a page reads what sites read, in both modes.

The page is served from 127.0.0.1 (a secure origin, so client hints exist) by this test's own
server, which also keeps the `Sec-CH-UA` header of each request. Playwright's own browser, no
network. Skipped where Chromium or Chrome can't start, and the visible half where no display
exists.
"""

import asyncio
import json
import os
import sys
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import no_profiles  # noqa: F401  (refuses real browser profiles)

from flnkit import browser as _browser

READ = """async () => {
  const canvas = document.createElement('canvas').getContext('webgl');
  const info = canvas && canvas.getExtension('WEBGL_debug_renderer_info');
  const hints = navigator.userAgentData && await navigator.userAgentData.getHighEntropyValues(
    ['fullVersionList', 'platformVersion', 'architecture', 'model', 'bitness']);
  const asked = (await navigator.permissions.query({name: 'notifications'})).state;
  return {
    agent: navigator.userAgent, brands: navigator.userAgentData && navigator.userAgentData.brands,
    hints, webdriver: navigator.webdriver, plugins: [...navigator.plugins].map(p => p.name),
    mimes: [...navigator.mimeTypes].map(m => m.type),
    gl: info ? [canvas.getParameter(info.UNMASKED_VENDOR_WEBGL),
                canvas.getParameter(info.UNMASKED_RENDERER_WEBGL)] : null,
    screen: [screen.width, screen.height], inner: [innerWidth, innerHeight],
    outer: [outerWidth, outerHeight], notification: Notification.permission, asked,
  };
}"""


class Page(BaseHTTPRequestHandler):
    """Answers any path with an empty page, keeping each request's `Sec-CH-UA`."""

    seen: list[str | None] = []

    def do_GET(self):
        Page.seen.append(self.headers.get("Sec-CH-UA"))
        body = b"<html><body>page</body></html>"
        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


@unittest.skipUnless(os.environ.get("FLND_TEST_BROWSER") == "1", "needs make test-browser")
class HiddenTest(unittest.TestCase):
    def setUp(self):
        self.loop = asyncio.new_event_loop()
        thread = threading.Thread(target=self.loop.run_forever, daemon=True)
        thread.start()
        self.addCleanup(thread.join)
        self.addCleanup(self.loop.call_soon_threadsafe, self.loop.stop)
        server = ThreadingHTTPServer(("127.0.0.1", 0), Page)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        self.url = f"http://127.0.0.1:{server.server_port}/"
        Page.seen = []

    def read(self, *, headless: bool):
        """What the page reads, and the `Sec-CH-UA` of its request, or a skip."""
        async def go():
            async with await _browser.launch(headless=headless) as chrome:
                tab = await chrome.tab()
                await tab.goto(self.url)
                return await tab.raw.evaluate(READ)

        Page.seen.clear()
        try:
            found = _browser.run_on(self.loop, go())
        except Exception as error:  # no browser, or no display for a visible one
            self.skipTest(f"{'hidden' if headless else 'visible'} Chrome did not start: "
                          f"{type(error).__name__}")
        return found, list(Page.seen)

    def test_each_thing_a_site_reads_is_alike_in_hidden_and_visible_mode(self):
        if sys.platform.startswith("linux") and not (
                os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY")):
            self.skipTest("no display for a visible window")
        hidden, hidden_hints = self.read(headless=True)
        visible, visible_hints = self.read(headless=False)
        for name in ("agent", "brands", "hints", "webdriver", "plugins", "mimes", "gl", "screen",
                     "notification", "asked"):
            with self.subTest(name):
                self.assertEqual(hidden[name], visible[name])
        self.assertEqual(hidden_hints, visible_hints)
        self.assertTrue(all(hidden_hints), "the request carries Sec-CH-UA")
        self.assertNotIn("Headless", json.dumps(hidden))
        self.assertNotIn("Headless", "".join(filter(None, hidden_hints)))
        self.assertIsNotNone(hidden["hints"], "client hints exist on a local page")
        self.assertNotIn("swiftshader", json.dumps(hidden["gl"]).lower())
        # Notification and its permission query agree.
        self.assertEqual(hidden["asked"],
                         {"default": "prompt"}.get(hidden["notification"], hidden["notification"]))

    def test_a_hidden_window_is_a_desktop_window_around_its_page(self):
        hidden, _ = self.read(headless=True)
        (width, height), (inner_w, inner_h), (outer_w, outer_h) = (
            hidden["screen"], hidden["inner"], hidden["outer"])
        self.assertGreater(outer_h, inner_h)
        self.assertGreaterEqual(outer_w, inner_w)
        self.assertGreaterEqual(width, outer_w)
        self.assertGreaterEqual(height, outer_h)


class OverrideTest(unittest.TestCase):
    def test_headless_brands_are_dropped_and_the_platform_kept(self):
        hints = {"brands": [{"brand": "HeadlessChrome", "version": "130"},
                            {"brand": "Chromium", "version": "130"}],
                 "fullVersionList": [{"brand": "HeadlessChrome", "version": "130.0.1"},
                                     {"brand": "Chromium", "version": "130.0.1"}],
                 "platform": "macOS", "platformVersion": "15.0.0", "architecture": "arm",
                 "bitness": "64", "mobile": False}
        override = _browser._override("Mozilla/5.0 Chrome/130", hints)
        assert override is not None
        meta = override["userAgentMetadata"]
        self.assertEqual(override["userAgent"], "Mozilla/5.0 Chrome/130")
        self.assertEqual(meta["brands"], [{"brand": "Chromium", "version": "130"}])
        self.assertEqual(meta["fullVersionList"], [{"brand": "Chromium", "version": "130.0.1"}])
        self.assertEqual((meta["platform"], meta["platformVersion"], meta["architecture"]),
                         ("macOS", "15.0.0", "arm"))
        self.assertIsNone(_browser._override("Mozilla/5.0", None))
        self.assertIsNone(_browser._override("Mozilla/5.0", {}))

    def test_the_hidden_script_names_this_platforms_gpu_and_adds_the_toolbar(self):
        script = _browser.hidden_script()
        key = sys.platform if sys.platform in _browser.SOFTWARE_GL else "linux"
        self.assertIn(json.dumps(_browser.SOFTWARE_GL[key]), script)
        self.assertIn(f"innerHeight + {_browser.TOOLBAR}", script)


if __name__ == "__main__":
    unittest.main()
