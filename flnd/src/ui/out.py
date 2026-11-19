"""Every line flnd prints: lines, folded warnings, errors with their hint, tables and the help.

src/docs/cli explains it.
"""

from __future__ import annotations

import argparse
import re
import string
import sys
import threading
from collections.abc import Callable, Sequence
from functools import lru_cache
from typing import Any

from flnd.ui import theme

__all__ = ["FOLD", "Folds", "above", "clean", "close", "error", "fill", "help", "line", "names",
           "plain", "spin", "styled", "table", "warn"]

# A part of a line: text in the body colour, or (text, style), the style made of theme tokens
# and Rich attributes ("bold bad").
Part = str | tuple[str, str]

# Seconds in which the same warning folds into one line.
FOLD = 10.0
# The left margin of the help, in columns.
MARGIN = 2

# ---------------------------------------------------------------------------------------------
# Text from a site
# ---------------------------------------------------------------------------------------------

_ESCAPE = re.compile(
    r"\x1b\[[0-?]*[ -/]*[@-~]|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)|\x1b[@-Z\\-_]"
)
# What Rich's own `strip_control_codes` drops, without importing Rich.
_CONTROLS = {code: None for code in (7, 8, 11, 12, 13)}


def clean(text: str) -> str:
    """`text` without terminal escape and control codes, so text from a site can't drive it."""
    return _ESCAPE.sub("", text).translate(_CONTROLS)


class _SafeFormatter(string.Formatter):
    """`str.format` that strips terminal escape and control codes from each field it formats."""

    def format_field(self, value: Any, format_spec: str) -> str:
        if isinstance(value, str):
            value = clean(value)
        return clean(super().format_field(value, format_spec))


_FORMATTER = _SafeFormatter()


@lru_cache(maxsize=256)
def names(message: str) -> frozenset[str]:
    """The top-level names of the `{fields}` in a template, nested specs included: `{user.name}`
    gives `user`."""
    found: set[str] = set()
    for _, field_name, format_spec, _ in string.Formatter().parse(message):
        if field_name:
            found.add(field_name.split(".")[0].split("[")[0])
        if format_spec and "{" in format_spec:
            found.update(names(format_spec))
    return frozenset(found)


def fill(message: str, *args: Any, **fields: Any) -> str:
    """A message template filled in, every field cleaned of escape codes.

    Args:
        message: the template; `{name}` marks a field.
        *args: positional values for the template.
        **fields: values for its named fields.
    """
    return _FORMATTER.format(message, *args, **fields) if (args or fields) else clean(message)


# ---------------------------------------------------------------------------------------------
# Lines
# ---------------------------------------------------------------------------------------------

def _stream(err: bool) -> Any:
    return sys.stderr if err else sys.stdout


def _console(stream: Any) -> Any:
    """A Rich console over `stream`, coloured only when the theme says so."""
    from rich.console import Console

    return Console(file=stream, color_system="auto" if theme.color(stream) else None,
                   highlight=False, markup=False, emoji=False, soft_wrap=True)


def _text(parts: Sequence[Part], *, mark: str = "", fancy: bool = True) -> Any:
    """Rich text made of `parts`, after the glyph `mark` in its kind's style."""
    from rich.text import Text

    text = Text()
    if mark:
        name, token = theme.MARKS[mark]
        text.append(theme.glyph(name, fancy) + " ", style=theme.style(f"bold {token}"))
    for part in parts:
        words, spec = (part, "text") if isinstance(part, str) else part
        text.append(clean(words), style=theme.style(spec))
    return text


# Where the lines for standard error go while the run's screen is drawn there (`above`).
_above: Callable[[Any], None] | None = None


def above(sink: Callable[[Any], None] | None) -> None:
    """Send the lines for standard error to `sink` (the run's screen), or back (None).

    The screen prints them above its live block, from its own thread. `sink` must take no lock
    another thread may hold: a Ctrl-C handler prints through it.
    """
    global _above
    _above = sink


