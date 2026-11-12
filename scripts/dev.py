"""Every development task of flntools, the same on macOS, Linux and Windows, with nothing but uv.

    uv run --no-project scripts/dev.py setup
    uv run --no-project scripts/dev.py check --python 3.11
    uv run --no-project scripts/dev.py flnd download ins:example_user

`main` takes the task name from `sys.argv[1]`, then an optional `--python X` (the environment's
`PYTHON` otherwise, 3.14 by default), and hands the rest to the task function of that name. Every
task takes the same two arguments: `python`, the version passed to uv's `--python`, and `args`,
the command-line arguments after the task name, which `setup`, `test`, `type` and `flnd` pass on. Each
task runs uv commands in the project root; uv creates and syncs `.venv` from `uv.lock`, so this
script only uses the stdlib and runs under `--no-project`, outside the environment it manages.
The Makefile is a shortcut to it and holds no logic of its own.

An unknown or missing task prints the task list to stderr and exits 2. A failing command stops
the task and exits with that command's own code, so make and CI see the failure.
"""

import os
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
# The Python setup builds .venv with when neither --python nor PYTHON says otherwise; releases also check 3.11.
DEFAULT_PYTHON = "3.14"

FFMPEG_SOURCE_HEADER = ("ffmpeg-source\n"
                        "The ffmpeg program in this bundle, as it reports itself. The source offer is in ffmpeg.txt.\n")


def setup(python: str, args: list[str]) -> None:
    """Create or update `.venv` from `uv.lock`, with the dev group; downloads no browser.

    `--extra archive` keeps pyzipper and py7zr installed for the archive tests, also once flnd's own
    dependencies drop them. A second run checks the lock and installs nothing, well under a second.
    """
    run(["uv", "sync", "--python", python, "--extra", "archive", *args])


def test(python: str, args: list[str]) -> None:
    """Run every unittest under `tests/`."""
    run(["uv", "run", "--python", python, "python", "-m", "unittest", "discover", "-s", "tests", *args])


def lint(python: str, args: list[str]) -> None:
    """Byte-compile `flnd` and `shared`, which catches syntax errors in every file."""
    run(["uv", "run", "--python", python, "python", "-m", "compileall", "-q", "flnd", "shared"])


def type(python: str, args: list[str]) -> None:  # noqa: A001 - the task's name; this module never needs the builtin
    """Type-check with pyright, whose Node.js comes from the `pyright[nodejs]` wheel."""
    run(["uv", "run", "--python", python, "pyright", *args])


def check(python: str, args: list[str]) -> None:
    """Run `lint`, `type` and `test` in that order, stopping at the first failure."""
    for step in (lint, type, test):
        step(python, [])


def flnd(python: str, args: list[str]) -> None:
    """Run `flnd` from the project environment, passing every remaining argument through."""
    run(["uv", "run", "--python", python, "flnd", *args])


def dist(python: str, args: list[str]) -> None:
    """Build the one-folder bundle `dist/flnd/` and add `licenses/ffmpeg-source.txt` to it."""
    # PyInstaller lives in the `dist` group, which `setup` leaves out; `uv run` below keeps it installed.
    run(["uv", "sync", "--python", python, "--group", "dist"])
    # This script runs outside the project environment, so the build runs inside it, through uv.
    build = "from shared import bundler; bundler.build('flnd/__main__.py', name='flnd', data=('licenses',))"
    run(["uv", "run", "--python", python, "--group", "dist", "python", "-c", build])
    # utf-8 on every OS: a shell's `>` on Windows would write the console's code page instead.
    (ROOT / "dist" / "flnd" / "licenses" / "ffmpeg-source.txt").write_text(ffmpeg_source(python), encoding="utf-8")


def ffmpeg_source(python: str = DEFAULT_PYTHON) -> str:
    """The text of `ffmpeg-source.txt`: the bundled ffmpeg's `-version` output, for the GPL source offer.

        Path("ffmpeg-source.txt").write_text(ffmpeg_source(), encoding="utf-8")

    Args:
        python: the Python version `uv run` uses to find imageio-ffmpeg's binary in the project environment.
    """
    find = "import imageio_ffmpeg; print(imageio_ffmpeg.get_ffmpeg_exe())"
    ffmpeg = run(["uv", "run", "--python", python, "python", "-c", find], capture_output=True).stdout.strip()
    version = run([ffmpeg, "-hide_banner", "-version"], capture_output=True).stdout
    return f"{FFMPEG_SOURCE_HEADER}\n{version}\n"


def run(command: list[str], *, capture_output: bool = False) -> subprocess.CompletedProcess[str]:
    """Run `command` in the project root, raising CalledProcessError when it fails.

    Always a list of arguments, never a shell string, so quoting can't differ between sh and cmd.exe.
    """
    return subprocess.run(command, cwd=ROOT, check=True, capture_output=capture_output, text=True)


TASKS: dict[str, Callable[[str, list[str]], None]] = {
    task.__name__: task for task in (setup, test, lint, type, check, flnd, dist)
}


def main(argv: list[str]) -> int:
    """Run the task `argv[0]` with the rest of `argv`, and return the exit code.

        main(["check", "--python", "3.11"])

    Args:
        argv: the task name, then an optional `--python X`, then the task's own arguments.

    Returns:
        0 on success, 2 for an unknown or missing task, or the failing command's own exit code.
    """
    if not argv or argv[0] not in TASKS:
        print(f"usage: dev.py <task> [--python X] [args...]\ntasks: {' '.join(TASKS)}", file=sys.stderr)
        return 2
    name, args = argv[0], argv[1:]
    python = os.environ.get("PYTHON") or DEFAULT_PYTHON
    # Only right after the task, so `flnd`'s own arguments are passed through untouched.
    if args[:1] == ["--python"] and len(args) > 1:
        python, args = args[1], args[2:]
    try:
        TASKS[name](python, args)
    except subprocess.CalledProcessError as error:
        # The command already printed its own error; a traceback here would only bury it.
        return error.returncode
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
