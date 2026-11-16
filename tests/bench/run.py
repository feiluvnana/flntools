"""The benchmark runner behind `make bench`: runs the groups, prints a table, and gates.

    make bench                                # every group
    make bench ARGS="net disk"                # only these groups
    make bench ARGS=--gate                    # fail on a regression against gates.json
    make bench ARGS="db --gate --tolerance 5"
    make bench ARGS=--record                  # store this run as the new baseline

The Makefile runs this file inside the project environment (`uv run python tests/bench/run.py …`).
The suite is offline: a loopback server, synthetic data and scratch folders. It is never part of
`make check`, because timings are noisy and the suite takes minutes; `check` guards performance
with counts instead.

How a run goes:
1. Each group named on the command line (all of `GROUPS` by default) runs as its own script in
   a fresh interpreter, so one group's imports and memory never slow the next. A group prints
   progress to stderr, which passes straight through, and one JSON report on stdout (see
   `kit.py`).
2. The numbers are written to `tests/bench/last.json` (ignored by git), with the date, the
   commit, the Python version and the machine.
3. A table compares each number with its baseline and its target in `tests/bench/gates.json`.
4. `--gate` exits 1 when any number is worse than its baseline by more than the tolerance
   (15% by default): run it by hand before a performance commit.
5. `--record` writes this run's numbers into `gates.json` as the new baselines, keeping every
   target. Record after a performance commit lands, so the next gate compares against it.

A group that fails (a crash, a wrong file count) is reported and makes the run exit 1. A group
that is skipped (no Chromium, no ffmpeg) is reported and doesn't.

`gates.json` holds, per metric: its unit, which way is better, the baseline measured on the
machine named in its `recorded` block, and, where a commit of the rebuild is gated on it, the
target and that commit's title. Baselines are only comparable on the machine that recorded
them: on another machine, run `--record` first, then compare later runs to that.
"""

from __future__ import annotations

import argparse
import datetime
import json
import os
import platform
import subprocess
import sys
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent.parent
GATES = HERE / "gates.json"
LAST = HERE / "last.json"
# Every group, in the order a full run takes them: quick ones first, the browser late.
GROUPS = ("startup", "net", "disk", "db", "walk", "cpu", "e2e", "browser", "media")
TOLERANCE = 15.0


def parse(argv: list[str]) -> argparse.Namespace:
    """Read the command line.

    Args:
        argv: the arguments after the script's name.

    Returns:
        The groups to run (all when none are named), and the flags.
    """
    parser = argparse.ArgumentParser(prog="make bench", description="Run flnd's benchmarks.")
    parser.add_argument("groups", nargs="*", metavar="group",
                        help=f"groups to run, of: {', '.join(GROUPS)} (default: all)")
    parser.add_argument("--gate", action="store_true",
                        help="exit 1 when a number is worse than its baseline beyond the tolerance")
    parser.add_argument("--tolerance", type=float, default=TOLERANCE, metavar="PCT",
                        help="how much worse than the baseline still passes, in percent "
                             f"(default: {TOLERANCE:g})")
    parser.add_argument("--record", action="store_true",
                        help="write this run's numbers into gates.json as the new baselines")
    args = parser.parse_args(argv)
    unknown = [group for group in args.groups if group not in GROUPS]
    if unknown:
        parser.error(f"unknown group {', '.join(unknown)}; the groups are {', '.join(GROUPS)}")
    args.groups = args.groups or list(GROUPS)
    return args


def run_group(group: str) -> dict[str, Any]:
    """Run one group's script in a fresh interpreter and return its report.

    Args:
        group: the group's name, the file `<group>.py` in this folder.

    Returns:
        The report: `metrics` and `skipped`, as `kit.Report.emit` printed them, and `failed`,
        a reason when the script exited with an error or printed no report.
    """
    print(f"{group}:", file=sys.stderr, flush=True)
    done = subprocess.run([sys.executable, str(HERE / f"{group}.py")], cwd=ROOT,
                          stdout=subprocess.PIPE, text=True)
    lines = done.stdout.strip().splitlines()
    if done.returncode != 0 or not lines:
        return {"metrics": {}, "skipped": None, "failed": f"exited with {done.returncode}"}
    report = json.loads(lines[-1])
    report["failed"] = None
    return report


def machine() -> dict[str, Any]:
    """The machine and Python the numbers were measured on, for `last.json` and `gates.json`."""
    found = {"platform": platform.platform(), "cpus": os.cpu_count(),
             "python": platform.python_version()}
    # macOS names its chip only through sysctl; elsewhere platform.processor() is enough.
    if sys.platform == "darwin":
        chip = subprocess.run(["sysctl", "-n", "machdep.cpu.brand_string"],
                              capture_output=True, text=True)
        found["cpu"] = chip.stdout.strip()
    else:
        found["cpu"] = platform.processor()
    return found


