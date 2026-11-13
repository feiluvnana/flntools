"""shared.log: levels, progress rows, the live area's frames, and the drawing thread.

Lines and rows go to a fake `theme.screen` over StringIO. The clock is faked too (`LogCase.tick`
moves it and draws one frame), so a row's 0.5 s and a frame's 0.1 s cost no time, and the live
area's own thread is off except in `DrawingThreadTest`. `RealTerminalCase` runs the live area
in a child process on a real pty and reads the raw bytes, which the fake screen can't show.
"""

import os
import re
import struct
import sys
import tempfile
import threading
import time
import unittest
from contextlib import ExitStack
from io import StringIO
from pathlib import Path
from unittest.mock import patch

try:
    import fcntl
    import pty
    import select
    import termios

    HAS_PTY = True
except ImportError:
    HAS_PTY = False

from shared import fmt, log, prompt, style, theme
from shared._log import live_area
from shared._theme import Screen


def visible_screen(output: str) -> list[str]:
    """Replay what a terminal shows after output: text, \\r, \\n, cursor up and down, and the clears."""
    lines, row, column = [""], 0, 0
    for match in re.finditer(r"\x1b\[(\d*)([AB])|\x1b\[\?\d+[hl]|\x1b\[J|\x1b\[K|\r|\n|[^\x1b\r\n]+", output):
        token = match.group()
        if token == "\r":
            column = 0
        elif token == "\n":
            row, column = row + 1, 0
            if row == len(lines):
                lines.append("")
        elif match[2] == "A":
            row -= int(match[1] or 1)
        elif match[2] == "B":
            row += int(match[1] or 1)
            while row >= len(lines):
                lines.append("")
        elif token.startswith("\x1b[?"):
            continue
        elif token in ("\x1b[J", "\x1b[K"):
            lines[row] = lines[row][:column]
            if token == "\x1b[J":
                del lines[row + 1:]
        else:
            lines[row] = lines[row][:column] + token + lines[row][column + len(token):]
            column += len(token)
    return [line for line in lines if line]


class LogCase(unittest.TestCase):
    """Log lines and rows on a fake screen over StringIO, with a fake clock and no drawing thread.

    A test on a terminal moves `self.now` with `tick` and has the live area draw one frame, so
    rows that need 0.5 s to appear and frames 0.1 s apart cost no time. `DrawingThreadTest` is
    the one that runs the real thread on the real clock.
    """

    tty = False
    width = 80
    fake_clock = True

    def setUp(self):
        self.screen = Screen(dest=StringIO(), inp=StringIO(""), width=self.width, tty=self.tty, interactive=True)
        self.scope = theme.scope(screen=self.screen, color="none", unicode=True)
        self.scope.__enter__()
        self.previous_level = log.level
        self.now = 100.0
        if self.fake_clock:
            clock = patch("shared._log.clock", lambda: self.now)
            clock.start()
            self.addCleanup(clock.stop)
        self.area = live_area(self.screen)
        self.area.threaded = False

    def tick(self, seconds: float = 0.0) -> None:
        """Move the fake clock on and draw one frame."""
        self.now += seconds
        self.area.tick()

    def tearDown(self):
        log.level = self.previous_level
        self.scope.__exit__(None, None, None)

    def output(self) -> str:
        return self.screen.dest.getvalue()

    def lines(self) -> list[str]:
        return self.output().splitlines()


