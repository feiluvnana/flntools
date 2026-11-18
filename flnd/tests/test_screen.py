"""The run's screen (`flnd.ui.screen`) and the snapshot it draws (`flnd.engine.run.Tally`).

The block is drawn into pyte, a terminal emulator, and read back as the cells a person sees.
"""

from __future__ import annotations

import ast
import io
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
from flnd.engine.events import FileState, HoldState, Phase, State, TargetState
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
    holds=(HoldState("twt", None, "pages", 132, "429 from api.example.com", 3),),
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
        self.assertEqual(rows[0], "  flnd download · 3 targets · 01:42")
        self.assertRegex(rows[2],
                         r"✓ ins:example_user +posts reels +412 +1,208 +0 +2\.1 GB +–$")
        self.assertRegex(rows[3],
                         r"⠋ twt:example_creator +media +37 +12 +1 +180\.0 MB +31\.0 MB/s$")
        self.assertRegex(rows[4], r"· tik:example_handle +videos +– +– +– +– +–$")
        self.assertRegex(rows[5],
                         r"⠋ media/1290_2\.mp4 +━{18}╺━{11} +63% +38\.0 MB +12\.0 MB/s$")
        self.assertEqual(rows[7].strip(), "+ 4 more")
        self.assertTrue(rows[8].endswith("held 30 s: 429 from api.example.com (×3)"), rows[8])
        self.assertRegex(rows[9], r"^  42\.0 MB/s · 449 saved · 1 failed +ctrl\+c stop · twice:"
                                  r" stop now$")

    def test_size_and_speed_never_touch(self) -> None:
        going = TargetState("ins:example_user", ("posts",), Phase.DOWNLOADING, found=2, saved=1,
                            bytes=int(3.9 * MB), speed=513.9 * 1024)
        file = FileState("ins:example_user", "posts/1.mp4", 1, int(3.9 * MB), 513.9 * 1024)
        rows = draw(State(targets=(going,), files=(file,)))
        for row in (rows[2], rows[3]):
            with self.subTest(row):
                self.assertRegex(row, r"3\.9 MB +513\.9 KB/s$")

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
        with self.tally.hold("ins", None, "pages", 30, "429 from api.example.com", count=2):
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

    def test_an_idle_screen_writes_nothing(self) -> None:
        tty = _TTY()
        shown = self.shown(lambda: MOCKUP, tty)
        shown.tick()
        drawn = len(tty.getvalue())
        for _ in range(50):
            self.clock.advance(0.1)
            shown.tick()
        self.assertEqual(len(tty.getvalue()) - drawn, 0)

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

    def test_the_screen_reads_nothing_but_the_snapshot(self) -> None:
        tree = ast.parse(Path(screen.__file__).read_text(encoding="utf-8"))
        modules = {node.module for node in ast.walk(tree)
                   if isinstance(node, ast.ImportFrom) and node.module}
        self.assertEqual({m for m in modules if m.startswith("flnd.engine")},
                         {"flnd.engine.events"})

    def test_ctrl_c_mid_redraw_never_waits_on_the_screen(self) -> None:
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
