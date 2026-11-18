"""Tests for `flnd.ui`: the log lines, progress, groups and scoping of `flnd.ui.log`; the theme's
tokens and glyphs; `flnd.ui.out`'s lines, folded warnings, errors, tables and help."""

import contextvars
import io
import os
import subprocess
import sys
import threading
import unittest
from unittest import mock

from rich.console import Console as RichConsole

import flnd
from flnd import cli
from flnd.engine import run as _run
from flnd.errors import UsageError
from flnd.ui import out, theme
from flnd.ui.log import ui
from flnkit import clock as _time


class UICase(unittest.TestCase):
    """Base test case capturing `ui` output in a `StringIO` buffer with a fake clock."""

    def setUp(self) -> None:
        self.buf = io.StringIO()
        self.fake_clock = _time.Fake(start=100.0)
        self.enterContext(_time.use(self.fake_clock))
        self.enterContext(ui.scope(color="none", dest=self.buf, width=80, level="info"))

    def output(self) -> str:
        """Return captured output."""
        return self.buf.getvalue()


class LoggingTest(UICase):
    """Tests for log levels, formatting, nested specs, and escape sanitization."""

    def test_raw_console(self) -> None:
        self.assertIsInstance(ui.raw, RichConsole)

    def test_levels_filter(self) -> None:
        ui.debug("hidden")
        ui.info("shown")
        ui.ok("good")
        ui.warn("careful")
        ui.error("bad")
        out = self.output()
        self.assertNotIn("hidden", out)
        self.assertIn("shown", out)
        self.assertIn("good", out)
        self.assertIn("careful", out)
        self.assertIn("bad", out)

    def test_quiet_shows_only_error(self) -> None:
        with ui.scope(level="quiet"):
            ui.debug("d")
            ui.info("i")
            ui.ok("o")
            ui.warn("w")
            ui.error("e")
        out = self.output()
        self.assertNotIn("w", out)
        self.assertIn("e", out)

    def test_invalid_level_raises(self) -> None:
        with self.assertRaises(ValueError):
            ui.level = "verbose"  # type: ignore[assignment]

    def test_unused_field_raises_even_when_filtered(self) -> None:
        with ui.scope(level="error"):
            with self.assertRaises(TypeError):
                ui.debug("hello {name}", typo="x")

    def test_nested_format_specs(self) -> None:
        ui.info("{n:{w}d}", n=5, w=4)
        self.assertIn("   5", self.output())

    def test_sanitizes_csi_osc_and_control_codes(self) -> None:
        evil = "\x1b[31mred\x1b[0m\x1b]8;;https://example.com\x07link\x1b]8;;\x1b\\\x07"
        ui.info("got {val}", val=evil)
        out = self.output()
        self.assertNotIn("\x1b", out)
        self.assertNotIn("\x07", out)
        self.assertIn("redlink", out)

    def test_markup_in_fields_is_literal(self) -> None:
        ui.info("msg {x}", x="[bold red]literal[/]")
        self.assertIn("[bold red]literal[/]", self.output())


class ScopeTest(UICase):
    """Tests for `scope`."""

    def test_scope_color_and_restore(self) -> None:
        buf2 = io.StringIO()
        with ui.scope(color="256", dest=buf2, width=100, level="debug"):
            ui.debug("colored")
        self.assertIn("\x1b[", buf2.getvalue())
        self.assertEqual(ui.level, "info")

    def test_scope_dest_flushed_immediately(self) -> None:
        flushed: list[str] = []

        class FlushStream(io.StringIO):
            def flush(self) -> None:
                flushed.append(self.getvalue())
                super().flush()

        stream = FlushStream()
        with ui.scope(dest=stream, level="info"):
            self.assertEqual(flushed, [])
            ui.info("message one")
            self.assertGreaterEqual(len(flushed), 1)
            self.assertIn("message one", flushed[-1])
            count = len(flushed)
            ui.warn("message two")
            self.assertGreater(len(flushed), count)
            self.assertIn("message two", flushed[-1])


# ---------------------------------------------------------------------------------------------
# The theme and `flnd.ui.out`
# ---------------------------------------------------------------------------------------------

class Terminal(io.StringIO):
    """A UTF-8 text buffer that says it is a terminal."""

    encoding = "utf-8"

    def isatty(self) -> bool:
        return True


class Pipe(io.StringIO):
    """A UTF-8 text buffer that is not a terminal, as a pipe to another program is."""

    encoding = "utf-8"


class Ascii(io.StringIO):
    """A terminal whose encoding isn't UTF-8 (cp1252, as on an old Windows console)."""

    encoding = "cp1252"

    def isatty(self) -> bool:
        return True


