"""The one error hierarchy (`flnd.errors`) and its policy (docs/errors.md).

Three groups of tests:
1. `ClassesTest`: one test per class, raising it where flnd raises it and checking its place in
   the tree and its `hint`, `target` and `retryable`.
2. `PolicyTest`: one test per row of the policy table, through the real HTTP client, the job
   and the engine, on a fake network and a fake clock.
3. `CliTest`: a usage error, and a login a source needs, through a whole download; the exit
   code of each class and the crash file are tests/test_cli.py's.

The network, the clock and the browser are faked; nothing here touches a real site.
"""

from __future__ import annotations

import errno
import io
import threading
import unittest
from pathlib import Path
from unittest import mock

from fakes import download, fetch
from test_commands import FlndCase
from test_engine import EngineCase, make_source

import flnd
from flnd import _files, _net, _time, _tools, job, sources
from flnd._ui import ui
from flnd.engine import _span
from flnd.errors import (
    Blocked,
    Changed,
    DiskError,
    Error,
    LoginError,
    NetworkError,
    NotFound,
    SiteError,
    SourceError,
    Stopped,
    ToolError,
    UsageError,
    classify,
    disk,
)
from flnd.source import File, Source, Target, target
from flnd.source import cookies as login_cookies

SITE = "https://example.com"
# The secrets the redaction test plants: none may reach a message.
TOKEN = "s3cr3t-token-value"
PASSWORD = "hunter2-example"
ALL = (Error, UsageError, LoginError, SiteError, Blocked, NotFound, Changed, NetworkError,
       DiskError, ToolError, SourceError)


class HttpCase(unittest.TestCase):
    """The real client on `_net.fake`, a fake clock, and jitter pinned to the full step."""

    def setUp(self) -> None:
        self.clock = self.enterContext(_time.use(_time.Fake()))
        self.enterContext(mock.patch.object(_net.random, "uniform", lambda low, high: high))
        self.enterContext(ui.scope(dest=io.StringIO(), color=False, width=80))
        self.seen: list[_net.Request] = []

    def serve(self, answer: _net.Reply | Exception):
        """A route that counts its requests and gives `answer` to each."""
        def route(request: _net.Request) -> _net.Reply:
            self.seen.append(request)
            if isinstance(answer, Exception):
                raise answer
            return answer

        return route


# ---------------------------------------------------------------------------------------------
# 1. One test per class
# ---------------------------------------------------------------------------------------------

