"""Tests for `flnd._http`: `fetch`'s statuses, retries and secrets, `Response`, and `fake`.

The network is `http.fake`, and the retry wait a fake `clock` whose jitter is pinned at its top,
so a wait reads 1, 2, 4 s and nothing here opens a socket or sleeps. Downloads and pacing are in
test_http_client.
"""

import io
import threading
import time
import unittest
import warnings
from unittest import mock

import httpx

from flnd import _base
from flnd import _http as http
from flnd import _html as selector
from flnd import _time as clock
from flnd._ui import _brief, ui

SITE = "https://example.com"


class HttpTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.clock = self.enterContext(clock.use(clock.Fake()))
        self.enterContext(mock.patch.object(http.random, "uniform", lambda low, high: high))
        self.enterContext(ui.scope(dest=io.StringIO(), color=False, width=80))
        self.seen: list[httpx.Request] = []

    def serve(self, *answers: httpx.Response | Exception):
        """A route answering `answers` in turn, the last one again and again."""
        queue = list(answers)

        def route(request: httpx.Request) -> httpx.Response:
            self.seen.append(request)
            answer = queue.pop(0) if len(queue) > 1 else queue[0]
            if isinstance(answer, Exception):
                raise answer
            return answer

        return route


class FetchTest(HttpTestCase):
    def test_returns_a_response_and_a_get_takes_a_dict_body_as_its_query(self):
        def route(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"page": request.url.params["page"]}, headers={"X-Id": "7"})

        with http.fake({f"{SITE}/items": route}):
            page = http.fetch(f"{SITE}/items?sort=new", body={"page": 2})
        self.assertIsInstance(page, http.Response)
        self.assertEqual((page.status, page.ok, page.url), (200, True, f"{SITE}/items?sort=new&page=2"))
        self.assertEqual((page.headers["x-id"], page.headers["X-ID"]), ("7", "7"))
        self.assertEqual(page.json, {"page": "2"})

    def test_a_post_sends_a_dict_or_list_as_json_and_bytes_or_str_as_they_are(self):
        with http.fake({f"{SITE}/api": self.serve(httpx.Response(201))}):
            http.fetch(f"{SITE}/api", method="post", headers={"X-A": "b"}, body={"id": 1})
            http.fetch(f"{SITE}/api", method="PUT", body=[1, 2])
            http.fetch(f"{SITE}/api", method="POST", body=b"\x00raw")
            http.fetch(f"{SITE}/api", method="POST", body="text")
        self.assertEqual(self.seen[0].method, "POST")
        self.assertEqual(self.seen[0].headers["x-a"], "b")
        self.assertEqual([request.content for request in self.seen], [b'{"id":1}', b"[1,2]", b"\x00raw", b"text"])
        self.assertEqual(self.seen[0].headers["content-type"], "application/json")

    def test_check_false_returns_a_4xx_or_5xx_with_its_status(self):
        for status in (401, 404, 410, 429, 500):
            with self.subTest(status=status), http.fake({SITE + "/*": status}):
                page = http.fetch(f"{SITE}/example_user", check=False)
                self.assertEqual((page.status, page.ok), (status, False))

    def test_a_refusal_raises_with_its_response(self):
        with http.fake({f"{SITE}/gone": http.Response(410, f"{SITE}/gone", {}, b'{"reason": "gone"}')}):
            with self.assertRaises(http.RefusedError) as raised:
                http.fetch(f"{SITE}/gone")
        self.assertEqual(raised.exception.status, 410)
        assert raised.exception.response is not None
        self.assertEqual(raised.exception.response.json, {"reason": "gone"})
        self.assertIsInstance(raised.exception, _base.RefusedError)
        self.assertIn(f"GET {SITE}/gone: HTTP 410", str(raised.exception))

    def test_a_429_on_every_try_raises_rate_limited(self):
        with http.fake({SITE + "/": httpx.Response(429, headers={"Retry-After": "12"})}):
            with self.assertRaises(http.RateLimitedError) as raised:
                http.fetch(f"{SITE}/")
        self.assertEqual(raised.exception.retry_after, 12.0)
        self.assertEqual(self.clock.slept, [12.0, 12.0])
        # Kept apart from refusals, so a caller catching those doesn't swallow a rate limit.
        self.assertNotIsInstance(raised.exception, _base.RefusedError)

    def test_a_429s_retry_after_as_a_date_or_missing(self):
        later = time.strftime("%a, %d %b %Y %H:%M:%S GMT", time.gmtime(clock.wall() + 60))
        for header, low, high in (({"Retry-After": later}, 55.0, 61.0), ({}, None, None)):
            with self.subTest(header=header), http.Client(retry=http.Retry(1)) as client, \
                    http.fake({SITE + "/": httpx.Response(429, headers=header)}):
                with self.assertRaises(http.RateLimitedError) as raised:
                    client.fetch(f"{SITE}/")
                retry_after = raised.exception.retry_after
                if low is None or high is None:
                    self.assertIsNone(retry_after)
                else:
                    assert retry_after is not None
                    self.assertTrue(low <= retry_after <= high, retry_after)


