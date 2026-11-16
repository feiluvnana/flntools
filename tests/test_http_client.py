"""`http.Client`: the token bucket per host, 429 waits, and staged, resumed, segmented downloads.

The network is `http.fake`, and waits a fake `clock`, so nothing opens a socket or sleeps.
"""

import io
import tempfile
import threading
import unittest
from pathlib import Path
from typing import Iterator
from unittest import mock

import httpx

from flnd import _time as clock, _http as http
from flnd._ui import ui

SITE = "https://www.example.com"
FILE = f"{SITE}/file.bin"
BODY = bytes(range(256)) * 16 + b"tail"


class Cut(httpx.SyncByteStream):
    """A body that sends `head`, then drops the connection."""

    def __init__(self, head: bytes):
        self.head = head

    def __iter__(self) -> Iterator[bytes]:
        yield self.head
        raise httpx.ReadError("connection reset")


class Sink:
    """A progress sink that keeps what it was told."""

    def __init__(self) -> None:
        self.total: float | None = None
        self.count = 0.0
        self.lock = threading.Lock()

    def advance(self, n: float) -> None:
        with self.lock:
            self.count += n


class ClientCase(unittest.TestCase):
    def setUp(self) -> None:
        self.fake = self.enterContext(clock.use(clock.Fake()))
        self.enterContext(mock.patch.object(http.random, "uniform", lambda low, high: high))
        self.enterContext(ui.scope(dest=io.StringIO(), color=False, width=80))
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.dest = Path(folder.name) / "file.bin"
        self.stage = self.dest.with_name(".file.bin.stage")
        self.sent: list[tuple[float, httpx.Request]] = []

    def client(self, **options) -> http.Client:
        client = http.Client(**options)
        self.addCleanup(client.close)
        return client

    def logged(self, answer):
        """A route that records each request, then answers as `answer` would."""
        def route(request: httpx.Request) -> httpx.Response:
            self.sent.append((self.fake.now, request))
            return http._answer(answer(request) if callable(answer) else answer, request)

        return route

    def ranges(self) -> list[str | None]:
        return [request.headers.get("range") for _, request in self.sent]


class PaceTest(ClientCase):
    def test_a_rate_is_a_token_bucket_per_host_one_token_deep(self):
        client = self.client(rate=2.0)
        with http.fake({SITE + "/*": self.logged(b"ok"), "https://cdn.example.com/*": self.logged(b"ok")}):
            for path in ("a", "b", "c"):
                client.fetch(f"{SITE}/{path}")
            client.fetch("https://cdn.example.com/a")
            self.fake.advance(10.0)
            client.fetch(f"{SITE}/d")
            client.fetch(f"{SITE}/e")
        self.assertEqual([at for at, _ in self.sent], [0.0, 0.5, 1.0, 1.0, 11.0, 11.5])
        self.assertEqual(self.fake.slept, [0.5, 0.5, 0.5])

    def test_threads_waiting_together_queue_one_interval_apart(self):
        client = self.client(rate=4.0)
        with http.fake({SITE + "/*": b"ok"}):
            for _ in range(4):
                client._pace.wait("www.example.com", None)
        self.assertEqual(self.fake.slept, [0.25, 0.25, 0.25])

    def test_a_429_honours_retry_after_and_a_rate_limit_reset(self):
        client = self.client()
        answers = iter([httpx.Response(429, headers={"retry-after": "12"}), httpx.Response(200)])
        with http.fake({SITE + "/*": lambda request: next(answers)}):
            self.assertEqual(client.fetch(f"{SITE}/a").status, 200)
        reset = str(int(clock.wall()) + 20)
        answers = iter([httpx.Response(429, headers={"x-rate-limit-reset": reset}), httpx.Response(200)])
        with http.fake({SITE + "/*": lambda request: next(answers)}):
            self.assertEqual(client.fetch(f"{SITE}/b").status, 200)
        self.assertEqual(self.fake.slept, [12.0, 21.0])

    def test_stop_ends_a_wait(self):
        stop = threading.Event()
        stop.set()
        with http.fake({SITE + "/*": 429}), self.assertRaises(http.StoppedError):
            self.client(stop=stop).fetch(f"{SITE}/a")


