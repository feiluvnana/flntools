"""Command-line apps backed by `cyclopts` and `rich`, plus two-stage Ctrl-C handling.

    app = cli.App(name="notes", help="Manage notes.", logs=Path(".notes/logs"))

    @app.command
    def add(text: str, *, count: int = 1) -> int:
        return 0

    raise SystemExit(app.run())

`App.run` is where an error that escaped a command ends, and where it becomes an exit code:
1. A command returns its own code: 0 when all went well, 1 when some files or targets failed,
   3 when a login is needed (`flnd download` picks it from the run's report).
2. A `flnd.Error` prints its message on one line, then its `hint`, and never a traceback; its
   class picks the code (`EXIT_CODES`).
3. A cyclopts parse error is a usage error: one line, exit 2.
4. Ctrl-C (`flnd.Stopped` is one) exits 130.
5. Anything else is a bug: one line naming a crash file that holds the traceback, exit 4. With
   `-v` (or `--verbose`) the traceback prints inline instead.
"""

from __future__ import annotations

import signal
import sys
import threading
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

import cyclopts
from cyclopts.exceptions import CycloptsError
from rich.console import Console
from rich.theme import Theme

from flnd.errors import (
    DiskError, Error, LoginError, NetworkError, SiteError, SourceError, ToolError, UsageError,
)

__all__ = ["App", "EXIT_CODES", "Stopping", "exit_code", "stopping"]

CLI_PALETTE: dict[str, str] = {
    "cyclopts.name": "#02BF87",
    "cyclopts.required_marker": "#ED567A bold",
    "cyclopts.choices": "color(243)",
    "cyclopts.env_var": "color(243)",
    "cyclopts.default": "color(243)",
    "cyclopts.required": "#ED567A",
    "cyclopts.border": "#7D84D6",
    "cyclopts.usage": "#7D84D6 bold",
}


# The exit code of each kind of error that ends a command (docs/cli). A cron job can tell
# "some files failed" (1) from "flnd itself stopped" (4). The first class an error is wins.
EXIT_CODES: tuple[tuple[type[Error], int], ...] = (
    (UsageError, 2),
    (LoginError, 3),
    (SiteError, 1),
    (NetworkError, 1),
    (DiskError, 4),
    (ToolError, 4),
    (SourceError, 4),
)
# The code for Ctrl-C: 128 + SIGINT, as shells report it.
STOPPED = 130
# The code for a bug: an exception that isn't a `flnd.Error`.
CRASHED = 4
# The flags that print a crash's traceback inline instead of into a file.
VERBOSE = ("-v", "--verbose")


@dataclass(frozen=True)
class Stopping:
    """Two-stage interrupt state: `soon` after first Ctrl-C, `now` after second."""

    soon: threading.Event
    now: threading.Event


class App:
    """Command-line application wrapping `cyclopts.App`.

    Args:
        name: the program's name, as help shows it.
        help: the text `--help` shows above the commands.
        logs: the folder a crash file is written to (`crash-<time>.txt`); None to name no file
            and print the traceback instead.
    """

    def __init__(self, *, name: str = "", help: str = "", logs: Path | None = None) -> None:
        self._raw = _cyclopts_app(name, help)
        self.logs = logs

    @property
    def raw(self) -> cyclopts.App:
        """Underlying `cyclopts.App` for advanced configuration."""
        return self._raw

    def command(self, target: Callable[..., Any] | str | None = None, /) -> Any:
        """Register a command handler as `@app.command` or `@app.command('name')`."""
        if callable(target):
            return self._raw.command(target)
        return self._raw.command(name=target)

    def group(
        self, name: str, *, help: str = "", default: Callable[..., Any] | None = None
    ) -> App:
        """Create and attach a nested subcommand group `App`."""
        child = App(name=name, help=help)
        if default is not None:
            child._raw = _cyclopts_app(name, help, default=default)
        self._raw.command(child._raw)
        return child

    def run(self, argv: Sequence[str] | None = None) -> int:
        """Parse `argv`, run the matched command, and return its exit code.

        Nothing escapes it but `SystemExit`: every error ends as one line (and a hint) on
        stderr and an exit code, as the module docstring lists.

        Args:
            argv: the arguments after the program's name; None reads `sys.argv`.

        Returns:
            The exit code: the command's own, or the one its error maps to.
        """
        from flnd._ui import ui

        tokens = sys.argv[1:] if argv is None else list(argv)
        try:
            return _exit_code(self._raw(tokens))
        except Error as error:
            _report(ui, error)
            return exit_code(error)
        except CycloptsError as error:
            ui.error("{error}", error=str(error))
            return EXIT_CODES[0][1]
        except KeyboardInterrupt:
            return STOPPED
        except Exception as error:
            return self._crashed(ui, error, inline=any(flag in tokens for flag in VERBOSE))

    def _crashed(self, ui: Any, error: Exception, *, inline: bool) -> int:
        """Report a bug: one line naming the crash file, or the traceback itself with `-v`.

        The crash file is `<logs>/crash-<time>.txt`. When it can't be written (no `logs`, a
        read-only disk), the traceback prints inline, so it is never lost.

        Args:
            ui: the terminal to report on.
            error: the exception that escaped the command.
            inline: whether to print the traceback instead of writing a file.

        Returns:
            `CRASHED`, the exit code of a bug.
        """
        # Imported here: only a crash needs it, and `flnd --help` shouldn't pay for it.
        import traceback

        trace = "".join(traceback.format_exception(error))
        line = f"unexpected {type(error).__name__}: {error}"
        path = None if inline or self.logs is None else _crash_file(self.logs, trace)
        if path is None:
            ui.raw.print(trace, markup=False, highlight=False, end="")
            ui.error("{line}", line=line)
        else:
            ui.error("{line}; the details are in {path}", line=line, path=str(path))
        return CRASHED

    def __call__(self, argv: Sequence[str] | None = None) -> int:
        """Invoke `run(argv)` so `App` works directly as a console script entry point."""
        return self.run(argv)


