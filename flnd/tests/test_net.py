"""`flnkit.net`, the HTTP library: requests, what each failure raises, secrets, the fake, and
downloads into a part (resumed, written off the loop).

The network is `_net.fake`, the clock a fake one, and each call runs on a loop of its own
(`fakes.fetch`, `fakes.download`, or `asyncio.run`), so nothing opens a socket or sleeps, except
the slow-disk test, which times the loop for real.
"""

import asyncio
import http.server
import io
import tempfile
import threading
import time
import unittest
from collections.abc import Iterator
from pathlib import Path
from unittest import mock

from fakes import download, fetch

from flnd.errors import Blocked, Error, NetworkError, NotFound, SiteError, Stopped
from flnd.ui.log import ui
from flnkit import clock as _time
from flnkit import files as _files
from flnkit import html as _html
from flnkit import net as _net

SITE = "https://www.example.com"
FILE = f"{SITE}/file.bin"
BODY = bytes(range(256)) * 16 + b"tail"


class NetCase(unittest.TestCase):
    def setUp(self) -> None:
        self.clock = self.enterContext(_time.use(_time.Fake()))
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

    def test_a_429_is_blocked_for_the_sites_wait(self):
        # The site's wait: a date a minute away, a reset 20 s away (and a second more, for clocks
        # a little apart), or None when it says nothing.
        later = time.strftime("%a, %d %b %Y %H:%M:%S GMT", time.gmtime(_time.wall() + 60))
        for given, low, high in (({"Retry-After": "12"}, 12.0, 12.0),
                                 ({"Retry-After": later}, 55.0, 61.0),
                                 ({"x-rate-limit-reset": str(int(_time.wall()) + 20)}, 21.0, 21.0),
                                 ({}, None, None)):
            with self.subTest(given=given), _net.fake({SITE + "/": _net.reply(429, headers=given)}):
                with self.assertRaises(Blocked) as raised:
                    fetch(f"{SITE}/")
                wait = raised.exception.wait
                self.assertTrue(wait == low if low is None else low <= wait <= high, wait)
                self.assertEqual(raised.exception.status, 429)
        self.assertEqual(self.clock.slept, [])


