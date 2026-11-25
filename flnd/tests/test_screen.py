"""The run's screen (`flnd.ui.screen`) and the snapshot it draws (`flnd.engine.run.Tally`).

The block is drawn into pyte, a terminal emulator, and read back as the cells a person sees.
"""

from __future__ import annotations

import ast
import asyncio
import io
import json
import os
import subprocess
import sys
import tempfile
import textwrap
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

import msgspec
import pyte
from rich.cells import cell_len
from rich.console import Console

import flnd
from flnd.engine import run as job
from flnd.engine.events import FileState, HoldState, Phase, Resources, State, TargetState
from flnd.plugin import Target
from flnd.ui import screen
from flnd.ui.log import ui
from flnkit import clock

MB = 1 << 20
MOCKUP = State(
    version=1, elapsed=102,
    targets=(
        TargetState("ins:example_user", ("posts", "reels"), Phase.DONE, found=1620, saved=412,
                    skipped=1208, bytes=2100 * MB),
        TargetState("twt:example_creator", ("media",), Phase.DOWNLOADING, found=60, saved=37,
                    skipped=12, failed=1, bytes=180 * MB, speed=31 * MB),
        TargetState("tik:example_handle", ("videos",), Phase.WAITING),
    ),
    files=(FileState("twt:example_creator", "media/1290_2.mp4", 24 * MB, 38 * MB, 12 * MB),
           FileState("twt:example_creator", "media/1287_1.mp4", 48 * MB, 210 * MB, 16 * MB)),
    more=4,
    holds=(HoldState("twt", "pages", 132, "429 from api.example.com", 3),),
    now="twt:example_creator · media, page 4 · held 30 s: 429 from api.example.com",
    saved=449, skipped=1220, failed=1, bytes=2280 * MB, speed=42 * MB,
)


def draw(state: State, *, width: int = 100, height: int = 40) -> list[str]:
    """The block for `state` as a terminal of `width` by `height` shows it, one string a row."""
    sink = io.StringIO()
    console = Console(file=sink, force_terminal=True, width=width, color_system=None)
    console.print(screen.frame(state, command="download", width=width, height=height))
    term = pyte.Screen(width, height)
    pyte.Stream(term).feed(sink.getvalue().replace("\n", "\r\n"))
    return [row.rstrip() for row in term.display if row.strip()]


class _TTY(io.StringIO):
    """A terminal that counts what is written to it."""

    encoding = "utf-8"

    def isatty(self) -> bool:
        return True


