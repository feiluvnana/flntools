"""Tests for `shared.http`: `fetch`'s statuses, retries, per-host limit and secrets, `Response`, and `download`.

The network is faked with `httpx.MockTransport` on a client each test makes, and the retry wait
with a fake `_sleep`, so nothing here opens a socket or sleeps, apart from the per-host tests,
whose transports sleep briefly so that requests overlap. `download`'s CDN (`Cdn`) honours
`Range` and `If-Range` and can cut a body short, for resume, restart and split downloads; its
files go to a temporary folder.
"""

import fnmatch
import io
import json
import os
import tempfile
import threading
import time
import unittest
from pathlib import Path
from typing import Callable
from unittest import mock

import httpx

import shared
from shared import _http as http
from shared import _selector as selector
from shared import theme
from shared._theme import Screen

Handler = Callable[[httpx.Request], httpx.Response]


def client(handler: Handler, **options) -> httpx.Client:
    """A client whose requests go to `handler`, never the network."""
    return httpx.Client(transport=httpx.MockTransport(handler), **options)


class FakeSleep:
    """Stands in for `_http._sleep`, recording each wait instead of sleeping."""

    def __init__(self) -> None:
        self.waits: list[float] = []

    def __call__(self, seconds: float) -> None:
        self.waits.append(seconds)


class HttpTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.sleep = FakeSleep()
        patcher = mock.patch.object(http, "_sleep", self.sleep)
        patcher.start()
        self.addCleanup(patcher.stop)


class FetchTest(HttpTestCase):
    def test_returns_a_response(self):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"page": request.url.params["page"]}, headers={"X-Id": "7"})

        with client(handler) as fake:
            page = http.fetch("https://example.com/items", client=fake, params={"page": 2})
        self.assertIsInstance(page, http.Response)
        self.assertEqual((page.status, page.ok, page.url), (200, True, "https://example.com/items?page=2"))
        self.assertEqual(page.headers["x-id"], "7")
        self.assertEqual(page.headers["X-ID"], "7")
        self.assertEqual(page.json, {"page": "2"})

    def test_sends_method_headers_and_body(self):
        seen: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(request)
            return httpx.Response(201)

        with client(handler) as fake:
            http.fetch("https://example.com/api", client=fake, method="POST", headers={"X-A": "b"}, json={"id": 1})
        self.assertEqual(seen[0].method, "POST")
        self.assertEqual(seen[0].headers["x-a"], "b")
        self.assertEqual(seen[0].content, b'{"id":1}')

    def test_timeout_is_passed(self):
        seen: list[dict] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(request.extensions["timeout"])
            return httpx.Response(200)

        with client(handler, timeout=99.0) as fake:
            http.fetch("https://example.com/", client=fake, timeout=5.0)
            http.fetch("https://example.com/", client=fake)
        self.assertEqual(seen[0], {"connect": 5.0, "read": 5.0, "write": 5.0, "pool": 5.0})
        # The default, 30 s, overrides the client's own timeout too.
        self.assertEqual(seen[1]["read"], 30.0)

    def test_soft_returns_none_for_401_403_and_404(self):
        for status in (401, 403, 404):
            with self.subTest(status=status), client(lambda request, status=status: httpx.Response(status)) as fake:
                self.assertIsNone(http.fetch("https://example.com/example_user", client=fake, soft=True))
                with self.assertRaises(http.RefusedError) as raised:
                    http.fetch("https://example.com/example_user", client=fake)
                self.assertEqual(raised.exception.status, status)

    def test_other_4xx_raise_even_when_soft(self):
        with client(lambda request: httpx.Response(410, json={"reason": "gone"})) as fake:
            with self.assertRaises(http.RefusedError) as raised:
                http.fetch("https://example.com/gone", client=fake, soft=True)
        self.assertEqual(raised.exception.status, 410)
        # The refusing response stays on the error, so a caller can read the site's reason.
        self.assertEqual(raised.exception.response.json, {"reason": "gone"})
        self.assertIsInstance(raised.exception, shared.RefusedError)
        self.assertIsInstance(raised.exception, http.Error)
        self.assertIn("GET https://example.com/gone: HTTP 410", str(raised.exception))

    def test_429_raises_rate_limited_without_sleeping(self):
        calls = []

        def handler(request: httpx.Request) -> httpx.Response:
            calls.append(request)
            return httpx.Response(429, headers={"Retry-After": "12"})

        with client(handler) as fake:
            with self.assertRaises(http.RateLimitedError) as raised:
                http.fetch("https://example.com/", client=fake, soft=True)
        self.assertEqual(raised.exception.retry_after, 12.0)
        self.assertEqual(len(calls), 1)
        self.assertEqual(self.sleep.waits, [])
        # Kept apart from refusals, so a caller catching those doesn't swallow a rate limit.
        self.assertNotIsInstance(raised.exception, shared.RefusedError)

    def test_429_retry_after_as_a_date_or_missing(self):
        later = time.strftime("%a, %d %b %Y %H:%M:%S GMT", time.gmtime(time.time() + 60))
        for header, low, high in (({"Retry-After": later}, 55.0, 61.0), ({}, None, None)):
            with self.subTest(header=header), client(lambda request, h=header: httpx.Response(429, headers=h)) as fake:
                with self.assertRaises(http.RateLimitedError) as raised:
                    http.fetch("https://example.com/", client=fake)
                retry_after = raised.exception.retry_after
                if low is None:
                    self.assertIsNone(retry_after)
                else:
                    assert retry_after is not None
                    self.assertTrue(low <= retry_after <= high, retry_after)

    def test_3xx_from_a_client_that_does_not_follow_is_returned(self):
        with client(lambda request: httpx.Response(302, headers={"Location": "/next"})) as fake:
            page = http.fetch("https://example.com/", client=fake)
        self.assertEqual((page.status, page.ok), (302, False))