class FailureTest(NetCase):
    """What each failure raises, for the scheduler's retry policy: one try, never a wait."""

    def test_a_5xx_or_a_reset_is_retryable_and_a_4xx_is_not(self):
        for answer, error, retryable in ((_net.reply(502), SiteError, True),
                                         (ConnectionRefusedError("refused"), NetworkError, True),
                                         (_net.reply(403), SiteError, False)):
            self.seen.clear()
            with self.subTest(answer=answer), _net.fake({SITE + "/": self.serve(answer)}):
                with self.assertRaises(error) as raised:
                    fetch(f"{SITE}/")
                self.assertEqual((raised.exception.retryable, len(self.seen)), (retryable, 1))
        self.assertEqual(self.clock.slept, [])

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

    def test_the_cached_cookie_header_is_what_the_jar_itself_gives(self):
        # Every cookie scope the cache must keep: a host-only cookie, a domain one, a path one,
        # a secure one, and an expired one, against urllib's own `add_cookie_header`.
        jar = [{"name": "a", "value": "1", "domain": ".example.com", "path": "/"},
               {"name": "h", "value": "2", "domain": "www.example.com", "path": "/"},
               {"name": "p", "value": "3", "domain": ".example.com", "path": "/media"},
               {"name": "s", "value": "4", "domain": ".example.com", "path": "/", "secure": True},
               {"name": "old", "value": "5", "domain": ".example.com", "path": "/", "expires": 1}]
        session = _net.Session(cookies=jar)
        urls = ["https://www.example.com/", "http://www.example.com/a", "https://cdn.example.com/x",
                "https://cdn.example.com/media", "https://cdn.example.com/media/1.jpg?q=1",
                "https://cdn.example.com/mediaX", "https://example_user:pw@www.example.com/a",
                "https://example.org/", "https://www.example.com:8443/a"]
        for url in urls:
            for _ in range(2):  # built, then from the cache
                probe = _net.urllib.request.Request(_net._bare(url))
                session.jar.add_cookie_header(probe)
                with self.subTest(url=url):
                    self.assertEqual(session._cookie_header(url), probe.get_header("Cookie") or "")

    def test_a_path_or_secure_cookie_goes_only_where_it_did(self):
        jar = [{"name": "a", "value": "1", "domain": ".example.com", "path": "/"},
               {"name": "p", "value": "2", "domain": ".example.com", "path": "/media"},
               {"name": "s", "value": "3", "domain": ".example.com", "path": "/", "secure": True}]
        session = _net.Session(cookies=jar)
        with _net.fake(self.serve(_net.reply(200))):
            for url in ("https://cdn.example.com/media/1", "https://cdn.example.com/page",
                        "http://cdn.example.com/media/1", "http://cdn.example.com/page"):
                fetch(url, session=session)
        self.assertEqual([request.headers.get("cookie") for request in self.seen],
                         ["p=2; a=1; s=3", "a=1; s=3", "p=2; a=1", "a=1"])

    def test_two_domains_never_share_a_cookie_header(self):
        jar = [{"name": "a", "value": "1", "domain": ".example.com", "path": "/"},
               {"name": "b", "value": "2", "domain": "cdn.example.org", "path": "/"}]
        session = _net.Session(cookies=jar)
        with _net.fake(self.serve(_net.reply(200))):
            for url in ("https://www.example.com/", "https://cdn.example.org/",
                        "https://www.example.com/", "https://other.example.net/"):
                fetch(url, session=session)
        self.assertEqual([request.headers.get("cookie") for request in self.seen],
                         ["a=1", "b=2", "a=1", None])

    def test_a_set_cookie_drops_the_cached_header(self):
        session = _net.Session(cookies=[{"name": "a", "value": "1", "domain": ".example.com",
                                         "path": "/"}])
        renewed = {"Set-Cookie": "a=2; Domain=.example.com; Path=/"}
        route = self.serve(_net.reply(200), _net.reply(200, headers=renewed), _net.reply(200))
        with _net.fake(route):
            for _ in range(3):
                fetch("https://www.example.com/", session=session)
        self.assertEqual([request.headers["cookie"] for request in self.seen],
                         ["a=1", "a=1", "a=2"])
        self.assertEqual(session.names["a"], "2")

    def test_a_cookie_added_through_the_jar_drops_the_cached_header(self):
        session = _net.Session(cookies=[{"name": "a", "value": "1", "domain": ".example.com",
                                         "path": "/"}])
        with _net.fake(self.serve(_net.reply(200))):
            fetch("https://www.example.com/", session=session)
            session.add([{"name": "b", "value": "2", "domain": ".example.com", "path": "/"}])
            fetch("https://www.example.com/", session=session)
            session.jar.set_cookie(_net._cookie_from_dict(
                {"name": "c", "value": "3", "domain": ".example.com", "path": "/"}))
            fetch("https://www.example.com/", session=session)
            session.jar.clear(".example.com", "/", "a")
            fetch("https://www.example.com/", session=session)
        self.assertEqual([request.headers["cookie"] for request in self.seen],
                         ["a=1", "a=1; b=2", "a=1; b=2; c=3", "b=2; c=3"])
        self.assertEqual(dict(session.names), {"b": "2", "c": "3"})

    def test_a_cookie_that_expires_is_no_longer_sent(self):
        now = int(time.time())
        session = _net.Session(cookies=[
            {"name": "a", "value": "1", "domain": ".example.com", "path": "/"},
            {"name": "soon", "value": "2", "domain": ".example.com", "path": "/",
             "expires": now + 60}])
        with _net.fake(self.serve(_net.reply(200))):
            fetch("https://www.example.com/", session=session)
            with mock.patch("time.time", lambda: now + 60):
                fetch("https://www.example.com/", session=session)
        self.assertEqual([request.headers["cookie"] for request in self.seen],
                         ["a=1; soon=2", "a=1"])
        self.assertEqual([cookie["name"] for cookie in session.cookies], ["a"])

    def test_prepare_runs_before_every_try(self):
        stamps = iter(range(100))

        def prepare(request):
            request.headers["x-signature"] = str(next(stamps))

        session = _net.Session(prepare=prepare)
        route = self.serve(ConnectionRefusedError("refused"), _net.reply(500), _net.reply(200))
        with _net.fake(route):
            fetch("https://a.example/", session=session, retry=True)
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
        for route in (403, 429, 500, connection_lost):
            session = _net.Session(headers={"Authorization": self.TOKEN}, cookies=[
                {"name": "sessionid", "value": self.COOKIE, "domain": "www.example.com", "path": "/"}])
            with _net.fake({SITE + "/*": route}), self.assertRaises(Error) as raised:
                fetch(url, session=session)
            self.assert_no_secret(str(raised.exception))
            self.assert_no_secret(repr(raised.exception))

    def test_a_urls_userinfo_never_reaches_a_message(self):
        url = f"https://example_user:{self.PASSWORD}@www.example.com/a"
        for route in (404, 500, ConnectionRefusedError("refused")):
            with self.subTest(route=route), _net.fake({SITE + "/*": self.serve(route)}):
                with self.assertRaises(Error) as raised:
                    fetch(url)
                self.assert_no_secret(str(raised.exception))
                self.assertNotIn("example_user:", str(raised.exception))

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
            with self.assertRaises(NotFound):
                fetch(f"{SITE}/int")
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


