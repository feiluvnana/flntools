"""Tests for `flncore.log`: stable keys, single renderer, and the JSONL journal."""

from __future__ import annotations

import io
import json
import re
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from datetime import date
from pathlib import Path
from typing import Any

import flnd
from flncore import clock as _time
from flncore.log import REDRAW, UI, Journal, State
from flnd._events import Failed, Saved
from flnd.source import Target


class _TTY(io.StringIO):
    """Text buffer simulating a terminal."""

    def isatty(self) -> bool:
        return True


class _PiecewiseFile:
    """A text file that writes in four pieces and lets other threads run between them."""

    def __init__(self, file: Any) -> None:
        self._inner = file

    def write(self, text: str) -> int:
        step = max(1, len(text) // 4)
        for start in range(0, len(text), step):
            self._inner.write(text[start:start + step])
            time.sleep(0)
        return len(text)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)


class _GateClock:
    """A clock whose reads wait until the test has closed the journal."""

    def __init__(self) -> None:
        self.entered = threading.Event()
        self.closed = threading.Event()

    def _read(self) -> float:
        self.entered.set()
        self.closed.wait(2)
        return time.time()

    now = property(_read)
    wall = property(_read)

    def sleep(self, seconds: float, *, stop: Any = None) -> bool:
        return True


# Raises SIGINT inside the handle's write; the handler logs, which writes to the journal.
_SIGNAL_PROBE = """
import io, signal, sys
from flncore.log import UI
ui = UI()
with ui.scope(dest=io.StringIO(), color=False, level="info"), ui.file(sys.argv[1]) as j:
    signal.signal(signal.SIGINT, lambda *_: ui.warn("press Ctrl-C again to quit now"))
    real = j._file
    class Handle:
        def write(self, text):
            real.write(text)
            if not getattr(Handle, "fired", False):
                Handle.fired = True
                signal.raise_signal(signal.SIGINT)
            return len(text)
        def __getattr__(self, name):
            return getattr(real, name)
    j._file = Handle()
    j.write({"type": "Log", "message": "event"})
"""


class ItemTest(unittest.TestCase):
    """Progress items with stable keys and states."""

    def test_item_lifecycle(self) -> None:
        ui = UI()
        buf = io.StringIO()
        with ui.scope(dest=buf, color=False, level="info"):
            it = ui.item("file1", "Downloading file1", total=100)
            self.assertEqual(it.key, "file1")
            self.assertEqual(it.label, "Downloading file1")
            self.assertEqual(it.state, State.ACTIVE)
            self.assertEqual(it.count, 0)
            self.assertEqual(it.total, 100)

            it.advance(25)
            self.assertEqual(it.count, 25)

            it.note("merging")
            self.assertEqual(it._note, "merging")

            it.ok("all done")
            self.assertEqual(it.state, State.DONE)
            self.assertEqual(it._note, "all done")

    def test_item_skip_and_fail(self) -> None:
        ui = UI()
        buf = io.StringIO()
        with ui.scope(dest=buf, color=False, level="info"):
            it1 = ui.item("f1", "file 1")
            it1.skip("already saved")
            self.assertEqual(it1.state, State.SKIPPED)

            it2 = ui.item("f2", "file 2")
            it2.fail("disk full")
            self.assertEqual(it2.state, State.FAILED)

    def test_item_context_manager_success(self) -> None:
        ui = UI()
        buf = io.StringIO()
        with ui.scope(dest=buf, color=False, level="info"):
            with ui.item("k1", "item 1") as it:
                it.advance(10)
            self.assertEqual(it.state, State.DONE)

    def test_item_context_manager_exception(self) -> None:
        ui = UI()
        buf = io.StringIO()
        with ui.scope(dest=buf, color=False, level="info"):
            with self.assertRaises(ValueError):
                with ui.item("k2", "item 2") as it:
                    raise ValueError("corrupted chunk")
            self.assertEqual(it.state, State.FAILED)
            self.assertIn("corrupted chunk", it._note)