@contextmanager
def stopping() -> Iterator[Stopping]:
    """Install a two-stage Ctrl-C handler setting `soon` then `now` on SIGINT."""
    state = Stopping(soon=threading.Event(), now=threading.Event())
    if threading.current_thread() is not threading.main_thread():
        yield state
        return
    previous = signal.getsignal(signal.SIGINT)

    def handle(_signum: int, _frame: Any) -> None:
        if state.soon.is_set():
            state.now.set()
        else:
            state.soon.set()

    signal.signal(signal.SIGINT, handle)
    try:
        yield state
    finally:
        signal.signal(signal.SIGINT, previous)


def exit_code(error: BaseException | None) -> int:
    """The exit code an error ends a command with, by its class; 0 for None.

    Args:
        error: what ended the command, or the worst failure of a run; None when nothing failed.

    Returns:
        0 for None, 130 for Ctrl-C, the code `EXIT_CODES` gives a `flnd.Error`, and 4 for
        anything else (a bug).

    Example:
        >>> exit_code(LoginError("ins: not logged in")), exit_code(None)
        (3, 0)
    """
    if error is None:
        return 0
    if isinstance(error, KeyboardInterrupt):
        return STOPPED
    return next((code for kind, code in EXIT_CODES if isinstance(error, kind)), CRASHED)


def _report(ui: Any, error: Error) -> None:
    """Print an error the person can act on: its message, then its hint, and no traceback."""
    ui.error("{error}", error=str(error))
    if error.hint:
        ui.info("{hint}", hint=error.hint)


def _crash_file(logs: Path, trace: str) -> Path | None:
    """Write `trace` to a new crash file in `logs`.

    Args:
        logs: the folder for crash files, made when missing.
        trace: the formatted traceback.

    Returns:
        The file's path, `crash-<YYYYmmdd-HHMMSS>.txt`; None when it can't be written, so the
        caller prints the traceback instead.
    """
    import datetime

    stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    path = logs / f"crash-{stamp}.txt"
    try:
        logs.mkdir(parents=True, exist_ok=True)
        path.write_text(trace, encoding="utf-8")
    except OSError:
        return None
    return path


def _cyclopts_app(
    name: str, help: str, *, default: Callable[..., Any] | None = None
) -> cyclopts.App:
    """Build a configured `cyclopts.App` instance."""
    theme = Theme(CLI_PALETTE)
    return cyclopts.App(
        name=name or None,
        help=help or None,
        console=Console(theme=theme),
        error_console=Console(stderr=True, theme=theme),
        version_flags=["-V", "--version"],
        default_command=default,
        exit_on_error=False,
        print_error=False,
        result_action="return_value",
        suppress_keyboard_interrupt=False,
    )


def _exit_code(result: Any) -> int:
    """Normalize a command return value into a process exit code."""
    if isinstance(result, bool):
        return 0 if result else 1
    if isinstance(result, int):
        return result
    return 0
