"""
Unit tests for flnd utils using standard unittest.
"""

import io
import tempfile
import unittest
from pathlib import Path

import httpx

from flnd.utils import sanitize_name, download_file, scroll_page_to_end, show_steps
from shared import theme
from shared._theme import Screen


class MockPage:
    def __init__(self, heights=None):
        self.heights = heights or [1000]
        self.call_idx = 0
        self.wheel_calls = 0
        self.timeouts = 0

    class Mouse:
        def __init__(self, outer):
            self.outer = outer
        def wheel(self, x, y):
            self.outer.wheel_calls += 1

    @property
    def mouse(self):
        return self.Mouse(self)

    def wait_for_timeout(self, ms):
        self.timeouts += 1

    def evaluate(self, script):
        idx = min(self.call_idx, len(self.heights) - 1)
        val = self.heights[idx]
        self.call_idx += 1
        return val


class TestUtils(unittest.TestCase):
    def setUp(self):
        self.screen = Screen(out=io.StringIO(), inp=io.StringIO(""), width=200, tty=False, interactive=False)
        scope = theme.scope(screen=self.screen, color="none", unicode=True)
        scope.__enter__()
        self.addCleanup(scope.__exit__, None, None, None)

    def client(self, response):
        return httpx.Client(transport=httpx.MockTransport(lambda request: response))

    def test_sanitize_name(self):
        self.assertEqual(sanitize_name("My:Album*Name? / 2026"), "My_Album_Name_2026")
        self.assertEqual(sanitize_name("   hello   world   "), "hello_world")

    def test_download_deduplication(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            dest = Path(tmp_dir) / "images" / "test.jpg"
            dest.parent.mkdir(parents=True)
            dest.write_bytes(b"dummy image bytes")

            # Downloading to an existing file must skip (returns False)
            downloaded = download_file("http://fake.example.com/test.jpg", dest)
            self.assertFalse(downloaded)
        self.assertEqual(self.screen.out.getvalue(), "• skip test.jpg\n")

    def test_download_saves_with_a_bar(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            dest = Path(tmp_dir) / "posts" / "a.jpg"
            with self.client(httpx.Response(200, content=b"x" * 1536)) as client:
                self.assertTrue(download_file("https://media.example/a.jpg", dest, client=client))
            self.assertEqual(dest.read_bytes(), b"x" * 1536)
            self.assertFalse(dest.with_suffix(".jpg.part").exists())
        self.assertEqual(self.screen.out.getvalue(), "✓ posts/a.jpg ━━━━━━━━━━━━━━━━━━━━ 1.5 KB/1.5 KB\n")

    def test_download_http_error_ends_the_bar_at_error(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            dest = Path(tmp_dir) / "posts" / "a.jpg"
            with self.client(httpx.Response(404)) as client:
                self.assertFalse(download_file("https://media.example/a.jpg", dest, client=client))
            self.assertEqual(list(dest.parent.iterdir()), [])
        self.assertEqual(self.screen.out.getvalue(), "✗ posts/a.jpg HTTP 404 for URL: https://media.example/a.jpg\n")

    def test_scroll_reports_each_step(self):
        steps = []
        scroll_page_to_end(MockPage([1000]), max_idle_rounds=2, step_delay_ms=1, on_step=steps.append)
        self.assertEqual(steps, [1, 2, 3])
        self.assertEqual(show_steps(3), "3 steps")

    def test_scroll_page_to_end_stops_on_idle(self):
        # Height never changes, should stop after max_idle_rounds
        page = MockPage([1000, 1000, 1000, 1000, 1000])
        scroll_page_to_end(page, max_idle_rounds=3, step_delay_ms=1)
        self.assertTrue(page.wheel_calls >= 3)
        self.assertTrue(page.wheel_calls <= 6)

    def test_scroll_page_to_end_stops_on_limit(self):
        page = MockPage([1000, 2000, 3000, 4000])
        items = []
        def get_count():
            return len(items)

        def custom_scroll(p):
            items.append("item")

        scroll_page_to_end(page, get_count=get_count, limit=3, step_delay_ms=1, custom_scroll_action=custom_scroll)
        self.assertEqual(len(items), 3)

    def test_scroll_page_to_end_stops_on_has_more_false(self):
        page = MockPage([1000, 2000, 3000])
        has_more = [True]
        call_count = [0]
        def check():
            call_count[0] += 1
            if call_count[0] >= 2:
                has_more[0] = False
            return has_more[0]

        scroll_page_to_end(page, check_has_more=check, step_delay_ms=1)
        self.assertFalse(has_more[0])


if __name__ == "__main__":
    unittest.main()