class RetryTest(HttpTestCase):
    def test_retries_a_5xx_then_succeeds(self):
        route = self.serve(httpx.Response(503), httpx.Response(500), httpx.Response(200))
        with http.fake({SITE + "/": route}):
            self.assertEqual(http.fetch(f"{SITE}/").status, 200)
        self.assertEqual(self.clock.slept, [1.0, 2.0])

    def test_a_5xx_on_every_try_raises_incomplete_and_waits_stop_growing_at_the_cap(self):
        with http.Client(retry=http.Retry(7)) as client, http.fake({SITE + "/": self.serve(httpx.Response(502))}):
            with self.assertRaises(http.IncompleteError) as raised:
                client.fetch(f"{SITE}/")
        self.assertIn("502", str(raised.exception))
        self.assertEqual(len(self.seen), 7)
        self.assertEqual(self.clock.slept, [1.0, 2.0, 4.0, 8.0, 8.0, 8.0])

    def test_each_wait_is_jittered_between_half_and_all_of_its_step(self):
        with mock.patch.object(http.random, "uniform", wraps=lambda low, high: low):
            with http.Client(retry=http.Retry(4)) as client, http.fake({SITE + "/": 500}):
                client.fetch(f"{SITE}/", check=False)
        self.assertEqual(self.clock.slept, [0.5, 1.0, 2.0])

    def test_retries_connection_errors_up_to_times(self):
        def refused(request: httpx.Request) -> httpx.Response:
            self.seen.append(request)
            raise httpx.ConnectError("connection refused", request=request)

        with http.fake({SITE + "/": refused}), self.assertRaises(http.Error) as raised:
            http.fetch(f"{SITE}/")
        self.assertEqual((len(self.seen), len(self.clock.slept)), (3, 2))
        self.assertIn("ConnectError", str(raised.exception))
        self.assertIsInstance(raised.exception.__cause__, httpx.ConnectError)

    def test_one_try_never_waits_and_a_4xx_is_never_tried_again(self):
        with http.Client(retry=http.Retry(1)) as client, http.fake({SITE + "/": 500}):
            with self.assertRaises(http.IncompleteError):
                client.fetch(f"{SITE}/")
        with http.fake({SITE + "/": self.serve(httpx.Response(403))}), self.assertRaises(http.RefusedError):
            http.fetch(f"{SITE}/")
        self.assertEqual((len(self.seen), self.clock.slept), (1, []))

    def test_errors_another_try_would_repeat_are_raised_at_once(self):
        route = self.serve(httpx.UnsupportedProtocol("no", request=httpx.Request("GET", SITE)))
        with http.fake({SITE + "/": route}), self.assertRaises(http.Error):
            http.fetch(f"{SITE}/")
        self.assertEqual(len(self.seen), 1)

    def test_a_post_is_never_tried_again_after_a_5xx_or_a_timeout(self):
        timeout = httpx.ReadTimeout("slow", request=httpx.Request("POST", SITE))
        for answer, error in ((httpx.Response(503), http.IncompleteError), (timeout, http.Error)):
            self.seen.clear()
            with self.subTest(answer=answer), http.fake({SITE + "/": self.serve(answer)}):
                with self.assertRaises(error):
                    http.fetch(f"{SITE}/", method="POST", body={"a": 1})
                self.assertEqual(len(self.seen), 1)
        self.assertEqual(self.clock.slept, [])

    def test_a_post_is_tried_again_after_a_429(self):
        route = self.serve(httpx.Response(429, headers={"Retry-After": "3"}), httpx.Response(200))
        with http.fake({SITE + "/": route}):
            self.assertEqual(http.fetch(f"{SITE}/", method="POST", body=b"x").status, 200)
        self.assertEqual(self.clock.slept, [3.0])

    def test_a_429s_retry_after_counts_toward_times_even_at_zero(self):
        route = self.serve(httpx.Response(429, headers={"Retry-After": "0"}))
        with http.Client(retry=http.Retry(3)) as client, http.fake({SITE + "/": route}):
            with self.assertRaises(http.RateLimitedError):
                client.fetch(f"{SITE}/")
        self.assertEqual(len(self.seen), 3)
        self.assertEqual(self.clock.slept, [0.0, 0.0])

    def test_statuses_and_methods_say_what_is_tried_again(self):
        retry = http.Retry(2, statuses=(503,), methods=("GET", "POST"))
        with http.Client(retry=retry) as client, http.fake({SITE + "/*": self.serve(httpx.Response(503))}):
            client.fetch(f"{SITE}/a", method="POST", check=False)
            with http.fake({SITE + "/*": self.serve(httpx.Response(500))}):
                client.fetch(f"{SITE}/b", check=False)
        self.assertEqual([request.url.path for request in self.seen], ["/a", "/a", "/b"])

    def test_times_below_one_is_a_mistake(self):
        with self.assertRaises(ValueError):
            http.Retry(0)


