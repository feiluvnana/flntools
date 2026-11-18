"""Benchmark group `ui`: what the run's screen costs, and how fast a full-screen view opens.

The screen is `src/ui/screen.py`'s `Screen`, drawing the run's snapshot (`run.state`, made by
`flnd.engine.run.Tally`) of 20 targets and 6 files in flight. Its output goes to a counting sink
that says it is a UTF-8 terminal, so the block draws as it would on a real one.

What it times:
1. `overhead`: 100,000 file events through a `flnd.engine.run.Run`, each a `Tally.count` and
   an event to the reader, with the screen on versus off, as the percentage the screen adds;
2. `idle_bytes`: the bytes the screen writes in 5 s while nothing changes;
3. `frame`: one draw of that screen, 120 columns by 60 lines, from a new snapshot;
4. `open`: from a fresh interpreter's start to the first frame of `target list`'s list
   (`src/ui/lists.py`, Textual inline) in a pseudo-terminal; P17's goal is 250 ms.
"""

from __future__ import annotations

import os
import select
import statistics
import subprocess
import sys
import threading
import time
from contextlib import ExitStack
from typing import Any

import kit

from flnd.engine import run as job
from flnd.engine.events import Skipped, State
from flnd.engine.run import Run
from flnd.plugin import Target
from flnd.ui.screen import Screen

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


def tally(emit: Any = None) -> tuple[job.Tally, list[Target]]:
    """A tally with one listing per target and `FILES` files in flight, as a run makes it."""
    counted = job.Tally(emit)
    chosen = targets()
    for target in chosen:
        counted.start(target, "posts")
        counted.count(target, "posts", found=40, queued=30, saved=10)
    for n in range(FILES):
        flight = counted.fly(f"file{n}", chosen[n], f"posts/2026-01-02_p{n}.mp4")
        flight.size(64 << 20)
        flight.advance((n + 1) * (8 << 20))
    return counted, chosen


def run_events(*, on: bool) -> float:
    """Time `EVENTS` file events through a `Run`, the screen on or off, in seconds."""
    async def work(emit: Any, stop: Any) -> None:
        counted, chosen = tally(emit)
        async with counted.publishing():
            for n in range(EVENTS):
                target = chosen[n % TARGETS]
                counted.count(target, "posts", skipped=1)
                await emit(Skipped(target, "posts", str(n)))

    def read() -> None:
        run = Run(work, name="bench")
        with Screen(lambda: run.state, command="bench", stream=Sink(tty=on), width=WIDTH,
                    height=HEIGHT):
            for _ in run:
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
    newest = [State()]
    counted, _ = tally(lambda state: newest.__setitem__(0, state))
    counted.publish()
    with ExitStack() as stack:
        stack.enter_context(Screen(lambda: newest[0], command="bench", stream=sink,
                                   width=WIDTH, height=HEIGHT))
        # The screen's own first draw is not idle.
        time.sleep(0.3)
        sink.bytes = 0
        time.sleep(IDLE)
        written = sink.bytes
    report.add("idle_bytes", written, "B")


def frame(report: kit.Report) -> None:
    """Time step 3: one draw of the whole screen, each from a newer snapshot."""
    counted, _ = tally()
    states = [counted.publish(force=True) for _ in range(FRAMES)]
    at = iter(states)
    screen = Screen(lambda: next(at), command="bench", stream=Sink(), width=WIDTH,
                    height=HEIGHT)
    screen.__enter__()
    # The thread would draw too; the draws timed here are the bench's own.
    screen._done.set()
    screen._thread.join()

    def draw() -> None:
        nonlocal at
        at = iter(states)
        screen._drawn = -1
        for _ in range(FRAMES):
            screen.tick()

    report.add("frame", kit.best(draw, runs=5) / FRAMES * 1e3, "ms")
    screen._read = lambda: states[-1]
    screen.__exit__(None, None, None)


LIST = """
from flnd.ui import lists
rows = [lists.TargetRow(f"ins:example_user_{n}", True, ("posts",), n, n << 20, 60.0)
        for n in range(3)]
lists.targets(lambda: rows, follow=print, retry=print, add=print, delete=print)
"""


def first_frame() -> float:
    """Seconds from starting the list's process to its title on the terminal; it is then killed."""
    import pty

    main, child = pty.openpty()
    start = time.perf_counter()
    env = {**os.environ, "TERM": "xterm-256color", "COLUMNS": "100", "LINES": "30"}
    process = subprocess.Popen([sys.executable, "-c", LIST], stdin=child, stdout=child,
                               stderr=child, env=env)
    os.close(child)
    seen, took = b"", float("inf")
    try:
        while select.select([main], [], [], 10)[0]:
            seen += os.read(main, 65536)
            if b"Targets" in seen:
                took = time.perf_counter() - start
                break
    finally:
        process.kill()
        process.wait()
        os.close(main)
    return took


def open_list(report: kit.Report) -> None:
    """Time step 4: the median of 15 starts, as `kit.median_wall` takes for a process."""
    if os.name == "nt":
        report.skip("no pseudo-terminal on Windows")
        return
    took = [first_frame() for _ in range(1 if kit.SMOKE else 15)]
    report.add("open", statistics.median(took) * 1e3, "ms")


def main() -> None:
    """Run every step and emit the report."""
    report = kit.Report("ui")
    overhead(report)
    idle_bytes(report)
    frame(report)
    open_list(report)
    report.emit()


if __name__ == "__main__":
    main()
