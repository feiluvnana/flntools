"""Terminal output and progress bars backed by Rich.

    ui.level = "debug"
    ui.ok("{n} saved", n=3)
    with ui.progress("photos", total=10) as row:
        row.advance()
"""

from __future__ import annotations

import functools
import os
import re
import string
import threading
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from typing import IO, Any, Literal, cast

from rich.console import Console as RichConsole
from rich.console import RenderableType
from rich.control import strip_control_codes
from rich.progress import Progress as RichProgress
from rich.progress import ProgressColumn, Task, TaskID
from rich.text import Text
from rich.theme import Theme

import shared
from shared import _clock as clock

__all__ = ["Error", "Group", "Row", "UI", "ui"]

Level = Literal["debug", "info", "ok", "warn", "error", "quiet"]
LEVELS: tuple[Level, ...] = ("debug", "info", "ok", "warn", "error", "quiet")
LEVEL_RANKS: dict[str, int] = {
    "debug": 0, "info": 1, "ok": 2, "warn": 3, "error": 4, "quiet": 4,
}

PALETTE: dict[str, str] = {
    "info": "color(243)",
    "ok": "#02BF87",
    "warn": "yellow",
    "error": "#ED567A",
    "debug": "color(242)",
    "text": "color(252)",
    "faint": "color(238)",
    "accent": "#7D84D6",
    "size": "color(245)",
}

GLYPHS: dict[str, tuple[str, str]] = {
    "bullet": ("•", "*"),
    "tick": ("✓", "+"),
    "bang": ("!", "!"),
    "cross": ("✗", "x"),
    "dot": ("·", "."),
    "bar_full": ("━", "="),
    "bar_empty": ("─", "-"),
}
LOG_GLYPHS: dict[str, str] = {
    "info": "bullet", "ok": "tick", "warn": "bang", "error": "cross", "debug": "dot",
}
SPIN_FRAMES: tuple[tuple[str, ...], tuple[str, ...]] = (
    ("⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏"),
    ("|", "/", "-", "\\"),
)
BAR_CELLS = 20

_ESCAPE = re.compile(
    r"\x1b\[[0-?]*[ -/]*[@-~]"
    r"|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)"
    r"|\x1b[@-Z\\-_]"
)


class Error(shared.Error):
    """Base error raised by `shared.ui`."""


class _SafeFormatter(string.Formatter):
    """Formatter that strips raw CSI/OSC escapes and control codes from fields."""

    def format_field(self, value: Any, format_spec: str) -> str:
        if isinstance(value, str):
            value = _sanitize(value)
        return _sanitize(super().format_field(value, format_spec))


_FORMATTER = _SafeFormatter()


class _RowColumn(ProgressColumn):
    """Custom Rich column rendering `Row` bars."""

    def __init__(self, owner: UI) -> None:
        super().__init__()
        self._ui = owner

    def render(self, task: Task) -> RenderableType:
        """Render one task as a progress row."""
        row_obj = cast(Row, task.fields["row"])
        return row_obj._render(self._ui)


