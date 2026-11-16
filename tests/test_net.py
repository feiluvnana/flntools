"""`flnd._net`, the HTTP library: requests, the retry policy, secrets, the fake, pace and caps,
and downloads into a part (resumed, mirrored, segmented, written off the loop).

The network is `_net.fake`, the waits a fake clock whose jitter is pinned at its top (so a wait
reads 1, 2, 4, 8 s), and each call runs on a loop of its own (`fakes.fetch`, `fakes.download`,
or `asyncio.run`), so nothing opens a socket or sleeps, except the slow-disk test, which times
the loop for real.
"""

import asyncio
import io
import tempfile
import threading
import time
import unittest
from pathlib import Path
from typing import Iterator
from unittest import mock

from fakes import download, fetch
from flnd import _files, _html, _net, _time
from flnd._ui import _brief, ui
from flnd.errors import Blocked, Error, NetworkError, NotFound, SiteError, Stopped

SITE = "https://www.example.com"
FILE = f"{SITE}/file.bin"
BODY = bytes(range(256)) * 16 + b"tail"


class NetCase(unittest.TestCase):
    def setUp(self) -> None:
        self.clock = self.enterContext(_time.use(_time.Fake()))
        self.enterContext(mock.patch.object(_net.random, "uniform", lambda low, high: high))
        self.enterContext(ui.scope(dest=io.StringIO(), color=False, width=80))
        self.seen: list[_net.Request] = []
        self.sent: list[tuple[float, _net.Request]] = []
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.dest = Path(folder.name) / "file.bin"
        self.stage = self.dest.with_name("file.bin.part")

    def serve(self, *answers):
        """A route answering `answers` in turn, the last one again and again."""
        queue = list(answers)

        def route(request: _net.Request):
            self.seen.append(request)
            answer = queue.pop(0) if len(queue) > 1 else queue[0]
            if isinstance(answer, Exception):
                raise answer
            return answer

        return route

    def logged(self, answer):
        """A route that records each request and when, then answers as `answer` would."""
        def route(request: _net.Request):
            self.sent.append((self.clock.now, request))
            return answer(request) if callable(answer) else answer

        return route

    def ranges(self) -> list[str | None]:
        return [request.headers.get("range") for _, request in self.sent]


class Sink:
    """A progress sink that keeps what it was told."""

    def __init__(self) -> None:
        self.total: float | None = None
        self.count = 0.0

    def advance(self, n: float) -> None:
        self.count += n


def cut(head: bytes) -> Iterator[bytes]:
    """A body that sends `head`, then drops the connection."""
    yield head
    raise ConnectionResetError("connection reset")


# ---------------------------------------------------------------------------------------------
# Requests, statuses and the retry policy
# ---------------------------------------------------------------------------------------------


class FetchTest(NetCase):
    def test_returns_a_response_and_a_get_takes_a_dict_body_as_its_query(self):
        def route(request):
            return _net.reply(200, json={"page": request.url.params["page"]}, headers={"X-Id": "7"})

        with _net.fake({f"{SITE}/items": route}):
            page = fetch(f"{SITE}/items?sort=new", body={"page": 2})
        self.assertIsInstance(page, _net.Response)
        self.assertEqual((page.status, page.ok, page.url), (200, True, f"{SITE}/items?sort=new&page=2"))
        self.assertEqual((page.headers["x-id"], page.headers["X-ID"]), ("7", "7"))
        self.assertEqual(page.json, {"page": "2"})

    def test_a_post_sends_a_dict_or_list_as_json_and_bytes_or_str_as_they_are(self):
        with _net.fake({f"{SITE}/api": self.serve(_net.reply(201))}):
            fetch(f"{SITE}/api", method="post", headers={"X-A": "b"}, body={"id": 1})
            fetch(f"{SITE}/api", method="PUT", body=[1, 2])
            fetch(f"{SITE}/api", method="POST", body=b"\x00raw")
            fetch(f"{SITE}/api", method="POST", body="text")
        self.assertEqual(self.seen[0].method, "POST")
        self.assertEqual(self.seen[0].headers["x-a"], "b")
        self.assertEqual([request.content for request in self.seen], [b'{"id":1}', b"[1,2]", b"\x00raw", b"text"])
        self.assertEqual(self.seen[0].headers["content-type"], "application/json")

    def test_check_false_returns_a_4xx_or_5xx_with_its_status(self):
        for status in (401, 404, 410, 429, 500):
            with self.subTest(status=status), _net.fake({SITE + "/*": status}):
                page = fetch(f"{SITE}/example_user", check=False)
                self.assertEqual((page.status, page.ok), (status, False))

    def test_a_refusal_raises_with_its_response(self):
        with _net.fake({f"{SITE}/gone": _net.Response(410, f"{SITE}/gone", {}, b'{"reason": "gone"}')}):
            with self.assertRaises(SiteError) as raised:
                fetch(f"{SITE}/gone")
        self.assertEqual(raised.exception.status, 410)
        self.assertEqual(raised.exception.response.json, {"reason": "gone"})
        # A 410 is gone for good: never retried, and recorded as gone.
        self.assertIsInstance(raised.exception, NotFound)
        self.assertFalse(raised.exception.retryable)
        self.assertIn(f"GET {SITE}/gone: HTTP 410", str(raised.exception))

    def test_a_429_three_times_raises_blocked_after_two_waits(self):
        with _net.fake({SITE + "/": self.serve(_net.reply(429, headers={"Retry-After": "12"}))}):
            with self.assertRaises(Blocked) as raised:
                fetch(f"{SITE}/")
        self.assertEqual(raised.exception.wait, 12.0)
        self.assertEqual(self.clock.slept, [12.0, 12.0])
        self.assertEqual(len(self.seen), _net.ROUNDS)
        self.assertEqual(raised.exception.status, 429)
        self.assertTrue(raised.exception.retryable)

    def test_a_429s_retry_after_as_a_date_a_reset_or_missing(self):
        # The first wait is the site's: a date a minute away, a reset 20 s away (and a second
        # more, for clocks a little apart), or the backoff's 1 s when it says nothing.
        for header, low, high in (("date", 55.0, 61.0), ("reset", 21.0, 21.0), (None, 1.0, 1.0)):
            later = time.strftime("%a, %d %b %Y %H:%M:%S GMT", time.gmtime(_time.wall() + 60))
            given = {"date": {"Retry-After": later},
                     "reset": {"x-rate-limit-reset": str(int(_time.wall()) + 20)}}.get(header, {})
            self.clock.slept.clear()
            with self.subTest(header=header), \
                    _net.fake({SITE + "/": self.serve(_net.reply(429, headers=given), _net.reply(200))}):
                fetch(f"{SITE}/")
                self.assertTrue(low <= self.clock.slept[0] <= high, self.clock.slept)