class FrameTest(unittest.TestCase):
    def setUp(self) -> None:
        self.enterContext(clock.use(clock.Fake()))

    def test_the_mockup_state_draws_its_rows(self) -> None:
        rows = draw(MOCKUP)
        self.assertEqual(rows[0], "  flnd download · 1/3 targets · 2 left · 01:42")
        self.assertRegex(rows[2],
                         r"✓ ins:example_user +posts reels +412 +1,208 +0 +2\.1 GB +–$")
        self.assertRegex(rows[3],
                         r"⠋ twt:example_creator +media +37 +12 +1 +180\.0 MB +31\.0 MB/s$")
        self.assertRegex(rows[4], r"· tik:example_handle +videos +– +– +– +– +–$")
        self.assertRegex(rows[5],
                         r"⠋ media/1290_2\.mp4 +━{18}╺━{11} +63% +38\.0 MB +12\.0 MB/s$")
        self.assertEqual(rows[7].strip(), "+ 4 more")
        self.assertTrue(rows[8].endswith("held 30 s: 429 from api.example.com (×3)"), rows[8])
        self.assertEqual(rows[9], "  42.0 MB/s · 449 saved · 1 failed")

    def test_size_and_speed_never_touch(self) -> None:
        going = TargetState("ins:example_user", ("posts",), Phase.DOWNLOADING, found=2, saved=1,
                            bytes=int(3.9 * MB), speed=513.9 * 1024)
        file = FileState("ins:example_user", "posts/1.mp4", 1, int(3.9 * MB), 513.9 * 1024)
        rows = draw(State(targets=(going,), files=(file,)))
        for row in (rows[2], rows[3]):
            with self.subTest(row):
                self.assertRegex(row, r"3\.9 MB {2,}513\.9 KB/s$")

    def test_columns_keep_spacing_at_maximum_widths_and_30_fps(self) -> None:
        self.assertTrue(screen.TICK <= 1 / 10 and job.TICK <= 1 / 10)
        going = TargetState("ins:" + "x" * 24, ("posts", "reels"), Phase.DOWNLOADING,
                            found=999999, saved=999999, skipped=999999, failed=999999,
                            bytes=int(1023.9 * MB), speed=1023.9 * 1024)
        file = FileState(going.text, "posts/" + "y" * 20 + ".mp4",
                         int(1023.9 * MB), int(1023.9 * MB), 1023.9 * 1024)
        rows = draw(State(targets=(going,), files=(file,)), width=100)
        self.assertRegex(rows[1], r"target {2,}categories {2,}saved {2,}skipped {2,}failed {2,}size"
                                  r" {2,}speed$")
        self.assertRegex(rows[2], r"x{24} {2,}posts reels {2,}999,999 {2,}999,999 {2,}999,999"
                                  r" {2,}1023\.9 MB {2,}1023\.9 KB/s$")
        self.assertRegex(rows[3], r"100% {2,}1023\.9 MB {2,}1023\.9 KB/s$")

    def test_no_speed_once_a_target_ended_or_nothing_is_in_flight(self) -> None:
        done = TargetState("ins:example_user", ("posts",), Phase.DONE, found=1, saved=1,
                           bytes=5, speed=2 * MB)
        rows = draw(State(targets=(done,), saved=1, speed=2 * MB))
        self.assertRegex(rows[2], r"5\.0 B +–$")
        self.assertRegex(rows[-1], r"^  – · 1 saved · 0 failed")

    def test_cells_not_characters_bound_a_wide_label(self) -> None:
        wide = "tlg:" + "频道" * 20
        for cut, cells in ((screen.middle, 28), (screen.left, 24)):
            with self.subTest(cut.__name__):
                self.assertLessEqual(cell_len(cut(wide, cells)), cells)
                self.assertGreaterEqual(cell_len(cut(wide, cells)), cells - 1)
        state = State(targets=(TargetState(wide, ("posts",), Phase.LISTING),))
        row = next(line for line in draw(state) if "tlg:" in line)
        long = TargetState("ins:" + "x" * 40, ("posts",), Phase.LISTING)
        narrow = draw(State(targets=(long,)))
        # The categories start in the same cell after a wide label as after a narrow one.
        self.assertEqual(cell_len(row[:row.index("posts")]), narrow[2].index("posts"))

    def test_a_short_terminal_folds_the_targets_past_its_room(self) -> None:
        many = State(targets=tuple(TargetState(f"ins:example_user_{n}", (), Phase.WAITING)
                                   for n in range(30)))
        rows = draw(many, height=20)
        self.assertEqual(sum("example_user_" in row for row in rows), 11)
        self.assertIn("  + 19 targets", rows)
        self.assertEqual(sum("example_user_" in row for row in draw(many, height=60)), 30)

    def test_a_short_terminal_keeps_active_and_waiting_targets_visible(self) -> None:
        targets = (
            *(TargetState(f"ins:done_{n}", (), Phase.DONE) for n in range(15)),
            TargetState("ins:active_1", (), Phase.DOWNLOADING),
            *(TargetState(f"ins:wait_{n}", (), Phase.WAITING) for n in range(10)),
        )
        rows = draw(State(targets=targets), height=16)
        self.assertIn("15/26 targets · 11 left", rows[0])
        self.assertTrue(any("ins:active_1" in row for row in rows))
        self.assertTrue(any("ins:wait_0" in row for row in rows))
        self.assertFalse(any("ins:done_0" in row for row in rows))

    def test_files_and_long_now_stay_within_terminal_bounds(self) -> None:
        state = State(
            targets=(TargetState("ins:example_user", ("posts",), Phase.DOWNLOADING),),
            files=(FileState("ins:example_user", "posts/1.mp4", 1, 10, 1),),
            more=2,
            now="line1\nline2\t" + "x" * 200,
        )
        block = screen.frame(state, command="get", width=80, height=13)
        lines = block.plain.split("\n")
        self.assertLessEqual(len(lines), 13)
        self.assertTrue(all(cell_len(line) <= 79 for line in lines))
        self.assertTrue(any("line1 line2" in line for line in lines))

    def test_short_terminal_with_files_and_resources_never_exceeds_height(self) -> None:
        files = tuple(
            FileState("ins:example_user", f"posts/{n}.jpg", 50, 100, 10) for n in range(6)
        )
        state = msgspec.structs.replace(
            MOCKUP, files=files, more=2,
            resources=Resources(5, 312 << 20, 14.4, browser=3, browser_rss=200 << 20),
        )
        for h in (14, 12, 8, 5):
            with self.subTest(height=h):
                lines = screen.frame(state, command="get", width=100, height=h).plain.split("\n")
                self.assertLessEqual(len(lines), h)
                self.assertTrue(all(cell_len(line) <= 99 for line in lines))
                self.assertTrue(any("twt:example_creator" in line for line in lines))
                self.assertTrue(any("saved" in line for line in lines))

    def test_narrow_terminal_adapts_columns_bars_and_footer_cleanly(self) -> None:
        for w in (80, 60, 45, 30):
            with self.subTest(width=w):
                lines = screen.frame(MOCKUP, command="get", width=w, height=30).plain.split("\n")
                self.assertTrue(all(cell_len(line) <= w - 1 for line in lines))
                foot = lines[-1]
                self.assertNotIn("ctrl+c", foot)
                if w == 60:
                    self.assertTrue(any("%" in line for line in lines))
                if w in (60, 80):
                    self.assertTrue(any("saved" in line for line in lines))