class ClassesTest(HttpCase):
    def test_the_tree(self):
        for kind in ALL:
            with self.subTest(kind.__name__):
                self.assertTrue(issubclass(kind, Error))
                self.assertIs(getattr(flnd, kind.__name__), kind)
                # No builtin base but Exception: `except ValueError` never catches one by accident.
                for builtin in (ValueError, RuntimeError, OSError, TypeError, KeyError):
                    self.assertFalse(issubclass(kind, builtin), builtin.__name__)
        for kind in (Blocked, NotFound, Changed):
            self.assertTrue(issubclass(kind, SiteError))
        self.assertFalse(issubclass(Stopped, Error))

    def test_error(self):
        error = Error("it broke", hint="do this", target="aaa:example_user", retryable=True)
        self.assertEqual((str(error), error.hint, error.target, error.retryable),
                         ("it broke", "do this", "aaa:example_user", True))
        plain = Error("it broke")
        self.assertEqual((plain.hint, plain.target, plain.retryable), ("", None, False))

    def test_usage_error(self):
        with self.assertRaises(UsageError), flnd.open("unused-home") as fl:
            fl.download("nosuchsource:example_user")
        with self.assertRaises(UsageError):
            _span("yesterday")
        with self.assertRaises(UsageError), flnd.open("unused-home") as fl:
            fl.logout("nosuchsource")

    def test_login_error(self):
        made = job.failure(self.refusal(403, "https://api.aaa.example/me"),
                           Target("aaa:example_user", kind="user"), make_source("aaa")())
        self.assertIsInstance(made, LoginError)
        self.assertIn("flnd login aaa", made.hint)
        self.assertEqual(made.target, "aaa:example_user")
        self.assertFalse(made.retryable)

    def test_site_error(self):
        error = self.refusal(400, f"{SITE}/api")
        self.assertIs(type(error), SiteError)
        self.assertEqual(error.status, 400)
        self.assertEqual(error.response.status, 400)
        self.assertFalse(error.retryable)

    def test_blocked(self):
        with _net.fake({f"{SITE}/": _net.reply(429, headers={"Retry-After": "9"})}):
            with self.assertRaises(Blocked) as caught:
                fetch(f"{SITE}/")
        self.assertEqual((caught.exception.wait, caught.exception.status), (9.0, 429))
        self.assertTrue(caught.exception.retryable)

    def test_not_found(self):
        for status in (404, 410):
            with self.subTest(status=status):
                error = self.refusal(status, f"{SITE}/gone")
                self.assertIsInstance(error, NotFound)
                self.assertFalse(error.retryable)

    def test_changed(self):
        with _net.fake({f"{SITE}/api": _net.reply(200, html="<h1>Log in</h1>")}):
            page = fetch(f"{SITE}/api")
        with self.assertRaises(Changed) as caught:
            page.json
        self.assertIn(f"expected JSON from {SITE}/api", str(caught.exception))
        self.assertFalse(caught.exception.retryable)

    def test_network_error(self):
        reset = ConnectionResetError("connection reset")
        with _net.fake({f"{SITE}/": self.serve(reset)}):
            with self.assertRaises(NetworkError) as caught:
                fetch(f"{SITE}/", retries=0)
        self.assertTrue(caught.exception.retryable)
        self.assertIn("connection reset", str(caught.exception))

    def test_disk_error(self):
        made = disk(OSError(errno.ENOSPC, "No space left on device"), "media/a.jpg")
        self.assertEqual(str(made), "media/a.jpg: No space left on device")
        self.assertIn("free some space", made.hint)
        self.assertEqual(disk(OSError(errno.ENAMETOOLONG, "File name too long")).hint,
                         "use a shorter --home or folder template")
        refused = OSError(errno.EACCES, "Permission denied")
        with mock.patch.object(_files.os, "replace", side_effect=refused):
            with self.assertRaises(DiskError) as caught:
                _files.write(Path(self.enterContext(_folder())) / "a.txt", "x")
        self.assertIn("Permission denied", str(caught.exception))
        self.assertFalse(caught.exception.retryable)

    def test_tool_error(self):
        with self.assertRaises(ToolError) as caught:
            _tools.find("flnd-no-such-tool-example")
        self.assertIn("not found on PATH", str(caught.exception))
        self.assertEqual(caught.exception.hint,
                         "install flnd-no-such-tool-example, or put it on PATH")

    def test_source_error(self):
        with self.assertRaisesRegex(SourceError, "other.example"):
            class Outside(Source):
                code, name, domains = "out", "Outside", ("aaa.example",)
                api = 1
                filename = "{date}_{post}{index}"

                @target("other.example/{user}")
                def user(self, site, user): ...

        with self.assertRaises(SourceError):
            File([])
        # An unexpected error in a listing is a bug in the source: a SourceError naming it.
        made = classify(KeyError("id"), target="aaa:example_user")
        self.assertIsInstance(made, SourceError)
        self.assertEqual((str(made), made.target),
                         ("unexpected KeyError: 'id'", "aaa:example_user"))
        self.assertIsInstance(made.__cause__, KeyError)

    def test_stopped(self):
        self.assertTrue(issubclass(Stopped, KeyboardInterrupt))
        self.assertFalse(issubclass(Stopped, Error))
        stop = threading.Event()
        stop.set()
        with self.assertRaises(Stopped), _net.fake({f"{SITE}/": 503}):
            try:
                fetch(f"{SITE}/", stop=stop)
            except Error:  # never catches a stop
                self.fail("except Error caught a Stopped")
        self.assertIs(classify(Stopped()).__class__, Stopped)

    def refusal(self, status: int, url: str) -> SiteError:
        """What the client raises for `status` at `url`, after its tries."""
        with _net.fake({url: status}):
            try:
                fetch(url, retries=0)
            except SiteError as error:
                return error
        raise AssertionError(f"no error for HTTP {status}")