class RetryTest(NetCase):
    def test_retries_a_5xx_then_succeeds(self):
        route = self.serve(_net.reply(503), _net.reply(500), _net.reply(200))
        with _net.fake({SITE + "/": route}):
            self.assertEqual(fetch(f"{SITE}/").status, 200)
        self.assertEqual(self.clock.slept, [1.0, 2.0])

    def test_a_5xx_on_every_try_waits_1_2_4_8_s_then_raises(self):
        with _net.fake({SITE + "/": self.serve(_net.reply(502))}):
            with self.assertRaises(SiteError) as raised:
                fetch(f"{SITE}/")
        self.assertEqual(raised.exception.status, 502)
        self.assertTrue(raised.exception.retryable)
        self.assertEqual(len(self.seen), 1 + _net.RETRIES)
        self.assertEqual(self.clock.slept, [1.0, 2.0, 4.0, 8.0])

    def test_the_waits_stop_growing_at_8_s(self):
        with _net.fake({SITE + "/": 502}), self.assertRaises(SiteError):
            fetch(f"{SITE}/", retries=6)
        self.assertEqual(self.clock.slept, [1.0, 2.0, 4.0, 8.0, 8.0, 8.0])

    def test_each_wait_is_jittered_between_half_and_all_of_its_step(self):
        with mock.patch.object(_net.random, "uniform", wraps=lambda low, high: low):
            with _net.fake({SITE + "/": 500}):
                fetch(f"{SITE}/", check=False, retries=3)
        self.assertEqual(self.clock.slept, [0.5, 1.0, 2.0])

    def test_retries_connection_errors(self):
        with _net.fake({SITE + "/": self.serve(ConnectionRefusedError("connection refused"))}), \
                self.assertRaises(NetworkError) as raised:
            fetch(f"{SITE}/")
        self.assertEqual((len(self.seen), len(self.clock.slept)), (5, 4))
        self.assertIn("connection refused", str(raised.exception))
        self.assertTrue(raised.exception.retryable)

    def test_no_retries_never_waits_and_a_4xx_is_never_tried_again(self):
        with _net.fake({SITE + "/": 500}), self.assertRaises(SiteError):
            fetch(f"{SITE}/", retries=0)
        with _net.fake({SITE + "/": self.serve(_net.reply(403))}), self.assertRaises(SiteError):
            fetch(f"{SITE}/")
        self.assertEqual((len(self.seen), self.clock.slept), (1, []))

    def test_an_unknown_route_or_a_bad_url_is_raised_at_once(self):
        with _net.fake({}), self.assertRaises(NetworkError) as raised:
            fetch(f"{SITE}/")
        self.assertFalse(raised.exception.retryable)
        for url in (f"{SITE}/bad\n", "ftp://example.com/a", "nonsense"):
            with self.subTest(url=url), self.assertRaises(Error):
                fetch(url)
        self.assertEqual(self.clock.slept, [])

    def test_a_curl_error_that_wont_heal_is_not_retryable(self):
        class Resolve(OSError):
            code = 6

        made = _net._network(_net.Request("GET", SITE), Resolve("Could not resolve host"))
        self.assertFalse(made.retryable)
        reset = _net._network(_net.Request("GET", SITE), ConnectionResetError("reset"))
        self.assertTrue(reset.retryable)

    def test_a_post_is_never_tried_again_after_a_5xx_or_a_timeout(self):
        for answer, error in ((_net.reply(503), SiteError), (TimeoutError("slow"), NetworkError)):
            self.seen.clear()
            with self.subTest(answer=answer), _net.fake({SITE + "/": self.serve(answer)}):
                with self.assertRaises(error):
                    fetch(f"{SITE}/", method="POST", body={"a": 1})
                self.assertEqual(len(self.seen), 1)
        self.assertEqual(self.clock.slept, [])

    def test_a_post_is_tried_again_after_a_429(self):
        route = self.serve(_net.reply(429, headers={"Retry-After": "3"}), _net.reply(200))
        with _net.fake({SITE + "/": route}):
            self.assertEqual(fetch(f"{SITE}/", method="POST", body=b"x").status, 200)
        self.assertEqual(self.clock.slept, [3.0])

    def test_stop_ends_a_wait(self):
        stop = threading.Event()
        stop.set()
        with _net.fake({SITE + "/*": 429}), self.assertRaises(Stopped):
            fetch(f"{SITE}/a", stop=stop)


