"""The CLI's frame: exit codes per error class, no traceback for a person's mistake, the crash
file, two-stage Ctrl-C (`flnd.engine.run.stopping`), and the prompts of `flnd.ui.forms`.

Each error is raised from a library call (`Flnd.check`, which `flnd check` makes), so the test
goes through the CLI's real path from flags to exit code. Nothing touches a site.
"""

from __future__ import annotations

import io
import os
import signal
import tempfile
import threading
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

from flnd import cli
from flnd.engine import home as _home
from flnd.engine import run as _run
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
)
from flnd.ui import forms as _prompt


class TerminalCase(unittest.TestCase):
    """In a temporary folder (the home is `./.flnd`), with `$FLND_HOME` unset."""

    def setUp(self) -> None:
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        previous = os.getcwd()
        os.chdir(folder.name)
        self.addCleanup(os.chdir, previous)
        self.enterContext(mock.patch.dict(os.environ))
        os.environ.pop("FLND_HOME", None)

    def fail_with(self, error: BaseException, *flags: str) -> tuple[int, str, str]:
        """Run `flnd check` with `Flnd.check` raising `error`: the code, stdout and stderr."""
        out, err = io.StringIO(), io.StringIO()
        with mock.patch("flnd._library.Flnd.check", side_effect=error), \
                redirect_stdout(out), redirect_stderr(err):
            code = cli.main(["check", *flags])
        return code, out.getvalue(), err.getvalue()


class ExitCodeTest(TerminalCase):
    def test_each_class_has_its_exit_code(self) -> None:
        cases = [(UsageError("bad flag"), 2), (LoginError("not logged in"), 3),
                 (SiteError("refused"), 1), (Blocked("wait"), 1), (NotFound("gone"), 1),
                 (Changed("new shape"), 1), (NetworkError("reset"), 1), (DiskError("full"), 4),
                 (ToolError("no ffmpeg"), 4), (SourceError("bug"), 4), (Error("other"), 4),
                 (Stopped("stopped"), 130), (KeyboardInterrupt(), 130),
                 (RuntimeError("bug"), 4)]
        for error, code in cases:
            with self.subTest(type(error).__name__):
                self.assertEqual(self.fail_with(error)[0], code)
                self.assertEqual(cli.exit_code(error), code)
        self.assertEqual(cli.exit_code(None), 0)

    def test_a_persons_mistake_is_one_line_and_a_hint_never_a_traceback(self) -> None:
        for error in (UsageError("give at least one target", hint="flnd download ins:example_user"),
                      LoginError("ins: not logged in", hint="run flnd login ins"),
                      DiskError("media: No space left on device", hint="free some space"),
                      ToolError("ffmpeg: executable not found", hint="install ffmpeg")):
            with self.subTest(type(error).__name__):
                _, out, err = self.fail_with(error)
                self.assertEqual(out, "")
                self.assertEqual(err.splitlines(), [f"error: {error}", f"  {error.hint}"])
                self.assertFalse((Path(".flnd") / _home.LOGS).exists())

    def test_a_flag_mistake_exits_2_with_the_usage(self) -> None:
        err = io.StringIO()
        with redirect_stderr(err), redirect_stdout(io.StringIO()):
            self.assertEqual(cli.main(["download", "--nope"]), 2)
        self.assertIn("flnd: error: unrecognized arguments: --nope", err.getvalue())
        self.assertNotIn("Traceback", err.getvalue())

    def test_a_bug_writes_a_crash_file_and_exits_4(self) -> None:
        code, _, err = self.fail_with(RuntimeError("an example bug"))
        self.assertEqual(code, 4)
        crashes = list((Path(".flnd") / _home.LOGS).glob("crash-*.txt"))
        self.assertEqual(len(crashes), 1)
        self.assertIn("Traceback", crashes[0].read_text(encoding="utf-8"))
        self.assertEqual(err.splitlines(), ["error: unexpected RuntimeError: an example bug; "
                                            f"the details are in {crashes[0]}"])

    def test_with_verbose_a_bug_prints_its_traceback_instead(self) -> None:
        code, _, err = self.fail_with(RuntimeError("an example bug"), "-v")
        self.assertEqual(code, 4)
        self.assertIn("Traceback", err)
        self.assertIn("RuntimeError: an example bug", err)
        self.assertFalse((Path(".flnd") / _home.LOGS).exists())

    def test_the_crash_file_goes_in_the_home_given(self) -> None:
        self.fail_with(RuntimeError("an example bug"), "--home", "elsewhere")
        self.assertEqual(len(list((Path("elsewhere") / _home.LOGS).glob("crash-*.txt"))), 1)