class ResourcesTest(unittest.TestCase):
    USE = Resources(5, 312 << 20, 14.4, browser=3, browser_rss=200 << 20, browser_cpu=9.0)

    def test_the_line_shows_flnd_and_the_browser_when_there_is_a_reading(self) -> None:
        rows = draw(msgspec.structs.replace(MOCKUP, resources=self.USE))
        self.assertIn("  use  flnd 5 processes · 312.0 MB · 14% · browser 3 processes · "
                      "200.0 MB · 9%", rows)
        self.assertFalse([row for row in draw(MOCKUP) if row.startswith("  use ")])

    def test_a_reading_is_sent_as_an_event_and_shown_on_the_snapshot(self) -> None:
        sent: list = []

        async def go() -> State:
            got = asyncio.Event()

            async def events(event) -> None:
                sent.append(event)
                got.set()

            async def off(fn):
                return fn()

            tally = job.Tally()
            async with tally.publishing(events, off):
                await got.wait()
                return tally.publish(force=True)

        with mock.patch.object(job, "RESOURCES_EVERY", 0.01), \
                mock.patch("flnd.engine.meter.Meter.read", return_value=self.USE):
            state = asyncio.run(go())
        self.assertEqual((sent[0], state.resources), (self.USE, self.USE))

    def test_json_gets_the_event_and_a_plain_log_prints_nothing(self) -> None:
        from flnd import cli

        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            cli._JournalView().show(self.USE)
        self.assertEqual((out.getvalue(), err.getvalue()), ("", ""))
        with redirect_stdout(out):
            cli._Json().show(self.USE)
        row = json.loads(out.getvalue())
        self.assertEqual((row["type"], row["procs"], row["browser"]), ("Resources", 5, 3))

    def test_the_meter_counts_the_process_and_the_browser_among_its_children(self) -> None:
        from flnd.engine.meter import Meter

        def fake(pid: int, name: str, rss: int, cpu: float) -> mock.Mock:
            proc = mock.MagicMock(pid=pid)
            proc.name.return_value = name
            proc.memory_info.return_value.rss = rss
            proc.cpu_percent.return_value = cpu
            return proc

        root = fake(1, "python", 100, 5.0)
        root.children.return_value = [fake(2, "node", 10, 1.0), fake(3, "Chromium Helper", 50, 2.0)]
        with mock.patch("psutil.Process", return_value=root):
            use = Meter().read()
        self.assertEqual(use, Resources(3, 160, 8.0, 1, 50, 2.0))