def commit() -> str:
    """The short hash of the checked-out commit, or "" outside a git checkout."""
    done = subprocess.run(["git", "rev-parse", "--short", "HEAD"], cwd=ROOT,
                          capture_output=True, text=True)
    return done.stdout.strip() if done.returncode == 0 else ""


def worse_by(value: float, baseline: float, better: str) -> float:
    """How much worse `value` is than `baseline`, in percent; negative when it is better.

    Args:
        value: this run's number.
        baseline: the recorded number, above zero.
        better: "lower" for a time, "higher" for a throughput.
    """
    ratio = value / baseline if better == "lower" else baseline / value
    return (ratio - 1) * 100


def meets(value: float, target: float, better: str) -> bool:
    """Whether `value` reaches `target`, in the direction `better` says."""
    return value <= target if better == "lower" else value >= target


def table(metrics: dict[str, dict[str, Any]], gates: dict[str, dict[str, Any]],
          tolerance: float) -> list[str]:
    """Print one line per metric, and return the metrics worse than the tolerance allows.

    A line shows the number, the baseline, the change, and the target with whether it is met.
    "-" stands for a baseline or target that `gates.json` doesn't have.

    Args:
        metrics: this run's numbers, by metric name.
        gates: `gates.json`'s metrics.
        tolerance: the percent worse than the baseline that still passes.

    Returns:
        The names of the regressions, in table order.
    """
    regressions = []
    print(f"\n{'metric':36} {'value':>16} {'baseline':>11} {'change':>13} {'target':>11}")
    for name, measured in metrics.items():
        gate = gates.get(name, {})
        value, unit, better = measured["value"], measured["unit"], measured["better"]
        baseline, target = gate.get("baseline"), gate.get("target")
        change = "-"
        if baseline:
            worse = worse_by(value, baseline, better)
            change = f"{abs(worse):.0f}% {'worse' if worse > 0 else 'better'}"
            if worse > tolerance:
                regressions.append(name)
                change += " !"
        goal = "-"
        if target is not None:
            goal = f"{target:g} {'met' if meets(value, target, better) else 'not met'}"
        shown = "-" if baseline is None else f"{baseline:g}"
        print(f"{name:36} {value:>10.3f} {unit:5} {shown:>11} {change:>13} {goal:>11}")
    return regressions


def record(metrics: dict[str, dict[str, Any]], gates: dict[str, Any],
           about: dict[str, Any]) -> None:
    """Write this run's numbers into `gates.json` as baselines, keeping every target.

    A metric this run didn't measure keeps its old baseline, so recording one group never
    erases another's.

    Args:
        metrics: this run's numbers.
        gates: the whole of `gates.json`, updated in place and written back.
        about: the date, commit and machine of this run.
    """
    for name, measured in metrics.items():
        entry = gates["metrics"].setdefault(name, {})
        entry.update(unit=measured["unit"], better=measured["better"], baseline=measured["value"])
    # The file is read by people too: metrics in group order, and each entry's fields in one
    # order, the number first and what it is gated on after.
    fields = ("unit", "better", "baseline", "target", "gate", "note")
    by_group = sorted(gates["metrics"].items(),
                      key=lambda item: GROUPS.index(item[0].split(".")[0]))
    gates["metrics"] = {name: {field: entry[field] for field in fields if field in entry}
                        for name, entry in by_group}
    gates["recorded"] = about
    GATES.write_text(json.dumps(gates, indent=2, ensure_ascii=False) + "\n", encoding="utf-8",
                     newline="\n")


def main(argv: list[str]) -> int:
    """Run the benchmarks the command line names, and return the exit code.

    Args:
        argv: the arguments after the script's name.

    Returns:
        0 when every group ran and, with `--gate`, nothing regressed; 1 otherwise.
    """
    args = parse(argv)
    metrics: dict[str, dict[str, Any]] = {}
    skipped, failed = {}, {}
    for group in args.groups:
        report = run_group(group)
        metrics.update(report["metrics"])
        if report["skipped"]:
            skipped[group] = report["skipped"]
        if report["failed"]:
            failed[group] = report["failed"]
    about = {"date": datetime.date.today().isoformat(), "commit": commit(), **machine()}
    LAST.write_text(json.dumps({**about, "metrics": metrics, "skipped": skipped,
                                "failed": failed}, indent=2) + "\n", encoding="utf-8")
    gates = json.loads(GATES.read_text(encoding="utf-8"))
    regressions = table(metrics, gates["metrics"], args.tolerance)
    for group, reason in skipped.items():
        print(f"skipped {group}: {reason}")
    for group, reason in failed.items():
        print(f"FAILED {group}: {reason}")
    if args.record:
        record(metrics, gates, about)
        print(f"recorded {len(metrics)} baselines in {GATES.relative_to(ROOT)}")
    if args.gate and regressions:
        print(f"gate: {len(regressions)} worse than the baseline by over {args.tolerance:g}%: "
              + ", ".join(regressions))
    elif args.gate:
        print(f"gate: nothing worse than the baseline by over {args.tolerance:g}%")
    return 1 if failed or (args.gate and regressions) else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
