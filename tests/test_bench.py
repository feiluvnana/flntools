"""The benchmark suite still runs against today's code: each group's smallest case, once.

`make bench` takes minutes and is run by hand, so a group whose script no longer matches flnd's
code (a renamed function, a changed store) could fail silently for weeks. This test runs every
group of `tests/bench/run.py` in smoke mode (`FLND_BENCH_SMOKE=1`, see `tests/bench/kit.py`):
each at its smallest size, each timed once, a few seconds in all. Its numbers are thrown away;
only "it ran and reported" is checked. A group may still skip what this machine lacks
(Chromium, ffmpeg), as a full run does.

The groups run 4 at once, each in its own interpreter as in a full run. Every group serves its
data from 127.0.0.1 or fakes the network in-process, so nothing leaves the machine.
"""

from __future__ import annotations

import importlib
import sys
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

BENCH = Path(__file__).resolve().parent / "bench"


def _runner():
    """`tests/bench/run.py`, imported with its folder on the path, as its groups import `kit`."""
    if str(BENCH) not in sys.path:
        sys.path.insert(0, str(BENCH))
    return importlib.import_module("run")


class BenchTest(unittest.TestCase):
    def test_every_bench_module_imports(self):
        _runner()
        # The groups and the hand checks: importing runs no case, but finds a stale import.
        for path in sorted(BENCH.glob("*.py")):
            with self.subTest(module=path.stem):
                importlib.import_module(path.stem)

    def test_every_group_runs_its_smallest_case(self):
        run = _runner()
        with ThreadPoolExecutor(4) as pool:
            reports = dict(zip(run.GROUPS, pool.map(lambda group: run.run_group(group, smoke=True),
                                                   run.GROUPS)))
        for group, report in reports.items():
            with self.subTest(group=group):
                self.assertIsNone(report["failed"], f"{group} failed in smoke mode")
                if not report["skipped"]:
                    self.assertTrue(report["metrics"], f"{group} reported no number")


if __name__ == "__main__":
    unittest.main()
