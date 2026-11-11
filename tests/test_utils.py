"""
Unit tests for flnd.utils: save, Downloads and the download history, and scroll.
"""

import io
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import httpx

from flnd.target import Target
from flnd.utils import SCROLL_AFTER_WAIT, Downloads, save, scroll, show_steps
from shared import http, store, theme
from shared import _http, _worker
from shared._theme import Screen


def client_for(handler):
    return http.Client(transport=httpx.MockTransport(handler))


class FakePage:
    """Answers browser.scroll's height question from a list, and records every other script."""

    def __init__(self, heights):
        self.heights = list(heights)
        self.scripts = []
        self.wheels = 0
        self.mouse = mock.Mock(wheel=self.wheel)

    def wheel(self, x, y):
        self.wheels += 1
        self.scripts.append(f"wheel {y}")

    def wait_for_timeout(self, ms):
        pass

    def evaluate(self, script):
        if "scrollHeight" in script and "scrollTo" not in script:
            return self.heights.pop(0) if len(self.heights) > 1 else self.heights[0]
        self.scripts.append(script)
        return None


class UtilsCase(unittest.TestCase):
    def setUp(self):
        self.screen = Screen(out=io.StringIO(), inp=io.StringIO(""), width=200, tty=False, interactive=False)
        scope = theme.scope(screen=self.screen, color="none", unicode=True)
        scope.__enter__()
        self.addCleanup(scope.__exit__, None, None, None)
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)
        for module in (_http, _worker):
            patcher = mock.patch.object(module, "_sleep", lambda seconds: None)
            patcher.start()
            self.addCleanup(patcher.stop)

    def lines(self):
        return self.screen.out.getvalue().splitlines()