class ResponseTest(unittest.TestCase):
    def test_json_on_a_non_json_body_raises_http_error(self):
        page = http.Response(200, f"{SITE}/?token=abc", {}, b"<html>nope</html>")
        with self.assertRaises(http.Error) as raised:
            page.json  # noqa: B018
        self.assertIn("<html>nope", str(raised.exception))
        self.assertNotIn("token=abc", str(raised.exception))

    def test_json_decodes_nested_values(self):
        page = http.Response(200, SITE, {}, b'{"a": [1, 2.5, null, true, "\\u00e9"]}')
        self.assertEqual(page.json, {"a": [1, 2.5, None, True, "é"]})

    def test_html_is_a_selection(self):
        page = http.Response(200, SITE, {"Content-Type": "text/html"}, b"<h1>Hello</h1><a href='/a'>a</a>")
        self.assertIsInstance(page.html, selector.Selection)
        self.assertEqual(page.html.css("h1::text").first, "Hello")
        self.assertEqual(page.html.css("a::attr(href)").get(), ["/a"])

    def test_text_uses_the_charset(self):
        page = http.Response(200, SITE, {"Content-Type": "text/plain; charset=latin-1"}, "café".encode("latin-1"))
        self.assertEqual(page.text, "café")
        unknown = http.Response(200, SITE, {"Content-Type": "text/plain; charset=nope"}, "café".encode())
        self.assertEqual(unknown.text, "café")

    def test_reads_are_cached(self):
        page = http.Response(200, SITE, {}, b'{"a": [1]}')
        self.assertIs(page.json, page.json)
        self.assertIs(page.html, page.html)

    def test_hashes_by_identity_and_passes_between_threads(self):
        page = http.Response(200, SITE, {"A": "1"}, b"x")
        same = http.Response(200, SITE, {"A": "1"}, b"x")
        self.assertEqual({page: 1}[page], 1)
        self.assertEqual(len({page, same}), 2)
        seen = []
        thread = threading.Thread(target=lambda: seen.extend([page.status, page.text, hash(page)]))
        thread.start()
        thread.join()
        self.assertEqual(seen, [200, "x", hash(page)])

    def test_frozen_with_read_only_headers(self):
        page = http.Response(200, SITE, {"A": "1"})
        with self.assertRaises(AttributeError):
            page.status = 500  # type: ignore[misc]
        with self.assertRaises(TypeError):
            page.headers["a"] = "2"  # type: ignore[index]
        self.assertEqual(dict(page.headers), {"a": "1"})

    def test_raw_is_the_httpx_response(self):
        with http.fake({SITE + "/": b"hi"}):
            page = http.fetch(f"{SITE}/")
        self.assertIsInstance(page.raw, httpx.Response)
        self.assertEqual(page.raw.status_code, 200)


