"""The one error hierarchy (`flnd.errors`) and its policy (src/docs/errors.md).

Four groups of tests:
1. `ClassesTest`: one test per class, raising it where flnd raises it and checking its place in
   the tree and its `hint`, `target` and `retryable`.
2. `PolicyTest`: one test per row of the policy table, through the real HTTP client, the job
   and the engine, on a fake network and a fake clock.
3. `CliTest`: a usage error, and a login a source needs, through a whole download; the exit
   code of each class and the crash file are tests/test_cli.py's.
4. `SplitTest`: the classes flnkit defines and flnd re-exports, `__module__`, the exit code by
   class, and flnd's hints that `classify` fills in.

The network, the clock and the browser are faked; nothing here touches a real site.
"""

from __future__ import annotations

import errno
import io
import threading
import unittest
from pathlib import Path
from unittest import mock

from fakes import EngineCase, download, fetch, login_cookies, make_plugin
from test_cli import FlndCase

import flnd
from flnd.engine import crawl as job
from flnd.engine.plugins import PLUGINS
from flnd.errors import (
    Blocked,
    Changed,
    DiskError,
    Error,
    LoginError,
    NetworkError,
    NotFound,
    PluginError,
    SiteError,
    Stopped,
    ToolError,
    UsageError,
    classify,
    disk,
)
from flnd.plugin import File, Plugin, Target, dig, route
from flnd.ui.log import ui
from flnkit import clock as _time
from flnkit import errors as kit
from flnkit import files as _files
from flnkit import files as _tools
from flnkit import net as _net

SITE = "https://example.com"
# The secrets the redaction test plants: none may reach a message.
TOKEN = "s3cr3t-token-value"
PASSWORD = "hunter2-example"
ALL = (Error, UsageError, LoginError, SiteError, Blocked, NotFound, Changed, NetworkError,
       DiskError, ToolError, PluginError)


class HttpCase(unittest.TestCase):
    """The real client on `_net.fake` and a fake clock."""

    def setUp(self) -> None:
        self.clock = self.enterContext(_time.use(_time.Fake()))
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
            fl.get("nosuchsource:example_user")
        with self.assertRaises(UsageError), flnd.open("unused-home") as fl:
            fl.get("ins:example_user", when="yesterday")
        with self.assertRaises(UsageError), flnd.open("unused-home") as fl:
            fl.auth.logout("nosuchsource")

    def test_login_error(self):
        made = job.failure(self.refusal(403, "https://api.aaa.example/me"),
                           Target("aaa:example_user", kind="user"), make_plugin("aaa")())
        self.assertIsInstance(made, LoginError)
        self.assertIn("flnd auth login aaa", made.hint)
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
                fetch(f"{SITE}/")
        self.assertTrue(caught.exception.retryable)
        self.assertIn("connection reset", str(caught.exception))

    def test_disk_error(self):
        made = disk(OSError(errno.ENOSPC, "No space left on device"), "media/a.jpg")
        self.assertEqual(str(made), "media/a.jpg: No space left on device")
        self.assertEqual(made.hint, "")  # flnkit's: what to do is flnd's to say
        self.assertIn("free some space", classify(made).hint)
        too_long = disk(OSError(errno.ENAMETOOLONG, "File name too long"))
        self.assertEqual(classify(too_long).hint, "use a shorter --home or folder template")
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

    def test_plugin_error(self):
        with self.assertRaisesRegex(PluginError, "other.example"):
            class Outside(Plugin):
                code, name, domains = "out", "Outside", ("aaa.example",)
                api = 2
                filename = "{date}_{post}{index}"

                @route("other.example/{user}")
                async def user(self, ctx, user):
                    yield user

        with self.assertRaises(PluginError):
            File(mirrors=[])
        # An unexpected error in a listing is a bug in the plugin: a PluginError naming it.
        made = classify(KeyError("id"), target="aaa:example_user")
        self.assertIsInstance(made, PluginError)
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
                fetch(url)
            except SiteError as error:
                return error
        raise AssertionError(f"no error for HTTP {status}")


# ---------------------------------------------------------------------------------------------
# 2. One test per row of the policy
# ---------------------------------------------------------------------------------------------

class PolicyTest(HttpCase):
    def test_a_404_or_410_is_gone_and_never_asked_again(self):
        for status in (404, 410):
            self.seen.clear()
            with self.subTest(status=status), \
                    _net.fake({f"{SITE}/": self.serve(_net.reply(status))}):
                with self.assertRaises(NotFound):
                    fetch(f"{SITE}/")
                self.assertEqual((len(self.seen), self.clock.slept), (1, []))

    def test_a_401_or_403_on_the_sources_domains_is_a_login_error_for_that_source(self):
        source = make_plugin("aaa")()
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
                download(f"{SITE}/a.mp4", dest)
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
                    fetch(url, headers={"Authorization": f"Bearer {TOKEN}"})
                self.assertNotIn(TOKEN, str(caught.exception))
                self.assertNotIn(PASSWORD, str(caught.exception))


