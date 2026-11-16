"""The CLI's frame: exit codes per error class, no traceback for a person's mistake, the crash
file, two-stage Ctrl-C (`flnd._run.stopping`), and the prompts of `flnd._prompt`.

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

from flnd import _home, _prompt, _run, _ui, cli
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
    def answers(self, *lines: str) -> io.StringIO:
        """Type `lines` into a terminal; returns what the prompts showed."""
        shown = io.StringIO()
        self.enterContext(mock.patch("sys.stdin", Terminal("".join(f"{x}\n" for x in lines))))
        self.enterContext(redirect_stderr(shown))
        return shown

    def test_choose_by_number_or_label_and_ask_again_after_a_wrong_answer(self) -> None:
        shown = self.answers("0", "x", "2", "b")
        self.assertEqual(_prompt.choose("Pick", {"a": 1, "b": 2}), 2)
        self.assertEqual(_prompt.choose("Pick", ["a", "b"]), "b")
        self.assertIn("  1. a\n  2. b\nPick [1-2]: ", shown.getvalue())
        self.assertEqual(shown.getvalue().count("pick a number from 1 to 2"), 2)

    def test_an_empty_line_takes_the_default(self) -> None:
        self.answers("", "", "")
        self.assertEqual(_prompt.choose("Pick", {"a": 1, "b": 2}, default=2), 2)
        self.assertEqual(_prompt.ask("Name", default="example_user"), "example_user")
        self.assertFalse(_prompt.confirm("Go on", default=False))

    def test_confirm_reads_yes_and_no(self) -> None:
        shown = self.answers("maybe", "y", "no")
        self.assertTrue(_prompt.confirm("Go on"))
        self.assertFalse(_prompt.confirm("Go on"))
        self.assertIn("Go on? [Y/n] ", shown.getvalue())

    def test_a_secret_is_read_without_echo(self) -> None:
        self.answers()
        with mock.patch("getpass.getpass", return_value="hunter2-example") as read:
            self.assertEqual(_prompt.ask("Password", secret=True), "hunter2-example")
        read.assert_called_once_with("Password: ")

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
        self.answers()
        with self.assertRaises(UsageError):
            _prompt.ask("Name")
        with self.assertRaises(UsageError):
            _prompt.choose("Pick", [])
        with self.assertRaises(UsageError):
            _prompt.choose("Pick", ["a"], default="z")

    def test_a_live_display_is_paused_while_asking(self) -> None:
        self.answers("typed")
        events: list[str] = []

        class Live:
            is_started = True

            def stop(self) -> None:
                events.append("stop")

            def start(self) -> None:
                events.append("start")

        with mock.patch.object(_ui.ui.raw, "_live_stack", [Live()]):
            self.assertEqual(_prompt.ask("Name"), "typed")
        self.assertEqual(events, ["stop", "start"])


if __name__ == "__main__":
    unittest.main()