class SecretsTest(HttpTestCase):
    COOKIE = "s3cr3t-cookie-value"
    TOKEN = "Bearer s3cr3t-token-value"
    PASSWORD = "pw-s3cr3t-value"

    def assert_no_secret(self, text: str) -> None:
        for secret in (self.COOKIE, "s3cr3t-token-value", self.PASSWORD):
            self.assertNotIn(secret, text)

    def test_no_secret_in_error_messages_or_log_lines(self):
        def connection_lost(request: httpx.Request) -> httpx.Response:
            # A transport whose error quotes the request, to show the message is scrubbed.
            msg = f"lost: {request.headers['cookie']} {request.headers['authorization']} {request.url}"
            raise httpx.ReadError(msg, request=request)

        url = f"https://example_user:{self.PASSWORD}@example.com/?auth={self.COOKIE}"
        with mock.patch.object(http.ui, "debug") as debug:
            for route in (403, 429, 500, connection_lost):
                with http.Client(headers={"Authorization": self.TOKEN}, retry=http.Retry(2)) as client, \
                        http.fake({SITE + "/*": route}), self.assertRaises(http.Error) as raised:
                    client.raw.cookies.set("sessionid", self.COOKIE)
                    client.fetch(url)
                self.assert_no_secret(str(raised.exception))
                self.assert_no_secret(repr(raised.exception))
        self.assertTrue(debug.called)
        self.assert_no_secret(repr(debug.call_args_list))

    def test_a_urls_userinfo_never_reaches_a_message(self):
        url = f"https://example_user:{self.PASSWORD}@example.com/a"
        for route in (404, 500, httpx.ConnectError("refused")):
            with self.subTest(route=route), http.fake({SITE + "/*": self.serve(route)}):
                with self.assertRaises(http.Error) as raised:
                    http.fetch(url)
                self.assert_no_secret(str(raised.exception))
                self.assertNotIn("example_user:", str(raised.exception))
        self.assertNotIn(self.PASSWORD, _brief(ValueError(f"GET {url}: refused")))
        self.assertIn("example.com", _brief(ValueError(f"GET {url}: refused")))

    def test_the_secrets_were_sent_and_a_responses_repr_names_no_value(self):
        route = self.serve(httpx.Response(200, headers={"Set-Cookie": f"next={self.COOKIE}"}))
        with http.Client(headers={"Authorization": self.TOKEN}) as client, http.fake({SITE + "/": route}):
            client.raw.cookies.set("sessionid", self.COOKIE)
            page = client.fetch(f"{SITE}/")
        self.assertIn(self.COOKIE, self.seen[0].headers["cookie"])
        self.assertEqual(self.seen[0].headers["authorization"], self.TOKEN)
        self.assert_no_secret(repr(page))