class TallyTest(unittest.TestCase):
    def setUp(self) -> None:
        self.clock = self.enterContext(clock.use(clock.Fake()))
        self.sent: list[State] = []
        self.tally = job.Tally(self.sent.append)
        self.user = Target("ins:example_user", kind="user")

    def test_a_file_in_flight_is_one_flight_however_often_it_is_asked(self) -> None:
        first = self.tally.fly("k", self.user, "posts/1.jpg")
        again = self.tally.fly("k", self.user, "posts/1.jpg")
        again.advance(7)
        self.assertIs(first, again)
        self.assertEqual(self.tally.publish().files[0].done, 7)

    def test_files_in_flight_stop_at_six_and_count_the_rest(self) -> None:
        self.tally.add(self.user)
        for n in range(8):
            self.tally.fly(f"k{n}", self.user, f"posts/{n}.jpg").advance(10)
        state = self.tally.publish()
        self.assertEqual(([f.path for f in state.files], state.more),
                         ([f"posts/{n}.jpg" for n in range(6)], 2))
        self.assertIn("    + 2 more", draw(state))

    def test_a_snapshot_is_new_only_when_something_changed(self) -> None:
        self.tally.add(self.user, ("posts",))
        first = self.tally.publish()
        for _ in range(50):
            self.clock.advance(0.1)
            self.assertIsNone(self.tally.publish())
        self.tally.count(self.user, "posts", found=1, saved=1, bytes=5)
        later = self.tally.publish()
        self.assertEqual((first.version, later.version, later.targets[0].saved), (1, 2, 1))
        self.assertEqual(self.sent, [first, later])

    def test_a_hold_shows_on_the_now_line_until_it_ends(self) -> None:
        self.tally.start(self.user, "posts")
        with self.tally.hold("ins", "pages", 30, "429 from api.example.com", count=2):
            state = self.tally.publish()
        self.assertEqual(state.now,
                         "ins:example_user · posts · held 30 s: 429 from api.example.com")
        self.assertEqual(self.tally.publish().now, "ins:example_user · posts")