class OutCase(unittest.TestCase):
    """Standard output and error replaced by `stdout` and `stderr`, under a fake clock."""

    def setUp(self) -> None:
        self.clock = self.enterContext(_time.use(_time.Fake(start=100.0)))
        self.enterContext(mock.patch.dict(os.environ, {"COLUMNS": "80", "TERM": "xterm-256color",
                                                       "COLORTERM": "truecolor"}))
        os.environ.pop("NO_COLOR", None)
        self.streams(Pipe(), Pipe())
        self.addCleanup(out.close)

    def streams(self, stdout: io.StringIO, stderr: io.StringIO) -> None:
        self.stdout, self.stderr = stdout, stderr
        self.enterContext(mock.patch("sys.stdout", stdout))
        self.enterContext(mock.patch("sys.stderr", stderr))


class ThemeTest(unittest.TestCase):
    def test_the_tokens(self) -> None:
        self.assertEqual(theme.TOKENS, {"text": "#D0D0D0", "dim": "#767676", "focus": "#7D84D6",
                                        "good": "#02BF87", "bad": "#ED567A", "warn": "#E8C468"})
        self.assertEqual(theme.style("bold bad"), "bold #ED567A")
        self.assertEqual(theme.style("dim"), "#767676")

    def test_the_glyphs_and_their_ascii(self) -> None:
        names = ("bullet", "cursor", "done", "failed", "waiting", "followed", "unfollowed",
                 "default", "bar")
        self.assertEqual("".join(theme.glyph(name, True) for name in names), "•❯✓✗·●○★┃")
        self.assertEqual("".join(theme.glyph(name, False) for name in names), "*>vx-o +|")
        self.assertEqual(theme.SPINNER, (tuple("⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"), tuple("|/-\\")))
        self.assertEqual(theme.spinner(0.25, True), "⠹")
        self.assertEqual(theme.spinner(0.25, False), "-")
        self.assertEqual(theme.PADDING, (0, 1))

    def test_no_colour_with_no_color_a_dumb_terminal_or_a_pipe(self) -> None:
        with mock.patch.dict(os.environ, {"TERM": "xterm-256color"}):
            os.environ.pop("NO_COLOR", None)
            self.assertTrue(theme.color(Terminal()))
            self.assertFalse(theme.color(Pipe()))
            with mock.patch.dict(os.environ, {"NO_COLOR": "1"}):
                self.assertFalse(theme.color(Terminal()))
            with mock.patch.dict(os.environ, {"TERM": "dumb"}):
                self.assertFalse(theme.color(Terminal()))

    def test_unicode_only_on_utf_8(self) -> None:
        with mock.patch.dict(os.environ, {"LANG": "en_US.UTF-8", "LC_ALL": ""}):
            self.assertTrue(theme.unicode(Terminal()))
            self.assertFalse(theme.unicode(Ascii()))


class LineTest(OutCase):
    def test_plain_on_a_pipe(self) -> None:
        out.line(("done: ", "bold good"), "3 files saved", mark="ok")
        out.table([[("ins:example_user", "bold focus"), "followed"], ["tik:example_handle", "-"]],
                  ("target", "follow"))
        self.assertEqual(self.stdout.getvalue(), "✓ done: 3 files saved\n"
                                                 "target              follow\n"
                                                 "ins:example_user    followed\n"
                                                 "tik:example_handle  -\n")

    def test_colour_on_a_terminal_and_none_with_no_color(self) -> None:
        self.streams(Terminal(), Terminal())
        out.line(("done", "good"))
        self.assertIn("\x1b[", self.stdout.getvalue())
        self.streams(Terminal(), Terminal())
        with mock.patch.dict(os.environ, {"NO_COLOR": "1"}):
            out.line(("done", "good"))
        self.assertEqual(self.stdout.getvalue(), "done\n")

    def test_ascii_glyphs_off_utf_8(self) -> None:
        self.streams(Ascii(), Ascii())
        with mock.patch.dict(os.environ, {"NO_COLOR": "1"}):
            out.line("saved", mark="ok")
            out.line("failed", mark="error", err=True)
            out.warn("held")
            out.error(UsageError("ins:example_user needs a login", hint="flnd login ins"))
        self.assertEqual(self.stdout.getvalue(), "v saved\n")
        self.assertEqual(self.stderr.getvalue(), "x failed\n! held\n"
                                                 "x ins:example_user needs a login\n"
                                                 "  hint  flnd login ins\n")

    def test_text_from_a_site_cannot_drive_the_terminal(self) -> None:
        out.line("a\x1b[2Jb\x07c", ("\x1b]8;;https://example.com\x07d", "dim"))
        self.assertEqual(self.stdout.getvalue(), "abcd\n")

    def test_an_error_then_its_hint(self) -> None:
        out.error(UsageError("give at least one target", hint="flnd download ins:example_user"))
        self.assertEqual(self.stderr.getvalue().splitlines(),
                         ["✗ give at least one target", "  hint  flnd download ins:example_user"])
        self.assertEqual(self.stdout.getvalue(), "")