class RetryTest(HttpTestCase):
    def test_retries_5xx_then_succeeds(self):
        statuses = iter([503, 500, 200])

        with client(lambda request: httpx.Response(next(statuses))) as fake:
            page = http.fetch("https://example.com/", client=fake)
        self.assertEqual(page.status, 200)
        self.assertEqual(self.sleep.waits, [1.0, 2.0])

    def test_5xx_on_every_attempt_raises_server_error(self):
        calls = []

        def handler(request: httpx.Request) -> httpx.Response:
            calls.append(request)
            return httpx.Response(502)

        with client(handler) as fake:
            with self.assertRaises(http.ServerError) as raised:
                http.fetch("https://example.com/", client=fake, attempts=4)
        self.assertEqual((raised.exception.status, len(calls)), (502, 4))
        self.assertEqual(self.sleep.waits, [1.0, 2.0, 4.0])

    def test_retries_connection_errors_up_to_attempts(self):
        calls = []

        def handler(request: httpx.Request) -> httpx.Response:
            calls.append(request)
            raise httpx.ConnectError("connection refused", request=request)

        with client(handler) as fake:
            with self.assertRaises(http.Error) as raised:
                http.fetch("https://example.com/", client=fake, attempts=3)
        self.assertEqual(len(calls), 3)
        self.assertEqual(len(self.sleep.waits), 2)
        self.assertIn("ConnectError", str(raised.exception))
        self.assertIsInstance(raised.exception.__cause__, httpx.ConnectError)

    def test_one_attempt_never_waits(self):
        with client(lambda request: httpx.Response(500)) as fake:
            with self.assertRaises(http.ServerError):
                http.fetch("https://example.com/", client=fake, attempts=1)
        self.assertEqual(self.sleep.waits, [])

    def test_waits_stop_growing_at_the_cap(self):
        with client(lambda request: httpx.Response(500)) as fake:
            with self.assertRaises(http.ServerError):
                http.fetch("https://example.com/", client=fake, attempts=7)
        self.assertEqual(self.sleep.waits, [1.0, 2.0, 4.0, 8.0, 8.0, 8.0])

    def test_4xx_is_not_retried(self):
        calls = []

        def handler(request: httpx.Request) -> httpx.Response:
            calls.append(request)
            return httpx.Response(403)

        with client(handler) as fake:
            with self.assertRaises(http.RefusedError):
                http.fetch("https://example.com/", client=fake)
        self.assertEqual(len(calls), 1)

    def test_errors_another_try_would_repeat_are_raised_at_once(self):
        calls = []

        def handler(request: httpx.Request) -> httpx.Response:
            calls.append(request)
            raise httpx.UnsupportedProtocol("no", request=request)

        with client(handler) as fake:
            with self.assertRaises(http.Error):
                http.fetch("https://example.com/", client=fake)
        self.assertEqual(len(calls), 1)

    def test_attempts_below_one_is_a_mistake(self):
        with self.assertRaises(ValueError):
            http.fetch("https://example.com/", attempts=0)


class ResponseTest(unittest.TestCase):
    def test_json_on_a_non_json_body_raises_http_error(self):
        page = http.Response(200, "https://example.com/?token=abc", {}, b"<html>nope</html>")
        with self.assertRaises(http.Error) as raised:
            page.json  # noqa: B018
        self.assertIn("<html>nope", str(raised.exception))
        self.assertNotIn("token=abc", str(raised.exception))

    def test_html_is_a_selection(self):
        page = http.Response(200, "https://example.com/", {"Content-Type": "text/html"},
                             b"<h1>Hello</h1><a href='/a'>a</a>")
        self.assertIsInstance(page.html, selector.Selection)
        self.assertEqual(page.html.css("h1::text").first, "Hello")
        self.assertEqual(page.html.css("a::attr(href)").all, ["/a"])

    def test_text_uses_the_charset(self):
        page = http.Response(200, "https://example.com/", {"Content-Type": "text/plain; charset=latin-1"},
                             "café".encode("latin-1"))
        self.assertEqual(page.text, "café")
        unknown = http.Response(200, "https://example.com/", {"Content-Type": "text/plain; charset=nope"},
                                "café".encode())
        self.assertEqual(unknown.text, "café")

    def test_reads_are_cached(self):
        page = http.Response(200, "https://example.com/", {}, b'{"a": [1]}')
        self.assertIs(page.json, page.json)
        self.assertIs(page.html, page.html)

    def test_hashes_by_identity_and_passes_between_threads(self):
        page = http.Response(200, "https://example.com/", {"A": "1"}, b"x")
        same = http.Response(200, "https://example.com/", {"A": "1"}, b"x")
        self.assertEqual(hash(page), hash(page))
        self.assertEqual({page: 1}[page], 1)
        self.assertEqual(len({page, same}), 2)
        self.assertNotEqual(page, same)
        seen = []
        thread = threading.Thread(target=lambda: seen.extend([page.status, page.text, hash(page)]))
        thread.start()
        thread.join()
        self.assertEqual(seen, [200, "x", hash(page)])

    def test_frozen_with_read_only_headers(self):
        page = http.Response(200, "https://example.com/", {"A": "1"})
        with self.assertRaises(AttributeError):
            page.status = 500  # type: ignore[misc]
        with self.assertRaises(TypeError):
            page.headers["a"] = "2"  # type: ignore[index]
        self.assertEqual(dict(page.headers), {"a": "1"})