class ResponseTest(unittest.TestCase):
    def test_json_on_a_non_json_body_raises_changed(self):
        page = _net.Response(200, f"{SITE}/?token=abc", {}, b"<html>nope</html>")
        with self.assertRaises(Error) as raised:
            page.json  # noqa: B018
        self.assertIn("<html>nope", str(raised.exception))
        self.assertNotIn("token=abc", str(raised.exception))

    def test_json_decodes_nested_values(self):
        page = _net.Response(200, SITE, {}, b'{"a": [1, 2.5, null, true, "\\u00e9"]}')
        self.assertEqual(page.json, {"a": [1, 2.5, None, True, "é"]})

    def test_html_is_a_document(self):
        page = _net.Response(200, SITE, {"Content-Type": "text/html"}, b"<h1>Hello</h1><a href='/a'>a</a>")
        self.assertIsInstance(page.html, _html.Document)
        self.assertEqual(page.html.first("h1").text, "Hello")

    def test_text_uses_the_charset(self):
        page = _net.Response(200, SITE, {"Content-Type": "text/plain; charset=latin-1"}, "café".encode("latin-1"))
        self.assertEqual(page.text, "café")
        unknown = _net.Response(200, SITE, {"Content-Type": "text/plain; charset=nope"}, "café".encode())
        self.assertEqual(unknown.text, "café")

    def test_reads_are_cached(self):
        page = _net.Response(200, SITE, {}, b'{"a": [1]}')
        self.assertIs(page.json, page.json)

    def test_frozen_with_read_only_headers_in_any_case(self):
        page = _net.Response(200, SITE, {"A": "1"})
        with self.assertRaises(AttributeError):
            page.status = 500  # type: ignore[misc]
        with self.assertRaises(TypeError):
            page.headers["a"] = "2"  # type: ignore[index]
        self.assertEqual((dict(page.headers), page.headers["A"]), ({"a": "1"}, "1"))

    def test_a_request_takes_a_str_url_and_dict_headers(self):
        request = _net.Request("get", "https://api.example.com/v1/a?x=1&x=2", {"X-A": "b"})
        self.assertEqual((request.method, request.url.host, request.url.path, request.url.params),
                         ("GET", "api.example.com", "/v1/a", {"x": "1"}))
        self.assertEqual((request.headers["x-a"], request.headers["X-A"]), ("b", "b"))

    def test_with_query_replaces_a_name_and_spells_values(self):
        self.assertEqual(_net.with_query("https://a.example/p?a=1&b=2", {"a": 3, "c": True, "d": None}),
                         "https://a.example/p?b=2&a=3&c=true&d=")
        self.assertEqual(_net.with_query("https://a.example/p", None), "https://a.example/p")


# ---------------------------------------------------------------------------------------------
# Cookies, hooks and secrets
# ---------------------------------------------------------------------------------------------


