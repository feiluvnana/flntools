import re
import threading
import time
import unittest
from io import StringIO

from shared import fmt, log, prompt, style, theme
from shared._log import live_area
from shared._theme import Screen


def visible_screen(output: str) -> list[str]:
    """Replay what a terminal shows after output: text, \\r, \\n, cursor-up, and clear-below."""
    lines, row, column = [""], 0, 0
    for match in re.finditer(r"\x1b\[(\d*)A|\x1b\[J|\x1b\[K|\r|\n|[^\x1b\r\n]+", output):
        token = match.group()
        if token == "\r":
            column = 0
        elif token == "\n":
            row, column = row + 1, 0
            if row == len(lines):
                lines.append("")
        elif token.endswith("A"):
            row -= int(match[1] or 1)
        elif token in ("\x1b[J", "\x1b[K"):
            lines[row] = lines[row][:column]
            if token == "\x1b[J":
                del lines[row + 1:]
        else:
            lines[row] = lines[row][:column] + token + lines[row][column + len(token):]
            column += len(token)
    return [line for line in lines if line]


class LogCase(unittest.TestCase):
    tty = False
    width = 80

    def setUp(self):
        self.screen = Screen(out=StringIO(), inp=StringIO(""), width=self.width, tty=self.tty, interactive=True)
        self.scope = theme.scope(screen=self.screen, color="none", unicode=True)
        self.scope.__enter__()
        self.previous_level = log.level

    def tearDown(self):
        log.level = self.previous_level
        self.scope.__exit__(None, None, None)

    def output(self) -> str:
        return self.screen.out.getvalue()

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
            self.screen.out.seek(0)
            self.screen.out.truncate()
            log.level = level
            for name in ("debug", "info", "ok", "warn", "error"):
                getattr(log, name)(name)
            self.assertEqual([line[2:] for line in self.lines()], expected, level)

    def test_unknown_level(self):
        with self.assertRaises(ValueError):
            log.level = "trace"

    def test_fields(self):
        log.ok("finished", folders=3)
        log.info("saved {path}", path="a", size=2, kind="mp4")
        log.info("{} and {}", 1, 2)
        self.assertEqual(self.lines(), ["✓ finished  folders=3", "• saved a  size=2  kind=mp4", "• 1 and 2"])

    def test_braces_without_arguments(self):
        log.info("saved {path}")
        log.info("{}")
        self.assertEqual(self.lines(), ["• saved {path}", "• {}"])

    def test_bad_format_raises(self):
        with self.assertRaises(KeyError):
            log.info("{missing}", other=1)

    def test_empty_message(self):
        log.ok()
        self.assertEqual(self.lines(), ["✓"])

    def test_styled_field_is_kept(self):
        with theme.scope(color="16m"):
            log.error("{msg}", msg=style.red.bold("disk full"))
        self.assertEqual(self.output(), "\x1b[31m✗\x1b[0m \x1b[1;31mdisk full\x1b[0m\n")

    def test_per_call_theme(self):
        with theme.scope(color="16m"):
            log.info("plain", theme=theme.branch(color="none", unicode=False))
            self.assertEqual(theme.color, "16m")
        self.assertEqual(self.output(), "* plain\n")


class PipeProgressTest(LogCase):
    def test_clean_exit(self):
        with log.progress("clip.mp4", total=3 * 1024 * 1024, show=fmt.size) as bar:
            bar.advance(1024 * 1024)
            bar.update(1258291)
            bar.caption = "part 3"
            self.assertEqual(self.output(), "")
        self.assertEqual(self.lines(), ["✓ clip.mp4 ━━━━━━━━──────────── 1.2 MB/3.0 MB"])
        self.assertTrue(bar.finished)
        self.assertEqual((bar.label, bar.done, bar.caption), ("clip.mp4", 1258291, ""))

    def test_exception(self):
        with self.assertRaises(KeyboardInterrupt):
            with log.progress("clip.mp4", total=10) as bar:
                bar.advance(4)
                bar.caption = "part 3"
                raise KeyboardInterrupt
        self.assertEqual(self.lines(), ["✗ clip.mp4 ━━━━━━━━──────────── 4/10 part 3"])

    def test_return_and_break_are_clean(self):
        for _ in range(2):
            with log.progress("loop") as bar:
                bar.advance()
                break
        self.assertEqual(self.lines(), ["✓ loop 1"])

    def test_spinner_and_late_total(self):
        with log.progress("scroll posts") as bar:
            for _ in range(12):
                bar.advance()
        with log.progress("late") as late:
            late.total = 4
            late.update(-5)
        self.assertEqual(self.lines(), ["✓ scroll posts 12", "✓ late ──────────────────── 0/4"])

    def test_nested_bars(self):
        with log.progress("run") as outer:
            with log.progress("file", total=2) as inner:
                inner.advance(2)
            outer.advance()
        self.assertEqual(self.lines(), ["✓ file ━━━━━━━━━━━━━━━━━━━━ 2/2", "✓ run 1"])

    def test_multi_with_finish_and_fail(self):
        with self.assertRaises(RuntimeError):
            with log.multi("download") as jobs:
                ins = jobs.add("ins:ada", total=12)
                twt = jobs.add("twt:bob")
                fbk = jobs.add("fbk:cy")
                ins.advance()
                ins.caption = "reel 3"
                twt.finish()
                fbk.fail()
                self.assertEqual(jobs.bars, [ins, twt, fbk])
                raise RuntimeError
        self.assertEqual(self.lines(), [
            "✗ download",
            "✗ ins:ada ━━────────────────── 1/12 reel 3",
            "✓ twt:bob",
            "✗ fbk:cy",
        ])

    def test_multi_clean_exit(self):
        with log.multi("download") as jobs:
            jobs.add("a", total=1).advance()
        self.assertEqual(self.lines(), ["✓ download", "✓ a ━━━━━━━━━━━━━━━━━━━━ 1/1"])

    def test_log_line_while_bar_open(self):
        with log.progress("run", total=2) as bar:
            log.info("hello")
            bar.advance()
        self.assertEqual(self.lines(), ["• hello", "✓ run ━━━━━━━━━━────────── 1/2"])