class PolicyEngineTest(EngineCase):
    def test_a_malformed_dig_path_fails_its_target_as_a_bug_in_the_plugin(self):
        report = self.report(make_plugin("aaa", lambda ctx: dig({"a": 1}, "a[")),
                             targets=["aaa:example_user"])
        error = report.targets[0].error
        self.assertIsInstance(error, PluginError)
        self.assertIn("dig: can't read", str(error))

    def test_a_file_that_fails_fails_alone(self):
        self.routes["https://cdn.example/bad.jpg"] = _net.reply(503)
        source = make_plugin("aaa", files=[File("https://cdn.example/bad.jpg", name="bad.jpg"),
                                           File("https://cdn.example/good.jpg", name="good.jpg")])
        report = self.report(source, targets=["aaa:example_user"])
        self.assertEqual([path.name for path in report.targets[0].written], ["good.jpg"])
        self.assertIsNone(report.targets[0].error)
        self.assertEqual(self.failures(), ["bad.jpg: GET https://cdn.example/bad.jpg: HTTP 503"])

    def test_a_refused_login_stops_its_source_and_the_others_go_on(self):
        self.routes["https://api.aaa.example/me"] = _net.reply(403)
        # One target at a time, so the one after the refused login hasn't started.
        refused = make_plugin("aaa", lambda ctx: ctx.fetch("https://api.aaa.example/me"),
                              browser=True)
        report = self.report(refused, make_plugin("bbb"),
                             targets=["aaa:example_user", "aaa:example_creator",
                                      "bbb:example_user"])
        first, second, other = report.targets
        self.assertIsInstance(first.error, LoginError)
        self.assertIs(second.error, first.error)
        self.assertIsNone(other.error)
        asked = [str(request.url) for request in self.requests if "aaa.example" in str(request.url)]
        self.assertEqual(asked, ["https://api.aaa.example/me"])

    def test_disk_full_stops_the_run(self):
        def full(ctx):
            raise disk(OSError(errno.ENOSPC, "No space left on device"), "media/aaa")

        ran = []
        with self.assertRaises(DiskError):
            self.report(make_plugin("aaa", full), make_plugin("bbb", ran.append),
                        targets=["aaa:example_user", "bbb:example_user"])
        self.assertEqual(ran, [])
        # Targets list at once, but each starts once the one before it has called its route.
        with self.assertRaises(DiskError):
            self.report(make_plugin("ccc", lambda ctx: ctx.target.value == "0" and full(ctx)
                                    or ran.append(ctx)),
                        targets=[f"ccc:{n}" for n in range(30)])
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
        with mock.patch.dict(PLUGINS, {"aaa": make_plugin("aaa")}):
            for argv in (("get", "aaa:example_user", "--when", "yesterday"),
                         ("get", "nosuchsource:example_user"), ("get", "--no-such-flag")):
                self.dest.seek(0)
                self.dest.truncate()
                with self.subTest(argv=argv):
                    code, _, out = self.run_app(*argv)
                    self.assertEqual(code, 2)
                    self.assertNotIn("Traceback", out)
                    errors = [line for line in out.splitlines() if line.startswith("x ")]
                    self.assertEqual(len(errors), 1)

    def test_a_flnkit_error_a_plugin_raises_reaches_the_cli_with_flnds_hint(self):
        def refused(ctx):
            raise kit.LoginError("the site asked for a login")

        with mock.patch.dict(PLUGINS, {"aaa": make_plugin("aaa", refused)}):
            code, out, err = self.run_app("get", "aaa:example_user")
        self.assertEqual(code, 3)
        # The end of the run names the command; flnkit's error had no hint at all.
        self.assertIn("  hint  run flnd auth login aaa", err)
        self.assertIn("  run flnd auth login aaa", err)
        self.assertNotIn("Traceback", out + err)

    def test_a_full_disk_says_what_it_said_before_the_split(self):
        def full(ctx):
            raise kit.disk(OSError(errno.ENOSPC, "No space left on device"), "media/aaa")

        with mock.patch.dict(PLUGINS, {"aaa": make_plugin("aaa", full)}):
            code, out, err = self.run_app("get", "aaa:example_user")
        self.assertEqual(code, 4)
        self.assertIn("media/aaa: No space left on device", err)
        self.assertIn("free some space on that disk, or use another --home", err)

    def test_a_login_the_source_needs_exits_3(self):
        source = make_plugin("aaa", login=login_cookies("sid", required=True))
        with mock.patch.dict(PLUGINS, {"aaa": source}):
            code, out, err = self.run_app("get", "aaa:example_user")
        self.assertEqual(code, 3)
        self.assertIn("  run flnd auth login aaa", err)
        self.assertNotIn("Traceback", out + err)