class SessionTest(NetCase):
    COOKIE = "s3cr3t-cookie-value"
    TOKEN = "Bearer s3cr3t-token-value"
    PASSWORD = "pw-s3cr3t-value"

    def assert_no_secret(self, text: str) -> None:
        for secret in (self.COOKIE, "s3cr3t-token-value", self.PASSWORD):
            self.assertNotIn(secret, text)

    def test_cookies_by_domain_and_set_cookie_kept_for_the_next_request(self):
        jar = [{"name": "sid", "value": "1", "domain": ".site.example", "path": "/"}]
        session = _net.Session(cookies=jar, headers={"User-Agent": "Agent/1"})
        route = self.serve(_net.reply(200, headers=[("Set-Cookie", "ct0=new; Domain=.site.example; Path=/"),
                                                   ("Set-Cookie", "b=2; Path=/")]))
        with _net.fake(route):
            fetch("https://api.site.example/a", session=session)
            fetch("https://api.site.example/b", session=session)
            fetch("https://other.example/c", session=session)
        self.assertEqual(self.seen[0].headers["cookie"], "sid=1")
        self.assertEqual(self.seen[0].headers["user-agent"], "Agent/1")
        self.assertEqual(sorted(self.seen[1].headers["cookie"].split("; ")), ["b=2", "ct0=new", "sid=1"])
        self.assertNotIn("cookie", self.seen[2].headers)
        self.assertEqual(session.names["ct0"], "new")
        self.assertIn("ct0", [cookie["name"] for cookie in session.cookies])

    def test_prepare_runs_before_every_try(self):
        stamps = iter(range(100))

        def prepare(request):
            request.headers["x-signature"] = str(next(stamps))

        session = _net.Session(prepare=prepare)
        route = self.serve(ConnectionRefusedError("refused"), _net.reply(500), _net.reply(200))
        with _net.fake(route):
            fetch("https://a.example/", session=session)
        self.assertEqual([request.headers["x-signature"] for request in self.seen], ["0", "1", "2"])

    def test_follows_redirects(self):
        routes = {f"{SITE}/old": _net.reply(301, headers={"Location": "/new"}), f"{SITE}/new": "here"}
        with _net.fake(routes):
            page = fetch(f"{SITE}/old")
        self.assertEqual((page.url, page.text), (f"{SITE}/new", "here"))

    def test_no_secret_in_error_messages_or_log_lines(self):
        def connection_lost(request):
            # A transport whose error quotes the request, to show the message is scrubbed.
            raise ConnectionResetError(f"lost: {request.headers['cookie']} "
                                       f"{request.headers['authorization']} {request.url}")

        url = f"https://example_user:{self.PASSWORD}@www.example.com/?auth={self.COOKIE}"
        with mock.patch.object(_net.ui, "debug") as debug:
            for route in (403, 429, 500, connection_lost):
                session = _net.Session(headers={"Authorization": self.TOKEN}, cookies=[
                    {"name": "sessionid", "value": self.COOKIE, "domain": "www.example.com", "path": "/"}])
                with _net.fake({SITE + "/*": route}), self.assertRaises(Error) as raised:
                    fetch(url, session=session, retries=1)
                self.assert_no_secret(str(raised.exception))
                self.assert_no_secret(repr(raised.exception))
        self.assertTrue(debug.called)
        self.assert_no_secret(repr(debug.call_args_list))

    def test_a_urls_userinfo_never_reaches_a_message(self):
        url = f"https://example_user:{self.PASSWORD}@www.example.com/a"
        for route in (404, 500, ConnectionRefusedError("refused")):
            with self.subTest(route=route), _net.fake({SITE + "/*": self.serve(route)}):
                with self.assertRaises(Error) as raised:
                    fetch(url, retries=0)
                self.assert_no_secret(str(raised.exception))
                self.assertNotIn("example_user:", str(raised.exception))
        self.assertNotIn(self.PASSWORD, _brief(ValueError(f"GET {url}: refused")))

    def test_the_secrets_were_sent_and_a_responses_repr_names_no_value(self):
        session = _net.Session(headers={"Authorization": self.TOKEN}, cookies=[
            {"name": "sessionid", "value": self.COOKIE, "domain": "www.example.com", "path": "/"}])
        route = self.serve(_net.reply(200, headers={"Set-Cookie": f"next={self.COOKIE}"}))
        with _net.fake({SITE + "/": route}):
            page = fetch(f"{SITE}/", session=session)
        self.assertIn(self.COOKIE, self.seen[0].headers["cookie"])
        self.assertEqual(self.seen[0].headers["authorization"], self.TOKEN)
        self.assert_no_secret(repr(page))
        self.assert_no_secret(repr(self.seen[0].headers))


class FakeTest(NetCase):
    def test_each_kind_of_route(self):
        def echo(request):
            return _net.Response(202, str(request.url), {}, request.url.path.encode())

        async def later(request):
            await asyncio.sleep(0)
            return {"async": True}

        routes = {
            f"{SITE}/dict": {"ok": True}, f"{SITE}/list": [1], f"{SITE}/bytes": b"\x00",
            f"{SITE}/str": "hé", f"{SITE}/int": 404, f"{SITE}/echo": echo, f"{SITE}/async": later,
            f"{SITE}/response": _net.Response(201, SITE, {"X-A": "1"}, b"made"),
        }
        with _net.fake(routes):
            self.assertEqual(fetch(f"{SITE}/dict").json, {"ok": True})
            self.assertEqual(fetch(f"{SITE}/list").json, [1])
            self.assertEqual(fetch(f"{SITE}/bytes").content, b"\x00")
            self.assertEqual(fetch(f"{SITE}/str").text, "hé")
            self.assertEqual(fetch(f"{SITE}/int", check=False).status, 404)
            self.assertEqual(fetch(f"{SITE}/echo?q=1").text, "/echo")
            self.assertEqual(fetch(f"{SITE}/async").json, {"async": True})
            made = fetch(f"{SITE}/response")
        self.assertEqual((made.status, made.headers["x-a"], made.content), (201, "1", b"made"))

    def test_the_longest_prefix_wins_and_an_exact_url_beats_both(self):
        routes = {f"{SITE}/*": "site", f"{SITE}/api/*": "api", f"{SITE}/api/me": "me"}
        with _net.fake(routes):
            texts = [fetch(f"{SITE}/{path}").text for path in ("a", "api/x", "api/me", "api/me?v=2")]
        self.assertEqual(texts, ["site", "api", "me", "me"])

    def test_an_unmatched_url_raises_and_is_never_sent(self):
        with _net.fake({f"{SITE}/a": "a"}), self.assertRaises(Error) as raised:
            fetch("https://other.example.com/b?token=abc")
        self.assertIn("GET https://other.example.com/b: no route in net.fake", str(raised.exception))
        self.assertEqual(self.clock.slept, [])

    def test_the_innermost_block_answers_and_each_ends_with_its_block(self):
        with _net.fake({SITE + "/": "outer"}):
            with _net.fake({SITE + "/": "inner"}):
                self.assertEqual(fetch(f"{SITE}/").text, "inner")
            self.assertEqual(fetch(f"{SITE}/").text, "outer")
        self.assertEqual(_net._FAKES, [])


# ---------------------------------------------------------------------------------------------
# Pace and caps
# ---------------------------------------------------------------------------------------------


