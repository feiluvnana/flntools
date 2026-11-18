"""Benchmark group `ui`: what the run's screen costs, and how fast a full-screen view opens.

The screen is today's renderer, `src/ui/log.py`'s `UI` drawing a `job.Board` (one text row per
target and category) and a byte bar per file in flight. Its output goes to a counting sink that
says it is a UTF-8 terminal, so the live block draws as it would on a real one.

What it times:
1. `overhead`: 100,000 file events through a `flnd.engine.run.Run`, each a `Board.count` that moves
   a row and sends a `Progress` event to the reader, with the screen on versus off, as the
   percentage the screen adds;
2. `idle_bytes`: the bytes the screen writes in 5 s while nothing changes, with 20 targets and
   6 files on it;
3. `frame`: one draw of that same screen, 120 columns by 60 lines;
4. `open`: a fresh interpreter importing textual, the floor under a full-screen list's first
   frame.
"""

from __future__ import annotations

import sys
import threading
import time
from typing import Any

import kit

from flnd.engine import job
from flnd.engine.run import Run
from flnd.source import Target
from flnd.ui.log import UI, Group, ui

EVENTS = kit.size(100_000, 2_000)
TARGETS, FILES = 20, 6
# Seconds the still screen is watched for.
IDLE = 0.2 if kit.SMOKE else 5.0
FRAMES = kit.size(200, 5)
WIDTH, HEIGHT = 120, 60


class Sink:
    """A stand-in for stderr that counts the bytes written to it and keeps none.

    Args:
        tty: whether it says it is a terminal, which turns the live screen on.
    """

    encoding = "utf-8"

    def __init__(self, *, tty: bool = True) -> None:
        self.bytes = 0
        self.tty = tty
        self._lock = threading.Lock()

    def write(self, text: str) -> int:
        with self._lock:
            self.bytes += len(text.encode())
        return len(text)

    def flush(self) -> None:
        pass

    def isatty(self) -> bool:
        return self.tty


def targets() -> list[Target]:
    """The run's targets, each named after the example handle."""
    return [Target(f"bnc:example_user_{n}", kind="user") for n in range(TARGETS)]


def board(rows: Any, emit: Any = None) -> tuple[job.Board, list[Target]]:
    """A board with one open `posts` row per target, as a run's listings start them."""
    chosen = targets()
    drawn = job.Board(rows, subject_width=max(len(str(t)) for t in chosen), label_width=5,
                      emit=emit)
    for target in chosen:
        drawn.start(target, "posts")
        drawn.count(target, "posts", found=40, queued=30, saved=10)
    return drawn, chosen


def screen(owner: UI) -> None:
    """Fill `owner` with the 20-target, 6-file screen: a board and six byte bars in flight."""
    board(Group(owner))
    for n in range(FILES):
        bar = owner.item(f"file{n}", f"example_user_{n}/posts/2026-01-02_p{n}.mp4",
                         total=64 << 20, unit="bytes")
        bar.count = (n + 1) * (8 << 20)


def run_events(*, on: bool) -> float:
    """Time `EVENTS` file events through a `Run`, the screen on or off, in seconds."""
    def work(emit: Any, stop: Any) -> None:
        with ui.group(drawn=ui._progress_enabled()) as rows:
            drawn, chosen = board(rows, emit)
            for n in range(EVENTS):
                drawn.count(chosen[n % TARGETS], "posts", saved=1)
            for target in chosen:
                drawn.listed(target, "posts")

    def read() -> None:
        with ui.scope(dest=Sink(tty=on), color=on, width=WIDTH):
            for _ in Run(work, name="bench"):
                pass

    return kit.best(read)


def overhead(report: kit.Report) -> None:
    """Time step 1: the screen's share of a run's per-event cost."""
    off = run_events(on=False)
    on = run_events(on=True)
    report.add("overhead", (on / off - 1) * 100, "%")


def idle_bytes(report: kit.Report) -> None:
    """Time step 2: what a still screen writes in `IDLE` seconds."""
    sink = Sink()
    owner = UI()
    with owner.scope(dest=sink, color=True, width=WIDTH):
        owner.raw.height = HEIGHT
        screen(owner)
        # The screen's own first draws are not idle.
        time.sleep(0.1)
        sink.bytes = 0
        time.sleep(IDLE)
        written = sink.bytes
        owner._wake.set()
    report.add("idle_bytes", written, "B")


def frame(report: kit.Report) -> None:
    """Time step 3: one draw of the whole screen, with the ticker stopped."""
    owner = UI()
    with owner.scope(dest=Sink(), color=True, width=WIDTH):
        owner.raw.height = HEIGHT
        screen(owner)
        owner._wake.set()
        owner._ticker.join()

        def draw() -> None:
            with owner._lock:
                for _ in range(FRAMES):
                    owner._redraw(force=True)

        report.add("frame", kit.best(draw, runs=5) / FRAMES * 1e3, "ms")


def main() -> None:
    """Run every step and emit the report."""
    report = kit.Report("ui")
    overhead(report)
    idle_bytes(report)
    frame(report)
    report.add("open", kit.median_wall([sys.executable, "-c", "import textual"]) * 1e3, "ms")
    report.emit()


if __name__ == "__main__":
    main()