# ---------------------------------------------------------------------------------------------
# 4. flnkit's classes, flnd's face of them
# ---------------------------------------------------------------------------------------------

KIT = ("Error", "UsageError", "LoginError", "SiteError", "Blocked", "NotFound", "Changed",
       "NetworkError", "DiskError", "ToolError", "Stopped")


class SplitTest(unittest.TestCase):
    def test_flnd_re_exports_flnkits_own_objects(self):
        from flnd import errors as flnd_errors
        from flnd import plugin as flnd_plugin

        self.assertEqual(sorted(kit.__all__), sorted([*KIT, "disk"]))
        for name in kit.__all__:
            with self.subTest(name):
                self.assertIs(getattr(flnd_errors, name), getattr(kit, name))
                if name != "disk":
                    self.assertIs(getattr(flnd, name), getattr(kit, name))
                if hasattr(flnd_plugin, name):
                    self.assertIs(getattr(flnd_plugin, name), getattr(kit, name))
        self.assertIs(flnd.LoginError, kit.LoginError)

    def test_each_class_names_the_module_it_is_defined_in(self):
        for name in KIT:
            with self.subTest(name):
                self.assertEqual(getattr(kit, name).__module__, "flnkit.errors")
        self.assertEqual(PluginError.__module__, "flnd.errors")
        self.assertTrue(issubclass(PluginError, kit.Error))

    def test_except_flnd_error_catches_what_flnkit_raises(self):
        with self.assertRaises(flnd.Error):
            raise kit.Changed("new shape")

    def test_the_exit_code_goes_by_class(self):
        from flnd import cli

        class LoginError(Exception):  # a plugin's own, merely named so
            pass

        class Refused(kit.LoginError):  # a subclass of the real one
            pass

        class Own(flnd.Error):
            pass

        cases = [(kit.Stopped(), 130), (kit.UsageError("x"), 2), (kit.LoginError("x"), 3),
                 (Refused("x"), 3), (kit.DiskError("x"), 4), (kit.ToolError("x"), 4),
                 (PluginError("x"), 4), (kit.Blocked("x"), 1), (Own("x"), 1),
                 (LoginError("x"), 4), (ValueError("x"), 4)]
        for error, code in cases:
            with self.subTest(type(error).__name__):
                self.assertEqual(cli.exit_code(error), code)

    def test_classify_fills_an_empty_hint_with_flnds(self):
        login = classify(kit.LoginError("not logged in"), target="ins:example_user")
        self.assertEqual(login.hint, "run flnd auth login ins")
        kept = classify(kit.LoginError("refused", hint="log in again"), target="ins:example_user")
        self.assertEqual(kept.hint, "log in again")
        self.assertEqual(classify(kit.LoginError("not logged in")).hint, "")
        full = classify(OSError(errno.ENOSPC, "No space left on device", "media/a.jpg"))
        self.assertIsInstance(full, DiskError)
        self.assertEqual(full.hint, "free some space on that disk, or use another --home")
        refused = kit.disk(OSError(errno.EACCES, "Permission denied"))
        self.assertEqual(classify(refused).hint, "check that you may write to that folder")

    def test_a_malformed_dig_path_escaping_a_plugin_is_a_plugin_error(self):
        with self.assertRaises(ValueError) as caught:
            dig({"a": 1}, "a[")
        made = classify(caught.exception, target="aaa:example_user")
        self.assertIsInstance(made, PluginError)
        self.assertNotIsInstance(made, Changed)
        self.assertIn("dig: can't read", str(made))

    def test_no_flnkit_hint_names_flnd(self):
        for path in sorted(Path(kit.__file__).parent.glob("*.py")):
            with self.subTest(path.name):
                self.assertEqual([hint for hint in _hints(path) if "flnd" in hint], [])


def _hints(path: Path) -> list[str]:
    """The text of every hint a module gives: each `hint=` argument and `*HINT*` constant."""
    import ast

    tree = ast.parse(path.read_text(encoding="utf-8"))
    constants = {target.id: node.value for node in tree.body if isinstance(node, ast.Assign)
                 for target in node.targets if isinstance(target, ast.Name)}
    values = [node.value for node in ast.walk(tree)
              if isinstance(node, ast.keyword) and node.arg == "hint"]
    values += [value for name, value in constants.items() if "HINT" in name]
    texts = []
    for value in values:
        if isinstance(value, ast.Name) and value.id in constants:
            value = constants[value.id]
        texts += [part.value for part in ast.walk(value)
                  if isinstance(part, ast.Constant) and isinstance(part.value, str)]
    return texts


def _folder():
    """A temporary folder, as a context manager giving its path."""
    import tempfile

    return tempfile.TemporaryDirectory()


if __name__ == "__main__":
    unittest.main()