# A signal raised while the main thread holds the fold's lock in `see`; the handler warns, as a
# second Ctrl-C does (`warn`), or drains every fold, as a third one's `out.close()` does.
_FOLD_PROBE = """
import signal, sys
from flnd.ui import out
from flnd.ui.log import ui
class Seen(dict):
    def get(self, *args):
        if not getattr(Seen, "fired", False):
            Seen.fired = True
            signal.raise_signal(signal.SIGINT)
        return super().get(*args)
if sys.argv[1] == "warn":
    signal.signal(signal.SIGINT, lambda *_: ui.warn("press Ctrl-C again to quit now"))
    ui._folds._seen = Seen()
    ui.warn("429 from api.example.com")
else:
    signal.signal(signal.SIGINT, lambda *_: out.close())
    out._folds._seen = Seen()
    out.warn("429 from api.example.com")
"""


def _child(code: str, *args: str) -> subprocess.CompletedProcess[str]:
    """Run `code` in a fresh Python, its output in UTF-8; it must end within 2 s."""
    env = {**os.environ, "PYTHONIOENCODING": "utf-8"}
    return subprocess.run([sys.executable, "-c", code, *args], capture_output=True, text=True,
                          encoding="utf-8", timeout=2, env=env)


class FoldTest(OutCase):
    def test_the_same_warning_within_10_s_folds(self) -> None:
        for _ in range(3):
            out.warn("429 from api.example.com")
            self.clock.advance(2)
        out.warn("another")
        self.assertEqual(self.stderr.getvalue(), "! 429 from api.example.com\n! another\n")
        self.clock.advance(10)
        out.warn("429 from api.example.com")
        self.assertEqual(self.stderr.getvalue().splitlines()[2:],
                         ["! 429 from api.example.com (×3)", "! 429 from api.example.com"])

    def test_a_window_that_passed_shows_its_count_at_the_next_warning(self) -> None:
        out.warn("held 30 s: 429 from api.example.com")
        self.clock.advance(2)
        out.warn("held 30 s: 429 from api.example.com")
        self.clock.advance(9)
        out.warn("cookie sid was rejected")
        self.assertEqual(self.stderr.getvalue().splitlines(),
                         ["! held 30 s: 429 from api.example.com",
                          "! held 30 s: 429 from api.example.com (×2)",
                          "! cookie sid was rejected"])

    def test_close_prints_what_is_still_folded_counting_the_first(self) -> None:
        out.warn("held 30 s")
        out.warn("held 30 s")
        out.close()
        self.assertEqual(self.stderr.getvalue(), "! held 30 s\n! held 30 s (×2)\n")

    def test_a_log_warning_folds_until_its_scope_ends(self) -> None:
        buf = io.StringIO()
        with ui.scope(color="none", dest=buf, width=80, level="info"):
            for _ in range(4):
                ui.warn("cookie {name} was rejected", name="sid")
            self.assertEqual(buf.getvalue(), "! cookie sid was rejected\n")
        self.assertEqual(buf.getvalue(), "! cookie sid was rejected\n"
                                         "! cookie sid was rejected (x4)\n")

    def test_a_held_repeat_prints_before_the_end_of_a_run(self) -> None:
        both = Pipe()
        self.streams(both, both)

        def events():
            for post in ("1", "2"):
                ui.warn("429 from api.example.com")
                yield flnd.Skipped(flnd.Target("ins:example_user", kind="user"), "posts", post)

        run = mock.MagicMock()
        run.__iter__.return_value = events()
        run.report = flnd.Report(skipped=2)
        run.state = flnd.State()
        args = mock.Mock(json=False, verbose=False, command=None)
        self.assertEqual(cli._watch(mock.Mock(home=None), run, args), 0)
        self.assertEqual(both.getvalue().splitlines(),
                         ["! 429 from api.example.com", "! 429 from api.example.com (×2)",
                          "  ✓ done in 0ms · 0 targets · 0 saved · 2 already saved"
                          " · 0 failed"])

    def test_a_signal_handler_never_waits_on_the_fold(self) -> None:
        for path in ("warn", "drain"):
            with self.subTest(path):
                try:
                    done = _child(_FOLD_PROBE, path)
                except subprocess.TimeoutExpired:
                    self.fail("the handler waited forever on the fold's lock")
                self.assertEqual(done.returncode, 0, done.stderr)
                self.assertIn("! 429 from api.example.com", done.stderr)

    def switch_often(self) -> None:
        """Make threads take turns every microsecond, so a race in the fold shows."""
        previous = sys.getswitchinterval()
        sys.setswitchinterval(1e-6)
        self.addCleanup(sys.setswitchinterval, previous)

    def test_two_threads_fold_into_one_line_and_one_count(self) -> None:
        self.switch_often()
        folds = out.Folds()
        shown: list[list[tuple[str, int]]] = []

        def warn() -> None:
            for _ in range(5000):
                shown.append(folds.see("429 from api.example.com"))

        threads = [threading.Thread(target=warn) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual([line for lines in shown for line in lines],
                         [("429 from api.example.com", 1)])
        self.assertEqual(folds.drain(), [("429 from api.example.com", 10000)])

    def test_a_drain_racing_a_warning_never_loses_a_count(self) -> None:
        # Every window starts with its first line and ends in a drain: N warnings in all are
        # the drained counts, plus 1 for each window that ended with no repeat.
        self.switch_often()
        folds = out.Folds()
        firsts: list[int] = []
        drained: list[int] = []
        done = threading.Event()

        def warn() -> None:
            for _ in range(20000):
                firsts.extend(times for _, times in folds.see("held 30 s"))
            done.set()

        def drain() -> None:
            while not done.is_set():
                drained.extend(times for _, times in folds.drain())

        threads = [threading.Thread(target=warn), threading.Thread(target=drain)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        drained.extend(times for _, times in folds.drain())
        self.assertEqual(set(firsts), {1})
        self.assertEqual(sum(drained) + len(firsts) - len(drained), 20000)

    def test_a_drain_while_another_thread_warns_returns_the_held_line(self) -> None:
        self.switch_often()
        stop = threading.Event()
        folds = out.Folds()

        def warn() -> None:
            while not stop.is_set():
                folds.see("cookie sid was rejected")

        # On the test's fake clock too: a window can't pass between two calls.
        other = threading.Thread(target=contextvars.copy_context().run, args=(warn,))
        other.start()
        lost = 0
        try:
            for _ in range(3000):
                folds.see("held 30 s")
                folds.see("held 30 s")
                lost += ("held 30 s", 2) not in folds.drain()
        finally:
            stop.set()
            other.join()
        self.assertEqual(lost, 0)

    def test_a_hard_exit_takes_no_lock_another_thread_holds(self) -> None:
        held, release = threading.Event(), threading.Event()

        def hold() -> None:
            with ui._lock:
                held.set()
                release.wait(5)

        holder = threading.Thread(target=hold)
        holder.start()
        self.assertTrue(held.wait(2))
        ended: list[BaseException] = []

        def quit_now() -> None:
            try:
                _run.quit_now()
            except BaseException as error:  # `os._exit`, replaced: the test reads it
                ended.append(error)

        # Replaced until the test ends, so a quit_now still waiting can't end the process.
        self.enterContext(mock.patch("os._exit", side_effect=SystemExit(130)))
        quitter = threading.Thread(target=quit_now)
        try:
            quitter.start()
            quitter.join(2)
            self.assertFalse(quitter.is_alive(), "quit_now waited on ui's lock")
        finally:
            release.set()
            holder.join()
            quitter.join()
        self.assertEqual([type(error) for error in ended], [SystemExit])

    def test_warnings_outside_any_scope_keep_their_count(self) -> None:
        warn = ("from flnd.ui.log import ui\n"
                "ui.warn('cookie sid was rejected')\nui.warn('cookie sid was rejected')\n")
        closed = warn + "from flnd.ui import out\nout.close()\nimport os\nos._exit(0)\n"
        for name, code in (("out.close", closed), ("exit", warn)):
            with self.subTest(name):
                done = _child(code)
                self.assertEqual(done.stderr.splitlines(),
                                 ["! cookie sid was rejected",
                                  "! cookie sid was rejected (×2)"], done.stderr)


class HelpTest(OutCase):
    def help(self) -> str:
        with self.assertRaises(SystemExit):
            cli.parser().parse_args(["-h"])
        return self.stdout.getvalue()

    def test_the_help_is_plain_on_a_pipe(self) -> None:
        text = self.help()
        self.assertNotIn("\x1b", text)
        self.assertEqual(text.splitlines()[0],
                         "  flnd 0.1.0 · download whole accounts from the web")
        self.assertIn("  download [target…]     Download what the targets have now\n", text)
        self.assertTrue(all(line == line.rstrip() for line in text.splitlines()))

    def test_the_help_draws_in_the_theme_on_a_terminal(self) -> None:
        self.streams(Terminal(), Pipe())
        text = self.help()
        # The commands are in `focus`, #7D84D6.
        self.assertIn("\x1b[38;2;125;132;214mdownload", text)


if __name__ == "__main__":
    unittest.main()