class FakeTest(HttpTestCase):
    def test_each_kind_of_route(self):
        def echo(request: httpx.Request) -> http.Response:
            return http.Response(202, str(request.url), {}, request.url.path.encode())

        routes = {
            f"{SITE}/dict": {"ok": True}, f"{SITE}/list": [1], f"{SITE}/bytes": b"\x00",
            f"{SITE}/str": "hé", f"{SITE}/int": 404, f"{SITE}/echo": echo,
            f"{SITE}/response": http.Response(201, SITE, {"X-A": "1"}, b"made"),
        }
        with http.fake(routes):
            self.assertEqual(http.fetch(f"{SITE}/dict").json, {"ok": True})
            self.assertEqual(http.fetch(f"{SITE}/list").json, [1])
            self.assertEqual(http.fetch(f"{SITE}/bytes").content, b"\x00")
            self.assertEqual(http.fetch(f"{SITE}/str").text, "hé")
            self.assertEqual(http.fetch(f"{SITE}/int", check=False).status, 404)
            self.assertEqual(http.fetch(f"{SITE}/echo?q=1").text, "/echo")
            made = http.fetch(f"{SITE}/response")
        self.assertEqual((made.status, made.headers["x-a"], made.content), (201, "1", b"made"))

    def test_the_longest_prefix_wins_and_an_exact_url_beats_both(self):
        routes = {f"{SITE}/*": "site", f"{SITE}/api/*": "api", f"{SITE}/api/me": "me"}
        with http.fake(routes):
            texts = [http.fetch(f"{SITE}/{path}").text for path in ("a", "api/x", "api/me", "api/me?v=2")]
        self.assertEqual(texts, ["site", "api", "me", "me"])

    def test_an_unmatched_url_raises_and_is_never_sent(self):
        with http.fake({f"{SITE}/a": "a"}), self.assertRaises(http.Error) as raised:
            http.fetch("https://other.example.com/b?token=abc")
        self.assertIn("GET https://other.example.com/b: no route in http.fake", str(raised.exception))
        self.assertEqual(self.clock.slept, [])

    def test_a_block_ends_with_the_network_back(self):
        original = httpx.Client._transport_for_url
        with http.fake({}), http.fake({}):
            self.assertIsNot(httpx.Client._transport_for_url, original)
        self.assertIs(httpx.Client._transport_for_url, original)


class ModuleClientTest(HttpTestCase):
    def test_keeps_no_cookies_between_calls(self):
        route = self.serve(httpx.Response(200, headers={"Set-Cookie": "sessionid=abc; Path=/"}))
        with http.fake({SITE + "/": route}):
            http.fetch(f"{SITE}/")
            http.fetch(f"{SITE}/")
        self.assertIs(http._client(), http._client())
        self.assertNotIn("cookie", self.seen[1].headers)

    def test_follows_redirects(self):
        routes = {f"{SITE}/old": httpx.Response(301, headers={"Location": f"{SITE}/new"}), f"{SITE}/new": "here"}
        with http.fake(routes):
            page = http.fetch(f"{SITE}/old")
        self.assertEqual((page.url, page.text), (f"{SITE}/new", "here"))

    def test_one_client_from_many_threads(self):
        results: dict[int, str] = {}

        def work(number: int) -> None:
            results[number] = http.fetch(f"{SITE}/", body={"n": number}).text

        with http.fake({SITE + "/": lambda request: httpx.Response(200, text=request.url.params["n"])}):
            threads = [threading.Thread(target=work, args=(number,)) for number in range(16)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()
        self.assertEqual(results, {number: str(number) for number in range(16)})


class BugFixTest(HttpTestCase):
    def test_invalid_url_raises_http_error(self):
        with self.assertRaises(http.Error):
            http.fetch(f"{SITE}/bad\n")
        with http.Client() as client, self.assertRaises(http.Error):
            client.fetch(f"{SITE}/bad\n")

    def test_a_raw_body_raises_no_deprecation(self):
        with warnings.catch_warnings(record=True) as caught, http.fake({f"{SITE}/raw": self.serve(httpx.Response(200))}):
            warnings.simplefilter("always", DeprecationWarning)
            http.fetch(f"{SITE}/raw", method="POST", body=b"raw-bytes")
        self.assertEqual([w for w in caught if issubclass(w.category, DeprecationWarning)], [])
        self.assertEqual(self.seen[0].content, b"raw-bytes")


if __name__ == "__main__":
    unittest.main()
