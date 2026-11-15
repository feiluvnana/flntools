"""Command-line apps backed by `cyclopts` and `rich`, plus two-stage Ctrl-C handling.

    app = cli.App("notes", version="1.0")

    @app.command
    def add(text: str, *, count: int = 1) -> int:
        return 0

    app()
"""

from __future__ import annotations

import signal
import sys
import threading
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any, Callable, NoReturn

import cyclopts
from cyclopts.exceptions import CycloptsError
from rich.console import Console

import shared

__all__ = ["App", "Error", "Stopping", "UsageError", "main", "stopping"]


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
    """Command-line application wrapping `cyclopts.App` with `rich` output."""

    def __init__(
        self,
        name: str,
        *,
        version: str | None = None,
        description: str = "",
        epilog: str = "",
        default: Callable[..., Any] | None = None,
    ) -> None:
        self.name = name
        self.raw = cyclopts.App(
            name=name,
            help=description or None,
            help_epilogue=epilog or None,
            version=version,
            version_flags=["-V", "--version"],
            default_command=default,
            exit_on_error=False,
            print_error=False,
            result_action="return_value",
            suppress_keyboard_interrupt=False,
        )

    def command(self, target: Callable[..., Any] | str | None = None, /) -> Any:
        """Register a command handler as `@app.command` or `@app.command('name')`."""
        if callable(target):
            return self.raw.command(target)
        return self.raw.command(name=target)

    def group(
        self, name: str, *, description: str = "", default: Callable[..., Any] | None = None
    ) -> App:
        """Add and return a subcommand group under `name`."""
        child = App(name, description=description, default=default)
        self.raw.command(child.raw)
        return child

    def run(self, argv: Sequence[str] | None = None) -> int:
        """Parse `argv`, run the matched command, and return its exit code."""
        tokens = sys.argv[1:] if argv is None else list(argv)
        try:
            return _exit_code(self.raw(tokens))
        except (UsageError, CycloptsError) as error:
            Console(stderr=True).print(f"[red]✗[/red] {error}")
            return 2
        except KeyboardInterrupt:
            return 130

    def __call__(self) -> NoReturn:
        """Run with `sys.argv` and exit with the resulting status code."""
        sys.exit(self.run())


def main(fn: Callable[..., Any]) -> NoReturn:
    """Run a single function `fn` as a standalone CLI program and exit."""
    app = App(fn.__name__.replace("_", "-"), default=fn)
    app()


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


def _exit_code(result: Any) -> int:
    """Normalize a command return value into a process exit code."""
    if isinstance(result, bool):
        return 0 if result else 1
    if isinstance(result, int):
        return result
    return 0