class LevelTest(LogCase):
    def test_each_level(self):
        log.level = "debug"
        log.debug("raw {n}", n=3)
        log.info("saved {path}", path="/tmp/a")
        log.ok("done")
        log.warn("retry {n}", n=2)
        log.error("failed: {err}", err="boom")
        self.assertEqual(self.lines(), ["· raw 3", "• saved /tmp/a", "✓ done", "! retry 2", "✗ failed: boom"])

    def test_thresholds(self):
        shown = {
            "debug": ["debug", "info", "ok", "warn", "error"],
            "info": ["info", "ok", "warn", "error"],
            "ok": ["ok", "warn", "error"],
            "warn": ["warn", "error"],
            "error": ["error"],
            "quiet": ["error"],
        }
        for level, expected in shown.items():
            self.screen.dest.seek(0)
            self.screen.dest.truncate()
            log.level = level
            for name in ("debug", "info", "ok", "warn", "error"):
                getattr(log, name)(name)
            self.assertEqual([line[2:] for line in self.lines()], expected, level)

    def test_unknown_level(self):
        with self.assertRaises(ValueError):
            log.level = "trace"

    def test_fields(self):
        log.info("saved {path}", path="a")
        log.info("{} and {}", 1, 2)
        log.info("{0[x]} {item.real}", {"x": 1}, item=2)
        self.assertEqual(self.lines(), ["• saved a", "• 1 and 2", "• 1 2"])

    def test_an_unused_keyword_raises(self):
        with self.assertRaises(TypeError):
            log.ok("finished", folders=3)
        with self.assertRaises(TypeError):
            log.info("saved {path}", path="a", size=2)
        with self.assertRaises(TypeError):
            log.info("plain", theme=theme)
        self.assertEqual(self.output(), "")

    def test_braces_without_arguments(self):
        log.info("saved {path}")
        log.info("{}")
        self.assertEqual(self.lines(), ["• saved {path}", "• {}"])

    def test_bad_format_raises(self):
        with self.assertRaises(KeyError):
            log.info("{missing}", "positional")

    def test_empty_message(self):
        log.ok()
        self.assertEqual(self.lines(), ["✓"])

    def test_styled_field_is_kept(self):
        with theme.scope(color="16m"):
            log.error("{msg}", msg=style.red.bold("disk full"))
        # The message's own red bold wins; the line's color is not started again after a final reset.
        self.assertEqual(self.output(), "\x1b[38;2;237;86;122m✗ \x1b[1;31mdisk full\x1b[0m\n")

    def test_the_whole_line_takes_its_levels_color(self):
        with theme.scope(color="16m"):
            log.ok("done")
            log.info("note")
            log.warn("careful")
        self.assertEqual(self.lines(), ["\x1b[38;2;2;191;135m✓ done\x1b[0m", "\x1b[36m• note\x1b[0m",
                                       "\x1b[33m! careful\x1b[0m"])

    def test_a_styled_part_keeps_its_style_and_the_line_goes_back_to_its_color(self):
        with theme.scope(color="16m"):
            log.warn("{x} failed", x=style.bold("a"))
        self.assertEqual(self.output(), "\x1b[33m! \x1b[1ma\x1b[0m\x1b[33m failed\x1b[0m\n")
        log.warn("{x} failed", x=style.bold("a"))
        self.assertEqual(self.output().splitlines()[-1], "! a failed")

    def test_a_palette_change_recolors_the_whole_line(self):
        with theme.scope(color="16m"):
            theme.palette.warn = "#010203"
            try:
                log.warn("careful")
            finally:
                del theme.palette._assigned["warn"]
        self.assertEqual(self.output(), "\x1b[38;2;1;2;3m! careful\x1b[0m\n")

    def test_progress_rows_keep_a_colored_glyph_only(self):
        with theme.scope(color="16m"), log.progress("a", total=2) as bar:
            bar.advance()
        self.assertEqual(self.lines(), ["\x1b[38;2;2;191;135m✓\x1b[0m " + "━" * 10 + "─" * 10 + " a " + "2"])


class PipeProgressTest(LogCase):
    def test_clean_exit(self):
        with log.progress("clip.mp4", total=3 * 1024 * 1024, show=fmt.size) as bar:
            bar.advance(1024 * 1024)
            bar.count = 1258291
            bar.caption = "part 3"
            self.assertEqual(self.output(), "")
        self.assertEqual(self.lines(), ["✓ " + "━" * 8 + "─" * 12 + " clip.mp4 " + "3.0 MB"])
        self.assertTrue(bar.finished)
        self.assertEqual((bar.label, bar.count, bar.caption), ("clip.mp4", 1258291, ""))

    def test_exception(self):
        with self.assertRaises(KeyboardInterrupt):
            with log.progress("clip.mp4", total=10) as bar:
                bar.advance(4)
                bar.caption = "part 3"
                raise KeyboardInterrupt
        self.assertEqual(self.lines(), ["✗ " + "━" * 8 + "─" * 12 + " clip.mp4 " + "4/10" + " part 3"])

    def test_count_and_total_are_set(self):
        with log.progress("upload") as bar:
            bar.total = 10
            bar.count = 5
            self.assertEqual((bar.count, bar.total), (5, 10))
            bar.count = 7
            self.assertEqual(bar.total, 10)
        self.assertEqual(self.lines(), ["✓ " + "━" * 14 + "─" * 6 + " upload " + "10"])

    def test_return_and_break_are_clean(self):
        for _ in range(2):
            with log.progress("loop") as bar:
                bar.advance()
                break
        self.assertEqual(self.lines(), ["✓ " + "━" * 20 + " loop 1"])

    def test_spinner_and_late_total(self):
        with log.progress("scroll posts") as bar:
            for _ in range(12):
                bar.advance()
        with log.progress("late") as late:
            late.total = 4
            late.count = -5
        self.assertEqual(self.lines(), ["✓ " + "━" * 20 + " scroll posts 12", "✓ " + "─" * 20 + " late " + "4"])

    def test_nested_bars(self):
        with log.progress("run") as outer:
            with log.progress("file", total=2) as inner:
                inner.advance(2)
            outer.advance()
        self.assertEqual(self.lines(), ["✓ " + "━" * 20 + " file " + "2", "✓ " + "━" * 20 + " run 1"])

    def test_group_with_finish_and_fail(self):
        with self.assertRaises(RuntimeError):
            with log.group("download") as jobs:
                ins = jobs.add("ins:ada", total=12)
                twt = jobs.add("twt:bob")
                fbk = jobs.add("fbk:cy")
                ins.advance()
                ins.caption = "reel 3"
                twt.finish()
                fbk.fail()
                self.assertEqual(jobs._bars, [ins, twt, fbk])
                raise RuntimeError
        self.assertEqual(self.lines(), [
            "✗ download",
            "✗ " + "━" * 2 + "─" * 18 + " ins:ada " + "1/12" + " reel 3",
            "✓ " + "━" * 20 + " twt:bob",
            "✗ " + "─" * 20 + " fbk:cy",
        ])

    def test_group_clean_exit(self):
        with log.group("download") as jobs:
            jobs.add("a", total=1).advance()
        self.assertEqual(self.lines(), ["✓ download", "✓ " + "━" * 20 + " a " + "1"])

    def test_log_line_while_bar_open(self):
        with log.progress("run", total=2) as bar:
            log.info("hello")
            bar.advance()
        self.assertEqual(self.lines(), ["• hello", "✓ " + "━" * 10 + "─" * 10 + " run " + "2"])