def _print(*renderables: Any, stream: Any, wrap: bool = False) -> None:
    """Print `renderables` on `stream`; with `wrap`, laid out to the width, its padding cut."""
    sink = _above
    if sink is not None and stream is sys.stderr and not wrap:
        for renderable in renderables:
            sink(renderable)
        return
    console = _console(stream)
    if not wrap:
        console.print(*renderables, soft_wrap=True)
    else:
        with console.capture() as captured:
            for renderable in renderables:
                console.print(renderable, soft_wrap=False)
        # A table pads each row to the width with plain spaces: they go.
        stream.write("".join(row.rstrip(" ") + "\n" for row in captured.get().splitlines()))
    _flush(stream)


def _flush(stream: Any) -> None:
    try:
        stream.flush()
    except (AttributeError, OSError, ValueError):
        pass


def line(*parts: Part, mark: str = "", err: bool = False) -> None:
    """Print one line made of `parts`.

    Args:
        *parts: text in the body colour, or (text, style) pairs.
        mark: a kind of line ("ok", "error"…) whose glyph starts it; "" for none.
        err: print on standard error instead of standard output.
    """
    stream = _stream(err)
    _print(_text(parts, mark=mark, fancy=theme.unicode(stream)), stream=stream)


def plain(text: str, *, err: bool = False) -> None:
    """Print `text` as it is, then a line break: JSON lines, a traceback, argparse's messages."""
    stream = _stream(err)
    stream.write(text if text.endswith("\n") else text + "\n")
    _flush(stream)


def count(n: int, noun: str) -> str:
    """`1 file`, `2 files`: `n` with its noun, pluralised."""
    from flnkit import text

    return text.count(n, noun)


def size(n: int) -> str:
    """A byte count as people read it: `1.2 GB`."""
    from flnkit import text

    return text.size(n)


def json(value: Any) -> None:
    """Print one event, report or row as one line of JSON (`flnd.engine.events.encode`), with
    its class's name in `"type"` when it has none."""
    from flnd.engine.events import encode

    text = encode(value).decode()
    if text.startswith("{") and not text.startswith('{"type"'):
        text = f'{{"type":"{type(value).__name__}"' + ("," + text[1:] if text != "{}" else "}")
    plain(text)


def spin(text: str, *, at: float | None) -> None:
    """Draw the spinner's frame at `at` seconds, then `text`, over the line, on a terminal.

    `at=None` clears the line. A pipe gets nothing: the lines around the wait say it all.
    """
    stream = sys.stdout
    if not theme.tty(stream):
        return
    stream.write("\r\x1b[2K")
    if at is not None:
        fancy = theme.unicode(stream)
        frame = _text([(theme.spinner(at, fancy) + " ", "focus"), (text, "dim")], fancy=fancy)
        _console(stream).print(frame, end="")
    _flush(stream)


def styled(kind: str, text: str, *, fancy: bool, times: int = 1) -> Any:
    """A log line of `kind` (debug, info, ok, warn, error): its glyph, then `text`.

    Args:
        kind: the line's level.
        text: the filled-in message.
        fancy: Unicode glyphs; False for ASCII.
        times: how many times the line came; more than 1 adds "(×n)".
    """
    from rich.text import Text

    name, token = theme.MARKS[kind]
    mark = theme.glyph(name, fancy)
    if times > 1:
        text = f"{text} ({theme.glyph('times', fancy)}{times})"
    if not text or kind == "debug":
        return Text(f"{mark} {text}" if text else mark, style=theme.style(token))
    head, colon, detail = text.partition(": ")
    if kind == "info":
        return Text.assemble((mark, theme.style(token)), " ", (head, theme.style("text")),
                             (colon + detail, theme.style(token)))
    return Text.assemble((f"{mark} {head}", theme.style(token)),
                         (colon + detail, theme.style(token)) if colon else ("", ""))


