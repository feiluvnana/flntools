"""The forms of `flnd.ui.forms`: each field's keys, Ctrl-C, and the answers without a terminal.

The fields are fed keys directly, which is fast; the rest drive `_plain` with fake keys and
a fake terminal size: keys in, the answer out, Ctrl-C as `Stopped`. A pseudo-terminal checks
the real raw mode, and a fake `msvcrt` the Windows keys.
"""

from __future__ import annotations

import io
import os
import select
import sys
import threading
import unittest
from contextlib import contextmanager
from unittest import mock

from flnkit.errors import Stopped, UsageError
from flnkit.ui import forms
from flnkit.ui.forms import PENDING, Option


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


@contextmanager
def drive(*runs: tuple[str, ...]):
    """Answer each question that opens next with the next of `runs`: key names (`enter`, `down`,
    `ctrl+c`, or one character), given to `_plain` through its raw-mode reader."""
    pending = [list(run) for run in runs]

    @contextmanager
    def raw():
        keys = pending.pop(0)
        yield lambda: [(key, key if len(key) == 1 else None) for key in [keys.pop(0)]]

    with mock.patch.object(forms, "_raw", raw), \
            mock.patch.object(forms, "_terminal_size", return_value=(100, 24)):
        yield


def drawn(renderable: object, width: int = 100) -> str:
    """`renderable` as plain text, each line's trailing spaces cut."""
    from rich.console import Console

    console = Console(width=width, color_system=None, file=io.StringIO())
    with console.capture() as captured:
        console.print(renderable)
    return "\n".join(line.rstrip() for line in captured.get().splitlines())


HOW = [Option("With your phone number", "phone", "a code comes by message"),
       Option("With a QR code", "qr", "scan it in the app"),
       Option("From a cookie file", "file", "a cookies.txt you exported")]


class FieldTest(unittest.TestCase):
    def test_each_field_answers_its_keys(self) -> None:
        cases = [
            ("first", forms._Select("How?", forms._options(HOW), 0), ["enter"], "phone"),
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
            "  ┃ ❯ With your phone number   a code comes by message",
            "  ┃   With a QR code           scan it in the app",
            "  ┃   From a cookie file       a cookies.txt you exported",
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

    def test_keys_come_in_and_the_answer_goes_out(self) -> None:
        with drive(("down", "enter"), ("x", "enter"), ("n",)):
            self.assertEqual(forms.select("How?", HOW), "qr")
            self.assertEqual(forms.text("Account"), "x")
            self.assertFalse(forms.confirm("Go on?"))
        # Each answer stays on the terminal, after its frame was erased and the cursor came back.
        shown = self.stdout.getvalue()
        for answer in ("  | How?\n  | With a QR code\n", "  | Account\n  | > x\n",
                       "  | Go on?\n  | No\n"):
            self.assertIn("\x1b[?25h" + answer, shown)

    def test_a_secret_stays_masked_on_the_terminal(self) -> None:
        with drive(("a", "b", "enter")):
            self.assertEqual(forms.text("Password", secret=True), "ab")
        self.assertTrue(self.stdout.getvalue().endswith("  | Password\n  | > **\n"))

    def test_ctrl_c_and_esc_leave_nothing_behind(self) -> None:
        with drive(("ctrl+c",), ("escape",), ("escape",)):
            with self.assertRaises(Stopped):
                forms.select("How?", HOW)
            self.assertIsNone(forms.text("Account"))
            self.assertFalse(forms.confirm("Go on?"))
        self.assertTrue(self.stdout.getvalue().endswith("\x1b[?25h"))
        self.assertNotIn("\n", self.stdout.getvalue().rsplit("\x1b[J", 1)[-1])

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
                mock.patch.object(forms, "_plain", lambda owner: events.append("asked")):
            forms.confirm("Go on?")
        self.assertEqual(events, ["hold", "asked", "release"])


class WindowsKeysTest(unittest.TestCase):
    """The Windows console's keys, read from `msvcrt.getwch()`, come out as the terminal's."""

    def read(self, *chars: str) -> list[tuple[str, str | None]]:
        fake = mock.Mock(getwch=mock.Mock(side_effect=list(chars)))
        with mock.patch.dict(sys.modules, {"msvcrt": fake}):
            return forms._wkeys()

    def test_keys_are_named_as_on_a_terminal(self) -> None:
        cases = [(("\r",), "enter"), (("\x08",), "backspace"), (("\x03",), "ctrl+c"),
                 (("\x1b",), "escape"), (("\x00", "H"), "up"), (("\xe0", "P"), "down"),
                 (("\xe0", "K"), "left"), (("\xe0", "M"), "right"), (("\xe0", "G"), "home"),
                 (("\xe0", "O"), "end"), (("\x00", "I"), "pageup"), (("\xe0", "Q"), "pagedown")]
        for chars, key in cases:
            with self.subTest(key):
                self.assertEqual(self.read(*chars), [(key, None)])
        self.assertEqual(self.read("a"), [("a", "a")])
        self.assertEqual(self.read("\xe0", "S"), [])

    def test_raw_gives_the_windows_reader(self) -> None:
        with mock.patch.object(sys, "platform", "win32"), forms._raw() as read:
            self.assertIs(read, forms._wkeys)


@unittest.skipIf(os.name == "nt", "no pseudo-terminal on Windows")
class WorkerThreadTest(unittest.TestCase):
    """The real raw mode, on a pseudo-terminal, from a worker thread too (the browser's question)."""

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
        self.plain = self.enterContext(mock.patch.object(forms, "_plain"))

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
        self.plain.assert_not_called()

    def test_frame_computes_reflowed_rows_on_width_shrink(self) -> None:
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


if __name__ == "__main__":
    unittest.main()
