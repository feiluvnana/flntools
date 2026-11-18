"""Tests for `flncore.cli` and the Charm help snapshot."""

from __future__ import annotations

import io
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from flncore.cli import OK, USAGE, App, exit_code, report
from flnd.errors import DiskError, Error, NetworkError, UsageError


def _demo_app() -> App:
    app = App(
        "flnd",
        version="0.2.0",
        help="download what you follow, and keep it up to date",
        codes={UsageError: 2},
    )
    target = app.group("target", "the targets this home knows")

    @app.command
    def get(*targets: str, all: bool = False) -> int:
        """Download what the targets have now.

        Args:
            targets: example targets.
            all: every category.
        """
        return OK

    @app.command
    def sync(*targets: str) -> int:
        """Download what is new for the followed targets."""
        return OK

    @app.command
    def status() -> int:
        """Last run, failures, logins, and disk."""
        return OK

    @target.command(name="list")
    def list_rows(*targets: str) -> int:
        """List targets this home knows."""
        return OK

    @target.command
    def follow(*targets: str) -> int:
        """Have sync keep these up to date."""
        return OK

    return app


class CharmHelpTest(unittest.TestCase):
    def test_help_matches_the_snapshot(self) -> None:
        written = (Path(__file__).resolve().parent / "data" / "flncore-help.txt").read_text(
            encoding="utf-8",
        )
        text = _demo_app().help_text()
        self.assertEqual(text.rstrip("\n"), written.rstrip("\n"))
        self.assertLessEqual(len(text.splitlines()), 24)


class CliRunTest(unittest.TestCase):
    def test_a_command_runs_and_returns_its_code(self) -> None:
        self.assertEqual(_demo_app().run(["status"]), OK)

    def test_a_usage_mistake_exits_2(self) -> None:
        err = io.StringIO()
        with mock.patch("sys.stderr", err):
            code = _demo_app().run(["--nope"])
        self.assertEqual(code, USAGE)
        self.assertIn("error:", err.getvalue())

    def test_a_library_error_is_reported_with_its_code(self) -> None:
        app = _demo_app()

        @app.command
        def broken() -> int:
            raise NetworkError("timed out", hint="retry later")

        err = io.StringIO()
        with mock.patch("sys.stderr", err):
            code = app.run(["broken"])
        self.assertEqual(code, 1)
        self.assertIn("timed out", err.getvalue())
        self.assertIn("retry later", err.getvalue())

    def test_json_errors_go_to_stdout(self) -> None:
        out = io.StringIO()
        with mock.patch("sys.stdout", out), mock.patch("sys.stderr", io.StringIO()):
            report(UsageError("bad flag", hint="read help"), json_out=True)
        line = out.getvalue()
        self.assertIn('"type": "Error"', line)
        self.assertIn("bad flag", line)

    def test_exit_code_table(self) -> None:
        self.assertEqual(exit_code(None), OK)
        self.assertEqual(exit_code(UsageError("x")), USAGE)
        self.assertEqual(exit_code(NetworkError("x")), 1)
        self.assertEqual(exit_code(DiskError("x")), 4)
        self.assertEqual(exit_code(Error("x")), 4)

    def test_a_bug_writes_a_crash_file(self) -> None:
        app = _demo_app()

        @app.command
        def kaboom() -> int:
            raise RuntimeError("demo bug")

        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        home = Path(folder.name)
        err = io.StringIO()
        with mock.patch("sys.stderr", err):
            code = app.run(["kaboom"], home=home)
        self.assertEqual(code, 4)
        crashes = list(home.glob("log/crash-*.txt"))
        self.assertEqual(len(crashes), 1)
        self.assertIn("RuntimeError", crashes[0].read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()