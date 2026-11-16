"""Benchmark group `ui`: the cost of the full-screen view's frames and of the live stats.

What it times:
1. `frame_1000`: one frame of the full screen (`flnd._screen.Screen`) rendered by Rich to a
   true-colour terminal of 200 × 60, with 1,000 listing rows and 1,000 files in flight, as the
   view draws it 10 times a second between two samples (the rows already picked);
2. `frame_1000_new`: the same frame right after a new `Stats` arrived, so it also picks and
   sorts the 1,000 rows, which happens once a second;
3. `frame_80x24`: the 1,000-row frame on an 80 × 24 terminal;
4. `sample`: one live-stats sample (`flnd._stats.Meter.poll`) of a busy run: 1,000 listing
   rows, 200 transfers, 16 hosts, and psutil reading flnd and 12 child processes standing in
   for Chromium's tree; `sample_listing`, one that also lists flnd's child processes (every
   `CHILDREN` seconds); and `sample_core`, their cost at one sample a second, as a share of
   one core (the gate: under 1%).

Every number is the best of several runs. Nothing touches the network.
"""

from __future__ import annotations

import io
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import kit

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from rich.console import Console  # noqa: E402
from rich.theme import Theme  # noqa: E402

from flnd import _net, _stats  # noqa: E402
from flnd import _time  # noqa: E402
from flnd._events import Host, Listing, Pause, Stats, System, Transfer, Transfers  # noqa: E402
from flnd._screen import Screen  # noqa: E402
from flnd._ui import PALETTE  # noqa: E402
from flnd.source import Target  # noqa: E402

ROWS = 1000
STATES = ("listing", "finishing", "done", "caught up", "failed")


def big_stats() -> Stats:
    """A `Stats` with `ROWS` listing rows and `ROWS` transfers."""
    listing = tuple(Listing(Target(f"ins:example_user{n}", kind="user"), "posts", pages=n % 40,
                            posts=n, found=n + 10, saved=n, state=STATES[n % len(STATES)])
                    for n in range(ROWS))
    files = tuple(Transfer(f"example_user{n}/2026-03-01_{n}.mp4", "cdn.example.com",
                           done=n * 1000, total=n * 2000 + 1, speed=1e6) for n in range(ROWS))
    return Stats(elapsed=60.0, saved=500, bytes=1 << 30, speed=8e6, eta=120.0, listing=listing,
                 transfers=Transfers(running=ROWS, queued=5000, files=files,
                                     hosts=tuple(Host(f"cdn{n}.example.com", 8, 8, 2, 32)
                                                 for n in range(16))),
                 pauses=(Pause("ins", "www.example.com", 120.0, "Retry-After"),),
                 system=System(tabs=4, cap=6, browser=600 << 20, ram=200 << 20, cpu=40.0,
                               ffmpeg=1, pillow=2, disk=100 << 30))


def screen(width: int, height: int) -> tuple[Screen, Console]:
    console = Console(file=io.StringIO(), width=width, height=height, force_terminal=True,
                      color_system="truecolor", theme=Theme(PALETTE))
    view = Screen("sync", console=console)
    for n in range(50):
        view.log.write(f"! www.example.com: rate limited; waiting {n}s\n")
    return view, console


def frames(report: kit.Report) -> None:
    stats = big_stats()
    view, console = screen(200, 60)
    view.show(stats)

    def draw() -> None:
        console.file.seek(0)
        console.file.truncate()
        console.print(view)

    draw()
    report.add("frame_1000", kit.best(draw, runs=50) * 1e3, "ms")

    def new() -> None:
        view.show(stats)
        draw()

    report.add("frame_1000_new", kit.best(new, runs=50) * 1e3, "ms")
    small, small_console = screen(80, 24)
    small.show(stats)

    def draw_small() -> None:
        small_console.file.seek(0)
        small_console.file.truncate()
        small_console.print(small)

    report.add("frame_80x24", kit.best(draw_small, runs=50) * 1e3, "ms")


class _Board:
    def __init__(self, rows: list[Listing]) -> None:
        self.rows = rows

    def listing(self) -> list[Listing]:
        return list(self.rows)

    def queued(self) -> int:
        return 5000


class _Row:
    def __init__(self, n: int) -> None:
        self.label, self.host, self.resumed = f"example_user/{n}.mp4", "cdn.example.com", 0
        self.bar = SimpleNamespace(count=float(n), total=float(n * 2 + 1))


def sampling(report: kit.Report) -> None:
    fake = _time.Fake()
    children = [subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
                for _ in range(12)]
    try:
        with _time.use(fake):
            meter = _stats.Meter()
            meter.start()
            limits = _net.Limits(paced=lambda host: host == "www.example.com", rate=1.0)
            for n in range(16):
                limits._caps[f"cdn{n}.example.com"] = _net.Cap(8, most=32)
            meter.add(_stats.SOURCE, SimpleNamespace(source=SimpleNamespace(code="ins"),
                                                     files=SimpleNamespace(limits=limits)))
            meter.add(_stats.BOARD, _Board(list(big_stats().listing)))
            for n in range(200):
                meter.add(_stats.TRANSFER, _Row(n))
            meter.add(_stats.HOME, Path.cwd())
            meter.add(_stats.CHAIN, SimpleNamespace(jobs={"photo": (2, 10), "video": (1, 3)}))

            def one() -> None:
                fake.advance(1.0)
                assert meter.poll(saved=1, skipped=0, failed=0, bytes=1) is not None

            def relisted() -> None:
                meter._children = None

            one()
            cheap = kit.best(one, runs=20)
            listing = kit.best(one, runs=20, before=relisted)
    finally:
        for child in children:
            child.kill()
            child.wait()
    # The children are listed once every `CHILDREN` seconds, and read again in between.
    every = int(_stats.CHILDREN / _stats.EVERY)
    cost = (listing + (every - 1) * cheap) / every
    report.add("sample", cheap * 1e3, "ms")
    report.add("sample_listing", listing * 1e3, "ms")
    # One sample a second, a listing every `CHILDREN`: the share of one core, in percent.
    report.add("sample_core", cost / _stats.EVERY * 100, "%")


def main() -> None:
    report = kit.Report("ui")
    frames(report)
    sampling(report)
    report.emit()


if __name__ == "__main__":
    main()