class Row:
    """Handle to a single active progress bar or spinner row."""

    def __init__(self, label: str, total: float | None, group: Group) -> None:
        self.label = label
        self._total = total
        self._count: float = 0
        self._group = group
        self._outcome: str | None = None
        self._lock = threading.Lock()
        self._task_id: TaskID | None = None

    @property
    def count(self) -> float:
        """Current completed count for the row."""
        with self._lock:
            return self._count

    @count.setter
    def count(self, value: float) -> None:
        """Set amount completed, clamped at zero."""
        with self._lock:
            self._count = max(value, 0)
        self._sync()

    @property
    def total(self) -> float | None:
        """Target total for the row, or None if indeterminate."""
        with self._lock:
            return self._total

    @total.setter
    def total(self, value: float | None) -> None:
        """Set target total for the row."""
        with self._lock:
            self._total = value
        self._sync()

    @property
    def raw(self) -> TaskID:
        """Underlying `rich.progress.TaskID` for custom Rich updates."""
        with self._lock:
            if self._task_id is None:
                raise Error("row is not attached to an active progress display")
            return self._task_id

    def advance(self, n: float = 1) -> None:
        """Increment the row's progress by `n`."""
        with self._lock:
            self._count = max(self._count + n, 0)
        self._sync()

    def part(self) -> Callable[[int, int | None], None]:
        """Return a `(done, total) -> None` callback that updates this row."""
        def update(done: int, total: int | None = None) -> None:
            with self._lock:
                self._total = total
                self._count = max(done, 0)
            self._sync()

        return update

    def finish(self) -> None:
        """Mark the row complete and finalize its display."""
        self._end("ok")

    def fail(self) -> None:
        """Mark the row failed and finalize its display."""
        self._end("error")

    def _end(self, outcome: str) -> None:
        with self._lock:
            if self._outcome is not None:
                return
            self._outcome = outcome
        self._sync()

    def _sync(self) -> None:
        if self._task_id is not None:
            self._group._ui._update_task(self._task_id, self._count, self._total)

    def _final(self, owner: UI) -> tuple[str, Text]:
        with self._lock:
            level = "info" if self._outcome is None else self._outcome
            return level, self._build_text(owner, level)

    def _render(self, owner: UI) -> Text:
        with self._lock:
            level = "info" if self._outcome is None else self._outcome
            return self._build_text(owner, level)

    def _build_text(self, owner: UI, level: str) -> Text:
        role = "accent" if level == "info" else level
        glyph = owner._glyph(LOG_GLYPHS[level])
        text = Text()
        text.append(glyph, style=role)
        text.append(" ")
        if self._total is None and self._count <= 0:
            text.append(self.label, style="text")
            if self._outcome is None:
                text.append(" " + owner._spinner(), style=role)
            return text
        text.append_text(_render_bar(owner, self._count, self._total, level, role))
        text.append(" " + self.label, style="text")
        count_str = _format_count(self._count, self._total, level)
        if count_str:
            text.append(" " + count_str, style="size")
        return text


class Group:
    """Container for managing multiple concurrent `Row` progress bars."""

    def __init__(self, owner: UI, title: str) -> None:
        self._ui = owner
        self.title = title
        self._rows: list[Row] = []
        self._lock = threading.Lock()

    def add(self, label: str, *, total: float | None = None) -> Row:
        """Add a new `Row` to the live progress group."""
        row = Row(label, total, self)
        with self._lock:
            self._rows.append(row)
        self._ui._attach_row(row)
        return row

    def _close(self, outcome: str) -> None:
        with self._lock:
            rows = list(self._rows)
        for row in rows:
            row._end(outcome)
            self._ui._detach_task(row._task_id)
        if self.title:
            title_text = Text(
                f"{self._ui._glyph(LOG_GLYPHS[outcome])} {self.title}", style=outcome,
            )
            self._ui._emit_text(outcome, title_text)
        for row in rows:
            level, text = row._final(self._ui)
            self._ui._emit_text(level, text)


