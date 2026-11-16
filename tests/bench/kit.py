"""The helpers every benchmark group shares: timing, the loopback server, and the report.

A benchmark group is one script in this folder (`startup.py`, `net.py`, …). `run.py` starts each
group in a fresh interpreter, so one group's imports, caches and garbage never slow another. A
group talks to the runner through this module:

1. it makes a `Report` named after itself;
2. it times its work with `best` (or `median_wall` for process starts) and calls `Report.add`
   once per number, which also prints a progress line to stderr for the person watching;
3. it ends with `Report.emit`, which prints one JSON object to stdout, the only thing the runner
   reads; or with `Report.skip` when this machine lacks what it needs (Chromium, ffmpeg).

Rules a change must keep:
- stdout carries the JSON report and nothing else, so progress goes to stderr;
- nothing here touches the network beyond 127.0.0.1, and all data is synthetic;
- every scratch folder is made with `tempfile` and removed when its group ends.
"""

from __future__ import annotations

import json
import statistics
import subprocess
import sys
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent


class Report:
    """The numbers one group measured, sent to the runner as JSON when the group ends.

    Each number is a metric named `<group>.<name>`, such as `disk.fs_write`. Its unit is a
    plain string ("ms", "us", "s", "GiB/s"), and `better` says which way is an improvement, so
    the runner's `--gate` knows whether a change is a regression.

    Args:
        group: the group's name, the file name of its script without `.py`.
    """

    def __init__(self, group: str) -> None:
        self.group = group
        self.metrics: dict[str, dict[str, Any]] = {}
        self.skipped: str | None = None

    def add(self, name: str, value: float, unit: str, *, better: str = "lower") -> None:
        """Record one number and print it to stderr as progress.

        Args:
            name: the metric's name within the group, in snake case.
            value: the measured number, in `unit`.
            unit: "ms", "us" (microseconds), "s" or "GiB/s".
            better: "lower" for a time, "higher" for a throughput.
        """
        self.metrics[f"{self.group}.{name}"] = {"value": round(value, 4), "unit": unit,
                                                "better": better}
        print(f"  {self.group}.{name:40} {value:12.3f} {unit}", file=sys.stderr, flush=True)

    def skip(self, reason: str) -> None:
        """Mark the whole group as skipped, with the reason the runner prints.

        Numbers already added stay in the report: a group may skip only its optional part.

        Args:
            reason: what this machine lacks, such as "ffmpeg is not on PATH".
        """
        self.skipped = reason
        print(f"  {self.group}: skipped: {reason}", file=sys.stderr, flush=True)

    def emit(self) -> None:
        """Print the report as one line of JSON on stdout, for `run.py` to read."""
        print(json.dumps({"metrics": self.metrics, "skipped": self.skipped}), flush=True)


def best(work: Callable[[], object], *, runs: int = 3,
         before: Callable[[], object] | None = None) -> float:
    """The fastest of several runs of `work`, in seconds.

    The minimum, not the mean, because noise on a quiet machine only ever adds time: the
    fastest run is the closest to what the code itself costs.

    Args:
        work: the code to time, called with no argument.
        runs: how many times to run it.
        before: untimed set-up called before each run, such as emptying the output folder.

    Returns:
        The shortest wall time of one run, in seconds.
    """
    fastest = float("inf")
    for _ in range(runs):
        if before is not None:
            before()
        start = time.perf_counter()
        work()
        fastest = min(fastest, time.perf_counter() - start)
    return fastest


def median_wall(argv: list[str], *, runs: int = 15) -> float:
    """The median wall time of a whole process, start to exit, in seconds.

    The median, not the minimum, for process starts: the first run warms the OS file cache, and
    the median of 15 is steady from one session to the next where the minimum is lucky.

    Args:
        argv: the command, as an argument list (never a shell string).
        runs: how many times to start it.

    Returns:
        The median wall time, in seconds.
    """
    times = []
    for _ in range(runs):
        start = time.perf_counter()
        subprocess.run(argv, capture_output=True, check=True)
        times.append(time.perf_counter() - start)
    return statistics.median(times)


@contextmanager
def serve(folder: Path) -> Iterator[str]:
    """Serve `folder` over HTTP on 127.0.0.1 from a child process, and yield its base URL.

    The server is `server.py`, run in its own interpreter so its work never competes with the
    client's for the GIL. It binds port 0, so the OS picks a free port, and prints the port as
    its first line, which is read here. The server is stopped when the block ends.

    Args:
        folder: the files to serve; `/name` answers `folder/name`.

    Yields:
        The base URL, such as "http://127.0.0.1:53124", with no trailing slash.
    """
    server = subprocess.Popen([sys.executable, str(HERE / "server.py"), str(folder)],
                              stdout=subprocess.PIPE, text=True)
    try:
        assert server.stdout is not None
        port = int(server.stdout.readline())
        yield f"http://127.0.0.1:{port}"
    finally:
        server.terminate()
        server.wait()