class TtyProgressTest(LogCase):
    tty = True
    width = 30

    def test_redraws_in_place(self):
        # A row's count and label start after 22 columns, so the 30-column screen would cut them off.
        self.screen._width = 60
        with log.progress("c", total=4) as bar:
            bar.advance(1)
            self.tick(0.6)
            self.assertEqual(visible_screen(self.output()), ["• " + "━" * 5 + "─" * 15 + " c " + "1/4"])
            bar.advance(1)
            self.tick(0.1)
            self.assertEqual(visible_screen(self.output()), ["• " + "━" * 10 + "─" * 10 + " c " + "2/4"])
        self.assertEqual(visible_screen(self.output()), ["✓ " + "━" * 10 + "─" * 10 + " c " + "4"])

    def test_exception_on_tty(self):
        self.screen._width = 60
        with self.assertRaises(ValueError), log.progress("clip", total=4) as bar:
            bar.caption = "x"
            raise ValueError
        self.assertEqual(visible_screen(self.output()), ["✗ " + "─" * 20 + " clip " + "0/4" + " x"])

    def test_log_line_above_open_bars(self):
        self.screen._width = 60
        with log.group("download") as jobs:
            jobs.add("a", total=2)
            with log.progress("inner", total=1):
                self.tick(0.6)
                log.warn("careful")
                self.tick(0.1)
                self.assertEqual(visible_screen(self.output()), [
                    "! careful", "• download", "• " + "─" * 20 + " a " + "0/2",
                    "• " + "─" * 20 + " inner " + "0/1",
                ])
        self.assertEqual(visible_screen(self.output()), [
            "! careful", "✓ " + "─" * 20 + " inner " + "1", "✓ download", "✓ " + "─" * 20 + " a " + "2",
        ])

    def test_live_area_capped_to_height(self):
        self.screen._height = 4
        self.screen._width = 60
        with log.group("download") as jobs:
            for name in "abcde":
                jobs.add(name, total=1)
            self.tick(0.6)
            rows = visible_screen(self.output())
            self.assertEqual(rows[:2], ["• download", "• " + "─" * 20 + " a " + "0/1"])
            self.assertEqual(rows[2:], ["… 4 more"])
            jobs.add("f", total=1)
            self.tick(0.1)
            self.assertEqual(len(visible_screen(self.output())), 3)

    def test_rows_cut_to_width(self):
        with log.progress("a-very-long-label-that-goes-past-the-width", total=10):
            self.tick(0.6)
            rows = visible_screen(self.output())
            self.assertEqual(len(rows), 1)
            self.assertEqual(len(rows[0]), 30)

    def test_a_spinner_row_changes_with_the_clock(self):
        with log.progress("wait"):
            self.tick(0.6)
            first = self.output()
            self.tick(0.25)
            self.assertNotEqual(self.output(), first)
        self.assertEqual(visible_screen(self.output()), ["✓ " + "━" * 20 + " wait"])