# ---------------------------------------------------------------------------------------------
# 2. One test per row of the policy
# ---------------------------------------------------------------------------------------------

class PolicyTest(HttpCase):
    def test_a_timeout_a_reset_or_a_5xx_is_retried_with_backoff_then_fails(self):
        timeout = TimeoutError("slow")
        for answer, kind in ((_net.reply(503), SiteError), (timeout, NetworkError)):
            self.seen.clear()
            self.clock.slept.clear()
            with self.subTest(kind=kind.__name__), _net.fake({f"{SITE}/": self.serve(answer)}):
                with self.assertRaises(kind) as caught:
                    fetch(f"{SITE}/")
                self.assertTrue(caught.exception.retryable)
                # The first try and 4 more, after waits of 1, 2, 4 and 8 s.
                self.assertEqual(len(self.seen), 5)
                self.assertEqual(self.clock.slept, [1.0, 2.0, 4.0, 8.0])

    def test_a_429_waits_what_the_site_asks_then_is_blocked(self):
        route = self.serve(_net.reply(429, headers={"Retry-After": "30"}))
        with _net.fake({f"{SITE}/": route}):
            with self.assertRaises(Blocked) as caught:
                fetch(f"{SITE}/")
        # The third 429 in a row is the last: two waits, then `Blocked`.
        self.assertEqual(self.clock.slept, [30.0, 30.0])
        self.assertEqual(caught.exception.wait, 30.0)

    def test_a_404_or_410_is_gone_and_never_asked_again(self):
        for status in (404, 410):
            self.seen.clear()
            with self.subTest(status=status), \
                    _net.fake({f"{SITE}/": self.serve(_net.reply(status))}):
                with self.assertRaises(NotFound):
                    fetch(f"{SITE}/")
                self.assertEqual((len(self.seen), self.clock.slept), (1, []))

    def test_a_401_or_403_on_the_sources_domains_is_a_login_error_for_that_source(self):
        source = make_source("aaa")()
        who = Target("aaa:example_user", kind="user")
        for status in (401, 403):
            with self.subTest(status=status):
                own = ClassesTest.refusal(self, status, "https://api.aaa.example/me")
                self.assertIsInstance(job.failure(own, who, source), LoginError)
                # The same status from a CDN is an expired link, not a login: it stays a refusal.
                other = ClassesTest.refusal(self, status, "https://cdn.example/a.jpg")
                self.assertIs(type(job.failure(other, who, source)), SiteError)

    def test_a_truncated_transfer_keeps_its_part_to_resume(self):
        body = b"0123456789" * 10

        def cut(request: _net.Request) -> _net.Reply:
            def pieces():
                yield body[:30]
                raise ConnectionResetError("connection reset")

            return _net.reply(200, headers={"content-length": str(len(body))}, chunks=pieces())

        folder = Path(self.enterContext(_folder()))
        dest = folder / "a.mp4"
        with _net.fake({f"{SITE}/a.mp4": cut}):
            with self.assertRaises(NetworkError) as caught:
                download(f"{SITE}/a.mp4", dest, retries=0)
        self.assertTrue(caught.exception.retryable)
        self.assertFalse(dest.exists())
        self.assertEqual(_files._part_path(dest).read_bytes(), body[:30])
        # The next try resumes from byte 30 with a Range request.
        with _net.fake({f"{SITE}/a.mp4": body}):
            download(f"{SITE}/a.mp4", dest)
        self.assertEqual(dest.read_bytes(), body)

    def test_a_parse_failure_is_changed_and_not_retried(self):
        with _net.fake({f"{SITE}/api": self.serve(_net.reply(200, html="<p>moved</p>"))}):
            with self.assertRaises(Changed) as caught:
                fetch(f"{SITE}/api").json
        self.assertEqual(len(self.seen), 1)
        self.assertFalse(caught.exception.retryable)

    def test_secrets_never_reach_a_message(self):
        url = f"https://user:{PASSWORD}@example.com/a?token={TOKEN}#{TOKEN}"
        reset = ConnectionError(f"reset while sending {TOKEN}")
        for answer in (_net.reply(403), _net.reply(429), reset):
            with self.subTest(answer=type(answer).__name__), \
                    _net.fake({"https://example.com/a": self.serve(answer)}):
                with self.assertRaises(Error) as caught:
                    fetch(url, retries=0, headers={"Authorization": f"Bearer {TOKEN}"})
                self.assertNotIn(TOKEN, str(caught.exception))
                self.assertNotIn(PASSWORD, str(caught.exception))