class StoppingTest(unittest.TestCase):
    def test_two_stage_ctrl_c_sets_soon_then_now(self) -> None:
        previous = signal.getsignal(signal.SIGINT)
        with _run.stopping() as stop:
            self.assertFalse(stop.soon.is_set())
            signal.raise_signal(signal.SIGINT)
            self.assertTrue(stop.soon.is_set())
            self.assertFalse(stop.now.is_set())
            signal.raise_signal(signal.SIGINT)
            self.assertTrue(stop.now.is_set())
        self.assertIs(signal.getsignal(signal.SIGINT), previous)

    def test_stopping_in_worker_thread_is_safe(self) -> None:
        results: list[tuple[bool, bool]] = []

        def worker() -> None:
            with _run.stopping() as stop:
                results.append((stop.soon.is_set(), stop.now.is_set()))

        thread = threading.Thread(target=worker)
        thread.start()
        thread.join()
        self.assertEqual(results, [(False, False)])


class Terminal(io.StringIO):
    """A standard input that says it is a terminal."""

    def isatty(self) -> bool:
        return True


class PromptTest(unittest.TestCase):
    def terminal(self) -> None:
        self.enterContext(mock.patch("sys.stdin", Terminal("")))

    def test_choose_returns_what_questionary_selects(self) -> None:
        self.terminal()
        answers = iter([2, "b"])
        ask = mock.Mock(side_effect=lambda *_, **__: mock.Mock(unsafe_ask=lambda: next(answers)))
        with mock.patch("flnd.ui.forms.questionary.select", ask):
            self.assertEqual(_prompt.choose("Pick", {"a": 1, "b": 2}), 2)
            self.assertEqual(_prompt.choose("Pick", ["a", "b"]), "b")

    def test_an_empty_line_takes_the_default(self) -> None:
        self.terminal()
        with mock.patch("flnd.ui.forms.questionary.select", return_value=mock.Mock(unsafe_ask=lambda: 2)), \
                mock.patch("flnd.ui.forms.questionary.text", return_value=mock.Mock(unsafe_ask=lambda: "")), \
                mock.patch("flnd.ui.forms.questionary.confirm", return_value=mock.Mock(unsafe_ask=lambda: False)):
            self.assertEqual(_prompt.choose("Pick", {"a": 1, "b": 2}, default=2), 2)
            self.assertEqual(_prompt.ask("Name", default="example_user"), "example_user")
            self.assertFalse(_prompt.confirm("Go on", default=False))

    def test_confirm_uses_questionary(self) -> None:
        self.terminal()
        values = iter([True, False])
        with mock.patch(
            "flnd.ui.forms.questionary.confirm",
            return_value=mock.Mock(unsafe_ask=lambda: next(values)),
        ):
            self.assertTrue(_prompt.confirm("Go on"))
            self.assertFalse(_prompt.confirm("Go on"))

    def test_a_secret_uses_questionary_password(self) -> None:
        self.terminal()
        with mock.patch(
            "flnd.ui.forms.questionary.password",
            return_value=mock.Mock(unsafe_ask=lambda: "hunter2-example"),
        ) as password:
            self.assertEqual(_prompt.ask("Password", secret=True), "hunter2-example")
        password.assert_called_once()

    def test_without_a_terminal_the_default_or_a_usage_error(self) -> None:
        self.enterContext(mock.patch("sys.stdin", io.StringIO("")))
        self.assertEqual(_prompt.ask("Name", default="example_user"), "example_user")
        self.assertTrue(_prompt.confirm("Go on"))
        self.assertEqual(_prompt.choose("Pick", ["a", "b"], default="a"), "a")
        with self.assertRaises(UsageError):
            _prompt.ask("Name")
        with self.assertRaises(UsageError):
            _prompt.choose("Pick", ["a", "b"])

    def test_ended_input_and_bad_choices_are_usage_errors(self) -> None:
        self.terminal()
        with mock.patch("flnd.ui.forms.questionary.text", return_value=mock.Mock(unsafe_ask=lambda: None)):
            with self.assertRaises(UsageError):
                _prompt.ask("Name")
        with self.assertRaises(UsageError):
            _prompt.choose("Pick", [])
        with self.assertRaises(UsageError):
            _prompt.choose("Pick", ["a"], default="z")

    def test_a_live_display_is_held_while_asking(self) -> None:
        self.terminal()
        events: list[str] = []
        from contextlib import contextmanager

        from flnd.ui import log

        @contextmanager
        def track():
            events.append("hold")
            yield
            events.append("release")

        with mock.patch.object(log.log, "hold", track), \
                mock.patch("flnd.ui.forms.questionary.text", return_value=mock.Mock(unsafe_ask=lambda: "typed")):
            self.assertEqual(_prompt.ask("Name"), "typed")
        self.assertEqual(events, ["hold", "release"])


if __name__ == "__main__":
    unittest.main()
