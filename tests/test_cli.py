"""Tests for `flnd._cli`: commands, groups, `.raw`, exit codes, help, and `stopping()`."""

from __future__ import annotations

import io
import signal
import threading
import unittest
from contextlib import redirect_stderr, redirect_stdout

import cyclopts

from flnd import _base
from flnd import _cli as cli


def _run_captured(app: cli.App, *argv: str) -> tuple[int, str, str]:
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = app.run(list(argv))
    return code, out.getvalue(), err.getvalue()


class ErrorAndExitCodeTest(unittest.TestCase):
    def test_error_hierarchy(self) -> None:
        self.assertTrue(issubclass(cli.Error, _base.Error))
        self.assertTrue(issubclass(cli.UsageError, cli.Error))
        self.assertTrue(issubclass(cli.UsageError, ValueError))
        self.assertFalse(hasattr(cli, "main"))

    def test_bool_int_and_none_exit_codes(self) -> None:
        app = cli.App(name="tool")

        @app.command
        def ok_true() -> bool:
            return True

        @app.command
        def fail_false() -> bool:
            return False

        @app.command
        def ret_int() -> int:
            return 7

        @app.command
        def ret_none() -> None:
            return None

        self.assertEqual(app.run(["ok-true"]), 0)
        self.assertEqual(app.run(["fail-false"]), 1)
        self.assertEqual(app.run(["ret-int"]), 7)
        self.assertEqual(app.run(["ret-none"]), 0)

    def test_usage_error_and_keyboard_interrupt(self) -> None:
        app = cli.App(name="tool")

        @app.command
        def check(value: str) -> int:
            if value == "bad":
                raise cli.UsageError("bad value")
            if value == "ctrlc":
                raise KeyboardInterrupt
            return 0

        code, _, err = _run_captured(app, "check", "bad")
        self.assertEqual(code, 2)
        self.assertIn("bad value", err)

        code, _, err = _run_captured(app, "unknown")
        self.assertEqual(code, 2)
        self.assertTrue(err)

        self.assertEqual(app.run(["check", "ctrlc"]), 130)


class CommandsGroupsAndHelpTest(unittest.TestCase):
    def test_commands_groups_raw_and_help(self) -> None:
        app = cli.App(name="notes", help="Manage notes.")
        self.assertIsInstance(app.raw, cyclopts.App)
        seen: list[object] = []

        @app.command
        def add_item(text: str, *, count: int = 1) -> int:
            """Add a note.

            Args:
                text: Note body.
                count: Number of copies.
            """
            seen.append((text, count))
            return 0

        auth = app.group("auth", help="Auth commands.")

        @auth.command("set-token")
        def set_token(token: str) -> int:
            """Save an auth token."""
            seen.append(token)
            return 3

        self.assertEqual(app.run(["add-item", "hello", "--count", "2"]), 0)
        self.assertEqual(app.run(["auth", "set-token", "tok"]), 3)
        self.assertEqual(seen, [("hello", 2), "tok"])

        code, out, _ = _run_captured(app, "add-item", "--help")
        self.assertEqual(code, 0)
        self.assertIn("Note body.", out)
        self.assertIn("Number of copies.", out)
        self.assertIn("Manage notes.", _run_captured(app, "--help")[1])

    def test_group_default(self) -> None:
        app = cli.App(name="tool")
        app.group("sync", default=lambda: 5)
        self.assertEqual(app.run(["sync"]), 5)


class StoppingTest(unittest.TestCase):
    def test_two_stage_ctrl_c_sets_soon_then_now(self) -> None:
        previous = signal.getsignal(signal.SIGINT)
        with cli.stopping() as stop:
            self.assertFalse(stop.soon.is_set())
            self.assertFalse(stop.now.is_set())
            signal.raise_signal(signal.SIGINT)
            self.assertTrue(stop.soon.is_set())
            self.assertFalse(stop.now.is_set())
            signal.raise_signal(signal.SIGINT)
            self.assertTrue(stop.soon.is_set())
            self.assertTrue(stop.now.is_set())
        self.assertIs(signal.getsignal(signal.SIGINT), previous)

    def test_stopping_in_worker_thread_is_safe(self) -> None:
        results: list[tuple[bool, bool]] = []

        def worker() -> None:
            with cli.stopping() as stop:
                results.append((stop.soon.is_set(), stop.now.is_set()))

        thread = threading.Thread(target=worker)
        thread.start()
        thread.join()
        self.assertEqual(results, [(False, False)])