class LimitsTest(NetCase):
    def paced(self, **options):
        return _net.Limits(paced=lambda host: host.endswith("www.example.com"), **options)

    def test_a_rate_is_a_token_bucket_per_host_one_token_deep(self):
        limits = self.paced(rate=2.0)
        session = _net.Session()
        routes = {SITE + "/*": self.logged(b"ok"), "https://cdn.example.com/*": self.logged(b"ok")}
        with _net.fake(routes):
            for path in ("a", "b", "c"):
                fetch(f"{SITE}/{path}", limits=limits, session=session)
            fetch("https://cdn.example.com/a", limits=limits, session=session)
            self.clock.advance(10.0)
            fetch(f"{SITE}/d", limits=limits, session=session)
            fetch(f"{SITE}/e", limits=limits, session=session)
        self.assertEqual([at for at, _ in self.sent], [0.0, 0.5, 1.0, 1.0, 11.0, 11.5])
        self.assertEqual(self.clock.slept, [0.5, 0.5, 0.5])

    def test_callers_waiting_together_queue_one_interval_apart(self):
        bucket = _net.Bucket(4.0)

        async def four():
            for _ in range(4):
                await bucket.take()

        asyncio.run(four())
        self.assertEqual(self.clock.slept, [0.25, 0.25, 0.25])

    def test_a_429_holds_the_bucket_and_halves_the_rate_for_20_clean_requests(self):
        bucket = _net.Bucket(1.0)

        async def run():
            await bucket.take()
            bucket.hold(30.0)
            bucket.slow()
            await bucket.take()               # waits out the hold
            for _ in range(3):
                await bucket.take()           # at half the rate
            for _ in range(_net.CLEAN):
                bucket.clean()
            await bucket.take()               # the full rate again
            await bucket.take()

        asyncio.run(run())
        self.assertEqual(self.clock.slept, [30.0, 2.0, 2.0, 1.0])

    def test_a_cap_holds_transfers_to_a_host_and_its_subdomains(self):
        limits = _net.Limits(caps={"cdn.example": 2}, cap=3)
        self.assertEqual((limits._most("cdn.example"), limits._most("v1.cdn.example"),
                          limits._most("other.example")), (2, 2, _net.MOST))
        self.assertEqual(limits.total, _net.PAGES + _net.SPREAD * _net.MOST + 2)
        at_once, most = 0, 0

        async def one():
            nonlocal at_once, most
            async with limits.slot("v1.cdn.example"):
                at_once += 1
                most = max(most, at_once)
                await asyncio.sleep(0.001)
                at_once -= 1

        async def many():
            await asyncio.gather(*(one() for _ in range(6)))

        asyncio.run(many())
        self.assertEqual(most, 2)

    def test_a_client_that_doesnt_pace_caps_a_paced_host_instead(self):
        limits = self.paced(rate=1.0, cap=1)

        async def run():
            for _ in range(3):
                async with limits.slot("www.example.com", pace=False):
                    pass

        asyncio.run(run())
        self.assertEqual(self.clock.slept, [])


CDN_FILE = "https://cdn.example.com/a.bin"