class FrameTest(LogCase):
    """What a frame writes: overwriting, synchronized output, the cursor, and how little."""

    tty = True
    # Wide enough for a row's cells, count and label, which start after 22 columns.
    width = 60

    def frames(self) -> list[str]:
        """Each synchronized frame's text, without its begin and end sequences."""
        return re.findall(r"\x1b\[\?2026h(.*?)\x1b\[\?2026l", self.output(), re.S)

    def test_every_frame_is_synchronized_and_rewrites_rows_without_erasing_first(self):
        with log.group("download") as rows:
            first, second = rows.add("a", total=4), rows.add("b", total=4)
            self.tick(0.6)
            first.advance(2)
            self.tick(0.1)
            second.advance(1)
            second.advance(1)
            self.tick(0.1)
            first.finish()
            self.tick(0.1)
        frames = self.frames()
        # Nothing but the cursor's hide and show is written outside a frame.
        outside = re.sub(r"\x1b\[\?2026h.*?\x1b\[\?2026l", "", self.output(), flags=re.S)
        self.assertEqual(outside, "\x1b[?25l\x1b[?25h")
        empty, half = "─" * 20, "━" * 10 + "─" * 10
        self.assertEqual(frames, [
            "\r• download\x1b[K\n• " + empty + " a " + "0/4" + "\x1b[K\n• " + empty + " b " + "0/4" + "\x1b[K\n",
            # A frame that changed one bar writes that bar's row alone: a cursor move, the row, its clear.
            "\x1b[2A\r• " + half + " a " + "2/4" + "\x1b[K\x1b[2B\r",
            "\x1b[1A\r• " + half + " b " + "2/4" + "\x1b[K\x1b[1B\r",
            "\x1b[2A\r✓ " + half + " a " + "4" + "\x1b[K\x1b[2B\r",
            # The last frame goes back up to write the lines the rows become, and clears nothing.
            "\r\x1b[3A✓ download\x1b[K\n✓ " + half + " a " + "4" + "\x1b[K\n✓ " + half + " b " + "4" + "\x1b[K\n",
        ])
        self.assertNotIn("\x1b[J", self.output())

    def test_the_area_is_cleared_below_only_when_it_shrinks(self):
        with log.group("download") as rows:
            rows.add("a", total=2)
            rows.add("b", total=2)
            self.tick(0.6)
            self.assertNotIn("\x1b[J", self.output())
            # A question takes the rows off the screen: three rows to none.
            with self.area.question():
                self.assertEqual(self.frames()[-1], "\x1b[3A\r\x1b[J")
            self.assertEqual(self.output().count("\x1b[J"), 1)

    def test_the_cursor_is_hidden_while_an_area_is_open(self):
        with log.progress("a", total=2):
            self.assertEqual(self.output(), "\x1b[?25l")
        self.assertTrue(self.output().endswith("\x1b[?25h"))
        self.assertEqual(self.output().count("\x1b[?25l"), 1)

    def test_the_cursor_is_shown_after_ctrl_c(self):
        with self.assertRaises(KeyboardInterrupt), log.progress("a", total=2):
            raise KeyboardInterrupt
        self.assertTrue(self.output().endswith("\x1b[?25h"))

    def test_the_cursor_stays_hidden_until_the_last_area_closes(self):
        with log.progress("outer"):
            with log.progress("inner"):
                pass
            self.assertEqual(self.output().count("\x1b[?25h"), 0)
        self.assertEqual(self.output().count("\x1b[?25h"), 1)

    def test_no_cursor_change_without_a_terminal(self):
        self.screen._tty = False
        with log.progress("a"):
            pass
        self.assertNotIn("\x1b[?25", self.output())

    def test_a_burst_of_fast_files_draws_no_rows(self):
        with log.group("download") as rows:
            for number in range(50):
                row = rows.add(f"file{number}.jpg", total=1)
                row.advance()
                row.finish()
                self.tick(0.002)
            self.assertLessEqual(self.area.frames, 2)
            self.assertNotIn("file", self.output())
        self.assertEqual(len(visible_screen(self.output())), 51)

    def test_a_row_appears_once_it_has_run_half_a_second(self):
        with log.group("download") as rows:
            rows.add("slow.mp4", total=2)
            self.tick(0.4)
            self.assertNotIn("slow", self.output())
            self.tick(0.2)
            self.assertIn("slow.mp4", self.output())

    def test_a_frame_with_nothing_new_writes_nothing(self):
        with log.progress("c", total=1_000_000, show=lambda n: f"{int(n) // 1000}k") as bar:
            bar.advance(1)
            self.tick(0.6)
            written = len(self.output())
            for _ in range(20):
                bar.advance(1)
                self.tick(0.1)
            self.assertEqual(len(self.output()), written)
            bar.advance(1000)
            self.tick(0.1)
            self.assertGreater(len(self.output()), written)

    def test_four_busy_bars_cost_at_most_ten_frames_a_second(self):
        with log.group("download") as rows:
            bars = [rows.add(f"file{number}", total=40_000) for number in range(4)]
            for step in range(10_000):
                for bar in bars:
                    bar.advance()
                if step % 100 == 99:
                    self.tick(0.1)
            self.assertLessEqual(self.area.frames, 100)
            self.assertGreater(self.area.frames, 5)
            self.assertEqual([bar.count for bar in bars], [10_000] * 4)

    def test_changing_a_row_never_takes_the_screens_lock(self):
        class Forbidden:
            def __enter__(self):
                raise AssertionError("the screen's lock was taken")

            def __exit__(self, *error):
                return None

            acquire = release = __enter__

        with log.group("download") as rows:
            bar = rows.add("a", total=10)
            real, self.screen.lock = self.screen.lock, Forbidden()
            try:
                bar.advance()
                bar.count = 5
                bar.total = 20
                bar.caption = "part 2"
                self.assertEqual((bar.count, bar.total, bar.caption), (5, 20, "part 2"))
            finally:
                self.screen.lock = real

    def test_a_slow_terminal_skips_a_frame_instead_of_queueing_it(self):
        writes = []
        original = self.screen.write

        def slow(text):
            writes.append(text)
            time.sleep(0.12)
            original(text)

        with log.progress("c", total=100) as bar:
            self.now += 0.6
            self.screen.write = slow
            bar.advance(50)
            self.tick()
            self.assertEqual(len(writes), 1)
            bar.advance(30)
            self.tick(0.1)
            self.assertEqual(len(writes), 1)
            self.tick(0.1)
            self.assertEqual(len(writes), 2)
            self.assertIn("80/100", writes[1])
            del self.screen.write

    def test_rows_nested_under_a_target_are_capped_to_its_slots(self):
        with log.group("download") as rows, ExitStack() as files:
            target = rows.add("ins:example_user")
            target.slots = 2
            with log.under(target):
                for name in ("a.jpg", "b.jpg", "c.jpg"):
                    files.enter_context(log.progress(name, total=2))
            self.tick(0.6)
            empty = "─" * 20
            self.assertEqual(visible_screen(self.output()), [
                "• download", "• ⠦" + " " * 20 + "ins:example_user", "  • " + empty + " a.jpg " + "0/2",
                "  • " + empty + " b.jpg " + "0/2",
            ])
            target.caption = "3 files · 3 running · 0 queued"
            self.tick(0.1)
            self.assertIn("3 files · 3 running", visible_screen(self.output())[1])
            self.assertEqual(len(visible_screen(self.output())), 4)

    def test_no_thread_without_a_terminal_or_an_area(self):
        self.area.threaded = True
        log.info("idle")
        self.assertIsNone(self.area.thread)
        self.screen._tty = False
        with log.progress("a"):
            self.assertIsNone(self.area.thread)
        self.assertEqual(self.lines(), ["• idle", "✓ " + "━" * 20 + " a"])


