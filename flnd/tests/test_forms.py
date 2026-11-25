"""The forms of `flnd.ui.forms`: each field's keys, Ctrl-C, and the answers without a terminal.

The fields are fed keys directly, which is fast; a few cases go through Textual's pilot, to
check the app that carries them: keys in, the answer out, Ctrl-C as `Stopped`.
"""

from __future__ import annotations

import asyncio
import io
import os
import select
import threading
import unittest
from contextlib import contextmanager
from unittest import mock

from flnd.errors import Stopped, UsageError
from flnd.ui import forms
from flnd.ui.forms import PENDING, Option


class Terminal(io.StringIO):
    """A standard input that says it is a terminal."""

    def isatty(self) -> bool:
        return True


def feed(owner: forms.Owner, *keys: str) -> object:
    """Press `keys` on `owner` until it answers; PENDING if it never does."""
    said: object = PENDING
    for key in keys:
        said = owner.press(key, key if len(key) == 1 else None)
        if said is not PENDING:
            break
    return said


def drive(*runs: tuple[str, ...]) -> mock._patch:
    """Run each app that opens next through Textual's pilot, pressing the next of `runs`."""
    pending = list(runs)

    def show(app) -> None:
        keys = pending.pop(0)

        async def go() -> None:
            async with app.run_test(size=(100, 24)) as pilot:
                await pilot.press(*keys)
                await pilot.pause()

        asyncio.run(go())

    return mock.patch.object(forms, "_show", side_effect=show)


def drawn(renderable: object, width: int = 100) -> str:
    """`renderable` as plain text, each line's trailing spaces cut."""
    from rich.console import Console

    console = Console(width=width, color_system=None, file=io.StringIO())
    with console.capture() as captured:
        console.print(renderable)
    return "\n".join(line.rstrip() for line in captured.get().splitlines())


HOW = [Option("In a browser window", "browser", "flnd opens Chromium; log in as usual"),
       Option("With a QR code", "qr", "scan it in the app"),
       Option("From a cookie file", "file", "a cookies.txt you exported")]


class FieldTest(unittest.TestCase):
    def test_each_field_answers_its_keys(self) -> None:
        cases = [
            ("first", forms._Select("How?", forms._options(HOW), 0), ["enter"], "browser"),
            ("down", forms._Select("How?", forms._options(HOW), 0), ["down", "j", "enter"], "file"),
            ("wraps", forms._Select("How?", forms._options(HOW), 0), ["up", "enter"], "file"),
            ("back", forms._Select("How?", forms._options(HOW), 0), ["escape"], None),
            ("typed", forms._Text("Account", secret=False, default=""),
             ["e", "x", "backspace", "y", "enter"], "ey"),
            ("cleared", forms._Text("Account", secret=False, default=""),
             ["a", "ctrl+u", "b", "enter"], "b"),
            ("default", forms._Text("Account", secret=False, default="example_user"),
             ["enter"], "example_user"),
            ("text back", forms._Text("Account", secret=False, default=""), ["escape"], None),
            ("yes", forms._Confirm("Go on?", default=False), ["y"], True),
            ("no", forms._Confirm("Go on?", default=True), ["n"], False),
            ("enter", forms._Confirm("Go on?", default=True), ["enter"], True),
            ("toggled", forms._Confirm("Go on?", default=True), ["right", "enter"], False),
            ("confirm back", forms._Confirm("Go on?", default=True), ["escape"], False),
        ]
        for name, field, keys, answer in cases:
            with self.subTest(name):
                self.assertEqual(feed(field, *keys), answer)

    def test_the_select_looks_like_huh(self) -> None:
        field = forms._Select("How?", forms._options(HOW), 0)
        self.assertEqual(drawn(field.view(True)), "\n".join([
            "  ┃ How?",
            "  ┃ ❯ In a browser window   flnd opens Chromium; log in as usual",
            "  ┃   With a QR code        scan it in the app",
            "  ┃   From a cookie file    a cookies.txt you exported",
            "",
            "  ↑↓ choose · enter confirm · esc back",
        ]))
        feed(field, "down", "enter")
        self.assertEqual(drawn(field.view(False)), "  | How?\n  | With a QR code")

    def test_a_secret_shows_bullets(self) -> None:
        field = forms._Text("Password", secret=True, default="")
        feed(field, "a", "b", "c")
        self.assertEqual(drawn(field.view(True)).splitlines()[1], "  ┃ > •••")


class AskTest(unittest.TestCase):
    def setUp(self) -> None:
        self.enterContext(mock.patch("sys.stdin", Terminal("")))
        self.stdout = self.enterContext(mock.patch("sys.stdout", io.StringIO()))

    def test_the_app_carries_keys_in_and_the_answer_out(self) -> None:
        with drive(("down", "enter"), ("x", "enter"), ("n",)):
            self.assertEqual(forms.select("How?", HOW), "qr")
            self.assertEqual(forms.text("Account"), "x")
            self.assertFalse(forms.confirm("Go on?"))
        # Each answer stays on the terminal.
        self.assertEqual(self.stdout.getvalue(), "  | How?\n  | With a QR code\n"
                         "  | Account\n  | > x\n  | Go on?\n  | No\n")

    def test_ctrl_c_and_esc_leave_nothing_behind(self) -> None:
        with drive(("ctrl+c",), ("escape",)):
            with self.assertRaises(Stopped):
                forms.select("How?", HOW)
            self.assertIsNone(forms.text("Account"))
        self.assertEqual(self.stdout.getvalue(), "")

    def test_a_paste_types_into_a_text_field(self) -> None:
        field = forms._Text("Account", secret=False, default="")
        field.paste("example\n_user")
        self.assertEqual(feed(field, "enter"), "example _user")

    def test_the_old_names_keep_their_answers(self) -> None:
        with drive(("enter",), ("escape",)):
            self.assertEqual(forms.choose("Pick", {"a": 1, "b": 2}, default=2), 2)
            with self.assertRaises(UsageError):
                forms.ask("Name")
        for name, call in [("empty", lambda: forms.choose("Pick", [])),
                           ("not a choice", lambda: forms.choose("Pick", ["a"], default="z"))]:
            with self.subTest(name), self.assertRaises(UsageError):
                call()

    def test_a_live_display_is_held_while_asking(self) -> None:
        from flnd.ui import screen

        events: list[str] = []

        @contextmanager
        def hold():
            events.append("hold")
            yield
            events.append("release")

        with mock.patch.object(screen, "paused", hold), \
                mock.patch.object(forms, "_show", lambda app: events.append("asked")):
            forms.confirm("Go on?")
        self.assertEqual(events, ["hold", "asked", "release"])


