import subprocess
import sys
import tempfile
import unittest
from io import StringIO
from pathlib import Path
from unittest import mock

from shared import log, theme
from shared._theme import Screen

try:
    import httpx
    from shared import http
    from shared import _http
except ImportError:  # the http extra is missing
    http = None

try:
    import parsel
except ImportError:
    parsel = None

URL = "https://example.com"


@unittest.skipIf(http is None, "the http extra is not installed")
class HttpCase(unittest.TestCase):
    def setUp(self):
        self.screen = Screen(out=StringIO(), inp=StringIO(""), width=200, tty=False, interactive=False)
        self.scope = theme.scope(screen=self.screen, color="none", unicode=True)
        self.scope.__enter__()
        self.previous_level = log.level
        self.waits = []
        patcher = mock.patch.object(_http, "_sleep", self.waits.append)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.requests = []

    def tearDown(self):
        log.level = self.previous_level
        self.scope.__exit__(None, None, None)

    def lines(self) -> list[str]:
        return self.screen.out.getvalue().splitlines()

    def client(self, *responses, **kwargs) -> "http.Client":
        """A client whose transport answers with `responses` in turn; an exception is raised instead."""
        queue = list(responses)

        def handler(request):
            self.requests.append(request)
            answer = queue.pop(0) if len(queue) > 1 else queue[0]
            if isinstance(answer, Exception):
                raise answer
            return answer

        return http.Client(transport=httpx.MockTransport(handler), **kwargs)


class DefaultsTest(HttpCase):
    def test_user_agent_and_headers(self):
        with self.client(httpx.Response(200)) as client:
            self.assertIsInstance(client, httpx.Client)
            client.get(URL)
        self.assertEqual(self.requests[0].headers["user-agent"], http.agents.chrome)
        with self.client(httpx.Response(200), user_agent=http.agents.iphone,
                         headers={"user-agent": "mine", "x-a": "1"}) as client:
            client.get(URL)
        self.assertEqual((self.requests[1].headers["user-agent"], self.requests[1].headers["x-a"]), ("mine", "1"))
        with self.client(httpx.Response(200), user_agent=None) as client:
            client.get(URL)
        self.assertTrue(self.requests[2].headers["user-agent"].startswith("python-httpx"))

    def test_agents(self):
        for name in ("chrome", "safari", "iphone", "android"):
            self.assertTrue(getattr(http.agents, name).startswith("Mozilla/5.0"))

    def test_cookie_forms(self):
        jar = httpx.Cookies({"a": "1"})
        for cookies in ({"a": "1"}, jar, [{"name": "a", "value": "1", "domain": ".example.com", "path": "/"}]):
            with self.subTest(type(cookies).__name__), self.client(httpx.Response(200), cookies=cookies) as client:
                client.get(URL + "/x")
                self.assertEqual(self.requests[-1].headers["cookie"], "a=1")

    def test_cookie_list_keeps_domains(self):
        cookies = [{"name": "sid", "value": "s", "domain": ".instagram.com", "path": "/"},
                   {"name": "tt", "value": "t", "domain": ".tiktok.com"}]
        with self.client(httpx.Response(200), cookies=cookies) as client:
            self.assertEqual({c.domain for c in client.cookies.jar}, {".instagram.com", ".tiktok.com"})
            client.get("https://www.instagram.com/")
        self.assertEqual(self.requests[0].headers["cookie"], "sid=s")

    def test_timeout_and_redirects(self):
        with self.client(httpx.Response(200)) as client:
            self.assertEqual(client.timeout.read, 30.0)
            self.assertTrue(client.follow_redirects)