class Folds:
    """The repeats of each warning within `FOLD` seconds, held back and counted.

    The first time a text comes it is shown at once, and its window of `FOLD` seconds opens.
    Repeats inside the window are held. When the window has passed (checked at the next `see`)
    or at `drain`, a text with held repeats is shown once more as "text (×n)", n counting every
    time it came in the window, the first included.

    A signal handler may warn while the thread it interrupted is inside `see` or `drain`. A
    thread never re-enters: inside already, its line is shown at once, unfolded, and `drain`
    gives nothing. Any other thread waits for the lock, which is held only for a few steps.
    """

    def __init__(self) -> None:
        # Each text: [when its window opened, how many times it came in it].
        self._seen: dict[str, list[float]] = {}
        self._lock = threading.RLock()
        self._inside = threading.local()

    def see(self, text: str) -> list[tuple[str, int]]:
        """The lines to show now for a warning `text`, each with how many times it came.

        The folds whose window has passed come first; then `text` itself, unless it is a repeat
        inside its window.

        Args:
            text: the warning's text.
        """
        from flnkit import clock  # asyncio comes with it: not for the help

        if getattr(self._inside, "on", False):
            return [(text, 1)]
        self._inside.on = True
        try:
            with self._lock:
                now = clock.now()
                shown = self._expired(now)
                entry = self._seen.get(text)
                if entry is not None:
                    entry[1] += 1
                    return shown
                self._seen[text] = [now, 1]
                return [*shown, (text, 1)]
        finally:
            self._inside.on = False

    def drain(self) -> list[tuple[str, int]]:
        """Every text with held repeats, with how many times it came; nothing is held after.

        Nothing comes back on a thread already inside `see` or `drain` (a signal handler).
        """
        if getattr(self._inside, "on", False):
            return []
        self._inside.on = True
        try:
            with self._lock:
                shown = [(text, int(count)) for text, (_, count) in self._seen.items()
                         if count > 1]
                self._seen.clear()
                return shown
        finally:
            self._inside.on = False

    def _expired(self, now: float) -> list[tuple[str, int]]:
        """Close every window that has passed: the texts with held repeats, to show again."""
        shown = []
        for text, (at, count) in list(self._seen.items()):
            if not 0 <= now - at < FOLD:
                del self._seen[text]
                if count > 1:
                    shown.append((text, int(count)))
        return shown


_folds = Folds()


def warn(text: str) -> None:
    """Print a warning on standard error; the same text within `FOLD` seconds folds into one."""
    for shown, times in _folds.see(text):
        _warn(shown, times)


def _warn(text: str, times: int) -> None:
    stream = sys.stderr
    _print(styled("warn", clean(text), fancy=theme.unicode(stream), times=times), stream=stream)


def error(error: BaseException) -> None:
    """Print an error on standard error: `✗ <message>`, then `  hint  <hint>` on its own line.

    A `flnd.Error` with no hint gets flnd's first (`flnd.errors.classify`), so an error flnkit
    raised says which flnd command to run.
    """
    from flnd.errors import Error, classify

    if isinstance(error, Error):
        classify(error)
    line((str(error), "bad"), mark="error", err=True)
    hint = str(getattr(error, "hint", "") or "")
    if hint:
        line(("  hint  ", "dim"), hint, err=True)


def close(*, cursor: bool = False, drain: bool = True) -> None:
    """Print the warnings still folded, `flnd.ui.log`'s too, and flush both streams.

    Args:
        cursor: also show the cursor again on a terminal, which a live view may have hidden.
        drain: False drops the folded repeats instead, taking no lock: for a hard exit, whose
            signal handler must not wait on a lock another thread may hold.
    """
    if drain:
        for text, times in _folds.drain():
            _warn(text, times)
        log = sys.modules.get("flnd.ui.log")
        if log is not None:
            log.ui.drain()
    for stream in (sys.stdout, sys.stderr):
        try:
            if cursor and stream is not None and stream.isatty():
                stream.write("\x1b[?25h")
        except (OSError, ValueError):
            pass
        _flush(stream)


# ---------------------------------------------------------------------------------------------
# Tables
# ---------------------------------------------------------------------------------------------

def table(rows: Sequence[Sequence[Part]], columns: Sequence[str]) -> None:
    """Print `rows` under `columns` on standard output: a bordered table on a terminal, plain
    columns two spaces apart elsewhere.

    Args:
        rows: each row's cells: text, or (text, style) pairs.
        columns: the column titles.
    """
    stream = sys.stdout
    if not theme.tty(stream):
        cells = [list(columns), *[[clean(_words(cell)) for cell in row] for row in rows]]
        widths = [max(len(cell) for cell in column) for column in zip(*cells)]
        for row in cells:
            plain("  ".join(cell.ljust(width) for cell, width in zip(row, widths)).rstrip())
        return
    from rich.table import Table

    grid = Table(box=theme.box(), border_style=theme.style("dim"),
                 header_style=theme.style("bold focus"), padding=theme.PADDING)
    for title in columns:
        grid.add_column(title)
    for row in rows:
        grid.add_row(*(_text([cell]) for cell in row))
    _print(grid, stream=stream, wrap=True)