class DrawingThreadTest(LogCase):
    """The one drawing thread, on the real clock."""

    tty = True
    width = 40
    fake_clock = False

    def setUp(self):
        super().setUp()
        self.area.threaded = True

    def test_a_spinner_is_redrawn_and_the_thread_ends_with_the_last_area(self):
        with log.progress("wait"):
            time.sleep(0.9)
            self.assertIsNotNone(self.area.thread)
            self.assertGreaterEqual(self.area.frames, 3)
            self.assertEqual(visible_screen(self.output())[0][:1], "•")
        self.assertEqual(visible_screen(self.output()), ["✓ " + "━" * 20 + " wait"])
        for _ in range(20):
            if self.area.thread is None:
                break
            time.sleep(0.05)
        self.assertIsNone(self.area.thread)

    def test_an_idle_area_never_wakes_the_thread(self):
        with log.progress("c", total=10) as bar:
            bar.advance()
            time.sleep(0.7)
            frames = self.area.frames
            time.sleep(0.4)
            self.assertEqual(self.area.frames, frames)
            bar.advance()
            time.sleep(0.3)
            self.assertGreater(self.area.frames, frames)

    def test_one_thread_serves_every_area(self):
        with log.progress("a"), log.progress("b"):
            first = self.area.thread
            self.assertIsNotNone(first)
            with log.progress("c"):
                self.assertIs(self.area.thread, first)


