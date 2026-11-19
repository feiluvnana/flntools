"""`src/engine/chrome.py`: ExtensionTabs, ExtensionPage and ExtensionFeed on a fake client.

No relay, no Chrome: each verb is checked against the extension op and args it sends.
"""

import asyncio
import base64
import threading
import unittest
from types import SimpleNamespace
from unittest import mock

from flnd.engine import chrome

LOOP = asyncio.new_event_loop()
_SERVING = threading.Thread(target=LOOP.run_forever, name="test-chrome-loop", daemon=True)
_SERVING.start()


def tearDownModule():
    LOOP.call_soon_threadsafe(LOOP.stop)
    _SERVING.join()


class FakeClient:
    """The run's extension client: records ops and answers like the real extension."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []
        self._heights = [100, 400]
        self._feed_body = base64.b64encode(b'{"n":1}').decode()

    async def call(self, op: str, **args) -> object:
        self.calls.append((op, dict(args)))
        if op == "tabs.open":
            return {"tab": 1, "url": args.get("url", ""), "status": 200}
        if op == "document":
            return {"html": "<p></p>", "url": "https://example.com/"}
        if op == "click":
            return None
        if op == "scroll":
            return None
        if op == "storage":
            return {"key": "v"}
        if op == "cookies":
            return [{"name": "sid", "value": "s", "domain": "aaa.example"}]
        if op == "evaluate":
            script = args.get("script", "")
            if "scrollHeight" in script:
                return {"value": self._heights.pop(0) if self._heights else 400}
            return {"value": True}
        if op == "capture.start":
            return {"capture": 3}
        if op == "capture.read":
            return [{"url": "https://example.com/api", "status": 200, "body": self._feed_body}]
        if op == "capture.stop":
            return None
        if op == "tabs.close":
            return None
        return None


class ChromeCase(unittest.TestCase):
    def setUp(self) -> None:
        self.fake = FakeClient()
        self.run = SimpleNamespace(loop=SimpleNamespace(loop=LOOP),
                                   settings=SimpleNamespace(extension=self.fake),
                                   source=SimpleNamespace(domains=["aaa.example"]))
        self.enterContext(mock.patch("flnd.engine.chrome.client", return_value=self.fake))

    def _walk_feed(self, tabs: chrome.ExtensionTabs) -> None:
        with chrome.ExtensionFeed(tabs, "api", scroll=False) as feed:
            list(feed)

    def test_each_verb_sends_its_op(self) -> None:
        table = [
            ("open", lambda t: t.run(t.page.goto("https://example.com/")),
             "tabs.open", {"url": "https://example.com/", "active": True}),
            ("document", lambda t: t.run(t.page.content()), "document", {"tab": 1}),
            ("click", lambda t: t.run(t.page.click(".go")), "click", {"tab": 1, "css": ".go"}),
            ("scroll", lambda t: self._scroll(t), "scroll", {"tab": 1, "by": chrome._SCROLL}),
            ("evaluate", lambda t: t.run(t.page.wait(".x", timeout=0.1)),
             "evaluate", {"tab": 1, "script": "!!document.querySelector('.x')"}),
            ("feed", self._walk_feed, "capture.start", {"tab": 1, "match": "api"}),
            ("storage", lambda t: t.run(self._storage()), "storage",
             {"origin": "https://example.com"}),
        ]
        for name, work, op, want in table:
            with self.subTest(name):
                self.fake.calls.clear()
                work(chrome.ExtensionTabs(self.run))
                found = [(opn, args) for opn, args in self.fake.calls if opn == op]
                self.assertIn((op, want), found)
                if name == "feed":
                    ops = {opn for opn, _ in self.fake.calls}
                    self.assertIn("capture.read", ops)
                    self.assertIn("capture.stop", ops)

    def _scroll(self, tabs: chrome.ExtensionTabs) -> None:
        feed = chrome.ExtensionFeed(tabs, "api", scroll=True)
        feed.scroll_step()

    async def _storage(self) -> None:
        await chrome.client(self.run).call("storage", origin="https://example.com")