"""Tests for `flnd.ui.log`: stable keys, single renderer, and the JSONL journal."""

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
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

import flnd
from flnd.engine.events import Failed, Saved
from flnd.plugin import Target
from flnd.testing import fake_clock
from flnd.ui.log import UI, Journal
from flnkit import clock as _time


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
from flnd.ui.log import UI
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


class JournalTest(unittest.TestCase):
    """Run journal recording JSON Lines."""

    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.journal_path = Path(self.temp_dir.name) / "run.jsonl"

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_writes_events_and_log_lines(self) -> None:
        ui = UI()
        buf = io.StringIO()
        with ui.scope(dest=buf, color=False, level="info"):
            with ui.file(self.journal_path) as j:
                self.assertIsInstance(j, Journal)
                j.write({"type": "Start", "command": "download"})
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

        for r in records:
            self.assertIn("at", r)

        log_rec = next(r for r in records if r["type"] == "Log")
        self.assertEqual(log_rec["level"], "warn")
        self.assertIn("rate limit warning", log_rec["message"])
        self.assertEqual(log_rec["fields"], {"source": "ins"})

    def test_writes_paths_and_errors_as_data(self) -> None:
        target = Target("ins:example_user", kind="user")
        error = flnd.LoginError("not logged in", hint="run flnd auth login ins chrome")
        with UI().file(self.journal_path) as j:
            j.write(Saved(target, "posts", "9001", Path("media/9001.jpg"), date(2026, 1, 2), 5))
            j.write(Failed(target, error))

        saved, failed = map(json.loads, self.journal_path.read_text("utf-8").splitlines())
        self.assertEqual(saved["type"], "Saved")
        self.assertEqual(saved["path"], "media/9001.jpg")
        self.assertEqual(saved["target"], {"text": "ins:example_user", "kind": "user"})
        self.assertEqual(failed["error"], {
            "class": "LoginError", "message": "not logged in",
            "hint": "run flnd auth login ins chrome",
        })

    def test_lines_from_many_threads_stay_whole(self) -> None:
        def write(j: Journal, n: int) -> None:
            for i in range(1000):
                j.write({"type": "Log", "thread": n, "i": i, "message": "x" * 200})

        with UI().file(self.journal_path) as j:
            # The journal promises whole lines whatever the handle does, so this
            # handle splits each write; a real one happens to write a short line in one go.
            j._file = _PiecewiseFile(j._file)
            threads = [threading.Thread(target=write, args=(j, n)) for n in range(4)]
            for t in threads:
                t.start()
            for t in threads:
                t.join()

        lines = self.journal_path.read_text("utf-8").splitlines()
        whole = [line for line in lines if re.fullmatch(r'\{"type":\s*"Log",.*"x{200}".*\}', line)]
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

    def test_lines_are_stamped_with_the_wall_clock(self) -> None:
        with UI().file(self.journal_path) as j:
            j.write({"type": "Log", "message": "now"})
            with fake_clock() as clock:
                clock.advance(90)
                j.write({"type": "Log", "message": "fake"})

        real, fake = map(json.loads, self.journal_path.read_text("utf-8").splitlines())
        gap = datetime.now(UTC) - datetime.fromisoformat(real["at"])
        self.assertLess(abs(gap.total_seconds()), 5)
        self.assertEqual(datetime.fromisoformat(fake["at"]),
                         datetime(2026, 1, 1, 0, 1, 30, tzinfo=UTC))

    def test_debug_lines_are_journalled_when_level_hides_them(self) -> None:
        ui = UI()
        buf = io.StringIO()
        with ui.scope(dest=buf, color=False, level="warn"), ui.file(self.journal_path):
            ui.debug("GET {url} -> {file}", url="https://cdn.example.com/1.jpg", file="posts/1.jpg")
        self.assertEqual(buf.getvalue(), "")
        record = json.loads(self.journal_path.read_text("utf-8").splitlines()[0])
        self.assertEqual(record["type"], "Log")
        self.assertEqual(record["level"], "debug")
        self.assertEqual(record["message"], "GET https://cdn.example.com/1.jpg -> posts/1.jpg")
        self.assertEqual(record["fields"], {"url": "https://cdn.example.com/1.jpg",
                                            "file": "posts/1.jpg"})

    def test_request_keeps_query_and_headers_and_scrubs_secrets(self) -> None:
        from types import SimpleNamespace

        ui = UI()
        buf = io.StringIO()
        client = SimpleNamespace(session=SimpleNamespace(
            headers={"User-Agent": "TestAgent/1.0"},
            proxy="https://user:pw@proxy.example.com:8080?k=1",
        ))
        url = "https://user:pw@cdn.example.com/v/1.jpg?oh=abc&oe=def#frag"
        with ui.scope(dest=buf, color=False, level="warn"), ui.file(self.journal_path):
            ui.request(
                client, url, "posts/1.jpg",
                headers={"Referer": "https://www.example.com/", "Cookie": "sessionid=secret"},
                offset=128, etag='"v1"',
            )
        self.assertEqual(buf.getvalue(), "")
        record = json.loads(self.journal_path.read_text("utf-8").splitlines()[0])
        fields = record["fields"]
        self.assertEqual(fields["method"], "GET")
        self.assertEqual(fields["url"], "https://cdn.example.com/v/1.jpg?oh=abc&oe=def")
        self.assertEqual(fields["impersonate"], "chrome")
        self.assertEqual(fields["proxy"], "https://proxy.example.com:8080")
        self.assertEqual(fields["file"], "posts/1.jpg")
        self.assertEqual(fields["headers"], {
            "User-Agent": "TestAgent/1.0",
            "Referer": "https://www.example.com/",
            "Cookie": "…",
            "Range": "bytes=128-",
            "If-Range": '"v1"',
        })
        self.assertNotIn("secret", self.journal_path.read_text("utf-8"))
        self.assertNotIn("pw@", self.journal_path.read_text("utf-8"))


if __name__ == "__main__":
    unittest.main()