class CapTest(NetCase):
    """The adaptive cap per host: halving on overload, growth while throughput rises."""

    def windows(self, cap, count, seconds):
        """Close `count` windows of clean 1,000-byte transfers, each taking `seconds(value)`;
        return the cap after each."""
        seen = []
        for _ in range(count):
            for _ in range(_net.STEP):
                cap.done(1000, seconds(cap.value))
            seen.append(cap.value)
        return seen

    def test_it_grows_by_one_a_window_while_throughput_rises_up_to_most(self):
        cap = _net.Cap(8, most=12)
        # A host that scales: every transfer takes 1 s however many run, so the rate is the cap.
        self.assertEqual(self.windows(cap, 6, lambda value: 1.0), [9, 10, 11, 12, 12, 12])

    def test_a_step_that_doesnt_raise_throughput_is_taken_back_and_held(self):
        cap = _net.Cap(8, most=32)
        # A saturated link: a transfer takes as long as the cap is wide, so the rate is flat.
        seen = self.windows(cap, 3, lambda value: value / 8)
        self.assertEqual(seen, [9, 8, 8])
        # Held for HOLD clean transfers from the step taken back, then it probes again.
        held = self.windows(cap, _net.HOLD // _net.STEP - 2, lambda value: value / 8)
        self.assertEqual(set(held), {8})
        self.assertEqual(self.windows(cap, 1, lambda value: value / 8), [9])

    def test_a_small_rise_is_noise_not_growth(self):
        cap = _net.Cap(8, most=32)
        # 9 at once moves 3% more than 8: under half of the 12.5% a scaling host would add.
        seen = self.windows(cap, 2, lambda value: 1.0 if value == 8 else 9 / 8 / 1.03)
        self.assertEqual(seen, [9, 8])

    def test_a_window_with_a_free_place_says_nothing(self):
        cap = _net.Cap(8, most=32)

        async def run():
            await cap.take()
            cap.give()                  # nobody waited: the host wasn't kept busy

        asyncio.run(run())
        self.assertEqual(self.windows(cap, 2, lambda value: 1.0), [8, 9])

    def test_a_failure_halves_once_a_round(self):
        cap = _net.Cap(8, most=32)

        async def run():
            first, second = await cap.take(), await cap.take()
            cap.failed(first)
            cap.failed(second)          # started with the first: the same overload
            third = await cap.take()
            cap.failed(third)
            return cap.value

        self.assertEqual(asyncio.run(run()), 2)
        cap = _net.Cap(3, most=32)
        for _ in range(3):
            cap.failed(cap._round)
        self.assertEqual(cap.value, 1)

    def test_a_lower_cap_holds_the_next_transfer_until_enough_finish(self):
        cap = _net.Cap(2, most=32)
        order = []

        async def one(name, hold):
            round = await cap.take()
            order.append(name)
            if hold:
                cap.failed(round)
                await asyncio.sleep(0.001)
            cap.give()

        async def run():
            await asyncio.gather(one("a", True), one("b", True), one("c", False))

        asyncio.run(run())
        self.assertEqual((order, cap.value, cap.busy), (["a", "b", "c"], 1, 0))

    def test_each_kind_of_overload_halves_the_hosts_cap_and_others_dont(self):
        cases = [
            (_net.reply(429), 4), (_net.reply(503), 4), (ConnectionResetError("reset"), 4),
            (TimeoutError("slow"), 4), (_net.reply(500), 8), (_net.reply(404), 8),
        ]
        for answer, left in cases:
            with self.subTest(answer=answer):
                limits = _net.Limits()
                with _net.fake({CDN_FILE: self.serve(answer, b"ok")}):
                    try:
                        fetch(CDN_FILE, limits=limits, retries=1)
                    except NotFound:
                        pass
                self.assertEqual(limits.load["cdn.example.com"].cap, left)

    def test_a_cut_off_download_halves_the_cap(self):
        limits = _net.Limits()
        routes = {CDN_FILE: self.serve(
            _net.reply(200, headers={"content-length": "99"}, chunks=cut(b"half")), b"ok")}

        async def run():
            session = _net.Session()
            client = _net.Client(session, limits=limits, retries=0)
            try:
                with _files.part(self.dest, resume=False) as held:
                    await client.fill(CDN_FILE, held)
            finally:
                await session.close()

        with _net.fake(routes), self.assertRaises(NetworkError):
            asyncio.run(run())
        self.assertEqual(limits.load["cdn.example.com"].cap, 4)

    def test_a_client_measures_a_saturated_host_and_stops_growing_on_its_plateau(self):
        # The fake serves one file at a time, each 10 ms: a link that is already full, so the
        # cap may probe once but never stays above where it started.
        limits = _net.Limits(cap=4)
        peak = 0

        async def route(request):
            nonlocal peak
            # Let every other transfer start (or queue) before this one ends.
            await asyncio.sleep(0)
            self.clock.advance(0.01)
            peak = max(peak, limits.load["cdn.example.com"].cap)
            return b"x" * 1000

        async def run():
            session = _net.Session()
            client = _net.Client(session, limits=limits)
            try:
                await asyncio.gather(*(client.fetch(f"{CDN_FILE}?n={n}") for n in range(200)))
            finally:
                await session.close()

        with _net.fake(route):
            asyncio.run(run())
        load = limits.load["cdn.example.com"]
        self.assertLessEqual(peak, 5)
        self.assertEqual((load.cap, load.busy, load.waiting, load.most), (4, 0, 0, _net.MOST))

    def test_a_hosts_own_cap_bounds_where_it_starts_and_how_far_it_grows(self):
        limits = _net.Limits(caps={"cdn.example": 10, "small.example": 4})

        async def open_both():
            async with limits.slot("v1.cdn.example"), limits.slot("small.example"):
                pass

        asyncio.run(open_both())
        grown = limits._caps["v1.cdn.example"]
        self.assertEqual(self.windows(grown, 5, lambda value: 1.0)[-1], 10)
        self.assertEqual(limits.load["small.example"].cap, 4)
        self.assertEqual(limits.load["v1.cdn.example"].most, 10)


# ---------------------------------------------------------------------------------------------
# Downloads
# ---------------------------------------------------------------------------------------------


class DownloadTest(NetCase):
    def test_returns_the_path_and_reports_to_a_sink(self):
        sink = Sink()
        with _net.fake({FILE: BODY}):
            saved = download(FILE, self.dest, progress=sink)
        self.assertEqual((saved, saved.read_bytes()), (self.dest, BODY))
        self.assertEqual((sink.total, sink.count), (len(BODY), len(BODY)))
        self.assertFalse(self.stage.exists())

    def test_writes_go_to_the_part_1_mib_at_a_time_on_the_writer_thread(self):
        size = 3 << 20
        threads: list[str] = []
        real = _files.Part.write

        def write(held, data):
            threads.append(threading.current_thread().name)
            return real(held, data)

        route = lambda request: _net.reply(200, headers={"content-length": str(size)},
                                           chunks=(bytes(65536) for _ in range(size // 65536)))
        with _net.fake({FILE: route}), mock.patch.object(_files.Part, "write", write):
            download(FILE, self.dest)
        self.assertEqual(self.dest.stat().st_size, size)
        self.assertEqual(len(threads), 3)
        self.assertTrue(all(name.startswith("flnd-writer") for name in threads), threads)

    def test_resumes_a_partial_part_with_a_range(self):
        self.stage.write_bytes(BODY[:6])
        sink = Sink()
        with _net.fake({FILE: self.logged(BODY)}):
            download(FILE, self.dest, progress=sink)
        self.assertEqual(self.dest.read_bytes(), BODY)
        self.assertEqual(self.ranges(), ["bytes=6-"])
        self.assertEqual((sink.total, sink.count), (len(BODY), len(BODY)))

    def test_a_dropped_connection_resumes_on_the_next_try(self):
        answers = iter([_net.reply(200, headers={"content-length": str(len(BODY))}, chunks=cut(BODY[:100])),
                        BODY])
        with _net.fake({FILE: self.logged(lambda request: next(answers))}):
            download(FILE, self.dest)
        self.assertEqual(self.dest.read_bytes(), BODY)
        self.assertEqual(self.ranges(), ["bytes=0-", "bytes=100-"])

    def test_a_416_on_a_whole_part_lands_it(self):
        self.stage.write_bytes(BODY)
        with _net.fake({FILE: self.logged(BODY)}):
            download(FILE, self.dest)
        self.assertEqual(self.dest.read_bytes(), BODY)
        self.assertEqual(self.ranges(), [f"bytes={len(BODY)}-"])

    def test_a_416_for_another_size_starts_over(self):
        self.stage.write_bytes(BODY + b"stale")
        with _net.fake({FILE: self.logged(BODY)}):
            download(FILE, self.dest)
        self.assertEqual(self.dest.read_bytes(), BODY)
        self.assertEqual(self.ranges(), [f"bytes={len(BODY) + 5}-", "bytes=0-"])

    def test_a_resume_names_the_etag_it_kept_and_a_changed_file_starts_over(self):
        def tagged(body, tag):
            def route(request):
                if request.headers.get("if-range") == tag:
                    return _net.reply(206, content=body[6:], headers={
                        "ETag": tag, "Content-Range": f"bytes 6-{len(body) - 1}/{len(body)}"})
                return _net.reply(200, content=body, headers={"ETag": tag})

            return route

        broken = lambda request: _net.reply(200, headers={"content-length": str(len(BODY)), "ETag": '"v1"'},
                                            chunks=cut(BODY[:6]))
        with _net.fake({FILE: broken}), self.assertRaises(NetworkError):
            download(FILE, self.dest, retries=0)
        self.assertEqual(self.stage.read_bytes(), BODY[:6])
        self.assertEqual(self.stage.with_name("file.bin.part.json").read_text(), '{"etag": "\\"v1\\""}')
        # The same file: the range comes, under its ETag.
        with _net.fake({FILE: self.logged(tagged(BODY, '"v1"'))}):
            download(FILE, self.dest)
        self.assertEqual(self.dest.read_bytes(), BODY)
        self.assertEqual([r.headers.get("if-range") for _, r in self.sent], ['"v1"'])
        self.assertEqual(sorted(path.name for path in self.dest.parent.iterdir()), ["file.bin"])

    def test_a_file_that_changed_since_the_part_comes_whole(self):
        self.stage.write_bytes(b"OLD BYTES")
        self.stage.with_name("file.bin.part.json").write_text('{"etag": "\\"v1\\""}')
        route = self.logged(lambda request: _net.reply(200, content=BODY, headers={"ETag": '"v2"'}))
        with _net.fake({FILE: route}):
            download(FILE, self.dest)
        self.assertEqual(self.dest.read_bytes(), BODY)
        self.assertEqual(self.sent[0][1].headers.get("if-range"), '"v1"')
        self.assertEqual(sorted(path.name for path in self.dest.parent.iterdir()), ["file.bin"])

    def test_a_weak_etag_is_never_an_if_range(self):
        self.assertIsNone(_net._validator({"etag": 'W/"x"'}))
        self.assertEqual(_net._validator({"etag": 'W/"x"', "last-modified": "Mon, 02 Jan 2026 00:00:00 GMT"}),
                         "Mon, 02 Jan 2026 00:00:00 GMT")
        self.assertEqual(_net._validator({"etag": '"x"'}), '"x"')

    def test_a_failed_mirror_empties_the_part_before_the_next(self):
        broken = lambda request: _net.reply(200, headers={"content-length": "5000"}, chunks=cut(b"A" * 100))
        routes = {f"{SITE}/a/file.bin": self.logged(broken), f"{SITE}/b/file.bin": self.logged(BODY)}
        with _net.fake(routes):
            download([f"{SITE}/a/file.bin", f"{SITE}/b/file.bin"], self.dest, retries=0)
        self.assertEqual(self.dest.read_bytes(), BODY)
        self.assertEqual(self.ranges(), ["bytes=0-", "bytes=0-"])

    def test_a_refusal_from_every_mirror_raises_and_keeps_nothing_in_place(self):
        with _net.fake({SITE + "/*": 404}), self.assertRaises(SiteError):
            download([f"{SITE}/a", f"{SITE}/b"], self.dest)
        self.assertFalse(self.dest.exists())

    def test_a_stop_keeps_the_part_for_the_next_run(self):
        stop = threading.Event()

        def pieces():
            yield BODY[:10]
            stop.set()
            yield BODY[10:]

        route = lambda request: _net.reply(200, headers={"content-length": str(len(BODY))}, chunks=pieces())
        with _net.fake({FILE: route}), self.assertRaises(Stopped):
            download(FILE, self.dest, stop=stop)
        self.assertEqual(self.stage.read_bytes(), BODY[:10])
        self.assertFalse(self.dest.exists())


class SlowDiskTest(unittest.TestCase):
    """The loop never stalls on a slow disk: a part's writes run on the writer thread.

    The disk is throttled to 20 MB/s (a USB stick, a network share): each write sleeps for its
    size at that rate. A probe on the loop wakes every 5 ms and keeps how late it woke; the
    transfers' writes must never hold it up for 100 ms. On the loop itself, they would hold it
    for every MiB they write (52 ms each), back to back while the chunks wait.
    """

    RATE = 20e6
    LIMIT = 0.100

    def test_the_loop_never_stalls_100_ms(self):
        real = _files.Part.write

        def slow(held, data):
            time.sleep(len(data) / self.RATE)
            return real(held, data)

        async def run(folder):
            lags, done = [], asyncio.Event()

            async def probe():
                loop = asyncio.get_running_loop()
                while not done.is_set():
                    at = loop.time()
                    await asyncio.sleep(0.005)
                    lags.append(loop.time() - at - 0.005)

            probing = asyncio.ensure_future(probe())
            client = _net.Client(_net.Session(), pace=False)

            async def one(n):
                with _files.part(folder / f"{n}.bin", resume=True) as held:
                    await client.fill(f"https://cdn.example/{n}.bin", held)

            await asyncio.gather(*(one(n) for n in range(2)))
            done.set()
            await probing
            return max(lags)

        body = lambda request: _net.reply(200, chunks=(bytes(16384) for _ in range(128)))
        with tempfile.TemporaryDirectory() as folder, _net.fake({"https://cdn.example/*": body}), \
                mock.patch.object(_files.Part, "write", slow):
            worst = asyncio.run(run(Path(folder)))
        self.assertLess(worst, self.LIMIT)


class ValveTest(unittest.TestCase):
    """A streamed transfer pauses once `HELD` bytes wait for the disk, and goes on at half."""

    def test_pauses_at_held_and_goes_on_at_half(self):
        class Answer:
            queue = asyncio.Queue()

            class curl:
                _curl = object()

        calls = []
        with mock.patch.object(_net, "_curl_pause", lambda handle, how: calls.append(how)):
            valve = _net._Valve(Answer)
            chunk = bytes(1 << 20)
            for _ in range(_net.HELD >> 20):
                Answer.queue.put_nowait(chunk)
            self.assertEqual(calls, [1])
            valve.took(len(chunk))
            self.assertEqual(calls, [1])
            for _ in range(1, (_net.HELD >> 20) // 2):
                valve.took(len(chunk))
            self.assertEqual(calls, [1, 0])
            valve.open()
            self.assertEqual(calls, [1, 0])


class SegmentTest(NetCase):
    def setUp(self) -> None:
        super().setUp()
        self.enterContext(mock.patch.object(_net, "SEGMENTED", 1000))

    def test_segments_are_off_by_default(self):
        with _net.fake({FILE: self.logged(BODY)}):
            download(FILE, self.dest)
        self.assertEqual(self.ranges(), ["bytes=0-"])

    def test_a_big_file_comes_in_4_ranges_into_one_file(self):
        sink = Sink()
        with _net.fake({FILE: self.logged(BODY)}):
            download(FILE, self.dest, progress=sink, segments=True)
        self.assertEqual(self.dest.read_bytes(), BODY)
        quarter = -(-len(BODY) // 4)
        spans = [f"bytes={start}-{min(start + quarter, len(BODY)) - 1}" for start in range(0, len(BODY), quarter)]
        self.assertEqual(self.ranges()[0], "bytes=0-")
        self.assertEqual(sorted(self.ranges()[1:]), sorted(spans))
        self.assertEqual((sink.total, sink.count), (len(BODY), len(BODY)))
        self.assertEqual(sorted(path.name for path in self.dest.parent.iterdir()), ["file.bin"])

    def test_a_server_without_ranges_comes_in_one_stream(self):
        with _net.fake({FILE: self.logged(lambda request: _net.reply(200, content=BODY))}):
            download(FILE, self.dest, segments=True)
        self.assertEqual(self.dest.read_bytes(), BODY)
        self.assertEqual(len(self.sent), 1)

    def test_a_failed_segment_keeps_the_first_one_and_the_next_try_streams_the_rest(self):
        quarter = -(-len(BODY) // 4)
        failures = iter([True])

        def flaky(request):
            if request.headers["range"].startswith(f"bytes={quarter * 2}-") and next(failures, False):
                return 500
            return BODY

        with _net.fake({FILE: self.logged(flaky)}):
            download(FILE, self.dest, segments=True)
        self.assertEqual(self.dest.read_bytes(), BODY)
        self.assertEqual(self.ranges()[-1], f"bytes={quarter}-")

    def test_a_part_a_crash_left_with_holes_starts_over(self):
        self.stage.write_bytes(bytes(len(BODY)))
        self.stage.with_name("file.bin.part.json").write_text('{"holes": true}')
        with _net.fake({FILE: self.logged(BODY)}):
            download(FILE, self.dest)
        self.assertEqual(self.dest.read_bytes(), BODY)
        self.assertEqual(self.ranges()[0], "bytes=0-")
        self.assertEqual(sorted(path.name for path in self.dest.parent.iterdir()), ["file.bin"])


class FollowTest(NetCase):
    def test_follows_redirects_with_one_head(self):
        def serve(request):
            self.seen.append(request)
            if request.url.host == "vm.example.com":
                return _net.reply(301, headers={"location": "https://www.example.com/@example_user/video/1"})
            return _net.reply(200)

        with _net.fake(serve):
            self.assertEqual(_net.follow("https://vm.example.com/ZM1/"),
                             "https://www.example.com/@example_user/video/1")
        self.assertEqual({request.method for request in self.seen}, {"HEAD"})


if __name__ == "__main__":
    unittest.main()
