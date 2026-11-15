"""`http.Client` pacing, 429 retries, and resumable `download`."""

import io
import tempfile
import threading
import unittest
from pathlib import Path

import httpx

from shared import clock, http
from shared._ui import ui

SITE = "https://www.example.com"


class ClientCase(unittest.TestCase):
    def setUp(self) -> None:
        self.fake = self.enterContext(clock.use(clock.Fake()))
        self.dest = io.StringIO()
        self.enterContext(ui.scope(dest=self.dest, color=False, width=80))
        self.answers: list[httpx.Response] = []
        self.sent: list[tuple[float, str, dict[str, str]]] = []

    def serve(self, request: httpx.Request) -> httpx.Response:
        self.sent.append((self.fake.now(), str(request.url), dict(request.headers)))
        return self.answers.pop(0) if self.answers else httpx.Response(200, content=b"ok")

    def client(self, *, stop: threading.Event | None = None, **options) -> http.Client:
        c = http.Client(**options)
        c._raw.close()
        c._raw = httpx.Client(transport=httpx.MockTransport(self.serve))
        self.addCleanup(c.close)
        if stop is not None:
            c._stop = stop
        return c

    def gaps(self) -> list[float]:
        return [later - earlier for (earlier, _, _), (later, _, _) in zip(self.sent, self.sent[1:])]


class RateAndBackoffTest(ClientCase):
    def test_rate_option_paces_requests_per_host(self):
        client = self.client(rate=2.0)
        for _ in range(3):
            client.fetch(f"{SITE}/items")
        self.assertEqual(self.gaps(), [0.5, 0.5])

    def test_429_honors_retry_after_and_x_rate_limit_reset(self):
        self.answers = [httpx.Response(429, headers={"retry-after": "12"})]
        client = self.client()
        self.assertEqual(client.fetch(f"{SITE}/a").status, 200)
        self.assertEqual(self.fake.slept, [12.0])

        self.fake.slept.clear()
        reset = str(int(clock.wall()) + 20)
        self.answers = [httpx.Response(429, headers={"x-rate-limit-reset": reset})]
        self.assertEqual(client.fetch(f"{SITE}/b").status, 200)
        self.assertEqual(self.fake.slept, [21.0])

    def test_stop_interrupts_wait(self):
        stop = threading.Event()
        stop.set()
        self.answers = [httpx.Response(429)]
        client = self.client(stop=stop)
        with self.assertRaises(http.StoppedError):
            client.fetch(f"{SITE}/a")


class DownloadResumeTest(ClientCase):
    def test_resumes_from_partial_file_with_range(self):
        body = b"0123456789abcdef"

        def handler(request: httpx.Request) -> httpx.Response:
            rng = request.headers.get("range")
            if rng is None:
                return httpx.Response(200, headers={"content-length": str(len(body))}, content=body[:6])
            start = int(rng.removeprefix("bytes=").rstrip("-"))
            return httpx.Response(206, headers={"content-length": str(len(body) - start)}, content=body[start:])

        with tempfile.TemporaryDirectory() as tmp:
            dest = Path(tmp) / "file.bin"
            client = self.client()
            client._raw.close()
            client._raw = httpx.Client(transport=httpx.MockTransport(handler))
            saved = client.download(f"{SITE}/file.bin", dest, attempts=2)
            self.assertEqual(saved.read_bytes(), body)


if __name__ == "__main__":
    unittest.main()