@unittest.skipIf(os.name == "nt", "no pseudo-terminal on Windows")
class WorkerThreadTest(unittest.TestCase):
    """Off the main thread (the browser's question) a plain prompt stands in for Textual."""

    def setUp(self) -> None:
        import pty

        self.main, child = pty.openpty()
        self.child = child
        self.addCleanup(os.close, self.main)
        self.addCleanup(os.close, child)
        self.enterContext(mock.patch("sys.stdin", open(child, closefd=False)))
        self.enterContext(mock.patch("sys.stdout", open(child, "w", closefd=False,
                                                        encoding="utf-8")))

    def answer(self, ask, keys: bytes) -> list[object]:
        """Run `ask` on a worker thread, type `keys` once it has drawn (the terminal is raw by
        then, so Ctrl-C is a byte, not a signal); what it returned or raised."""
        said: list[object] = []

        def work() -> None:
            try:
                said.append(ask())
            except Stopped as stop:
                said.append(stop)

        worker = threading.Thread(target=work)
        worker.start()
        while worker.is_alive():
            if select.select([self.main], [], [], 0.05)[0]:
                os.read(self.main, 65536)
                if keys:
                    os.write(self.main, keys)
                    keys = b""
            worker.join(0.01)
        return said

    def test_each_field_answers_and_the_terminal_is_restored(self) -> None:
        import termios

        def modes() -> list[object]:
            return termios.tcgetattr(self.child)

        before = modes()
        cases = [("confirm", lambda: forms.confirm("Go on?", default=False), b"y", True),
                 ("select", lambda: forms.select("How?", HOW), b"\x1b[B\r", "qr"),
                 ("text", lambda: forms.text("Account"), b"ab\x7fc\r", "ac"),
                 ("esc", lambda: forms.text("Account"), b"\x1b", None)]
        for name, ask, keys, answer in cases:
            with self.subTest(name):
                self.assertEqual(self.answer(ask, keys), [answer])
                self.assertEqual(modes(), before)

    def test_ctrl_c_raises_stopped(self) -> None:
        said = self.answer(lambda: forms.confirm("Go on?"), b"\x03")
        self.assertIsInstance(said[0], Stopped)


class NoTerminalTest(unittest.TestCase):
    def setUp(self) -> None:
        self.enterContext(mock.patch("sys.stdin", io.StringIO("")))
        self.show = self.enterContext(mock.patch.object(forms, "_show"))

    def test_select_and_text_name_what_was_needed(self) -> None:
        for name, call in [("select", lambda: forms.select("Which account?", ["a"])),
                           ("text", lambda: forms.text("Which account?")),
                           ("ask", lambda: forms.ask("Which account?")),
                           ("choose", lambda: forms.choose("Which account?", ["a"]))]:
            with self.subTest(name), self.assertRaises(UsageError) as caught:
                call()
            self.assertIn("'Which account?' needs an answer", str(caught.exception))

    def test_confirm_and_defaults_proceed_unasked(self) -> None:
        self.assertTrue(forms.confirm("Delete it?"))
        self.assertFalse(forms.confirm("Open a browser?", default=False))
        self.assertEqual(forms.ask("Name", default="example_user"), "example_user")
        self.assertEqual(forms.choose("Pick", ["a", "b"], default="b"), "b")
        self.show.assert_not_called()

    def test_frame_computes_reflowed_rows_on_width_shrink_and_app_has_on_resize(self) -> None:
        import os

        from rich.text import Text

        out = io.StringIO()
        with mock.patch("shutil.get_terminal_size",
                        return_value=os.terminal_size((40, 20))):
            drawn = forms._frame(out, Text("x" * 50 + "\n" + "y" * 10), 0)
        out.truncate(0)
        out.seek(0)
        # Shrink width to 20: 50-char line reflows to 3 rows + 1 row for 10-char line -> up=3.
        with mock.patch("shutil.get_terminal_size",
                        return_value=os.terminal_size((20, 20))):
            forms._frame(out, None, drawn)
        self.assertTrue(out.getvalue().startswith("\x1b[3A\r\x1b[J"), repr(out.getvalue()))

        # Textual Inline View defines on_resize.
        class DummyOwner:
            def view(self, fancy):
                return ""

        app = forms._app(DummyOwner())
        view_cls = next(iter(app.compose())).__class__
        self.assertTrue(hasattr(view_cls, "on_resize"))


if __name__ == "__main__":
    unittest.main()