class DownloadTest(NetCase):
    def test_returns_the_path_and_reports_to_a_sink(self):
        sink = Sink()
        with _net.fake({FILE: BODY}):
            saved = download(FILE, self.dest, progress=sink)
        self.assertEqual((saved, saved.read_bytes()), (self.dest, BODY))
        self.assertEqual((sink.total, sink.count), (len(BODY), len(BODY)))
        self.assertFalse(self.stage.exists())

    def test_writes_go_to_the_part_a_buffer_at_a_time_on_the_writer_thread(self):
        size = 3 * _net.BUFFER
        threads: list[str] = []
        real = _files.Part.write

        def write(held, data, **how):
            threads.append(threading.current_thread().name)
            return real(held, data, **how)

        route = lambda request: _net.reply(200, headers={"content-length": str(size)},
                                           chunks=(bytes(65536) for _ in range(size // 65536)))
        with _net.fake({FILE: route}), mock.patch.object(_files.Part, "write", write):
            download(FILE, self.dest)
        self.assertEqual(self.dest.stat().st_size, size)
        self.assertEqual(len(threads), 3)
        self.assertTrue(all(name.startswith("flnd-writer") for name in threads), threads)

    def test_a_large_body_is_hashed_as_it_is_written_without_reading_back(self):
        import hashlib

        body = bytes(range(256)) * (10 * _net.BUFFER // 256 // 4)
        threads: list[str] = []
        real = _files.Part.feed

        def feed(held, data):
            threads.append(threading.current_thread().name)
            return real(held, data)

        async def fill():
            session = _net.Session()
            try:
                with _files.part(self.dest, resume=True, digest=True) as held:
                    await _net.Client(session).fill(FILE, held)
            finally:
                await session.close()
            return held.saved

        route = lambda request: _net.reply(200, headers={"content-length": str(len(body))},
                                           chunks=(body[at:at + 65536]
                                                   for at in range(0, len(body), 65536)))
        with _net.fake({FILE: route}), mock.patch.object(_files.Part, "feed", feed), \
                mock.patch("flnkit.files._digest_fd", side_effect=AssertionError("read back")):
            saved = asyncio.run(fill())
        self.assertEqual(saved.hash, hashlib.sha256(body).digest())
        self.assertIn("flnd-writer", " ".join(threads))

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
            download(FILE, self.dest, retry=True)
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
            download(FILE, self.dest)
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

        def slow(held, data, **how):
            time.sleep(len(data) / self.RATE)
            return real(held, data, **how)

        async def run(folder):
            lags, done = [], asyncio.Event()

            async def probe():
                loop = asyncio.get_running_loop()
                while not done.is_set():
                    at = loop.time()
                    await asyncio.sleep(0.005)
                    lags.append(loop.time() - at - 0.005)

            probing = asyncio.ensure_future(probe())
            client = _net.Client(_net.Session())

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

    class Curl:
        _curl = object()

        def getinfo(self, info):
            return b"https://www.example.com/a" if "URL" in info.name else 200

    def test_pauses_at_held_and_goes_on_at_half(self):
        calls = []

        async def run():
            transfer = _net._Transfer(self.Curl(), lambda curl, plain: None)
            chunk = bytes(1 << 20)
            for _ in range(_net.HELD >> 20):
                self.assertEqual(transfer.write(chunk), len(chunk))
            self.assertEqual(calls, [1])
            self.assertEqual((transfer.status, transfer.url), (200, "https://www.example.com/a"))
            body = transfer.chunks()
            await anext(body)
            self.assertEqual(calls, [1])
            for _ in range(1, (_net.HELD >> 20) // 2):
                await anext(body)
            self.assertEqual(calls, [1, 0])
            transfer.quit()
            self.assertEqual(calls, [1, 0])
            self.assertEqual(transfer.write(chunk), _net.WRITE_ERROR)

        with mock.patch.object(_net, "_curl_pause", lambda handle, how: calls.append(how)):
            asyncio.run(run())

    def test_a_paused_transfer_is_let_go_when_it_closes(self):
        calls = []

        async def run():
            transfer = _net._Transfer(self.Curl(), lambda curl, plain: None)
            for _ in range(_net.HELD >> 20):
                transfer.write(bytes(1 << 20))
            transfer.quit()

        with mock.patch.object(_net, "_curl_pause", lambda handle, how: calls.append(how)):
            asyncio.run(run())
        self.assertEqual(calls, [1, 0])

    def test_the_head_is_the_last_answers_after_redirects(self):
        async def run():
            transfer = _net._Transfer(self.Curl(), lambda curl, plain: None)
            for line in (b"HTTP/1.1 302 Found\r\n", b"Location: /a\r\n", b"Set-Cookie: r=1\r\n",
                         b"\r\n", b"HTTP/1.1 200 OK\r\n", b"Content-Type: text/plain;\r\n",
                         b"  charset=utf-8\r\n", b"X-Two: a\r\n", b"X-Two: b\r\n", b"\r\n"):
                transfer.line(line)
            transfer.write(b"body")
            return transfer.headers

        self.assertEqual(asyncio.run(run()), [("content-type", "text/plain;  charset=utf-8"),
                                              ("x-two", "a"), ("x-two", "b")])


class HolesTest(NetCase):
    def test_a_part_written_in_segments_starts_a_plain_fill_over(self):
        # An older flnd's sidecar (`holes`) and today's (`segments`) alike.
        for side in ('{"holes": true}', '{"size": 4100, "segments": [[0, 4100, 9]]}'):
            with self.subTest(side):
                self.sent.clear()
                self.stage.write_bytes(bytes(len(BODY)))
                self.stage.with_name("file.bin.part.json").write_text(side)
                with _net.fake({FILE: self.logged(BODY)}):
                    download(FILE, self.dest)
                self.assertEqual(self.dest.read_bytes(), BODY)
                self.assertEqual(self.ranges()[0], "bytes=0-")
                self.assertEqual(sorted(path.name for path in self.dest.parent.iterdir()),
                                 ["file.bin"])
                self.dest.unlink()


    def test_a_segment_counts_its_bytes_only_once_they_are_on_disk(self):
        # A segmented fill's tail (under the part's 1 MiB buffer) is written through, so what
        # `wrote` counts, and the sidecar keeps, is on disk when a crash comes.
        body = bytes(range(256)) * ((2 * _net.BUFFER) // 256)
        end = _net.BUFFER + 100_000
        counted: list[tuple[int, int]] = []

        async def fill():
            session = _net.Session()
            try:
                with _files.part(self.dest, resume=True) as held:
                    meter = _net.Meter(None)
                    done = [0]

                    async def wrote(size):
                        done[0] += size
                        counted.append((done[0], len(self.stage.read_bytes())))

                    meter.wrote = wrote
                    await _net.Client(session).fill(FILE, held, progress=meter,
                                                    until=lambda: end)
                    held.drop()
            finally:
                await session.close()

        route = lambda request: _net.reply(200, headers={"content-length": str(len(body))},
                                           chunks=(body[at:at + 65536]
                                                   for at in range(0, len(body), 65536)))
        with _net.fake({FILE: route}):
            asyncio.run(fill())
        self.assertEqual(counted, [(_net.BUFFER, _net.BUFFER), (end, end)])


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


class HopTest(NetCase):
    """A session with a `prepare` hook follows redirects itself, one hop at a time."""

    def hopped(self, routes, method="GET", headers=None, body=None, *, file=False):
        def prepare(request):
            if request.url.host.endswith("example.com"):
                request.headers["x-signed"] = request.url.host

        session = _net.Session(prepare=prepare)
        client = _net.Client(session)

        def serve(request):
            self.seen.append(request)
            return routes[request.url.host + request.url.path]

        async def sent():
            try:
                if file:
                    with _files.part(self.dest, resume=True) as held:
                        return await client.fill("https://api.example.com/a", held,
                                                 headers=headers)
                return await client.fetch("https://api.example.com/a", method=method,
                                          headers=headers, body=body)
            finally:
                await session.close()

        with _net.fake(serve):
            return asyncio.run(sent())

    def test_a_hop_to_another_host_carries_no_header_of_the_callers_but_the_safe_ones(self):
        routes = {"api.example.com/a": _net.reply(302, headers={"Location": "https://cdn.example/v"}),
                  "cdn.example/v": _net.reply(200, content=b"abcd")}
        headers = {"Cookie": "sessionid=SESSION-SECRET", "Authorization": "Bearer FILE-SECRET",
                   "X-Token": "TOKEN-SECRET", "Referer": "https://www.example.com/",
                   "Accept-Language": "en"}
        for file in (False, True):
            with self.subTest(file=file):
                self.seen.clear()
                self.hopped(routes, headers=headers, file=file)
                first, second = self.seen
                self.assertEqual(first.headers["authorization"], "Bearer FILE-SECRET")
                self.assertNotIn("SECRET", str(dict(second.headers)))
                self.assertEqual((second.headers.get("referer"),
                                  second.headers.get("accept-language")),
                                 ("https://www.example.com/", "en"))
                self.assertNotIn("x-signed", second.headers)

    def test_a_hop_between_its_own_hosts_is_signed_again(self):
        routes = {"api.example.com/a": _net.reply(302, headers={"Location": "https://www.example.com/b"}),
                  "www.example.com/b": _net.reply(200, json={})}
        self.hopped(routes)
        self.assertEqual([request.headers["x-signed"] for request in self.seen],
                         ["api.example.com", "www.example.com"])

    def test_a_post_turns_into_a_get_on_a_301_302_or_303_and_stays_on_a_307_or_308(self):
        for status, method, body in ((301, "GET", None), (302, "GET", None),
                                     (303, "GET", None), (307, "POST", b'{"a":1}'),
                                     (308, "POST", b'{"a":1}')):
            with self.subTest(status=status):
                self.seen.clear()
                routes = {"api.example.com/a": _net.reply(status, headers={"Location": "/b"}),
                          "api.example.com/b": _net.reply(200, json={})}
                self.hopped(routes, method="POST", body={"a": 1})
                second = self.seen[1]
                self.assertEqual((second.method, second.content), (method, body))
                if method == "GET":
                    self.assertNotIn("content-type", second.headers)
                    self.assertNotIn("content-length", second.headers)

    def test_a_redirect_off_the_web_is_refused(self):
        routes = {"api.example.com/a": _net.reply(302, headers={"Location": "file:///etc/hosts"})}
        with self.assertRaisesRegex(NetworkError, "redirected off the web"):
            self.hopped(routes)
        self.assertEqual(len(self.seen), 1)


class _Loopback(http.server.BaseHTTPRequestHandler):
    """A server on 127.0.0.1 that keeps each request it got, as it came over the socket."""

    protocol_version = "HTTP/1.1"
    seen: list[tuple[str, str, list[tuple[str, str]], bytes]] = []

    def answer(self):
        size = int(self.headers.get("Content-Length") or 0)
        self.seen.append((self.command, self.path, list(self.headers.items()),
                          self.rfile.read(size)))
        status, headers, body = 200, [("X-Two", "a"), ("X-Two", "b")], b"body"
        if self.path == "/moved":
            status, headers, body = 302, [("Location", "/end"), ("Set-Cookie", "r=1; Path=/")], b""
        elif self.path == "/gone":
            status, body = 404, b"gone"
        self.send_response(status)
        for name, value in headers:
            self.send_header(name, value)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    do_GET = do_POST = do_PUT = do_HEAD = answer

    def log_message(self, *args):
        pass


class CurlTest(unittest.TestCase):
    """The kept handles send what curl_cffi's `AsyncSession` sent, byte for byte, on loopback."""

    # GETs between the others, on one handle: a handle a POST, a PUT or a HEAD used is set up
    # afresh, and a plain GET's handle is kept.
    ASKS = [("GET", "/a", None), ("GET", "/moved", None), ("POST", "/form", b"q=1"),
            ("GET", "/b", None), ("HEAD", "/c", None), ("GET", "/d", None),
            ("PUT", "/e", b"\x00\x01"), ("GET", "/gone", None), ("GET", "/f", b"x")]
    HEADERS = {"user-agent": "Agent/1", "range": "bytes=0-", "cookie": "a=1", "x-empty": ""}

    def setUp(self):
        server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Loopback)
        threading.Thread(target=server.serve_forever, args=(0.02,), daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        self.base = f"http://127.0.0.1:{server.server_port}"
        _Loopback.seen = []

    async def through_session(self):
        session = _net.Session(headers=self.HEADERS, handles=1)
        answers = []
        try:
            for method, path, body in self.ASKS:
                answer = await session.send(_net.Request(method, self.base + path, {}, body))
                read = await answer.read()
                answers.append((read.status, read.url, dict(read.headers), read.content))
        finally:
            await session.close()
        return answers

    async def through_curl_cffi(self):
        from curl_cffi.requests import AsyncSession

        answers = []
        async with AsyncSession(max_clients=1, impersonate=_net.IMPERSONATE) as raw:
            for method, path, body in self.ASKS:
                answer = await raw.request(method, self.base + path, headers=self.HEADERS,
                                           data=body, stream=True, timeout=_net.TIMEOUT,
                                           allow_redirects=True, discard_cookies=True)
                chunks = [chunk async for chunk in answer.aiter_content()]
                await answer.aclose()
                pairs = [(name.lower(), value) for name, value in answer.headers.multi_items()]
                answers.append((answer.status_code, str(answer.url), _net._joined(pairs),
                                b"".join(chunks)))
        return answers

    def test_the_same_requests_and_answers_as_curl_cffi(self):
        before = asyncio.run(self.through_curl_cffi())
        sent_before, _Loopback.seen = _Loopback.seen, []
        after = asyncio.run(self.through_session())
        for was, now in zip(sent_before, _Loopback.seen, strict=True):
            self.assertEqual(now, was)
        for was, now in zip(before, after, strict=True):
            self.assertEqual(now[:2], was[:2])
            self.assertEqual({k: v for k, v in now[2].items() if k != "date"},
                             {k: v for k, v in was[2].items() if k != "date"})
            self.assertEqual(now[3], was[3])
        self.assertEqual([answer[0] for answer in after], [200, 200, 200, 200, 200, 200, 200,
                                                           404, 200])

    def test_a_refused_connection_is_a_network_error(self):
        async def run():
            session = _net.Session()
            try:
                await session.send(_net.Request("GET", "http://127.0.0.1:9/"))
            finally:
                await session.close()

        with self.assertRaises(NetworkError) as raised:
            asyncio.run(run())
        self.assertIn("GET http://127.0.0.1:9/", str(raised.exception))

    def test_closing_half_way_gives_the_handle_back(self):
        async def run():
            session = _net.Session(handles=1)
            try:
                for _ in range(3):
                    answer = await session.send(_net.Request("GET", self.base + "/a"))
                    await answer.close()
                answer = await session.send(_net.Request("GET", self.base + "/b"))
                return (await answer.read()).content
            finally:
                await session.close()

        self.assertEqual(asyncio.run(run()), b"body")

    def test_an_https_url_tunnels_through_the_proxy_in_any_case(self):
        from curl_cffi import CurlOpt

        curl = mock.Mock()
        curls = _net._Curls(handles=1, timeout=1.0, proxy="http://127.0.0.1:9")
        for url, tunnel in [("HTTPS://example.com/", 1), ("https://example.com/", 1),
                            ("http://example.com/", 0)]:
            curls._ask(curl, _net.Request("GET", url), mock.Mock())
            curl.setopt.assert_any_call(CurlOpt.HTTPPROXYTUNNEL, tunnel)
            curl.reset_mock()


if __name__ == "__main__":
    unittest.main()