class TtyProgressTest(LogCase):
    tty = True
    width = 30

    def test_redraws_in_place(self):
        with log.progress("c", total=4) as bar:
            bar.advance(1)
            self.assertEqual(visible_screen(self.output()), ["• c ──────────────────── 0/4"])
            time.sleep(0.11)
            bar.advance(1)
            self.assertEqual(visible_screen(self.output()), ["• c ━━━━━━━━━━────────── 2/4"])
        self.assertIn("\x1b[1A\x1b[J", self.output())
        self.assertEqual(visible_screen(self.output()), ["✓ c ━━━━━━━━━━────────── 2/4"])

    def test_exception_on_tty(self):
        with self.assertRaises(ValueError), log.progress("clip", total=4) as bar:
            bar.caption = "x"
            raise ValueError
        self.assertEqual(visible_screen(self.output()), ["✗ clip ──────────────────── 0/4 x"])

    def test_log_line_above_open_bars(self):
        with log.multi("download") as jobs:
            jobs.add("a", total=2)
            with log.progress("inner", total=1):
                log.warn("careful")
                self.assertEqual(visible_screen(self.output()), [
                    "! careful", "• download", "• a ──────────────────── 0/2", "• inner ──────────────────── 0",
                ])
        self.assertEqual(visible_screen(self.output()), [
            "! careful", "✓ inner ──────────────────── 0/1", "✓ download", "✓ a ──────────────────── 0/2",
        ])

    def test_live_area_capped_to_height(self):
        self.screen._height = 4
        with log.multi("download") as jobs:
            for name in "abcde":
                jobs.add(name, total=1)
            rows = visible_screen(self.output())
            self.assertEqual(rows[:2], ["• download", "• a ──────────────────── 0/1"])
            self.assertEqual(rows[2:], ["… 4 more"])
            jobs.changed(force=True)
            self.assertEqual(len(visible_screen(self.output())), 3)

    def test_rows_cut_to_width(self):
        with log.progress("a-very-long-label-that-goes-past-the-width", total=10):
            rows = visible_screen(self.output())
            self.assertEqual(len(rows), 1)
            self.assertEqual(len(rows[0]), 30)

    def test_spinner_thread_redraws(self):
        with log.progress("wait"):
            time.sleep(0.35)
            self.assertGreaterEqual(self.output().count("\x1b[1A"), 2)
        self.assertEqual(visible_screen(self.output()), ["✓ wait"])
        time.sleep(0.15)
        self.assertIsNone(live_area(self.screen).spinner)


class QuietPipeTest(LogCase):
    def run_bars(self):
        with log.progress("good", total=2) as bar:
            bar.advance()
        with self.assertRaises(RuntimeError), log.progress("bad", total=2):
            raise RuntimeError
        with log.multi("download") as jobs:
            jobs.add("a").finish()
            jobs.add("b").fail()
        with self.assertRaises(RuntimeError), log.multi("upload") as jobs:
            jobs.add("c")
            raise RuntimeError

    def test_quiet_writes_only_bars_that_end_at_error(self):
        log.level = "quiet"
        self.run_bars()
        self.assertEqual(self.lines(), ["✗ bad ──────────────────── 0/2", "✗ b", "✗ upload", "✗ c"])

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
            time.sleep(0.15)
            self.assertEqual(self.output(), "")
        with self.assertRaises(RuntimeError), log.progress("bad", total=2):
            raise RuntimeError
        self.assertEqual(visible_screen(self.output()), ["✗ bad ──────────────────── 0/2"])

    def test_quiet_writes_only_bars_that_end_at_error(self):
        log.level = "quiet"
        self.run_bars()
        self.assertEqual(visible_screen(self.output()), ["✗ bad ──────────────────── 0/2", "✗ b", "✗ upload", "✗ c"])


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
        self.assertEqual(sorted(finals), sorted(f"✓ job {n} ━━━━━━━━━━━━━━━━━━━━ 1/1" for n in range(count)))
        self.assertEqual(finals[0], f"✓ job {count - 1} ━━━━━━━━━━━━━━━━━━━━ 1/1")
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
        self.assertEqual(visible_screen(self.output()), [
            "✓ second ━━━━━━━━━━━━━━━━━━━━ 1/1", "• first ──────────────────── 0/1",
        ])
        second_done.set()
        thread.join()
        self.assertEqual(visible_screen(self.output()), [
            "✓ second ━━━━━━━━━━━━━━━━━━━━ 1/1", "✓ first ──────────────────── 0/1",
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
        self.assertEqual(prompt.input("Name", default="ada"), "ada")
        thread.join()
        self.assertEqual(self.lines(), [
            "Name", "› ada", "✓ Name: ada", "• background", "✓ bar ━━━━━━━━━━━━━━━━━━━━ 1/1",
        ])


if __name__ == "__main__":
    unittest.main()