class ScreenTest(unittest.TestCase):
    def setUp(self) -> None:
        self.clock = self.enterContext(clock.use(clock.Fake()))

    def shown(self, read, tty: _TTY) -> screen.Screen:
        """A screen on `tty` whose ticks are the test's own, on the fake clock."""
        drawn = screen.Screen(read, command="download", stream=tty, width=100, height=40)
        drawn.__enter__()
        self.addCleanup(drawn.__exit__, None, None, None)
        drawn._done.set()
        drawn._thread.join()
        return drawn

    def test_screen_keeps_redrawing_and_ticks_elapsed_at_60_fps(self) -> None:
        self.assertEqual((screen.TICK, job.TICK), (1 / 10, 1 / 10))
        tty = _TTY()
        shown = self.shown(lambda: MOCKUP, tty)
        shown.tick()
        drawn = len(tty.getvalue())
        self.clock.advance(1.0)
        shown.tick()
        added = tty.getvalue()[drawn:]
        self.assertGreater(len(added), 0)
        self.assertIn("01:43", added)

    def test_a_frame_that_fails_goes_to_the_journal_and_the_screen_goes_on(self) -> None:
        tty, states = _TTY(), [State(version=5), MOCKUP]
        journal = Path(self.enterContext(tempfile.TemporaryDirectory())) / "run.jsonl"
        shown = self.shown(lambda: states[0], tty)
        with ui.file(journal), mock.patch.object(screen, "frame", side_effect=KeyError("rows")):
            shown.tick()
        self.assertNotIn("Traceback", tty.getvalue())
        self.assertIn("the screen could not draw: KeyError", journal.read_text(encoding="utf-8"))
        states.pop(0)
        shown.tick()
        self.assertIn("ins:example_user", tty.getvalue())

    def test_a_pipe_gets_no_block_and_then_the_summary(self) -> None:
        stdout, stderr = io.StringIO(), io.StringIO()
        target = MOCKUP.targets[1].text
        failed = flnd.Failed(Target(target, kind="user"), flnd.NotFound(
            "the site answered 404: the file is gone",
            hint="nothing to do; flnd won't ask for it again"),
            post="1290", path=Path("home/media/twt/example_creator/media/1290_2.mp4"))
        report = flnd.Report(saved=1021, bytes=3400 * MB, skipped=1220, failed=(failed,))
        with redirect_stdout(stdout), redirect_stderr(stderr):
            with screen.Screen(lambda: MOCKUP, command="download"):
                pass
            screen.summary(report, State(elapsed=252, targets=MOCKUP.targets))
        self.assertEqual(stdout.getvalue(), "  x done in 4m 12s - 3 targets - 1,021 saved"
                                            " (3.3 GB) - 1,220 already saved - 1 failed\n")
        self.assertEqual(stderr.getvalue().splitlines(), [
            "", "  x twt:example_creator - media/1290_2.mp4",
            "    the site answered 404: the file is gone",
            "    hint  nothing to do; flnd won't ask for it again", "",
            "  details  flnd status"])

    def test_a_question_asked_during_a_run_stays_on_the_screen(self) -> None:
        from types import SimpleNamespace

        from flnd.ui import forms

        tty, version = _TTY(), [1]
        shown = self.shown(lambda: msgspec.structs.replace(MOCKUP, version=version[0]), tty)
        shown.tick()
        with redirect_stdout(tty), redirect_stderr(tty), \
                mock.patch.object(forms, "interactive", return_value=True), \
                mock.patch.object(forms, "_app", lambda owner: SimpleNamespace(
                    return_value=owner.finish(True))), \
                mock.patch.object(forms, "_show", lambda app: None):
            self.assertTrue(forms.confirm("Download Chromium?"))
        for _ in range(3):
            version[0] += 1
            shown.tick()
        term = pyte.Screen(100, 40)
        pyte.Stream(term).feed(tty.getvalue().replace("\n", "\r\n"))
        rows = [row.rstrip() for row in term.display]
        self.assertTrue(any("Download Chromium?" in row for row in rows), rows)
        # One block, drawn below the answer: no stale copy is left above it.
        heads = [n for n, row in enumerate(rows) if "flnd download" in row]
        question = next(n for n, row in enumerate(rows) if "Download Chromium?" in row)
        self.assertEqual(len(heads), 1, rows)
        self.assertGreater(heads[0], question)

    def test_resize_shrink_and_grow_leaves_single_clean_block_in_pyte(self) -> None:
        from flnd.ui import out

        class ResizableTTY(_TTY):
            def __init__(self, cols: int, rows: int) -> None:
                super().__init__()
                self.cols, self.rows = cols, rows

            def size(self) -> tuple[int, int]:
                return self.cols, self.rows

        tty = ResizableTTY(100, 30)
        version = [1]
        shown = screen.Screen(
            lambda: msgspec.structs.replace(MOCKUP, version=version[0]),
            command="download", stream=tty, width=100, height=30,
        )
        term = pyte.Screen(100, 30)
        stream = pyte.Stream(term)

        def feed_new(offset: int) -> int:
            buf = tty.getvalue()
            chunk = buf[offset:]
            stream.feed(chunk.replace("\n", "\r\n"))
            return len(buf)

        with mock.patch("shutil.get_terminal_size",
                        side_effect=lambda *a: os.terminal_size(tty.size())):
            shown.__enter__()
            shown._done.set()
            shown._thread.join()
            try:
                shown.tick()
                pos = feed_new(0)

                # Shrink width 100 -> 50, emit a warning above, and redraw.
                tty.cols = 50
                shown._console.width = 50
                term.resize(30, 50)
                version[0] += 1
                out.warn("narrow warning")
                shown.tick()
                pos = feed_new(pos)

                # Grow width 50 -> 110 and redraw.
                tty.cols = 110
                shown._console.width = 110
                term.resize(30, 110)
                version[0] += 1
                shown.tick()
                feed_new(pos)
            finally:
                shown.__exit__(None, None, None)

        rows = [row.rstrip() for row in term.display]
        heads = [n for n, row in enumerate(rows) if "flnd download" in row]
        self.assertEqual(len(heads), 1, rows)
        self.assertTrue(any("narrow warning" in row for row in rows[:heads[0]]), rows)

    def test_resize_mid_tick_never_wraps_or_desyncs_cursor(self) -> None:
        tty = _TTY()
        shown = self.shown(lambda: MOCKUP, tty)
        shown.tick()
        orig_frame = screen.frame

        def shrink_after_frame(*args, **kwargs):
            res = orig_frame(*args, **kwargs)
            if kwargs["width"] >= 99:
                shown._console.width = 45
            return res

        pos = len(tty.getvalue())
        with mock.patch.object(screen, "frame", side_effect=shrink_after_frame):
            shown.tick()
        chunk = tty.getvalue()[pos:]
        self.assertEqual(shown._last_size, (44 if shown._console.legacy_windows else 45, 40))
        self.assertLessEqual(max(shown._last_widths), 44)
        self.assertEqual(chunk.count("flnd download"), 1, repr(chunk))

    def test_queued_warnings_on_resize_do_not_draw_stale_wide_frame(self) -> None:
        from flnd.ui import out

        tty = _TTY()
        shown = self.shown(lambda: MOCKUP, tty)
        shown.tick()
        pos = len(tty.getvalue())
        shown._console.width = 45
        out.warn("w1")
        out.warn("w2")
        shown.tick()
        chunk = tty.getvalue()[pos:]
        self.assertEqual(chunk.count("flnd download"), 1, repr(chunk))

    def test_resize_reflow_accounts_for_legacy_windows_column_offset(self) -> None:
        tty = _TTY()
        shown = self.shown(lambda: MOCKUP, tty)
        shown.tick()
        # Simulate a line of width 50 on a 50-col legacy_windows console (size reports 49).
        shown._last_widths = [50]
        shown._console.width = 50
        pos = len(tty.getvalue())
        old_lw = shown._console.legacy_windows
        shown._console.legacy_windows = True
        try:
            shown.tick()
        finally:
            shown._console.legacy_windows = old_lw
        chunk = tty.getvalue()[pos:]
        # With actual_cols = 49 + 1 = 50, a 50-cell line occupies 1 row -> up = 0 (no CURSOR_UP).
        self.assertNotIn("\x1b[1A", chunk)

    def test_resize_debounces_active_drag_and_draws_once_settled(self) -> None:
        tty = _TTY()
        shown = self.shown(lambda: MOCKUP, tty)
        now = [0.0]
        with mock.patch.object(screen.time, "monotonic", side_effect=lambda: now[0]):
            shown._tick(debounce=True)
            pos = len(tty.getvalue())

            # Active drag: width changes repeatedly -> clear once, draw nothing until settled.
            shown._console.width = 90
            now[0] = 0.1
            shown._tick(debounce=True)
            drag_chunk_1 = tty.getvalue()[pos:]
            self.assertEqual(drag_chunk_1.count("flnd download"), 0, repr(drag_chunk_1))

            drag_pos = len(tty.getvalue())
            for w, t in ((80, 0.2), (70, 0.3), (70, 0.4)):
                shown._console.width = w
                now[0] = t
                shown._tick(debounce=True)
            self.assertEqual(tty.getvalue()[drag_pos:], "")

            # Once settled past RESIZE_SETTLE, one clean frame is drawn at width 70.
            now[0] = 0.55
            shown._tick(debounce=True)
            settled = tty.getvalue()[drag_pos:]
            self.assertEqual(settled.count("flnd download"), 1, repr(settled))
            self.assertEqual(shown._last_size, (69 if shown._console.legacy_windows else 70, 40))

    def test_resize_smaller_ignores_stale_columns_and_lines_env(self) -> None:
        tty = _TTY()
        live_size = [(120, 30)]
        with mock.patch.dict(os.environ, {"COLUMNS": "120", "LINES": "30"}), \
                mock.patch("os.get_terminal_size", side_effect=lambda *a: os.terminal_size(
                    live_size[0])):
            shown = screen.Screen(lambda: MOCKUP, command="download", stream=tty)
            shown.__enter__()
            shown._done.set()
            shown._thread.join()
            try:
                shown.tick()
                live_size[0] = (50, 12)
                for _ in range(3):
                    shown.tick()
                self.assertEqual(
                    shown._last_size,
                    (49 if shown._console.legacy_windows else 50, 12),
                )
                self.assertLessEqual(max(shown._last_widths), 49)
                self.assertLessEqual(len(shown._last_widths), 11)
            finally:
                shown.__exit__(None, None, None)

    def test_the_screen_reads_nothing_but_the_snapshot(self) -> None:
        tree = ast.parse(Path(screen.__file__).read_text(encoding="utf-8"))
        modules = {node.module for node in ast.walk(tree)
                   if isinstance(node, ast.ImportFrom) and node.module}
        self.assertEqual({m for m in modules if m.startswith("flnd.engine")},
                         {"flnd.engine.events"})

    def test_ctrl_c_mid_redraw_never_waits_on_the_screen(self) -> None:
        if sys.platform == "win32": self.skipTest("pty is Unix only")
        child = subprocess.run([sys.executable, "-c", _MID_REDRAW], capture_output=True,
                               text=True, timeout=20, env={**os.environ, "PYTHONPATH": ""})
        self.assertEqual(child.returncode, 130, child.stderr)