class UI:
    """Central terminal logger and progress coordinator."""

    Error = Error
    Group = Group
    Row = Row

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._console: RichConsole = _make_console()
        self._level: Level = "info"
        self._groups: list[Group] = []
        self._progress: RichProgress | None = None
        self._unicode: bool | None = None

    @property
    def raw(self) -> RichConsole:
        """Underlying `rich.console.Console` for tables, panels, or custom markup."""
        return self._console

    @property
    def level(self) -> Level:
        """Minimum log level displayed (`debug`, `info`, `ok`, `warn`, `error`, `quiet`)."""
        return self._level

    @level.setter
    def level(self, value: Level) -> None:
        """Set the minimum log level displayed."""
        if value not in LEVELS:
            raise ValueError(f"ui.level must be one of {', '.join(LEVELS)}, not {value!r}")
        self._level = value

    def debug(self, message: str = "", /, *args: Any, **fields: Any) -> None:
        """Log a debug-level message with optional structured key-value fields."""
        self._log("debug", message, args, fields)

    def info(self, message: str = "", /, *args: Any, **fields: Any) -> None:
        """Log an informational message with optional structured key-value fields."""
        self._log("info", message, args, fields)

    def ok(self, message: str = "", /, *args: Any, **fields: Any) -> None:
        """Log a success message with optional structured key-value fields."""
        self._log("ok", message, args, fields)

    def warn(self, message: str = "", /, *args: Any, **fields: Any) -> None:
        """Log a warning message with optional structured key-value fields."""
        self._log("warn", message, args, fields)

    def error(self, message: str = "", /, *args: Any, **fields: Any) -> None:
        """Log an error message with optional structured key-value fields."""
        self._log("error", message, args, fields)

    @contextmanager
    def progress(self, label: str, *, total: float | None = None) -> Iterator[Row]:
        """Context manager that displays a single live progress `Row`."""
        with self.group("") as grp:
            yield grp.add(label, total=total)

    @contextmanager
    def group(self, title: str = "") -> Iterator[Group]:
        """Context manager that displays a `Group` of multiple progress rows."""
        grp = Group(self, title)
        self._open_group(grp)
        try:
            yield grp
        except BaseException:
            self._close_group(grp, "error")
            raise
        self._close_group(grp, "ok")

    @contextmanager
    def scope(
        self,
        *,
        color: str | bool | None = None,
        dest: IO[str] | None = None,
        width: int | None = None,
        level: Level | None = None,
    ) -> Iterator[UI]:
        """Temporarily override console output stream, width, color, or log level."""
        with self._lock:
            prev_console = self._console
            prev_level = self._level
            prev_unicode = self._unicode
            if level is not None:
                self.level = level
            self._console = _scoped_console(prev_console, color=color, dest=dest, width=width)
            self._unicode = _detect_unicode(self._console.file)
        try:
            yield self
        finally:
            with self._lock:
                self._console = prev_console
                self._level = prev_level
                self._unicode = prev_unicode

    def _log(
        self,
        level: str,
        message: str,
        args: tuple[Any, ...],
        fields: dict[str, Any],
    ) -> None:
        unused = set(fields) - _field_names(message) if fields else ()
        if unused:
            raise TypeError(f"ui message {message!r} has no field {', '.join(sorted(unused))}")
        if LEVEL_RANKS[level] < LEVEL_RANKS[self._level]:
            return
        formatted = (
            _FORMATTER.vformat(message, args, fields)
            if (args or fields)
            else _sanitize(message)
        )
        self._emit_text(level, self._build_log_text(level, formatted))

    def _build_log_text(self, level: str, message: str) -> Text:
        glyph = self._glyph(LOG_GLYPHS[level])
        if not message or level == "debug":
            return Text(f"{glyph} {message}" if message else glyph, style=level)
        head, colon, detail = message.partition(": ")
        if level == "info":
            return Text.assemble((glyph, "info"), " ", (head, "text"), (colon + detail, "info"))
        return Text.assemble(
            (f"{glyph} {head}", level),
            (colon + detail, f"dim {level}") if colon else ("", ""),
        )

    def _emit_text(self, level: str, text: Text) -> None:
        if LEVEL_RANKS[level] < LEVEL_RANKS[self._level]:
            return
        with self._lock:
            if self._progress is not None and self._groups and self._level != "quiet":
                self._progress.console.print(text, highlight=False)
            else:
                self._console.print(text, highlight=False)

    def _open_group(self, grp: Group) -> None:
        with self._lock:
            self._groups.append(grp)
            if self._progress is None and self._level != "quiet":
                self._progress = RichProgress(
                    _RowColumn(self),
                    console=self._console,
                    transient=True,
                    disable=not self._console.is_terminal,
                )
                self._progress.start()

    def _close_group(self, grp: Group, outcome: str) -> None:
        grp._close(outcome)
        with self._lock:
            if grp in self._groups:
                self._groups.remove(grp)
            if not self._groups and self._progress is not None:
                self._progress.stop()
                self._progress = None

    def _attach_row(self, row: Row) -> None:
        with self._lock:
            if self._progress is not None:
                row._task_id = self._progress.add_task(
                    row.label, total=row.total, row=row,
                )

    def _update_task(self, task_id: TaskID, completed: float, total: float | None) -> None:
        with self._lock:
            if self._progress is not None:
                self._progress.update(task_id, completed=completed, total=total)

    def _detach_task(self, task_id: TaskID | None) -> None:
        with self._lock:
            if task_id is not None and self._progress is not None:
                try:
                    self._progress.remove_task(task_id)
                except KeyError:
                    pass

    def _use_unicode(self) -> bool:
        if self._unicode is not None:
            return self._unicode
        return _detect_unicode(self._console.file)

    def _glyph(self, name: str) -> str:
        pair = GLYPHS[name]
        return pair[0] if self._use_unicode() else pair[1]

    def _spinner(self) -> str:
        frames = SPIN_FRAMES[0] if self._use_unicode() else SPIN_FRAMES[1]
        return frames[int(clock.now() * 10) % len(frames)]


