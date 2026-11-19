"""The CLI's frame: exit codes per error class, no traceback for a person's mistake, the crash
file, two-stage Ctrl-C (`flnd.engine.run.stopping`). The prompts are
tests/test_forms.py's.

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
from flnd.engine import run as _run
from flnd.engine import store as _store
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
)
from flnd.ui import out


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
                 (ToolError("no ffmpeg"), 4), (PluginError("bug"), 4), (Error("other"), 1),
                 (Stopped("stopped"), 130), (RuntimeError("bug"), 4)]
        for error, code in cases:
            with self.subTest(type(error).__name__):
                self.assertEqual(self.fail_with(error)[0], code)
                self.assertEqual(cli.exit_code(error), code)
        self.assertEqual(cli.exit_code(None), 0)
        # A bare Ctrl-C ends the command with 130 too, though only `Stopped` has that code.
        self.assertEqual(self.fail_with(KeyboardInterrupt())[0], 130)

    def test_a_persons_mistake_is_one_line_and_a_hint_never_a_traceback(self) -> None:
        for error in (UsageError("give at least one target", hint="flnd download ins:example_user"),
                      LoginError("ins: not logged in", hint="run flnd login ins"),
                      DiskError("media: No space left on device", hint="free some space"),
                      ToolError("ffmpeg: executable not found", hint="install ffmpeg")):
            with self.subTest(type(error).__name__):
                _, out, err = self.fail_with(error)
                self.assertEqual(out, "")
                self.assertEqual(err.splitlines(), [f"x {error}", f"  hint  {error.hint}"])
                self.assertFalse((Path(".flnd") / _store.LOGS).exists())

    def test_a_flag_mistake_exits_2_as_a_usage_error(self) -> None:
        err = io.StringIO()
        with redirect_stderr(err), redirect_stdout(io.StringIO()):
            self.assertEqual(cli.main(["download", "--nope"]), 2)
        self.assertEqual(err.getvalue().splitlines(),
                         ["x flnd: unrecognized arguments: --nope",
                          "  hint  flnd -h lists what it takes"])

    def test_a_bug_writes_a_crash_file_and_exits_4(self) -> None:
        code, _, err = self.fail_with(RuntimeError("an example bug"))
        self.assertEqual(code, 4)
        crashes = list((Path(".flnd") / _store.LOGS).glob("crash-*.txt"))
        self.assertEqual(len(crashes), 1)
        self.assertIn("Traceback", crashes[0].read_text(encoding="utf-8"))
        self.assertEqual(err.splitlines(), ["x flnd hit a bug: RuntimeError: an example bug",
                                            f"  hint  details in {crashes[0]}"])

    def test_with_verbose_a_bug_prints_its_traceback_instead(self) -> None:
        code, _, err = self.fail_with(RuntimeError("an example bug"), "-v")
        self.assertEqual(code, 4)
        self.assertIn("Traceback", err)
        self.assertIn("RuntimeError: an example bug", err)
        self.assertFalse((Path(".flnd") / _store.LOGS).exists())

    def test_the_crash_file_goes_in_the_home_given(self) -> None:
        self.fail_with(RuntimeError("an example bug"), "--home", "elsewhere")
        self.assertEqual(len(list((Path("elsewhere") / _store.LOGS).glob("crash-*.txt"))), 1)


class HelpLengthTest(unittest.TestCase):
    """`flnd -h` fits in 24 lines of 80 columns (PLAN.md 8.1), on a pipe, a terminal, in ASCII."""

    class Pipe(io.StringIO):
        encoding = "utf-8"

    class Terminal(Pipe):
        def isatty(self) -> bool:
            return True

    class Ascii(Terminal):
        encoding = "cp1252"

    def test_flnd_h_fits_in_24_lines(self) -> None:
        self.enterContext(mock.patch.dict(os.environ, {"COLUMNS": "80"}))
        for kind in (self.Pipe, self.Terminal, self.Ascii):
            with self.subTest(kind.__name__):
                stdout = kind()
                with mock.patch("sys.stdout", stdout), redirect_stderr(io.StringIO()):
                    self.assertEqual(cli.main(["-h"]), 0)
                lines = out.clean(stdout.getvalue()).splitlines()
                self.assertLessEqual(len(lines), 24, "\n".join(lines))
                self.assertTrue(all(len(line) <= 80 for line in lines), "\n".join(lines))


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


if __name__ == "__main__":
    unittest.main()