class RetryTest(HttpCase):
    def test_connection_error_429_and_503(self):
        failures = [httpx.ConnectError("refused"), httpx.Response(429), httpx.Response(503)]
        for failure in failures:
            with self.subTest(repr(failure)), self.client(failure, httpx.Response(200)) as client:
                self.assertEqual(client.get(URL).status_code, 200)

    def test_backoff_waits_and_gives_up(self):
        log.level = "debug"
        with self.client(httpx.Response(503), retries=3) as client:
            self.assertEqual(client.get(URL).status_code, 503)
        self.assertEqual(self.waits, [1.0, 2.0, 4.0])
        self.assertEqual(len(self.requests), 4)
        self.assertEqual(self.lines()[0], f"· retry 1 {URL}: 503")

    def test_connection_error_raises_after_retries(self):
        with self.client(httpx.ConnectError("refused")) as client:
            with self.assertRaises(httpx.ConnectError):
                client.get(URL)
        self.assertEqual(len(self.requests), 3)

    def test_retry_after(self):
        with self.client(httpx.Response(429, headers={"Retry-After": "7"}), httpx.Response(200)) as client:
            client.get(URL)
        with self.client(httpx.Response(429, headers={"Retry-After": "3600"}), httpx.Response(200)) as client:
            client.get(URL)
        self.assertEqual(self.waits, [7.0, 60.0])

    def test_retry_after_date(self):
        self.assertEqual(_http._retry_after_seconds("Wed, 21 Oct 2015 07:28:00 GMT"), 0.0)
        self.assertIsNone(_http._retry_after_seconds("soon"))

    def test_no_retry_on_400(self):
        with self.client(httpx.Response(400)) as client:
            self.assertEqual(client.get(URL).status_code, 400)
        self.assertEqual(len(self.requests), 1)

    def test_post_only_with_retry_true(self):
        with self.client(httpx.Response(503), httpx.Response(200)) as client:
            self.assertEqual(client.post(URL, json={"q": 1}).status_code, 503)
        with self.client(httpx.Response(503), httpx.Response(200)) as client:
            response = client.request("POST", URL, json={"q": 1}, retry=True)
            self.assertEqual(response.status_code, 200)
        self.assertEqual(self.requests[-1].content, b'{"q":1}')
        with self.client(httpx.Response(503), httpx.Response(200)) as client:
            self.assertEqual(client.request("GET", URL, retry=False).status_code, 503)

    def test_retries_off(self):
        with self.client(httpx.Response(503), httpx.Response(200), retries=0) as client:
            self.assertEqual(client.get(URL).status_code, 503)
        self.assertEqual(self.waits, [])

    def test_stream_is_retried(self):
        with self.client(httpx.Response(503), httpx.Response(200, content=b"ok")) as client:
            with client.stream("GET", URL) as response:
                self.assertEqual(response.read(), b"ok")


class JsonTest(HttpCase):
    def test_success(self):
        with self.client(httpx.Response(200, json={"a": [1]})) as client:
            self.assertEqual(client.json(URL), {"a": [1]})
            self.assertEqual(client.json(URL, method="POST", json={"q": 1}), {"a": [1]})
        self.assertEqual(self.requests[1].method, "POST")

    def test_status_error(self):
        with self.client(httpx.Response(404)) as client:
            with self.assertRaises(httpx.HTTPStatusError):
                client.json(URL)

    def test_json_error(self):
        body = "<html>" + "x" * 500
        with self.client(httpx.Response(200, text=body, headers={"content-type": "text/html"})) as client:
            with self.assertRaises(http.JSONError) as caught:
                client.json(URL)
        error = caught.exception
        self.assertEqual((error.status, error.content_type, error.body), (200, "text/html", body[:200]))
        self.assertIn("expected JSON from https://example.com, got 200 text/html", str(error))
        self.assertIsInstance(error, ValueError)


@unittest.skipIf(parsel is None, "the selector extra is not installed")
class HtmlTest(HttpCase):
    def test_selector(self):
        with self.client(httpx.Response(200, html="<h1>Hi</h1>")) as client:
            page = client.html(URL)
        self.assertEqual(page.css("h1").text, "Hi")

    def test_404_raises(self):
        with self.client(httpx.Response(404)) as client:
            with self.assertRaises(httpx.HTTPStatusError):
                client.html(URL)