def _make_console(
    *,
    file: IO[str] | None = None,
    color_system: Literal["auto", "standard", "256", "truecolor", "windows"] | None = "auto",
    no_color: bool | None = None,
    width: int | None = None,
    force_terminal: bool | None = None,
) -> RichConsole:
    """Create a `rich.console.Console` configured with the shared palette theme."""
    return RichConsole(
        theme=Theme(PALETTE),
        stderr=file is None,
        file=file,
        color_system=color_system,
        no_color=no_color,
        width=width,
        force_terminal=force_terminal,
        soft_wrap=True,
    )


def _scoped_console(
    prev: RichConsole,
    *,
    color: str | bool | None,
    dest: IO[str] | None,
    width: int | None,
) -> RichConsole:
    """Build a scoped Rich console inheriting unchanged settings from `prev`."""
    file = dest if dest is not None else prev.file
    w = width if width is not None else prev._width
    color_map: dict[Any, tuple[Any, bool | None, bool | None]] = {
        None: (prev.color_system, prev.no_color, prev._force_terminal),
        False: (None, True, False),
        "none": (None, True, False),
        True: ("truecolor", False, True),
        "16m": ("truecolor", False, True),
        "truecolor": ("truecolor", False, True),
        "256": ("256", False, True),
        "8": ("standard", False, True),
        "standard": ("standard", False, True),
    }
    if color not in color_map:
        raise ValueError(f"unknown color setting {color!r}")
    cs, nc, ft = color_map[color]
    return _make_console(file=file, color_system=cs, no_color=nc, width=w, force_terminal=ft)


def _detect_unicode(stream: Any) -> bool:
    """Return whether `stream` supports UTF-8 without ambiguous-width issues."""
    encoding = getattr(stream, "encoding", None) or ""
    if encoding.lower().replace("-", "").replace("_", "") != "utf8":
        return False
    locale = os.environ.get("LC_ALL") or os.environ.get("LANG") or ""
    if locale.lower().startswith(("ja", "ko", "zh")):
        return False
    modern = ("WT_SESSION", "TERM", "ANSICON", "TERM_PROGRAM")
    if os.name == "nt" and not any(os.environ.get(k) for k in modern):
        return os.environ.get("ConEmuANSI") == "ON"
    return True


def _sanitize(text: str) -> str:
    """Strip raw CSI/OSC/ESC sequences and control codes from `text`."""
    return strip_control_codes(_ESCAPE.sub("", text))


@functools.lru_cache(maxsize=256)
def _field_names(message: str) -> frozenset[str]:
    """Extract all root field names from `message`, including nested format specs."""
    names: set[str] = set()
    for _, field_name, format_spec, _ in string.Formatter().parse(message):
        if field_name:
            names.add(field_name.split(".")[0].split("[")[0])
        if format_spec and "{" in format_spec:
            names.update(_field_names(format_spec))
    return frozenset(names)


def _render_bar(
    owner: UI, count: float, total: float | None, level: str, role: str,
) -> Text:
    """Render the bar cells or spinner for a `Row`."""
    if total is None and level == "info":
        return Text(owner._spinner().ljust(BAR_CELLS), style=role)
    filled = BAR_CELLS if total is None or total <= 0 else round(
        BAR_CELLS * min(max(count, 0), total) / total,
    )
    full = owner._glyph("bar_full") * filled
    empty = owner._glyph("bar_empty") * (BAR_CELLS - filled)
    return Text.assemble((full, role), (empty, "faint"))


def _format_count(count: float, total: float | None, level: str) -> str:
    """Format the count/total readout for a `Row`."""
    if level == "ok" and total is not None:
        return str(total)
    if total is not None:
        return f"{count}/{total}"
    return str(count) if count > 0 else ""


ui = UI()
debug = ui.debug
info = ui.info
ok = ui.ok
warn = ui.warn
error = ui.error
progress = ui.progress
group = ui.group
scope = ui.scope
