"""A command line from annotated functions: cyclopts, Charm help, errors and tables."""

from __future__ import annotations

import json
import sys
import traceback
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, TextIO

from cyclopts import App as _CycloptsApp
from cyclopts.exceptions import CycloptsError
from rich.console import Console
from rich.table import Table
from rich.text import Text
from rich.theme import Theme

from flnd.errors import Error, UsageError
from flnd.ui.log import PALETTE

__all__ = ["App", "OK", "FAILED", "USAGE", "BROKE", "STOPPED", "exit_code", "report"]

OK, FAILED, USAGE, BROKE, STOPPED = 0, 1, 2, 4, 130

_BASE_CODES: dict[str, int] = {
    "UsageError": USAGE,
    "SiteError": FAILED,
    "NetworkError": FAILED,
    "DiskError": BROKE,
    "ToolError": BROKE,
    "Error": BROKE,
}

_SKIP_HELP = frozenset({
    "--help", "-h", "--help (-h)", "--version", "-V", "--version (-V)",
})


def exit_code(
    error: BaseException | None,
    codes: Mapping[type[BaseException], int] | None = None,
) -> int:
    """The exit code an error ends a command with; 0 for None."""
    if error is None:
        return OK
    if isinstance(error, KeyboardInterrupt):
        return STOPPED
    table = {**_BASE_CODES, **(_class_codes(codes) if codes else {})}
    for kind in type(error).__mro__:
        name = kind.__name__
        if kind.__module__ == "flnd.errors" and name in table:
            return table[name]
    return BROKE


def _class_codes(codes: Mapping[type[BaseException], int] | None) -> dict[str, int]:
    if not codes:
        return {}
    return {cls.__name__: code for cls, code in codes.items()}


def _plain_description(description: Any) -> str:
    if description is None:
        return ""
    from io import StringIO

    from cyclopts.help.inline_text import InlineText

    if isinstance(description, InlineText):
        buf = StringIO()
        Console(file=buf, width=500, highlight=False, color_system=None).print(description)
        text = buf.getvalue().strip()
    else:
        text = str(description).strip()
    return text.split("\n\n")[0].split("\n")[0].strip()


def _stderr_console(file: TextIO | None = None) -> Console:
    stream = file if file is not None else sys.stderr
    return Console(
        file=stream,
        theme=Theme(PALETTE),
        highlight=False,
        soft_wrap=True,
    )


class CharmFormatter:
    """Cyclopts help formatter in the Charm Huh look: one line per command, no boxes."""

    def render_usage(self, console: Console, options: Any, usage: Any) -> None:
        if not usage:
            return
        body = str(usage)
        if " COMMAND" in body:
            return
        if body.strip().startswith("Usage:"):
            body = body.split(":", 1)[1].strip()
        console.print(Text.assemble(
            ("Usage: ", f"bold {PALETTE['accent']}"),
            (body, "bold"),
        ))

    def render_description(self, console: Console, options: Any, description: Any) -> None:
        return

    def __call__(self, console: Console, options: Any, panel: Any) -> None:
        if not panel.entries:
            return
        for entry in panel.entries:
            label = entry.display_labels[0] if entry.display_labels else ""
            if label in _SKIP_HELP or label.startswith("--help") or label.startswith("--version"):
                continue
            desc = _plain_description(entry.description)
            dim = "color(243)"
            console.print(Text.assemble(
                ("  ", ""),
                (label, f"bold {PALETTE['ok']}"),
                (" " * max(1, 28 - len(label)), ""),
                (desc, dim if desc else ""),
            ))


def report(error: Error, *, json_out: bool = False, stream: TextIO | None = None) -> None:
    """Print an error's message and hint; never a traceback."""
    err = stream if stream is not None else sys.stderr
    if json_out:
        sys.stdout.write(_error_json_line(error))
        sys.stdout.flush()
    out = _stderr_console(err)
    out.print(Text.assemble(("error: ", "bold #ED567A"), (str(error), "#ED567A")))
    if error.hint:
        out.print(Text.assemble(("  ", ""), (str(error.hint), "color(243)")))


def _error_json_line(error: Error) -> str:
    payload = {
        "type": "Error",
        "error": {
            "class": type(error).__name__,
            "message": str(error),
            "hint": error.hint,
            "retryable": error.retryable,
        },
    }
    return json.dumps(payload, ensure_ascii=False) + "\n"