# A child process with the screen redrawing all the time, each write slowed down while Rich
# holds its locks. The second Ctrl-C's warning must reach the screen through its queue
# (`out.above`), or the child exits 7; three Ctrl-Cs must quit within 2 s, or it exits 9.
_MID_REDRAW = textwrap.dedent("""
    import os, pty, signal, threading, time
    from flnd.engine.events import State
    from flnd.engine.run import stopping
    from flnd.ui import screen

    master, slave = pty.openpty()
    seen = bytearray()
    def drain():
        while True:
            seen.extend(os.read(master, 65536))
    threading.Thread(target=drain, daemon=True).start()

    class Slow:
        # Each write waits while Rich holds its locks: a Ctrl-C lands mid-redraw.
        encoding = "utf-8"
        def __init__(self):
            self.file = os.fdopen(slave, "w", encoding="utf-8")
        def write(self, text):
            time.sleep(0.05)
            return self.file.write(text)
        def flush(self):
            self.file.flush()
        def isatty(self):
            return True
        def fileno(self):
            return self.file.fileno()

    version = 0
    def read():
        global version
        version += 1
        return State(version=version)
    threading.Timer(2.0, lambda: os._exit(9)).start()
    with stopping(), screen.Screen(read, command="download", stream=Slow()):
        time.sleep(0.3)
        for _ in range(2):
            os.kill(os.getpid(), signal.SIGINT)
            time.sleep(0.12)
        time.sleep(0.5)
        if b"Ctrl-C again to quit now" not in bytes(seen):
            os._exit(7)
        os.kill(os.getpid(), signal.SIGINT)
        time.sleep(0.5)
    os._exit(0)
""")


if __name__ == "__main__":
    unittest.main()
