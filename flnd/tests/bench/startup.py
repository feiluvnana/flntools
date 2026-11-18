"""Benchmark group `startup`: how long a fresh interpreter takes to import flnd and show help.

Each number is the median wall time of 15 process starts (`kit.median_wall`), so it includes
the interpreter's own start. `python -c pass` is listed as the floor every other number sits on.
`flnd --help` is the number the CLI rebuild is gated on, since it is what a person waits for.
"""

from __future__ import annotations

import sys

import kit

# What each metric starts: the name, then the interpreter's arguments.
COMMANDS = (
    ("python_pass", ["-c", "pass"]),
    ("import_flnd", ["-c", "import flnd"]),
    ("import_flnd_plugin", ["-c", "import flnd.plugin"]),
    ("import_flnd_cli", ["-c", "import flnd.cli"]),
    ("help", ["-m", "flnd", "--help"]),
)


def main() -> None:
    """Time each command of `COMMANDS` and emit the report."""
    report = kit.Report("startup")
    for name, args in COMMANDS:
        report.add(name, kit.median_wall([sys.executable, *args]) * 1e3, "ms")
    report.emit()


if __name__ == "__main__":
    main()
