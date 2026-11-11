"""flnd/net.py: retries, signing, backing off, mirrors and atomic downloads, on httpx.MockTransport."""

import io
import stat
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import httpx

from flnd import net
from flnd.sources.base import FetchError
from shared import theme
from shared._theme import Screen


class Broken(httpx.SyncByteStream):
    """A body that sends one chunk, then drops the connection."""

    def __iter__(self):
        yield b"half"
        raise httpx.ReadError("connection reset")


class NetCase(unittest.TestCase):
    def setUp(self):
        self.screen = Screen(out=io.StringIO(), inp=io.StringIO(""), width=300, tty=False, interactive=False)
        scope = theme.scope(screen=self.screen, color="none", unicode=True)
        scope.__enter__()
        self.addCleanup(scope.__exit__, None, None, None)
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.tmp = Path(folder.name)
        self.now = 0.0
        self.sleeps = []
        for name, value in (("_sleep", self.sleep), ("_clock", lambda: self.now)):
            patcher = mock.patch.object(net, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        # No jitter, so the waits are exact.
        patcher = mock.patch.object(net._random, "uniform", lambda low, high: 1.0)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.requests = []
        self.signatures = []
        self.answers = []

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.now += seconds

    def serve(self, request):
        self.requests.append(request)
        self.signatures.append(request.headers.get("x-signature"))
        answer = self.answers.pop(0) if len(self.answers) > 1 else self.answers[0]
        if isinstance(answer, Exception):
            raise answer
        return answer

    def client(self, *answers, sign=lambda request: request, wait=1.0, headers=None, jar=()):
        self.answers = list(answers)
        self.backoff = net.Backoff("ins")
        with mock.patch.object(net, "_base_transport", lambda: httpx.MockTransport(self.serve)):
            return net.client(user_agent=None, headers=headers or {}, jar=list(jar), sign=sign, wait=wait,
                              backoff=self.backoff)

    def lines(self):
        return self.screen.out.getvalue().splitlines()


class ClientTest(NetCase):
    def test_headers_cookies_and_httpx_user_agent(self):
        jar = [{"name": "sid", "value": "1", "domain": ".site.example", "path": "/"}]
        with self.client(httpx.Response(200), headers={"x-app": "7"}, jar=jar) as http:
            http.get("https://api.site.example/a")
        request = self.requests[0]
        self.assertEqual((request.headers["x-app"], request.headers["cookie"]), ("7", "sid=1"))
        self.assertTrue(request.headers["user-agent"].startswith("python-httpx/"))

    def test_source_user_agent(self):
        with mock.patch.object(net, "_base_transport", lambda: httpx.MockTransport(self.serve)):
            self.answers = [httpx.Response(200)]
            with net.client(user_agent="Agent/1", headers={}, jar=[], sign=lambda r: r, wait=1.0,
                            backoff=net.Backoff("tik")) as http:
                http.get("https://a.example/")
        self.assertEqual(self.requests[0].headers["user-agent"], "Agent/1")

    def test_a_dead_connection_costs_three_attempts(self):
        refused = httpx.ConnectError("refused")
        with self.client(refused) as http, self.assertRaises(httpx.ConnectError):
            http.get("https://a.example/")
        self.assertEqual(len(self.requests), 3)
        self.assertEqual(self.sleeps, [1.0, 2.0])

    def test_5xx_then_success_with_retry_after(self):
        busy = httpx.Response(503, headers={"retry-after": "7"})
        with self.client(busy, httpx.Response(200, json={"ok": 1})) as http:
            self.assertEqual(http.get("https://a.example/").json(), {"ok": 1})
        self.assertEqual(self.sleeps, [7.0])

    def test_retry_after_is_capped(self):
        with self.client(httpx.Response(503, headers={"retry-after": "900"}), httpx.Response(200)) as http:
            http.get("https://a.example/")
        self.assertEqual(self.sleeps, [60.0])

    def test_last_response_is_returned_when_attempts_run_out(self):
        with self.client(httpx.Response(500)) as http:
            self.assertEqual(http.get("https://a.example/").status_code, 500)
        self.assertEqual(len(self.requests), 3)

    def test_4xx_is_not_retried(self):
        with self.client(httpx.Response(404)) as http:
            self.assertEqual(http.get("https://a.example/").status_code, 404)
        self.assertEqual(len(self.requests), 1)

    def test_signing_runs_before_every_attempt(self):
        stamps = iter(range(100))

        def sign(request):
            request.headers["x-signature"] = str(next(stamps))
            return request

        with self.client(httpx.ConnectError("refused"), httpx.Response(500), httpx.Response(200), sign=sign) as http:
            http.get("https://a.example/")
        self.assertEqual(self.signatures, ["0", "1", "2"])

    def test_the_source_wait_is_used(self):
        seen = []

        def wait(error, attempt):
            seen.append((type(error).__name__, attempt))
            return 0.5 if attempt == 1 else None

        with self.client(httpx.ConnectError("x"), httpx.ConnectError("x"), httpx.Response(200), wait=wait) as http:
            http.get("https://a.example/")
        self.assertEqual(seen, [("ConnectError", 1), ("ConnectError", 2)])
        self.assertEqual(self.sleeps, [0.5, 2.0])
        with self.client(httpx.ConnectError("x"), httpx.Response(200), wait=3.0) as http:
            http.get("https://a.example/")
        self.assertEqual(self.sleeps[-1], 3.0)


class BackoffTest(NetCase):
    def test_429_is_waited_out_and_sent_again(self):
        with self.client(httpx.Response(429), httpx.Response(429), httpx.Response(200)) as http:
            self.assertEqual(http.get("https://a.example/").status_code, 200)
        self.assertEqual(sum(self.sleeps), 90.0)
        warnings = [line for line in self.lines() if line.startswith("!")]
        self.assertEqual(warnings, ["! ins: rate limited; waiting 30.0s (attempt 1)",
                                    "! ins: rate limited; waiting 1m (attempt 2)"])
        self.assertEqual(self.backoff.strikes, 0)

    def test_a_rate_limit_never_stops_and_waits_double_up_to_15_minutes(self):
        answers = [httpx.Response(429)] * 7 + [httpx.Response(200)]
        with self.client(*answers) as http:
            self.assertEqual(http.get("https://a.example/").status_code, 200)
        # 429s are not attempts: eight requests, where three attempts are the limit for errors.
        self.assertEqual(len(self.requests), 8)
        self.assertEqual(sum(self.sleeps), 30 + 60 + 120 + 240 + 480 + 900 + 900)
        self.assertIn("! ins: rate limited; waiting 15m (attempt 7)", self.lines())

    def test_retry_after_is_honored_without_a_cap(self):
        with self.client(httpx.Response(429, headers={"retry-after": "3600"}), httpx.Response(200)) as http:
            http.get("https://a.example/")
        self.assertEqual(sum(self.sleeps), 3600.0)
        self.assertIn("! ins: rate limited; waiting 1h (attempt 1)", self.lines())

    def test_the_wait_shows_a_countdown_row(self):
        backoff = net.Backoff("ins")
        backoff.hit("challenged")
        backoff.wait_turn()
        self.assertEqual(self.sleeps, [1.0] * 30)
        self.assertTrue(any(line.startswith("✓ ins: waiting") and "30.0s" in line for line in self.lines()),
                        self.lines())

    def test_jitter_is_20_percent(self):
        for pick, expected in ((min, 24.0), (max, 36.0)):
            self.now = 0.0
            backoff = net.Backoff("ins")
            with mock.patch.object(net._random, "uniform", pick):
                backoff.hit("rate limited")
            backoff.wait_turn()
            self.assertAlmostEqual(self.now, expected)

    def test_a_burst_is_one_wait_and_success_clears(self):
        backoff = net.Backoff("ins")
        backoff.hit("rate limited")
        backoff.hit("rate limited")
        self.assertEqual(backoff.strikes, 1)
        backoff.wait_turn()
        backoff.hit("rate limited")
        self.assertEqual(backoff.strikes, 2)
        backoff.wait_turn()
        backoff.clear()
        backoff.hit("rate limited")
        self.assertEqual(backoff.strikes, 1)

    def test_ctrl_c_ends_the_wait(self):
        backoff = net.Backoff("ins")
        backoff.hit("rate limited")
        net.stopping.set()
        self.addCleanup(net.stopping.clear)
        with self.assertRaises(KeyboardInterrupt):
            backoff.wait_turn()
        self.assertEqual(self.sleeps, [])

    def test_pause_jitter_is_40_percent(self):
        for pick, expected in ((min, 6.0), (max, 14.0)):
            with mock.patch.object(net._random, "uniform", pick):
                self.assertAlmostEqual(net.jittered(10.0), expected)


class DownloadTest(NetCase):
    def download(self, http, source, name="a.jpg", **options):
        return net.download(http, source, self.tmp / "posts" / name, label=f"posts/{name}", backoff=self.backoff,
                            **options)

    def test_atomic_with_a_bar_and_mode_0644(self):
        with self.client(httpx.Response(200, content=b"x" * 1536)) as http:
            self.assertTrue(self.download(http, "https://cdn.example/a.jpg", headers={"Referer": "https://site/"}))
        dest = self.tmp / "posts" / "a.jpg"
        self.assertEqual(dest.read_bytes(), b"x" * 1536)
        self.assertEqual(stat.S_IMODE(dest.stat().st_mode) & 0o644, 0o644)
        self.assertEqual(list(dest.parent.iterdir()), [dest])
        self.assertEqual(self.requests[0].headers["referer"], "https://site/")
        self.assertEqual(self.lines(), ["✓ posts/a.jpg ━━━━━━━━━━━━━━━━━━━━ 1.5 KB/1.5 KB"])

    def test_a_streaming_error_is_retried_and_no_part_is_left(self):
        with self.client(httpx.Response(200, stream=Broken()), httpx.Response(200, content=b"whole")) as http:
            self.assertTrue(self.download(http, "https://cdn.example/a.jpg"))
        self.assertEqual((self.tmp / "posts" / "a.jpg").read_bytes(), b"whole")
        self.assertEqual(len(self.requests), 2)
        self.assertEqual([path.name for path in (self.tmp / "posts").iterdir()], ["a.jpg"])

    def test_a_dead_connection_costs_three_attempts_and_is_raised(self):
        with self.client(httpx.ConnectError("refused")) as http, self.assertRaises(httpx.ConnectError):
            self.download(http, "https://cdn.example/a.jpg")
        self.assertEqual(len(self.requests), 3)
        self.assertEqual(list((self.tmp / "posts").iterdir()), [])

    def test_a_short_body_is_retried(self):
        short = httpx.Response(200, headers={"content-length": "10"}, content=b"12345")
        with self.client(short, httpx.Response(200, content=b"1234567890")) as http:
            self.assertTrue(self.download(http, "https://cdn.example/a.jpg"))
        self.assertEqual(len(self.requests), 2)

    def test_a_refusal_is_logged_and_not_retried(self):
        with self.client(httpx.Response(403)) as http:
            self.assertFalse(self.download(http, "https://cdn.example/a.jpg"))
        self.assertEqual(len(self.requests), 1)
        self.assertIn("✗ posts/a.jpg: HTTP 403 for https://cdn.example/a.jpg", self.lines())
        self.assertFalse((self.tmp / "posts" / "a.jpg").exists())

    def test_mirrors_are_tried_in_order(self):
        with self.client(httpx.Response(404), httpx.Response(200, content=b"7z")) as http:
            self.assertTrue(self.download(http, ["https://a.example/f.7z", "https://b.example/f.7z"], "f.7z"))
        self.assertEqual([request.url.host for request in self.requests], ["a.example", "b.example"])
        self.assertIn("! mirror 1 failed; trying next", self.lines())

    def test_a_fetch_function_with_progress(self):
        def fetch(dest, progress):
            dest.write_bytes(b"media")
            progress(5, 5)

        with self.client(httpx.Response(200)) as http:
            self.assertTrue(self.download(http, fetch, "m.mp4"))
        self.assertEqual((self.tmp / "posts" / "m.mp4").read_bytes(), b"media")
        self.assertEqual(self.requests, [])
        self.assertEqual(self.lines(), ["✓ posts/m.mp4 ━━━━━━━━━━━━━━━━━━━━ 5.0 B/5.0 B"])

    def test_a_429_on_a_download_pauses_and_retries(self):
        with self.client(httpx.Response(429), httpx.Response(200, content=b"ok")) as http:
            self.assertTrue(self.download(http, "https://cdn.example/a.jpg"))
        self.assertEqual(sum(self.sleeps), 30.0)

    def test_a_429_during_a_mirror_is_waited_out_on_that_mirror(self):
        with self.client(httpx.Response(429), httpx.Response(429), httpx.Response(200, content=b"ok")) as http:
            self.assertTrue(self.download(http, ["https://a.example/f", "https://b.example/f"]))
        self.assertEqual({request.url.host for request in self.requests}, {"a.example"})
        self.assertNotIn("! mirror 1 failed; trying next", self.lines())


class FollowTest(NetCase):
    def test_follows_redirects_with_one_head(self):
        def serve(request):
            self.requests.append(request)
            if request.url.host == "vm.tiktok.com":
                return httpx.Response(301, headers={"location": "https://www.tiktok.com/@a/video/7301234567890123456"})
            return httpx.Response(200)

        with mock.patch.object(net, "_base_transport", lambda: httpx.MockTransport(serve)):
            self.assertEqual(net.follow("https://vm.tiktok.com/ZM1/"), "https://www.tiktok.com/@a/video/7301234567890123456")
        self.assertEqual({request.method for request in self.requests}, {"HEAD"})

    def test_retry_after_dates(self):
        self.assertIsNone(net.retry_after_seconds("soon"))
        self.assertEqual(net.retry_after_seconds("Wed, 21 Oct 2015 07:28:00 GMT"), 0.0)

    def test_fetch_error_keeps_the_status(self):
        self.assertEqual(FetchError("x", status=403).status, 403)


if __name__ == "__main__":
    unittest.main()