class QuietPipeTest(LogCase):
    def run_bars(self):
        with log.progress("good", total=2) as bar:
            bar.advance()
        with self.assertRaises(RuntimeError), log.progress("bad", total=2):
            raise RuntimeError
        with log.group("download") as jobs:
            jobs.add("a").finish()
            jobs.add("b").fail()
        with self.assertRaises(RuntimeError), log.group("upload") as jobs:
            jobs.add("c")
            raise RuntimeError

    def test_quiet_writes_only_bars_that_end_at_error(self):
        log.level = "quiet"
        self.run_bars()
        self.assertEqual(self.lines(), ["✗ " + "─" * 20 + " bad " + "0/2", "✗ " + "─" * 20 + " b", "✗ upload", "✗ " + "─" * 20 + " c"])

    def test_other_levels_write_every_bar(self):
        log.level = "error"
        self.run_bars()
        self.assertEqual(len(visible_screen(self.output())), 7)


class QuietTtyTest(QuietPipeTest):
    tty = True

    def test_quiet_draws_no_running_bars(self):
        log.level = "quiet"
        with log.progress("good", total=2) as bar, log.progress("spin"):
            bar.advance()
            self.tick(0.6)
            self.assertEqual(self.output(), "")
        with self.assertRaises(RuntimeError), log.progress("bad", total=2):
            raise RuntimeError
        self.assertEqual(visible_screen(self.output()), ["✗ " + "─" * 20 + " bad " + "0/2"])

    def test_quiet_writes_only_bars_that_end_at_error(self):
        log.level = "quiet"
        self.run_bars()
        self.assertEqual(visible_screen(self.output()), ["✗ " + "─" * 20 + " bad " + "0/2", "✗ " + "─" * 20 + " b", "✗ upload", "✗ " + "─" * 20 + " c"])