class SaveTest(UtilsCase):
    def test_skip_when_the_file_exists(self):
        dest = self.tmp / "images" / "test.jpg"
        dest.parent.mkdir(parents=True)
        dest.write_bytes(b"dummy image bytes")
        with client_for(lambda request: self.fail("no request for a file that exists")) as client:
            self.assertFalse(save(client, "https://media.example/test.jpg", dest))
        self.assertEqual(self.lines(), ["• skip test.jpg"])

    def test_success_draws_a_bar_and_records_history(self):
        dest = self.tmp / "posts" / "a.jpg"
        seen = []
        with store.open(self.tmp / "state.db") as db, client_for(
            lambda request: seen.append(request.headers["Referer"]) or httpx.Response(200, content=b"x" * 1536)
        ) as client:
            history = db.table("downloads")
            self.assertTrue(save(client, "https://media.example/a.jpg", dest,
                                 headers={"Referer": "https://site.example/"}, key="ins:a", history=history))
            row = history["ins:a"]
        self.assertEqual(dest.read_bytes(), b"x" * 1536)
        self.assertFalse(dest.with_name("a.jpg.part").exists())
        self.assertEqual(seen, ["https://site.example/"])
        self.assertEqual((row["path"], row["size"]), (str(dest), 1536))
        self.assertRegex(row["at"], r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\+00:00$")
        self.assertEqual(self.lines(), ["✓ posts/a.jpg ━━━━━━━━━━━━━━━━━━━━ 1.5 KB/1.5 KB"])

    def test_http_error_returns_false_and_logs(self):
        dest = self.tmp / "posts" / "a.jpg"
        with store.open(self.tmp / "state.db") as db, client_for(lambda request: httpx.Response(404)) as client:
            history = db.table("downloads")
            self.assertFalse(save(client, "https://media.example/a.jpg", dest, key="ins:a", history=history))
            self.assertEqual(len(history), 0)
        self.assertFalse(dest.exists() or dest.with_name("a.jpg.part").exists())
        self.assertIn("✗ posts/a.jpg: HTTP 404 for https://media.example/a.jpg", self.lines())

    def test_network_error_is_raised_for_the_pool_to_retry(self):
        def refuse(request):
            raise httpx.ConnectError("refused", request=request)

        with client_for(refuse) as client, self.assertRaises(httpx.ConnectError):
            save(client, "https://media.example/a.jpg", self.tmp / "posts" / "a.jpg")


class DownloadsTest(UtilsCase):
    def downloads(self, limit=None):
        return Downloads("ins", Target("ins", "someone", "ins:someone"), self.tmp / "downloads", limit)

    def test_saves_in_the_pool_and_records_history(self):
        bodies = {"/a.jpg": b"a", "/b.jpg": b"bb", "/gone.jpg": None}

        def serve(request):
            body = bodies[request.url.path]
            return httpx.Response(200, content=body) if body else httpx.Response(404)

        folder = self.tmp / "downloads" / "ins" / "someone" / "posts"
        (folder / "old.jpg").parent.mkdir(parents=True)
        (folder / "old.jpg").write_bytes(b"old")
        with client_for(serve) as client, self.downloads() as files:
            self.assertTrue(files.add(client, "https://m.example/a.jpg", folder / "a.jpg", key="a"))
            self.assertTrue(files.add(client, "https://m.example/b.jpg", folder / "b.jpg", key="b"))
            self.assertFalse(files.add(client, "https://m.example/a.jpg", folder / "a.jpg", key="a"))
            self.assertTrue(files.add(client, "https://m.example/gone.jpg", folder / "gone.jpg", key="gone"))
            self.assertTrue(files.add(client, "https://m.example/old.jpg", folder / "old.jpg", key="old"))
        self.assertEqual(files.pool.submitted, 4)
        self.assertEqual(files.saved, 2)
        self.assertEqual((folder / "b.jpg").read_bytes(), b"bb")
        with store.open(self.tmp / "state.db") as db:
            history = dict(db.table("downloads").items())
        self.assertEqual(sorted(history), ["ins:a", "ins:b"])
        self.assertEqual(history["ins:b"]["size"], 2)
        self.assertIn("• skip a.jpg", self.lines())
        self.assertIn("• skip old.jpg", self.lines())
        self.assertIn("✗ posts/gone.jpg: HTTP 404 for https://m.example/gone.jpg", self.lines())

    def test_limit_counts_queued_files(self):
        with client_for(lambda request: httpx.Response(200, content=b"x")) as client, \
                self.downloads(limit=2) as files:
            added = [files.add(client, f"https://m.example/{n}.jpg", self.tmp / "p" / f"{n}.jpg", key=str(n))
                     for n in range(4)]
            self.assertTrue(files.full)
        self.assertEqual(added, [True, True, False, False])
        self.assertEqual(files.saved, 2)
        self.assertIn("• Reached limit of 2 downloads.", self.lines())

    def test_network_errors_are_retried_twice_then_logged(self):
        attempts = []

        def refuse(request):
            attempts.append(request.url.path)
            raise httpx.ConnectError("refused", request=request)

        with http.Client(transport=httpx.MockTransport(refuse), retries=0) as client, self.downloads() as files:
            files.add(client, "https://m.example/a.jpg", self.tmp / "posts" / "a.jpg", key="a")
        self.assertEqual(len(attempts), 3)
        self.assertEqual(files.saved, 0)
        self.assertIn("✗ posts/a.jpg: ConnectError refused", self.lines())


class ScrollTest(UtilsCase):
    def test_wheel_steps_and_500_more_after_each_wait(self):
        page = FakePage([1000, 2000, 2000, 2000, 2000, 2000])
        scroll(page, "posts", count=lambda: 0, more=lambda: True, delay=0)
        # Two steps that grow the page, then four idle ones.
        self.assertEqual(page.wheels, 6)
        self.assertEqual(page.scripts[:4], ["wheel 2000", SCROLL_AFTER_WAIT, "wheel 2000", SCROLL_AFTER_WAIT])
        self.assertEqual(self.lines(), ["✓ scroll posts 6 steps"])
        self.assertEqual(show_steps(3), "3 steps")

    def test_more_and_count(self):
        found = [0]
        page = FakePage([1000])

        def step(p):
            found[0] += 1

        scroll(page, "profile", count=lambda: found[0], more=lambda: found[0] < 3, delay=0, step=step)
        self.assertEqual(found[0], 3)

    def test_a_failing_step_is_ignored(self):
        page = FakePage([1000])
        page.mouse.wheel = mock.Mock(side_effect=RuntimeError("closed"))
        scroll(page, "posts", count=lambda: 0, more=lambda: True, delay=0)
        self.assertEqual(page.mouse.wheel.call_count, 5)


if __name__ == "__main__":
    unittest.main()