def _crash_path(home: Path) -> Path:
    stamp = datetime.now(tz=UTC).strftime("%Y%m%d-%H%M%S")
    return home.expanduser() / "log" / f"crash-{stamp}.txt"


def _crashed(error: Exception, *, home: Path | None, verbose: bool) -> int:
    trace = "".join(traceback.format_exception(error))
    err = _stderr_console()
    if verbose:
        err.print(trace, end="")
        return BROKE
    path = _crash_path(home or Path(".flnd"))
    line = f"unexpected {type(error).__name__}: {error}"
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(trace, encoding="utf-8")
    except OSError:
        err.print(trace, end="")
    else:
        err.print(Text.assemble(
            ("error: ", "bold #ED567A"),
            (f"{line}; the details are in {path}", "#ED567A"),
        ))
    return BROKE


@dataclass
class App:
    """A CLI built from annotated command functions (cyclopts underneath).

    Args:
        name: the program name shown in help.
        version: the version string on the first help line.
        codes: extra error classes mapped to exit codes (merged with the defaults).
        help: one-line tagline after the version on the root help screen.
    """

    name: str
    version: str = ""
    codes: dict[type[BaseException], int] | None = None
    help: str = ""

    def __post_init__(self) -> None:
        formatter = CharmFormatter()
        prologue = f"{self.name} {self.version}" if self.version else self.name
        if self.help:
            prologue = f"{prologue} · {self.help}"
        self._codes = {**_BASE_CODES, **_class_codes(self.codes)}
        self._cyclopts = _CycloptsApp(
            name=self.name,
            help=self.help,
            version=self.version or None,
            version_flags=(),
            help_formatter=formatter,
            help_format="plaintext",
            help_prologue=prologue,
            exit_on_error=False,
            suppress_keyboard_interrupt=False,
            result_action="return_value",
            console=_stderr_console(),
            error_console=_stderr_console(),
        )

    def group(self, name: str, help: str = "") -> App:
        """Register a named group of subcommands and return its `App`."""
        child = _CycloptsApp(
            name=name,
            help=help,
            version_flags=(),
            help_formatter=CharmFormatter(),
            help_format="plaintext",
            exit_on_error=False,
        )
        self._cyclopts.command(child)
        grouped = App(name, help=help, version="", codes=self.codes)
        grouped._cyclopts = child
        grouped._codes = self._codes
        return grouped

    def command(self, function: Callable[..., Any] | None = None, **kwargs: Any) -> Any:
        """Register `function` as a command, or return a decorator."""
        if function is None:
            return lambda fn: self.command(fn, **kwargs)
        return self._cyclopts.command(function, **kwargs)

    def show(self, rows: Sequence[Sequence[Any]], columns: Sequence[str]) -> None:
        """Print rows as an aligned table in the Charm look."""
        table = Table(
            show_header=True,
            header_style=f"bold {PALETTE['accent']}",
            box=None,
            padding=(0, 2),
            expand=False,
        )
        for column in columns:
            table.add_column(str(column))
        for row in rows:
            table.add_row(*[str(cell) for cell in row])
        _stderr_console().print(table)

    def help_text(self, *tokens: str, width: int = 120) -> str:
        """The help screen for `tokens`, as plain text (for snapshots)."""
        from io import StringIO

        buffer = StringIO()
        console = Console(
            file=buffer,
            width=width,
            force_terminal=True,
            color_system=None,
            highlight=False,
            theme=Theme(PALETTE),
        )
        self._cyclopts.help_print(list(tokens), console=console)
        return buffer.getvalue()

    def run(
        self,
        tokens: Sequence[str] | None = None,
        *,
        home: Path | None = None,
        json_out: bool = False,
    ) -> int:
        """Parse and run one command; print errors and return an exit code."""
        from flnd.ui import log

        argv = list(tokens) if tokens is not None else sys.argv[1:]
        verbose = "-v" in argv or "--verbose" in argv
        if verbose:
            log.log.level = "debug"
        try:
            result = self._cyclopts(
                argv,
                exit_on_error=False,
                print_error=False,
                verbose=verbose,
            )
        except KeyboardInterrupt:
            _stderr_console().print(Text("stopped", style="yellow"))
            return STOPPED
        except CycloptsError as error:
            msg = str(error).split("\n")[0]
            report(UsageError(msg), json_out=json_out)
            return USAGE
        except Error as error:
            report(error, json_out=json_out)
            return exit_code(error, self.codes)
        except Exception as error:
            return _crashed(error, home=home, verbose=verbose)
        if isinstance(result, int):
            return result
        return OK