class DownloadTest(ClientCase):
    def test_writes_reach_the_disk_1_mib_at_a_time(self):
        size = 3 << 20
        seen: list[int] = []

        def pieces() -> Iterator[bytes]:
            for _ in range(size // 65536):
                seen.append(self.stage.stat().st_size if self.stage.exists() else 0)
                yield bytes(65536)

        route = lambda request: httpx.Response(200, headers={"content-length": str(size)}, content=pieces())
        with http.fake({FILE: route}):
            self.client().download(FILE, self.dest)
        self.assertEqual(sorted(set(seen)), [0, 1 << 20, 2 << 20])
        self.assertEqual(self.dest.stat().st_size, size)

    def test_returns_the_path_and_reports_to_a_sink(self):
        sink = Sink()
        with http.fake({FILE: BODY}):
            saved = http.download(FILE, self.dest, progress=sink)
        self.assertEqual((saved, saved.read_bytes()), (self.dest, BODY))
        self.assertEqual((sink.total, sink.count), (len(BODY), len(BODY)))
        self.assertFalse(self.stage.exists())

    def test_resumes_a_partial_stage_with_a_range(self):
        self.stage.write_bytes(BODY[:6])
        sink = Sink()
        with http.fake({FILE: self.logged(BODY)}):
            self.client().download(FILE, self.dest, progress=sink)
        self.assertEqual(self.dest.read_bytes(), BODY)
        self.assertEqual(self.ranges(), ["bytes=6-"])
        self.assertEqual((sink.total, sink.count), (len(BODY), len(BODY)))

    def test_a_dropped_connection_resumes_on_the_next_try(self):
        cut = httpx.Response(200, headers={"content-length": str(len(BODY))}, stream=Cut(BODY[:100]))
        answers = iter([cut, BODY])
        with http.fake({FILE: self.logged(lambda request: next(answers))}):
            self.client().download(FILE, self.dest)
        self.assertEqual(self.dest.read_bytes(), BODY)
        self.assertEqual(self.ranges(), ["bytes=0-", "bytes=100-"])

    def test_a_416_on_a_whole_stage_commits_it(self):
        self.stage.write_bytes(BODY)
        with http.fake({FILE: self.logged(BODY)}):
            self.client().download(FILE, self.dest)
        self.assertEqual(self.dest.read_bytes(), BODY)
        self.assertEqual(self.ranges(), [f"bytes={len(BODY)}-"])

    def test_a_416_for_another_size_starts_over(self):
        self.stage.write_bytes(BODY + b"stale")
        with http.fake({FILE: self.logged(BODY)}):
            self.client().download(FILE, self.dest)
        self.assertEqual(self.dest.read_bytes(), BODY)
        self.assertEqual(self.ranges(), [f"bytes={len(BODY) + 5}-", "bytes=0-"])

    def test_a_failed_mirror_drops_its_stage_before_the_next(self):
        cut = lambda request: httpx.Response(200, headers={"content-length": "5000"}, stream=Cut(b"A" * 100))
        routes = {f"{SITE}/a/file.bin": self.logged(cut), f"{SITE}/b/file.bin": self.logged(BODY)}
        with http.fake(routes):
            self.client(retry=http.Retry(1)).download([f"{SITE}/a/file.bin", f"{SITE}/b/file.bin"], self.dest)
        self.assertEqual(self.dest.read_bytes(), BODY)
        self.assertEqual(self.ranges(), ["bytes=0-", "bytes=0-"])

    def test_a_refusal_from_every_mirror_raises_and_keeps_nothing_in_place(self):
        with http.fake({SITE + "/*": 404}), self.assertRaises(http.RefusedError):
            self.client().download([f"{SITE}/a", f"{SITE}/b"], self.dest)
        self.assertFalse(self.dest.exists())

    def test_a_stop_keeps_the_stage_for_the_next_run(self):
        stop = threading.Event()

        def pieces() -> Iterator[bytes]:
            yield BODY[:10]
            stop.set()
            yield BODY[10:]

        route = lambda request: httpx.Response(200, headers={"content-length": str(len(BODY))}, content=pieces())
        with http.fake({FILE: route}), self.assertRaises(http.StoppedError):
            self.client(stop=stop).download(FILE, self.dest)
        self.assertEqual(self.stage.read_bytes(), BODY[:10])
        self.assertFalse(self.dest.exists())


class SegmentTest(ClientCase):
    def setUp(self) -> None:
        super().setUp()
        self.enterContext(mock.patch.object(http, "SEGMENTED", 1000))
        self.lock = threading.Lock()

    def logged(self, answer):
        route = super().logged(answer)

        def locked(request: httpx.Request) -> httpx.Response:
            with self.lock:
                return route(request)

        return locked

    def test_a_big_file_comes_in_4_ranges_into_one_file(self):
        sink = Sink()
        with http.fake({FILE: self.logged(BODY)}):
            self.client().download(FILE, self.dest, progress=sink)
        self.assertEqual(self.dest.read_bytes(), BODY)
        quarter = -(-len(BODY) // 4)
        spans = [f"bytes={start}-{min(start + quarter, len(BODY)) - 1}" for start in range(0, len(BODY), quarter)]
        self.assertEqual(self.ranges()[0], "bytes=0-")
        self.assertEqual(sorted(self.ranges()[1:]), sorted(spans))
        self.assertEqual((sink.total, sink.count), (len(BODY), len(BODY)))
        self.assertEqual(sorted(path.name for path in self.dest.parent.iterdir()), ["file.bin"])

    def test_a_small_file_or_a_server_without_ranges_comes_in_one_stream(self):
        whole = lambda request: httpx.Response(200, content=BODY)
        with http.fake({FILE: self.logged(whole)}):
            self.client().download(FILE, self.dest)
        self.assertEqual(self.dest.read_bytes(), BODY)
        self.assertEqual(len(self.sent), 1)

    def test_a_failed_segment_keeps_the_first_one_and_the_next_try_streams_the_rest(self):
        quarter = -(-len(BODY) // 4)
        failures = iter([True])

        def flaky(request: httpx.Request):
            if request.headers["range"].startswith(f"bytes={quarter * 2}-") and next(failures, False):
                return 500
            return BODY

        with http.fake({FILE: self.logged(flaky)}):
            self.client().download(FILE, self.dest)
        self.assertEqual(self.dest.read_bytes(), BODY)
        self.assertEqual(self.ranges()[-1], f"bytes={quarter}-")

    def test_segments_that_all_fail_leave_an_empty_stage_and_no_holes(self):
        with mock.patch.object(http._Segments, "one", side_effect=KeyboardInterrupt), \
                http.fake({FILE: BODY}), self.assertRaises(KeyboardInterrupt):
            self.client().download(FILE, self.dest)
        self.assertEqual(sorted(path.name for path in self.dest.parent.iterdir()), [".file.bin.stage"])
        self.assertEqual(self.stage.stat().st_size, 0)


if __name__ == "__main__":
    unittest.main()
