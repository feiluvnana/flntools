"""Tests for `shared.http`: `fetch`'s statuses, retries, per-host limit and secrets, `Response`.

The network is faked with `httpx.MockTransport` on a client each test makes, and the retry wait
with a fake `clock`, so nothing here opens a socket or sleeps, apart from the per-host tests,
whose transports sleep briefly so that requests overlap. `download` is tested in test_download.
"""

import threading
import time
import unittest
from typing import Callable
from unittest import mock

import httpx

import shared
from shared import _http as http
from shared import _selector as selector
from shared import clock

Handler = Callable[[httpx.Request], httpx.Response]


def client(handler: Handler, **options) -> httpx.Client:
    """A client whose requests go to `handler`, never the network."""
    return httpx.Client(transport=httpx.MockTransport(handler), **options)


class HttpTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.clock = self.enterContext(clock.use(clock.Fake()))


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
        self.assertEqual(self.clock.slept, [])
        # Kept apart from refusals, so a caller catching those doesn't swallow a rate limit.
        self.assertNotIsInstance(raised.exception, shared.RefusedError)

    def test_429_retry_after_as_a_date_or_missing(self):
        later = time.strftime("%a, %d %b %Y %H:%M:%S GMT", time.gmtime(clock.wall() + 60))
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
        self.assertEqual(self.clock.slept, [1.0, 2.0])

    def test_5xx_on_every_attempt_raises_server_error(self):
        calls = []

        def handler(request: httpx.Request) -> httpx.Response:
            calls.append(request)
            return httpx.Response(502)

        with client(handler) as fake:
            with self.assertRaises(http.ServerError) as raised:
                http.fetch("https://example.com/", client=fake, attempts=4)
        self.assertEqual((raised.exception.status, len(calls)), (502, 4))
        self.assertEqual(self.clock.slept, [1.0, 2.0, 4.0])

    def test_retries_connection_errors_up_to_attempts(self):
        calls = []

        def handler(request: httpx.Request) -> httpx.Response:
            calls.append(request)
            raise httpx.ConnectError("connection refused", request=request)

        with client(handler) as fake:
            with self.assertRaises(http.Error) as raised:
                http.fetch("https://example.com/", client=fake, attempts=3)
        self.assertEqual(len(calls), 3)
        self.assertEqual(len(self.clock.slept), 2)
        self.assertIn("ConnectError", str(raised.exception))
        self.assertIsInstance(raised.exception.__cause__, httpx.ConnectError)

    def test_one_attempt_never_waits(self):
        with client(lambda request: httpx.Response(500)) as fake:
            with self.assertRaises(http.ServerError):
                http.fetch("https://example.com/", client=fake, attempts=1)
        self.assertEqual(self.clock.slept, [])

    def test_waits_stop_growing_at_the_cap(self):
        with client(lambda request: httpx.Response(500)) as fake:
            with self.assertRaises(http.ServerError):
                http.fetch("https://example.com/", client=fake, attempts=7)
        self.assertEqual(self.clock.slept, [1.0, 2.0, 4.0, 8.0, 8.0, 8.0])

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


if __name__ == "__main__":
    unittest.main()