def _words(part: Part) -> str:
    return part if isinstance(part, str) else part[0]


# ---------------------------------------------------------------------------------------------
# The help
# ---------------------------------------------------------------------------------------------

def help(parser: argparse.ArgumentParser, *, tail: Sequence[tuple[str, Sequence[Part]]] = ()
         ) -> None:
    """Print `parser`'s help on standard output, in the theme.

    The top parser shows its version and description, then a row per command; a command shows
    its usage line and its description. Both then show their flags, and the rows of `tail`.

    Args:
        parser: the parser whose help it is.
        tail: rows to end with: a name, then its text.
    """
    from rich.padding import Padding
    from rich.table import Table

    stream = sys.stdout
    fancy = theme.unicode(stream)
    commands = next((action for action in parser._actions
                     if isinstance(action, argparse._SubParsersAction)), None)
    grid = Table.grid(padding=(0, 3))
    grid.add_column(no_wrap=True)
    grid.add_column()
    blocks: list[list[tuple[Any, Any]]] = []
    if commands is not None:
        blocks.append([_command(choice, commands.choices[choice.dest], fancy)
                       for choice in commands._get_subactions()])
    else:
        blocks.append([_flag(action) for action in parser._actions
                       if not action.option_strings])
    # Flags in the order of their groups: the command's own, then the global ones.
    blocks.append([_flag(action) for group in parser._action_groups
                   for action in group._group_actions if action.option_strings])
    blocks.append([(_text([(name, "focus")]), _text(parts, fancy=fancy))
                   for name, parts in tail])
    for block in [block for block in blocks if block]:
        if grid.row_count:
            grid.add_row("", "")
        for row in block:
            grid.add_row(*row)
    _print(Padding(_head(parser, commands is not None, fancy), (0, 0, 1, MARGIN)),
           Padding(grid, (0, 0, 0, MARGIN)), stream=stream, wrap=True)


def _head(parser: argparse.ArgumentParser, top: bool, fancy: bool) -> Any:
    """The top parser's version and description, or a command's usage and description."""
    from rich.text import Text

    if top:
        version = next((action.version for action in parser._actions
                        if getattr(action, "version", None)), parser.prog)
        dot = theme.glyph("waiting", fancy)
        return _text([(str(version), "bold focus"), (f" {dot} {parser.description or ''}",
                                                     "dim")])
    head = _text([(parser.prog, "bold focus"), (f" {_arguments(parser, fancy)} [options]", "dim")])
    for paragraph in (parser.description or "").split("\n\n"):
        # Each paragraph is reflowed: a docstring's own line breaks don't show.
        head.append("\n\n")
        head.append(Text(clean(" ".join(paragraph.split())), style=theme.style("text")))
    return head


def _command(choice: argparse.Action, sub: argparse.ArgumentParser, fancy: bool
             ) -> tuple[Any, Any]:
    return (_text([(choice.dest, "focus"), (f" {_arguments(sub, fancy)}".rstrip(), "dim")]),
            _text([choice.help or ""]))


def _flag(action: argparse.Action) -> tuple[Any, Any]:
    if action.option_strings:
        name = ", ".join(action.option_strings)
        if action.metavar:
            name += f" {action.metavar}"
    else:
        name = str(action.metavar or action.dest)
    return _text([(name, "focus")]), _text([(action.help or "", "text")])


def _arguments(parser: argparse.ArgumentParser, fancy: bool) -> str:
    """A command's positional arguments as its usage shows them: `[target…]`, `<source>`; a
    group's own commands as `list|clean`."""
    more = theme.glyph("more", fancy)
    shown = []
    for action in parser._actions:
        if isinstance(action, argparse._SubParsersAction):
            shown.append("|".join(action.choices))
            continue
        if action.option_strings:
            continue
        name = str(action.metavar or action.dest)
        shown.append({"*": f"[{name}{more}]", "+": f"<{name}{more}>", "?": f"[{name}]"}
                     .get(str(action.nargs), f"<{name}>"))
    return " ".join(shown)