class PerHostLimitTest(unittest.TestCase):
    def test_never_more_than_6_connections_to_one_host_across_threads(self):
        lock = threading.Lock()
        in_flight: dict[str, int] = {}
        peak: dict[str, int] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            host = request.url.host
            with lock:
                in_flight[host] = in_flight.get(host, 0) + 1
                peak[host] = max(peak.get(host, 0), in_flight[host])
            time.sleep(0.02)  # long enough for the other threads to pile up behind the limit
            with lock:
                in_flight[host] -= 1
            return httpx.Response(200)

        # Two clients, to show the limit is the process's, not one client's.
        clients = [client(handler), client(handler)]
        errors: list[BaseException] = []

        def work(index: int) -> None:
            try:
                for _ in range(3):
                    http.fetch("https://cdn.example.com/f", client=clients[index % 2])
                    http.fetch("https://other.example.com/f", client=clients[index % 2])
            except BaseException as error:  # reported by the assertion below, not lost in the thread
                errors.append(error)

        threads = [threading.Thread(target=work, args=(index,)) for index in range(20)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        for fake in clients:
            fake.close()
        self.assertEqual(errors, [])
        self.assertEqual(peak["cdn.example.com"], http.MAX_PER_HOST)
        self.assertEqual(peak["other.example.com"], http.MAX_PER_HOST)


class SecretsTest(HttpTestCase):
    COOKIE = "s3cr3t-cookie-value"
    TOKEN = "Bearer s3cr3t-token-value"

    def secret_client(self, handler: Handler) -> httpx.Client:
        return client(handler, cookies={"sessionid": self.COOKIE}, headers={"Authorization": self.TOKEN})

    def assert_no_secret(self, text: str) -> None:
        for secret in (self.COOKIE, "s3cr3t-token-value"):
            self.assertNotIn(secret, text)

    def test_no_secret_in_error_messages_or_log_lines(self):
        def connection_lost(request: httpx.Request) -> httpx.Response:
            # A transport whose error quotes the request, to show the message is scrubbed.
            raise httpx.ReadError(f"lost: {request.headers['cookie']} {request.headers['authorization']}",
                                  request=request)

        handlers: list[Handler] = [
            lambda request: httpx.Response(403),
            lambda request: httpx.Response(429),
            lambda request: httpx.Response(500),
            connection_lost,
        ]
        with mock.patch.object(http.log, "debug") as debug:
            for handler in handlers:
                with self.secret_client(handler) as fake, self.assertRaises(http.Error) as raised:
                    http.fetch("https://example.com/?auth=s3cr3t-cookie-value", client=fake, attempts=2)
                self.assert_no_secret(str(raised.exception))
                self.assert_no_secret(repr(raised.exception))
        self.assertTrue(debug.called)
        self.assert_no_secret(repr(debug.call_args_list))

    def test_the_secrets_were_sent(self):
        seen: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(request)
            return httpx.Response(200, headers={"Set-Cookie": f"next={self.COOKIE}"})

        with self.secret_client(handler) as fake:
            page = http.fetch("https://example.com/", client=fake)
        self.assertIn(self.COOKIE, seen[0].headers["cookie"])
        self.assertEqual(seen[0].headers["authorization"], self.TOKEN)
        # A response's repr names its headers, never their values.
        self.assert_no_secret(repr(page))


class ModuleClientTest(HttpTestCase):
    def setUp(self) -> None:
        super().setUp()
        patcher = mock.patch.object(http, "_default", None)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_keeps_no_cookies_between_calls(self):
        seen: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(request)
            return httpx.Response(200, headers={"Set-Cookie": "sessionid=abc; Path=/"})

        default = http._client()
        self.addCleanup(default.close)
        default._transport = httpx.MockTransport(handler)  # the client's one transport, faked for this test
        http.fetch("https://example.com/")
        http.fetch("https://example.com/")
        self.assertIs(http._client(), default)
        self.assertNotIn("cookie", seen[1].headers)

    def test_follows_redirects(self):
        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/old":
                return httpx.Response(301, headers={"Location": "https://example.com/new"})
            return httpx.Response(200, text="here")

        default = http._client()
        self.addCleanup(default.close)
        default._transport = httpx.MockTransport(handler)
        page = http.fetch("https://example.com/old")
        self.assertEqual((page.url, page.text), ("https://example.com/new", "here"))


class ThreadsTest(HttpTestCase):
    def test_one_client_from_many_threads(self):
        with client(lambda request: httpx.Response(200, text=request.url.params["n"])) as fake:
            results: dict[int, str] = {}

            def work(number: int) -> None:
                results[number] = http.fetch("https://example.com/", client=fake, params={"n": number}).text

            threads = [threading.Thread(target=work, args=(number,)) for number in range(16)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()
        self.assertEqual(results, {number: str(number) for number in range(16)})


# `download`: a fake CDN on `httpx.MockTransport` that honours `Range` and `If-Range` as a real
# one does, can cut a body short after some bytes, and counts the connections it has open.

DATA = bytes(range(256)) * 40  # 10,240 bytes, every offset distinguishable


class Body(httpx.SyncByteStream):
    """A response body sent in `chunk`-byte pieces, which can drop the connection after `cut` bytes.

    `opened` and `closed` are called when the body starts and ends, so a test can count open connections.
    """

    def __init__(self, data: bytes, *, chunk: int = 1024, cut: int | None = None,
                 on_chunk: Callable[[], None] | None = None, closed: Callable[[], None] | None = None):
        self.data, self.chunk, self.cut, self.on_chunk, self.closed = data, chunk, cut, on_chunk, closed

    def __iter__(self):
        sent = 0
        while sent < len(self.data):
            if self.cut is not None and sent >= self.cut:
                raise httpx.ReadError("connection reset")
            piece = self.data[sent:sent + self.chunk]
            if self.cut is not None:
                piece = piece[: self.cut - sent]
            if self.on_chunk is not None:
                self.on_chunk()
            yield piece
            sent += len(piece)

    def close(self) -> None:
        if self.closed is not None:
            self.closed()
            self.closed = None


class Cdn:
    """A file server: `handler` answers whole files and ranges of `data`, validated by `etag`.

    Each answer can be changed through `script`, a list of callables taking the request and
    returning a response (or None for the normal answer), used one per request in order.
    """

    def __init__(self, data: bytes = DATA, *, etag: str | None = '"v1"', ranges: bool = True, chunk: int = 1024):
        self.data, self.etag, self.ranges, self.chunk = data, etag, ranges, chunk
        self.requests: list[httpx.Request] = []
        self.script: list[Callable[[httpx.Request], httpx.Response | None]] = []
        self.lock = threading.Lock()

    def handler(self, request: httpx.Request) -> httpx.Response:
        with self.lock:
            self.requests.append(request)
            step = self.script.pop(0) if self.script else None
        answer = step(request) if step is not None else None
        return answer if answer is not None else self.answer(request)

    def answer(self, request: httpx.Request, *, cut: int | None = None, data: bytes | None = None,
               etag: str | None = None) -> httpx.Response:
        data = self.data if data is None else data
        etag = self.etag if etag is None else etag
        headers = {"ETag": etag} if etag else {}
        if self.ranges:
            headers["Accept-Ranges"] = "bytes"
        wanted = request.headers.get("range")
        matches = request.headers.get("if-range") in (None, etag)
        if wanted and self.ranges and matches:
            first, _, last = wanted.removeprefix("bytes=").partition("-")
            start, end = int(first), int(last) + 1 if last else len(data)
            headers.update({"Content-Range": f"bytes {start}-{end - 1}/{len(data)}",
                            "Content-Length": str(end - start)})
            return httpx.Response(206, headers=headers, stream=Body(data[start:end], chunk=self.chunk, cut=cut))
        headers["Content-Length"] = str(len(data))
        return httpx.Response(200, headers=headers, stream=Body(data, chunk=self.chunk, cut=cut))

    def cut_at(self, at: int) -> Callable[[httpx.Request], httpx.Response]:
        """A script step: the normal answer, dropped after `at` bytes."""
        return lambda request: self.answer(request, cut=at)

    def ranges_asked(self) -> list[str | None]:
        return [request.headers.get("range") for request in self.requests]


class DownloadCase(HttpTestCase):
    def setUp(self) -> None:
        super().setUp()
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.tmp = Path(folder.name)
        self.cdn = Cdn()

    def download(self, url: str | list[str] = "https://cdn.example/v.mp4", dest: str | Path | None = None,
                 **options):
        with client(self.cdn.handler) as fake:
            return http.download(url, dest if dest is not None else self.tmp / "v.mp4", client=fake, **options)

    def leftovers(self) -> list[str]:
        return sorted(path.name for path in self.tmp.iterdir() if path.name.startswith("."))


class DownloadTest(DownloadCase):
    def test_saves_the_file_atomically_and_reports_progress(self):
        seen: list[tuple[int, int | None]] = []
        saved = self.download(progress=lambda done, total: seen.append((done, total)))
        self.assertEqual(saved, self.tmp / "v.mp4")
        self.assertEqual(saved.read_bytes(), DATA)
        self.assertEqual(self.leftovers(), [])
        self.assertEqual(seen[-1], (len(DATA), len(DATA)))
        self.assertEqual([done for done, _ in seen], sorted(done for done, _ in seen))
        request = self.cdn.requests[0]
        # Asked for as it is: a compressed body's bytes and ranges aren't the file's.
        self.assertEqual(request.headers["accept-encoding"], "identity")
        self.assertTrue(request.extensions.get(http.DOWNLOAD_EXTENSION))

    def test_a_folder_dest_is_named_from_the_url_safely(self):
        for url, name in (("https://cdn.example/a/clip%20one.mp4?sig=1", "clip_one.mp4"),
                          ("https://cdn.example/a/..%2F..%2Fx:y.mp4", "x_y.mp4"),
                          ("https://cdn.example/CON", "CON_"),
                          ("https://cdn.example/", "download")):
            with self.subTest(url=url):
                saved = self.download(url, self.tmp)
                self.assertEqual(saved, self.tmp / name)
                saved.unlink()
        self.assertEqual(self.download("https://cdn.example/b.mp4", f"{self.tmp}/new/"), self.tmp / "new" / "b.mp4")

    def test_mirrors_are_tried_in_order(self):
        self.cdn.script = [lambda request: httpx.Response(404)]
        saved = self.download(["https://a.example/v.mp4", "https://b.example/v.mp4"])
        self.assertEqual(saved.read_bytes(), DATA)
        self.assertEqual([request.url.host for request in self.cdn.requests], ["a.example", "b.example"])

    def test_the_last_mirrors_refusal_is_raised(self):
        self.cdn.script = [lambda request: httpx.Response(404), lambda request: httpx.Response(403)]
        with self.assertRaises(http.RefusedError) as raised:
            self.download(["https://a.example/v.mp4", "https://b.example/v.mp4?sig=secret"])
        self.assertEqual(raised.exception.status, 403)
        self.assertNotIn("secret", str(raised.exception))
        self.assertEqual(self.leftovers(), [])

    def test_a_429_raises_without_sleeping_or_trying_a_mirror(self):
        self.cdn.script = [lambda request: httpx.Response(429, headers={"Retry-After": "7"})]
        with self.assertRaises(http.RateLimitedError) as raised:
            self.download(["https://a.example/v.mp4", "https://b.example/v.mp4"])
        self.assertEqual(raised.exception.retry_after, 7.0)
        self.assertEqual(self.sleep.waits, [])
        self.assertEqual(len(self.cdn.requests), 1)

    def test_a_5xx_is_retried_and_then_raised(self):
        self.cdn.script = [lambda request: httpx.Response(503)]
        self.assertEqual(self.download().read_bytes(), DATA)
        self.assertEqual(self.sleep.waits, [1.0])
        self.cdn.script = [lambda request: httpx.Response(503)] * 3
        with self.assertRaises(http.ServerError):
            self.download()
        self.assertEqual(self.sleep.waits, [1.0, 1.0, 2.0])

    def test_the_size_check(self):
        """Without a length from the server, the caller's `size` is what's checked; a short body costs every attempt."""
        self.cdn.script = [lambda request: httpx.Response(200, content=iter([DATA[:100]]))] * 3
        with self.assertRaises(http.IncompleteError):
            self.download(size=len(DATA))
        self.assertEqual(len(self.cdn.requests), 3)
        self.assertEqual(self.leftovers(), [])

    def test_check_sees_the_whole_part_before_the_rename(self):
        seen: list[bytes] = []
        self.download(check=lambda path: seen.append(path.read_bytes()))
        self.assertEqual(seen, [DATA])

    def test_a_check_that_finds_the_file_broken_downloads_it_again(self):
        results = [shared.BrokenError("cut off"), None]

        def check(path: Path) -> None:
            result = results.pop(0)
            if result is not None:
                raise result

        self.assertEqual(self.download(check=check).read_bytes(), DATA)
        # From the start: the bytes that were there were all the body had.
        self.assertEqual(self.cdn.ranges_asked(), [None, None])

    def test_a_check_that_refuses_the_file_is_raised_at_once_and_leaves_nothing(self):
        def check(path: Path) -> None:
            raise ValueError("a web page")

        with self.assertRaises(ValueError):
            self.download(check=check)
        self.assertEqual(len(self.cdn.requests), 1)
        self.assertEqual(sorted(path.name for path in self.tmp.iterdir()), [])

    def test_options_are_checked(self):
        for options in ({"attempts": 0}, {"parts": 0}, {"parts": "two"}, {"parts": True}):
            with self.subTest(options=options), self.assertRaises(ValueError):
                self.download(**options)
        with self.assertRaises(ValueError):
            self.download([])

    def test_no_secret_in_messages(self):
        def refused(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError(f"refused with {request.headers['cookie']}")

        self.cdn.script = [refused] * 3
        with self.assertRaises(http.Error) as raised, client(self.cdn.handler) as fake:
            http.download("https://cdn.example/v.mp4?token=abcdef", self.tmp / "v.mp4", client=fake,
                          headers={"Cookie": "sid=supersecret"})
        self.assertNotIn("supersecret", str(raised.exception))
        self.assertNotIn("abcdef", str(raised.exception))

    def test_a_label_draws_a_row(self):
        screen = Screen(dest=io.StringIO(), inp=io.StringIO(""), width=200, tty=False, interactive=False)
        with theme.scope(screen=screen, color="none", unicode=True):
            self.download(label="v.mp4")
        self.assertEqual(screen.dest.getvalue().splitlines(), ["✓ " + "━" * 20 + " v.mp4 " + "10.0 KB"])


class ResumeTest(DownloadCase):
    def test_a_resume_after_a_cut_sends_range_and_if_range(self):
        self.cdn.script = [self.cdn.cut_at(3000)]
        self.assertEqual(self.download().read_bytes(), DATA)
        self.assertEqual(self.cdn.ranges_asked(), [None, "bytes=3000-"])
        self.assertEqual(self.cdn.requests[1].headers["if-range"], '"v1"')
        self.assertEqual(self.leftovers(), [])

    def test_last_modified_is_the_validator_without_a_strong_etag(self):
        modified = "Wed, 21 Oct 2026 07:28:00 GMT"
        for etag in (None, 'W/"weak"'):
            with self.subTest(etag=etag):
                self.cdn.requests.clear()

                def first(request: httpx.Request, etag=etag) -> httpx.Response:
                    response = self.cdn.answer(request, cut=3000, etag=etag or "")
                    response.headers["Last-Modified"] = modified
                    if etag:
                        response.headers["ETag"] = etag
                    return response

                def rest(request: httpx.Request) -> httpx.Response:
                    return self.cdn.answer(request, etag=request.headers.get("if-range"))

                self.cdn.script = [first, rest]
                self.assertEqual(self.download().read_bytes(), DATA)
                self.assertEqual(self.cdn.requests[1].headers["if-range"], modified)

    def test_a_200_to_a_range_restarts_the_file(self):
        """The file changed on the server: `If-Range` no longer matches, so the server sends it whole."""
        changed = DATA[::-1]
        self.cdn.script = [self.cdn.cut_at(3000), lambda request: self.cdn.answer(request, data=changed, etag='"v2"')]
        self.assertEqual(self.download().read_bytes(), changed)
        self.assertEqual(self.cdn.ranges_asked(), [None, "bytes=3000-"])
        self.assertEqual(self.leftovers(), [])

    def test_a_changed_file_whose_restart_fails_raises_changed_error(self):
        changed = DATA[::-1]

        def cut_new(request: httpx.Request) -> httpx.Response:
            return self.cdn.answer(request, data=changed, etag='"v2"', cut=10)

        self.cdn.script = [self.cdn.cut_at(3000), cut_new, lambda request: httpx.Response(503)]
        with self.assertRaises(http.ChangedError) as raised:
            self.download()
        self.assertIsInstance(raised.exception, shared.BrokenError)
        self.assertIsInstance(raised.exception.__cause__, http.ServerError)

    def test_a_416_restarts_the_file(self):
        self.cdn.script = [self.cdn.cut_at(3000), lambda request: httpx.Response(416)]
        self.assertEqual(self.download().read_bytes(), DATA)
        self.assertEqual(self.cdn.ranges_asked(), [None, "bytes=3000-", None])

    def test_a_part_json_from_an_earlier_run_resumes(self):
        """A Ctrl-C, here raised from `progress`, keeps the part and its `.part.json` for the next call."""
        def interrupt(done: int, total: int | None) -> None:
            if done >= 4096:
                raise KeyboardInterrupt

        with self.assertRaises(KeyboardInterrupt):
            self.download("https://cdn.example/v.mp4?sig=secret", progress=interrupt)
        parts = self.leftovers()
        self.assertEqual(len(parts), 2)
        self.assertTrue(fnmatch.fnmatch(parts[0], ".v.mp4.*.part"), parts)
        self.assertEqual(parts[1], parts[0] + ".json")
        # The signed link's token stays off the disk: the sidecar has the URL without its query.
        self.assertNotIn("secret", (self.tmp / parts[1]).read_text())
        info = json.loads((self.tmp / parts[1]).read_text())
        self.assertEqual((info["url"], info["validator"], info["size"]), ("https://cdn.example/v.mp4", '"v1"', len(DATA)))
        kept = (self.tmp / parts[0]).stat().st_size
        self.assertGreaterEqual(kept, 4096)

        # The next run, even through a fresh link (an expired one refreshed), asks only for the rest.
        self.cdn.requests.clear()
        self.assertEqual(self.download("https://cdn.example/v.mp4?sig=fresh").read_bytes(), DATA)
        self.assertEqual(self.cdn.ranges_asked(), [f"bytes={kept}-"])
        self.assertEqual(self.leftovers(), [])

    def test_a_file_never_adopts_a_siblings_part(self):
        """fixes #5: `a.rar` and `a.rar.001` in one folder each keep their own part."""
        def interrupt(done: int, total: int | None) -> None:
            if done >= 4096:
                raise KeyboardInterrupt

        with self.assertRaises(KeyboardInterrupt):
            self.download(dest=self.tmp / "a.rar.001", progress=interrupt)
        sibling = self.leftovers()
        self.assertEqual(len(sibling), 2)
        self.cdn.requests.clear()
        self.assertEqual(self.download(dest=self.tmp / "a.rar").read_bytes(), DATA)
        # A fresh download, not a resume of the sibling's bytes, which are still there.
        self.assertEqual(self.cdn.ranges_asked(), [None])
        self.assertEqual(self.leftovers(), sibling)

    def test_a_refusal_keeps_a_resumable_part(self):
        self.cdn.script = [self.cdn.cut_at(3000), lambda request: httpx.Response(403)]
        with self.assertRaises(http.RefusedError):
            self.download()
        self.assertEqual(len(self.leftovers()), 2)

    def test_a_part_without_a_validator_is_never_kept(self):
        self.cdn = Cdn(etag=None, ranges=False)
        self.cdn.script = [self.cdn.cut_at(3000)] * 3
        with self.assertRaises(http.Error):
            self.download()
        # Each attempt starts again: without a validator, a resume could splice two files.
        self.assertEqual(self.cdn.ranges_asked(), [None, None, None])
        self.assertEqual(self.leftovers(), [])

    def test_resume_false_keeps_nothing(self):
        self.cdn.script = [self.cdn.cut_at(3000), lambda request: httpx.Response(403)]
        with self.assertRaises(http.RefusedError):
            self.download(resume=False)
        self.assertEqual(self.cdn.ranges_asked(), [None, None])
        self.assertEqual(self.leftovers(), [])

    def test_a_broken_sidecar_is_deleted_and_the_file_downloaded_whole(self):
        part = self.tmp / ".v.mp4.0badc0de.part"
        part.write_bytes(b"junk")
        (self.tmp / ".v.mp4.0badc0de.part.json").write_text("{not json")
        self.assertEqual(self.download().read_bytes(), DATA)
        self.assertEqual(self.cdn.ranges_asked(), [None])
        self.assertEqual(self.leftovers(), [])


class FixesTest(DownloadCase):
    """The step 8 review's findings, each pinned."""

    def test_a_429_during_a_resume_keeps_the_part_and_the_next_call_resumes(self):
        self.cdn.script = [self.cdn.cut_at(3000), lambda request: httpx.Response(429, headers={"Retry-After": "3"})]
        with self.assertRaises(http.RateLimitedError):
            self.download()
        self.assertEqual(self.sleep.waits, [1.0])  # the wait after the cut, none for the 429
        self.assertEqual(len(self.leftovers()), 2)
        self.cdn.requests.clear()
        self.assertEqual(self.download().read_bytes(), DATA)
        self.assertEqual(self.cdn.ranges_asked(), ["bytes=3000-"])

    def test_a_429_during_a_split_keeps_every_range_and_the_next_call_resumes(self):
        def limited(request: httpx.Request) -> httpx.Response | None:
            if request.headers.get("range") == "bytes=5120-7679":
                return httpx.Response(429)
            return None

        self.cdn.script = [limited] * 4
        with self.assertRaises(http.RateLimitedError):
            self.download(parts=4)
        self.assertEqual(self.sleep.waits, [])
        self.assertEqual(len(self.leftovers()), 2)
        self.cdn.requests.clear()
        self.assertEqual(self.download(parts=4).read_bytes(), DATA)
        self.assertTrue(all(asked is not None for asked in self.cdn.ranges_asked()), self.cdn.ranges_asked())

    def test_a_file_without_a_validator_is_never_split(self):
        """Without `If-Range`, ranges fetched at different times could come from two versions of the file."""
        self.cdn = Cdn(etag=None)
        self.assertEqual(self.download(parts=4).read_bytes(), DATA)
        self.assertEqual(self.cdn.ranges_asked(), [None])

    def test_progress_raising_before_the_body_gives_its_slot_back(self):
        url = "https://slots.cdn.example/v.mp4"

        def interrupt(done: int, total: int | None) -> None:
            raise KeyboardInterrupt

        with self.assertRaises(KeyboardInterrupt):
            self.download(url, progress=interrupt)
        slot = http._slot(httpx.URL(url))
        taken = [slot.acquire(blocking=False) for _ in range(http.MAX_PER_HOST)]
        for got in taken:
            if got:
                slot.release()
        self.assertTrue(all(taken), taken)

    def test_check_names_the_final_file_and_the_part_is_renamed_once(self):
        """A file named by its content goes straight to that name: no crash leaves `v.mp4` holding a WebM."""
        real, moved = os.replace, []

        def spy(src, dst):
            moved.append(Path(dst).name)
            return real(src, dst)

        with mock.patch.object(os, "replace", spy):
            saved = self.download(check=lambda path: self.tmp / "v.webm")
        self.assertEqual(saved, self.tmp / "v.webm")
        self.assertEqual(saved.read_bytes(), DATA)
        self.assertNotIn("v.mp4", moved)
        self.assertEqual(sorted(path.name for path in self.tmp.iterdir()), ["v.webm"])

    def test_a_sidecar_never_claims_bytes_not_on_disk_and_a_crash_resumes_whole(self):
        """Every save of a split's `.part.json`, read as a kill -9 would leave it, holds every byte it claims.

        The ranges write from their own threads; with a buffer in each handle, one range's save
        would count another's bytes still in memory, and a resume after a crash would skip them,
        leaving zeros that pass the size check.
        """
        real_save = http._State.save
        snapshots: list[tuple[bytes, str, str]] = []

        def save(state) -> None:
            real_save(state)
            if len(state.segments) > 1 and state.part.exists():
                # What the disk holds right now, as a process killed here would leave it.
                snapshots.append((state.part.read_bytes(), state.info.read_text(), state.part.name))

        def slow(request: httpx.Request) -> httpx.Response:
            response = self.cdn.answer(request)
            stream = response.stream
            assert isinstance(stream, Body)
            stream.chunk = 256
            stream.on_chunk = lambda: time.sleep(0.001)
            return response

        self.cdn.script = [slow] * 4
        with mock.patch.object(http._State, "save", save), mock.patch.object(http, "SAVE_EVERY", 512):
            self.assertEqual(self.download(parts=4).read_bytes(), DATA)
        self.assertGreater(len(snapshots), 1)
        for data, info, _ in snapshots:
            for start, end, done in json.loads(info)["segments"]:
                self.assertEqual(data[start:start + done], DATA[start:start + done], (start, end, done))

        # Resumed from the earliest crash point, the file comes out whole.
        data, info, name = snapshots[0]
        crashed = self.tmp / "crashed"
        crashed.mkdir()
        (crashed / name).write_bytes(data)
        (crashed / (name + ".json")).write_text(info)
        self.assertEqual(self.download(dest=crashed / "v.mp4", parts=4).read_bytes(), DATA)


class SplitTest(DownloadCase):
    def test_a_split_file_reassembles_byte_for_byte(self):
        saved = self.download(parts=4)
        self.assertEqual(saved.read_bytes(), DATA)
        # One whole-file request, read up to its quarter, and a range for each other quarter.
        self.assertEqual(sorted(self.cdn.ranges_asked(), key=str),
                         sorted([None, "bytes=2560-5119", "bytes=5120-7679", "bytes=7680-"], key=str))
        self.assertTrue(all(request.headers.get("if-range") == '"v1"' for request in self.cdn.requests[1:]))
        self.assertEqual(self.leftovers(), [])

    def test_auto_splits_only_from_16_mb(self):
        self.download(parts="auto")
        self.assertEqual(self.cdn.ranges_asked(), [None])
        self.cdn.requests.clear()
        with mock.patch.object(http, "SPLIT_MIN", 8192):
            self.assertEqual(self.download(parts="auto").read_bytes(), DATA)
        self.assertEqual(len(self.cdn.requests), 4)

    def test_a_server_without_ranges_is_never_split(self):
        self.cdn = Cdn(ranges=False)
        self.download(parts=4)
        self.assertEqual(self.cdn.ranges_asked(), [None])

    def test_a_cut_range_resumes_on_its_own(self):
        cuts = ["bytes=5120-7679"]

        def maybe_cut(request: httpx.Request) -> httpx.Response | None:
            # The third quarter's range is dropped after 100 bytes, once.
            if request.headers.get("range") in cuts:
                cuts.clear()
                return self.cdn.answer(request, cut=100)
            return None

        self.cdn.script = [maybe_cut] * 8
        self.assertEqual(self.download(parts=4).read_bytes(), DATA)
        self.assertIn("bytes=5220-7679", self.cdn.ranges_asked())

    def test_a_split_part_json_resumes_each_range(self):
        def interrupt(done: int, total: int | None) -> None:
            if done >= 6000:
                raise KeyboardInterrupt

        with mock.patch.object(http, "SAVE_EVERY", 1), self.assertRaises(KeyboardInterrupt):
            self.download(parts=4, progress=interrupt)
        info = json.loads(next(self.tmp.glob(".v.mp4.*.part.json")).read_text())
        self.assertEqual(len(info["segments"]), 4)
        self.cdn.requests.clear()
        self.assertEqual(self.download(parts=4).read_bytes(), DATA)
        self.assertTrue(all(asked is not None for asked in self.cdn.ranges_asked()))
        self.assertEqual(self.leftovers(), [])

    def test_never_more_than_6_connections_to_one_host_across_threads(self):
        """Four split downloads at once, each wanting 4 ranges, from one host: 6 open at most, and every slot given back."""
        lock = threading.Lock()
        state = {"open": 0, "peak": 0}

        def opened() -> None:
            with lock:
                state["open"] += 1
                state["peak"] = max(state["peak"], state["open"])

        def closed() -> None:
            with lock:
                state["open"] -= 1

        def handler(request: httpx.Request) -> httpx.Response:
            response = self.cdn.handler(request)
            opened()
            stream = response.stream
            assert isinstance(stream, Body)
            stream.closed = closed
            stream.on_chunk = lambda: time.sleep(0.002)
            return response

        url = "https://limits.cdn.example/v.mp4"
        results: dict[int, bytes] = {}
        with client(handler) as fake:
            def work(number: int) -> None:
                results[number] = http.download(url, self.tmp / f"{number}.mp4", client=fake, parts=4).read_bytes()

            threads = [threading.Thread(target=work, args=(number,)) for number in range(4)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()
        self.assertEqual(results, {number: DATA for number in range(4)})
        self.assertLessEqual(state["peak"], http.MAX_PER_HOST)
        self.assertGreater(state["peak"], 1)
        self.assertEqual(state["open"], 0)
        # Every slot came back: 6 can be taken again at once.
        slot = http._slot(httpx.URL(url))
        taken = [slot.acquire(blocking=False) for _ in range(http.MAX_PER_HOST)]
        for _ in taken:
            slot.release()
        self.assertTrue(all(taken))


if __name__ == "__main__":
    unittest.main()