class PolicyEngineTest(EngineCase):
    def test_a_file_that_fails_fails_alone(self):
        self.routes["https://cdn.example/bad.jpg"] = _net.reply(503)
        source = make_source("aaa", files=[File("https://cdn.example/bad.jpg", name="bad.jpg"),
                                           File("https://cdn.example/good.jpg", name="good.jpg")])
        report = self.report(source, targets=["aaa:example_user"])
        self.assertEqual([path.name for path in report.targets[0].written], ["good.jpg"])
        self.assertIsNone(report.targets[0].error)
        self.assertEqual(self.failures(), ["bad.jpg: GET https://cdn.example/bad.jpg: HTTP 503"])

    def test_a_refused_login_stops_its_source_and_the_others_go_on(self):
        self.routes["https://api.aaa.example/me"] = _net.reply(403)
        refused = make_source("aaa", lambda site: site.fetch("https://api.aaa.example/me"))
        report = self.report(refused, make_source("bbb"),
                             targets=["aaa:example_user", "aaa:example_creator",
                                      "bbb:example_user"])
        first, second, other = report.targets
        self.assertIsInstance(first.error, LoginError)
        self.assertIs(second.error, first.error)
        self.assertIsNone(other.error)
        asked = [str(request.url) for request in self.requests if "aaa.example" in str(request.url)]
        self.assertEqual(asked, ["https://api.aaa.example/me"])

    def test_disk_full_stops_the_run(self):
        def full(site):
            raise disk(OSError(errno.ENOSPC, "No space left on device"), "media/aaa")

        ran = []
        with self.assertRaises(DiskError):
            self.report(make_source("aaa", full), make_source("bbb", ran.append),
                        targets=["aaa:example_user", "bbb:example_user"])
        self.assertEqual(ran, [])


# ---------------------------------------------------------------------------------------------
# 3. Exit codes, and no traceback for a person's mistake
# ---------------------------------------------------------------------------------------------

class CliTest(FlndCase):
    """The exit code of each class is `tests/test_cli.py`'s; these go through a whole download."""

    def setUp(self):
        super().setUp()
        self.enterContext(_time.use(_time.Fake()))

    def test_a_usage_error_from_flnd_itself_has_no_traceback(self):
        with mock.patch.dict(sources.SOURCES, {"aaa": make_source("aaa")}):
            for argv in (("download", "aaa:example_user", "--when", "yesterday"),
                         ("download", "nosuchsource:example_user"), ("download", "--no-such-flag")):
                self.dest.seek(0)
                self.dest.truncate()
                with self.subTest(argv=argv):
                    code, _, out = self.run_app(*argv)
                    self.assertEqual(code, 2)
                    self.assertNotIn("Traceback", out)
                    errors = [line for line in out.splitlines() if "error: " in line]
                    self.assertEqual(len(errors), 1)

    def test_a_login_the_source_needs_exits_3(self):
        source = make_source("aaa", login=login_cookies("sid", required=True))
        with mock.patch.dict(sources.SOURCES, {"aaa": source}):
            code, out, err = self.run_app("download", "aaa:example_user", "--ui", "plain")
        self.assertEqual(code, 3)
        self.assertIn("  run flnd login aaa", out)
        self.assertNotIn("Traceback", out + err)


def _folder():
    """A temporary folder, as a context manager giving its path."""
    import tempfile

    return tempfile.TemporaryDirectory()


if __name__ == "__main__":
    unittest.main()
