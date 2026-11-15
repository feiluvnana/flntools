"""Command-line apps backed by `cyclopts` and `rich`, plus two-stage Ctrl-C handling.

    app = cli.App(name="notes", help="Manage notes.")

    @app.command
    def add(text: str, *, count: int = 1) -> int:
        return 0

    raise SystemExit(app.run())
"""

from __future__ import annotations

import signal
import sys
import threading
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any, Callable

import cyclopts
from cyclopts.exceptions import CycloptsError
from rich.console import Console
from rich.theme import Theme

import shared

__all__ = ["App", "Error", "Stopping", "UsageError", "stopping"]

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


class Error(shared.Error):
    """Base error for command-line parsing and execution."""


class UsageError(Error, ValueError):
    """Command-line usage error reported to stderr with exit code 2."""


@dataclass(frozen=True)
class Stopping:
    """Two-stage interrupt state: `soon` after first Ctrl-C, `now` after second."""

    soon: threading.Event
    now: threading.Event


class App:
    """Command-line application wrapping `cyclopts.App`."""

    def __init__(self, *, name: str = "", help: str = "") -> None:
        self._raw = _cyclopts_app(name, help)

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
        """Parse `argv`, run the matched command, and return its exit code."""
        tokens = sys.argv[1:] if argv is None else list(argv)
        try:
            return _exit_code(self._raw(tokens))
        except (UsageError, CycloptsError) as error:
            Console(stderr=True).print(f"[red]✗[/red] {error}")
            return 2
        except KeyboardInterrupt:
            return 130

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