class ThreadTest(LogCase):
    def test_bars_finish_out_of_order_from_threads(self):
        count = 6
        everyone_open = threading.Barrier(count)

        def job(number):
            with log.progress(f"job {number}", total=1) as bar:
                everyone_open.wait()
                time.sleep(0.01 * (count - number))
                bar.advance()

        threads = [threading.Thread(target=job, args=(number,)) for number in range(count)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        finals = [line for line in self.lines()]
        self.assertEqual(sorted(finals), sorted("✓ " + "━" * 20 + f" job {n} " + "1" for n in range(count)))
        self.assertEqual(finals[0], "✓ " + "━" * 20 + f" job {count - 1} " + "1")
        self.assertEqual(live_area(self.screen).groups, [])

    def test_bars_finish_out_of_order_on_tty(self):
        self.screen._tty = True
        first_open, second_done = threading.Event(), threading.Event()

        def first():
            with log.progress("first", total=1):
                first_open.set()
                second_done.wait(2)

        thread = threading.Thread(target=first)
        thread.start()
        first_open.wait(2)
        with log.progress("second", total=1) as bar:
            bar.advance()
        self.tick(0.6)
        self.assertEqual(visible_screen(self.output()), [
            "✓ " + "━" * 20 + " second " + "1", "• " + "─" * 20 + " first " + "0/1",
        ])
        second_done.set()
        thread.join()
        self.assertEqual(visible_screen(self.output()), [
            "✓ " + "━" * 20 + " second " + "1", "✓ " + "─" * 20 + " first " + "1",
        ])

    def test_many_threads_never_split_lines(self):
        def chatter(number):
            for line in range(200):
                log.info("thread {n} line {line} " + "x" * 50, n=number, line=line)

        threads = [threading.Thread(target=chatter, args=(number,)) for number in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        lines = self.lines()
        self.assertEqual(len(lines), 1600)
        for line in lines:
            self.assertRegex(line, r"^• thread \d line \d+ x{50}$")

    def test_threads_share_a_group(self):
        # C6: lines and row changes from several threads at once lose no count and raise nothing.
        self.screen._tty = True
        workers, steps = 8, 300
        start, errors = threading.Barrier(workers), []
        with log.group("download") as rows:
            shared_row = rows.add("all", total=workers * steps)

            def job(number):
                try:
                    own = rows.add(f"job {number}")
                    start.wait()
                    for step in range(steps):
                        shared_row.advance()
                        own.advance()
                        if step % 50 == 0:
                            log.info("job {n} at {step}", n=number, step=step)
                    own.total = steps
                    own.finish()
                except BaseException as error:  # a thread's error would otherwise only be printed
                    errors.append(error)

            threads = [threading.Thread(target=job, args=(number,)) for number in range(workers)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()
            self.assertEqual(errors, [])
            self.assertEqual(shared_row.count, workers * steps)
            self.assertEqual([row.count for row in rows._bars[1:]], [steps] * workers)
        screen = visible_screen(self.output())
        self.assertEqual(sum(line.startswith("• job ") for line in screen), workers * steps // 50)
        self.assertIn("✓ " + "━" * 20 + " all " + str(workers * steps), screen)
        self.assertEqual(live_area(self.screen).groups, [])

    def test_output_is_held_while_a_prompt_is_open(self):
        asking, logged = threading.Event(), threading.Event()

        def readkey():
            asking.set()
            logged.wait(2)
            return "enter"

        def background():
            asking.wait(2)
            log.info("background")
            with log.progress("bar", total=1) as bar:
                bar.advance()
            logged.set()

        self.screen.readkey = readkey
        thread = threading.Thread(target=background)
        thread.start()
        self.assertEqual(prompt.ask("Name", default="ada"), "ada")
        thread.join()
        self.assertEqual(self.lines(), [
            "┃ Name", "┃ > ada_", "enter submit", "✓ Name: ada", "• background", "✓ " + "━" * 20 + " bar " + "1",
        ])


# One cell of a bar, "━" or "─", as UTF-8 bytes.
CELL = rb"\xe2\x94[\x80\x81]"

LIVE_CHILD = """
import sys, time
sys.path.insert(0, {root!r})
from shared import log
with log.group("download") as rows:
    one, two = rows.add("a.mp4", total=10), rows.add("b.mp4", total=10)
    for step in range(10):
        one.advance()
        two.advance(2 if step < 5 else 0)
        if step == 4:
            log.warn("careful")
        time.sleep(0.12)
    one.finish()
    two.fail()
    time.sleep(0.2)
print("RESULT", flush=True)
"""

WAITING_CHILD = """
import signal, sys, time
# A test run started in the background has SIGINT ignored, and a child inherits that.
signal.signal(signal.SIGINT, signal.default_int_handler)
sys.path.insert(0, {root!r})
from shared import log
with log.progress("clip.mp4", total=10) as bar:
    print("OPEN", flush=True)
    time.sleep(30)
"""


def run_on_a_pty(source: str, *, interrupt_after: bytes | None = None) -> bytes:
    """Run a script on a pty 100 columns wide, and return every byte the terminal received.

    With `interrupt_after`, press Ctrl-C once those bytes arrive. The pty's line discipline is
    real, so a bare line feed, which a fake screen can't show, would reach the bytes as one.
    """
    pid, fd = pty.fork()
    if pid == 0:
        os.environ.update(TERM="xterm-256color", COLUMNS="100", LINES="30")
        os.execv(sys.executable, [sys.executable, "-c", source.format(root=str(Path(__file__).parent.parent))])
    fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack("HHHH", 30, 100, 0, 0))
    out, interrupted = b"", False
    end = time.monotonic() + 20
    try:
        while time.monotonic() < end:
            if select.select([fd], [], [], 0.1)[0]:
                try:
                    chunk = os.read(fd, 65536)
                except OSError:
                    break
                if not chunk:
                    break
                out += chunk
            if interrupt_after is not None and not interrupted and interrupt_after in out:
                time.sleep(0.8)  # long enough for the row to be drawn
                os.write(fd, b"\x03")
                interrupted = True
    finally:
        os.close(fd)
        os.waitpid(pid, 0)
    return out


@unittest.skipUnless(HAS_PTY, "needs a POSIX pty")
class RealTerminalCase(unittest.TestCase):
    """The live area on a real pty, checked on the raw bytes the terminal receives.

    The fake screen can't catch what a terminal's line discipline does to the bytes (the staircase
    bug of prompts, where a bare line feed no longer returned to column 0), nor what a terminal
    would show between two writes; the bytes are what a terminal gets, so these read them.
    """

    def test_frames_are_synchronized_overwrite_rows_and_hide_the_cursor(self):
        out = run_on_a_pty(LIVE_CHILD)
        self.assertIn(b"RESULT", out)
        frames = re.findall(rb"\x1b\[\?2026h(.*?)\x1b\[\?2026l", out, re.S)
        self.assertGreaterEqual(len(frames), 4)
        # Every row, and every line written above the rows, is inside a synchronized frame.
        outside = re.sub(rb"\x1b\[\?2026h.*?\x1b\[\?2026l", b"", out, flags=re.S)
        for text in (b"a.mp4", b"b.mp4", b"careful"):
            self.assertNotIn(text, outside)
        self.assertEqual(out.count(b"\x1b[?2026h"), out.count(b"\x1b[?2026l"))
        for frame in frames:
            # Rows are rewritten in place: a clear-below never comes before a row, and a row ends
            # with its own line's clear, not a screen's.
            self.assertIsNone(re.search(rb"\x1b\[J.*" + CELL, frame, re.S), frame)
            for ending in re.findall(rb"[^\x1b\n]*" + CELL + rb"[^\x1b\n]*(\x1b\[K)?", frame):
                self.assertEqual(ending, b"\x1b[K", frame)
        # No line feed without its carriage return, so no row staircases.
        self.assertIsNone(re.search(rb"(?<!\r)\n", out), out)
        # The cursor is hidden before the first frame and shown after the last.
        hidden, shown = out.index(b"\x1b[?25l"), out.rindex(b"\x1b[?25h")
        self.assertLess(hidden, out.index(b"\x1b[?2026h"))
        self.assertGreater(shown, out.rindex(b"\x1b[?2026l"))
        self.assertEqual(out.count(b"\x1b[?25l"), 1)

    def test_the_cursor_is_shown_after_ctrl_c(self):
        out = run_on_a_pty(WAITING_CHILD, interrupt_after=b"OPEN")
        self.assertIn(b"KeyboardInterrupt", out)
        self.assertIn(b"clip.mp4", out)
        self.assertGreater(out.rindex(b"\x1b[?25h"), out.rindex(b"\x1b[?25l"))
        self.assertIsNone(re.search(rb"(?<!\r)\n", out), out)


class RecordTest(LogCase):
    """`log.record`: a file that takes every line at debug, plain and timed, whatever the screen's level."""

    tty = True

    def setUp(self):
        super().setUp()
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.path = Path(folder.name) / "log" / "run.log"

    def recorded(self) -> list[str]:
        """The file's lines without their times, after checking each starts with one."""
        lines = self.path.read_text(encoding="utf-8").splitlines()
        for line in lines:
            self.assertRegex(line, r"^\d\d:\d\d:\d\d\.\d{3} ")
        return [line[13:] for line in lines]

    def test_every_level_reaches_the_file_while_the_screen_keeps_its_own(self):
        log.level = "warn"
        with log.record(self.path):
            log.debug("listed {n}", n=3)
            log.info("saved")
            log.warn("slow")
        log.debug("after the block")
        self.assertEqual(self.recorded(), ["· listed 3", "• saved", "! slow"])
        self.assertEqual(visible_screen(self.output()), ["! slow"])

    def test_rows_final_lines_are_written_plain_and_running_rows_are_not(self):
        log.level = "quiet"
        with theme.scope(color="16m"), log.record(self.path):
            with log.group("download") as rows:
                rows.add("ins:example_user").finish()
                self.tick(0.6)
            log.debug("next")
        self.assertEqual(self.recorded(), ["✓ download", "✓ ins:example_user", "· next"])
        self.assertNotIn("\x1b", self.path.read_text(encoding="utf-8"))

    def test_query_strings_and_fragments_are_cut_from_urls(self):
        with log.record(self.path):
            log.debug("get {url} then {other}", url="https://cdn.example.com/a.jpg?oe=1&sig=secret",
                      other="http://example.com/p#token=secret")
            log.debug("plain ?question and a.b?c stay")
        self.assertEqual(self.recorded(), ["· get https://cdn.example.com/a.jpg then http://example.com/p",
                                           "· plain ?question and a.b?c stay"])

    def test_appends_and_stops_with_the_block(self):
        with log.record(self.path):
            log.info("one")
        with log.record(self.path):
            log.info("two")
        log.info("three")
        self.assertEqual(self.recorded(), ["• one", "• two"])

    def test_threads_write_whole_lines(self):
        with log.record(self.path):
            threads = [threading.Thread(target=lambda n=n: [log.debug("thread {n} line {i}", n=n, i=i)
                                                             for i in range(50)]) for n in range(4)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()
        lines = self.recorded()
        self.assertEqual(len(lines), 200)
        self.assertTrue(all(re.fullmatch(r"· thread \d line \d+", line) for line in lines))

    def test_a_write_that_fails_never_reaches_the_caller(self):
        with log.record(self.path):
            with patch.object(log._records[0].file, "write", side_effect=OSError("disk full")):
                log.info("lost")
            log.info("kept")
        self.assertEqual(self.recorded(), ["• kept"])


if __name__ == "__main__":
    unittest.main()