class DownloadTest(HttpCase):
    def setUp(self):
        super().setUp()
        self.tmp = tempfile.TemporaryDirectory()
        self.dest = Path(self.tmp.name) / "ins" / "clip.mp4"

    def tearDown(self):
        self.tmp.cleanup()
        super().tearDown()

    def test_skip_when_file_exists(self):
        self.dest.parent.mkdir(parents=True)
        self.dest.write_bytes(b"old")
        with self.client(httpx.Response(200, content=b"new")) as client:
            self.assertFalse(client.download(URL, self.dest))
            self.assertEqual(self.requests, [])
            self.assertTrue(client.download(URL, self.dest, skip=False))
        self.assertEqual(self.dest.read_bytes(), b"new")
        self.assertEqual(self.lines()[0], "• skip clip.mp4")
        self.assertEqual(sorted(p.name for p in self.dest.parent.iterdir()), ["clip.mp4"])

    def test_progress_with_content_length(self):
        body = b"x" * 3000
        with self.client(httpx.Response(200, content=body)) as client:
            self.assertTrue(client.download(URL, self.dest, label="clip"))
        self.assertEqual(self.dest.read_bytes(), body)
        self.assertTrue(self.lines()[-1].startswith("✓ clip"))
        self.assertTrue(self.lines()[-1].endswith("2.9 KB/2.9 KB"))

    def test_progress_without_content_length(self):
        def chunks():
            yield b"ab"
            yield b"cd"

        with self.client(httpx.Response(200, content=chunks())) as client:
            client.download(URL, self.dest)
        self.assertEqual(self.dest.read_bytes(), b"abcd")
        self.assertEqual(self.lines()[-1], "✓ clip.mp4 4.0 B")

    def test_bad_status_leaves_no_file(self):
        with self.client(httpx.Response(403, content=b"denied")) as client:
            with self.assertRaises(httpx.HTTPStatusError):
                client.download(URL, self.dest)
        self.assertFalse(self.dest.exists())
        self.assertTrue(self.lines()[-1].startswith("✗ clip.mp4"))

    def test_partial_content_is_accepted(self):
        with self.client(httpx.Response(206, content=b"part")) as client:
            self.assertTrue(client.download(URL, self.dest))

    def test_module_download(self):
        transport = httpx.MockTransport(lambda request: httpx.Response(200, content=b"one-off"))
        real_init = http.Client.__init__

        def with_mock_transport(client, **kwargs):
            real_init(client, transport=transport, **kwargs)

        with mock.patch.object(http.Client, "__init__", with_mock_transport):
            self.assertTrue(http.download(URL, self.dest))
        self.assertEqual(self.dest.read_bytes(), b"one-off")


def run_python(code: str) -> subprocess.CompletedProcess:
    """Run code in a fresh interpreter from the repository root, so imports start clean."""
    root = Path(__file__).resolve().parent.parent
    return subprocess.run([sys.executable, "-c", code], cwd=root, capture_output=True, text=True, timeout=60)


def missing_extra_message(module: str, package: str) -> str:
    """The ImportError text from `from shared import <module>` when `package` cannot be imported."""
    code = (f"import sys; sys.modules[{package!r}] = None\n"
            f"try:\n    from shared import {module}\nexcept ImportError as error:\n    print(error)")
    return run_python(code).stdout.strip()


class LazyImportTest(unittest.TestCase):
    def test_phase_1_imports_no_extra(self):
        code = ("import sys\nfrom shared import cli, fmt, log, prompt, style, theme\n"
                "print(sorted(n for n in ('httpx', 'playwright', 'cryptography', 'parsel') if n in sys.modules))")
        self.assertEqual(run_python(code).stdout.strip(), "[]")

    def test_modules_load_on_first_use(self):
        code = ("import sys, shared\nbefore = 'shared._kv' in sys.modules\n"
                "print(before, shared.kv is sys.modules['shared._kv'], 'kv' in dir(shared))")
        self.assertEqual(run_python(code).stdout.strip(), "False True True")

    def test_unknown_name(self):
        import shared
        with self.assertRaises(AttributeError):
            shared.nothing

    def test_missing_extras(self):
        expected = {
            ("http", "httpx"): "shared.http needs httpx: pip install 'flntools[http]'",
            ("selector", "parsel"): "shared.selector needs parsel: pip install 'flntools[selector]'",
        }
        for (module, package), message in expected.items():
            self.assertEqual(missing_extra_message(module, package), message)


if __name__ == "__main__":
    unittest.main()