class JournalTest(unittest.TestCase):
    """Run journal recording JSON Lines without Progress events."""

    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.journal_path = Path(self.temp_dir.name) / "run.jsonl"

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_writes_events_and_skips_progress(self) -> None:
        ui = UI()
        buf = io.StringIO()
        with ui.scope(dest=buf, color=False, level="info"):
            with ui.file(self.journal_path) as j:
                self.assertIsInstance(j, Journal)
                j.write({"type": "Start", "command": "download"})
                j.write({"type": "Progress", "count": 10, "total": 100})
                j.write({"type": "Saved", "target": "ins:example_user", "size": 1024})
                ui.warn("{source}: rate limit warning", source="ins")
                j.write({"type": "Report", "saved": 1, "failed": 0})

        self.assertTrue(self.journal_path.is_file())
        lines = self.journal_path.read_text(encoding="utf-8").splitlines()
        self.assertEqual(len(lines), 4)

        records = [json.loads(line) for line in lines]
        types = [r["type"] for r in records]
        self.assertIn("Start", types)
        self.assertIn("Saved", types)
        self.assertIn("Log", types)
        self.assertIn("Report", types)
        self.assertNotIn("Progress", types)

        for r in records:
            self.assertIn("at", r)

        log_rec = next(r for r in records if r["type"] == "Log")
        self.assertEqual(log_rec["level"], "warn")
        self.assertIn("rate limit warning", log_rec["message"])
        self.assertEqual(log_rec["fields"], {"source": "ins"})

    def test_writes_paths_and_errors_as_data(self) -> None:
        target = Target("ins:example_user", kind="user")
        error = flnd.LoginError("not logged in", hint="run flnd login ins chrome")
        with UI().file(self.journal_path) as j:
            j.write(Saved(target, "posts", "9001", Path("media/9001.jpg"), date(2026, 1, 2), 5))
            j.write(Failed(target, error))

        saved, failed = map(json.loads, self.journal_path.read_text("utf-8").splitlines())
        self.assertEqual(saved["type"], "Saved")
        self.assertEqual(saved["path"], "media/9001.jpg")
        self.assertEqual(saved["target"]["value"], "example_user")
        self.assertEqual(failed["error"], {
            "class": "LoginError", "message": "not logged in",
            "hint": "run flnd login ins chrome",
        })

    def test_lines_from_many_threads_stay_whole(self) -> None:
        def write(j: Journal, n: int) -> None:
            for i in range(1000):
                j.write({"type": "Log", "thread": n, "i": i, "message": "x" * 200})

        with UI().file(self.journal_path) as j:
            # The journal promises whole lines whatever the handle does (PLAN.md 6.1), so this
            # handle splits each write; a real one happens to write a short line in one go.
            j._file = _PiecewiseFile(j._file)
            threads = [threading.Thread(target=write, args=(j, n)) for n in range(4)]
            for t in threads:
                t.start()
            for t in threads:
                t.join()

        lines = self.journal_path.read_text("utf-8").splitlines()
        whole = [line for line in lines if re.fullmatch(r'\{"type": "Log", .*"x{200}".*\}', line)]
        self.assertEqual(len(whole), 4000)
        self.assertEqual(len(lines), 4000)
        records = [json.loads(line) for line in whole]
        self.assertEqual({(r["thread"], r["i"]) for r in records},
                         {(n, i) for n in range(4) for i in range(1000)})

    def test_a_write_racing_the_close_is_dropped(self) -> None:
        clock = _GateClock()
        errors: list[BaseException] = []

        def write(j: Journal) -> None:
            with _time.use(clock):
                try:
                    j.write({"type": "Log", "message": "late"})
                except BaseException as e:
                    errors.append(e)

        j = UI().file(self.journal_path).__enter__()
        writer = threading.Thread(target=write, args=(j,))
        writer.start()
        self.assertTrue(clock.entered.wait(2))
        j.__exit__(None, None, None)
        clock.closed.set()
        writer.join(2)
        self.assertEqual(errors, [])

    def test_a_signal_handler_can_log_while_a_line_is_written(self) -> None:
        # A second Ctrl-C logs from the main thread while it may be inside `write`.
        try:
            done = subprocess.run(
                [sys.executable, "-c", _SIGNAL_PROBE, str(self.journal_path)],
                capture_output=True, text=True, timeout=2,
            )
        except subprocess.TimeoutExpired:
            self.fail("the handler's log line waited forever on the journal's lock")
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual(len(self.journal_path.read_text("utf-8").splitlines()), 2)


class RendererTest(unittest.TestCase):
    """Single renderer: terminal capping, no line wrapping, live flag."""

    def test_live_property(self) -> None:
        ui = UI()
        buf_plain = io.StringIO()
        with ui.scope(dest=buf_plain, color=False, level="info"):
            self.assertFalse(ui.live)

        buf_tty = _TTY()
        with ui.scope(dest=buf_tty, color=False, level="info"):
            self.assertTrue(ui.live)

        with ui.scope(dest=buf_tty, color=False, level="quiet"):
            self.assertFalse(ui.live)

    def test_lines_never_wrap(self) -> None:
        ui = UI()
        buf = _TTY()
        width = 40
        with ui.scope(dest=buf, color=False, width=width, level="info"):
            long_name = "path/to/very/deeply/nested/target/subfolder/video_long_name_2026.mp4"
            with ui.item("long1", long_name, total=100) as it:
                it.advance(50)
            buf.flush()

        for line in buf.getvalue().splitlines():
            # Stripping ANSI codes to measure rendered width
            clean = re.sub(r"\x1b\[[0-9;]*[a-zA-Z]", "", line)
            self.assertLessEqual(len(clean), width)

    def test_live_region_capped(self) -> None:
        ui = UI()
        buf = _TTY()
        height = 6
        width = 80
        with ui.scope(dest=buf, color=False, width=width, level="info"):
            ui._console._height = height
            items = []
            for i in range(10):
                it = ui.item(f"k{i}", f"item-{i}", total=100)
                items.append(it)
            buf.flush()
            out = buf.getvalue()
            self.assertIn("more", out)
            for it in items:
                it.ok()

    def test_the_ticker_redraws_active_rows_without_events(self) -> None:
        ui = UI()
        buf = _TTY()
        with ui.scope(dest=buf, color=False, width=80, level="info"):
            it = ui.item("tick", "tick-row", unit="none")
            time.sleep(REDRAW * 4)
            # No advance and no note since the item opened: the ticker's redraws are the
            # only updates since the first.
            self.assertGreaterEqual(buf.getvalue().count("tick-row"), 2)
            it.ok()


if __name__ == "__main__":
    unittest.main()
