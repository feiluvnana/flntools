"""Terminal output, progress bars, tables, trees, panels and log recording backed by Rich.

    ui.level = "debug"
    ui.ok("{n} saved", n=3)
    with ui.progress("photos", total=10) as bar:
        bar.advance()
"""

from __future__ import annotations

import functools
import os
import re
import string
import sys
import threading
from collections.abc import Callable, Iterable, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, replace
from datetime import datetime
from pathlib import Path
from typing import IO, Any, Literal, NamedTuple, cast

from rich.console import Console as RichConsole
from rich.console import RenderableType
from rich.control import strip_control_codes
from rich.errors import StyleError, StyleSyntaxError
from rich.panel import Panel
from rich.progress import Progress as RichProgress
from rich.progress import ProgressColumn, Task, TaskID
from rich.rule import Rule
from rich.style import Style
from rich.table import Table
from rich.text import Text
from rich.theme import Theme
from rich.tree import Tree

import shared
from shared import _clock as clock

__all__ = [
    "Console",
    "Count",
    "Error",
    "Group",
    "Row",
    "Status",
    "StatusRow",
    "UI",
    "ui",
]

Level = Literal["debug", "info", "ok", "warn", "error", "quiet"]
LEVELS: tuple[Level, ...] = ("debug", "info", "ok", "warn", "error", "quiet")
LEVEL_RANKS: dict[str, int] = {
    "debug": 0, "info": 1, "ok": 2, "warn": 3, "error": 4, "quiet": 4,
}
EVENTS: tuple[str, ...] = ("skip", "link", "shrink")

PALETTE: dict[str, str] = {
    "info": "color(243)",
    "ok": "#02BF87",
    "warn": "yellow",
    "error": "#ED567A",
    "debug": "color(242)",
    "title": "bold #7571F9",
    "cursor": "#F780E2",
    "chosen": "#02BF87",
    "text": "color(252)",
    "muted": "color(243)",
    "faint": "color(238)",
    "key": "color(241)",
    "hint": "color(239)",
    "label": "magenta",
    "cream": "#FFFDF5",
    "button": "color(237)",
    "accent": "#7D84D6",
    "glow": "#A9AEF0",
    "target": "bold color(252)",
    "category": "#9A96C9",
    "size": "color(245)",
    "path": "color(245)",
    "skip": "color(241)",
    "link": "#6FA3D8",
    "download": "#5FBDC9",
    "shrink": "#B86FB8",
    "wait": "#D7B65F",
}

GLYPHS: dict[str, tuple[str, str]] = {
    "bullet": ("•", "*"),
    "tick": ("✓", "+"),
    "bang": ("!", "!"),
    "cross": ("✗", "x"),
    "dot": ("·", "."),
    "skip": ("↷", ">"),
    "link": ("⤷", "="),
    "shrink": ("⟳", "~"),
    "found": ("◦", "#"),
    "download": ("⇣", "v"),
    "arrow": ("→", "->"),
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
COUNT_WIDTH = 4

_ESCAPE = re.compile(
    r"\x1b\[[0-?]*[ -/]*[@-~]"
    r"|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)"
    r"|\x1b[@-Z\\-_]"
)
_URL_IN_LINE = re.compile(r"\b[a-z][a-z0-9+.-]*://[^\s'\"<>]+", re.IGNORECASE)
_QUERY_IN_URL = re.compile(r"(\b[a-z][a-z0-9+.-]*://[^\s?#'\"<>]*)[?#][^\s'\"<>]*", re.IGNORECASE)

try:
    from shared._text import scrub_url as _scrub_url  # type: ignore[import-not-found]
except ImportError:
    def _scrub_url(url: str) -> str:
        """Strip userinfo, query and fragment from a URL."""
        cleaned = _QUERY_IN_URL.sub(r"\1", url)
        scheme, sep, rest = cleaned.partition("://")
        if not sep:
            return cleaned
        netloc, slash, path = rest.partition("/")
        if "@" in netloc:
            netloc = netloc.rsplit("@", 1)[-1]
        return f"{scheme}://{netloc}{slash}{path}"


class Error(shared.Error):
    """Base error raised by `shared.ui`."""


class Count(NamedTuple):
    """One counter in a status row."""

    number: int
    word: str
    role: str
    glyph: str


@dataclass(frozen=True)
class Status:
    """State rendered by a `StatusRow`."""

    subject: str
    label: str = ""
    counts: tuple[Count, ...] = ()
    running: tuple[Count, ...] = ()
    tail: tuple[str, ...] = ()
    note: str = ""
    subject_width: int = 0
    label_width: int = 0
    outcome: str | None = None
    elapsed: float | None = None
    width: int = 0


class Console:
    """Wrapper around `rich.console.Console` exposing `.raw`."""

    def __init__(self, raw: RichConsole | None = None) -> None:
        self.raw: RichConsole = raw or _make_console()

    @property
    def width(self) -> int:
        """Current console width in columns."""
        return self.raw.width

    @property
    def height(self) -> int:
        """Current console height in lines."""
        return self.raw.height

    @property
    def is_terminal(self) -> bool:
        """Whether the underlying stream is a terminal."""
        return self.raw.is_terminal

    def print(self, *objects: Any, style: str | Style | None = None) -> None:
        """Print renderables to the underlying Rich console."""
        self.raw.print(*objects, style=style, highlight=False)


class _SafeFormatter(string.Formatter):
    """Formatter that strips raw CSI/OSC escapes and control codes from fields."""

    def format_field(self, value: Any, format_spec: str) -> str:
        if isinstance(value, str):
            value = _sanitize(value)
        return _sanitize(super().format_field(value, format_spec))


_FORMATTER = _SafeFormatter()


class _Record:
    """Thread-safe log file sink with locked write and close."""

    def __init__(self, file: IO[str]) -> None:
        self._file = file
        self._lock = threading.Lock()
        self._closed = False

    def write(self, text: str) -> None:
        """Append `text` and flush if still open."""
        with self._lock:
            if self._closed:
                return
            try:
                self._file.write(text)
                self._file.flush()
            except OSError:
                pass

    def close(self) -> None:
        """Close the file under lock so concurrent writes never hit a closed stream."""
        with self._lock:
            if not self._closed:
                self._closed = True
                self._file.close()


class _RowColumn(ProgressColumn):
    """Custom Rich column rendering both `Row` bars and `StatusRow` lines."""

    def __init__(self, owner: UI) -> None:
        super().__init__()
        self._ui = owner

    def render(self, task: Task) -> RenderableType:
        """Render one task as either a status line or a progress row."""
        kind = task.fields.get("kind")
        if kind == "status":
            row = cast(StatusRow, task.fields["row"])
            return row._render(self._ui)
        row_obj = cast(Row, task.fields["row"])
        return row_obj._render(self._ui)


class Row:
    """One progress row inside `ui.progress` or `ui.group`."""

    def __init__(
        self,
        label: str,
        total: float | None,
        show: Callable[[Any], str],
        group: Group,
        *,
        wait: bool = False,
    ) -> None:
        """Initialize a progress row."""
        self.label = label
        self.wait = wait
        self.slots: int | None = None
        self._show = show
        self._total = total
        self._caption = ""
        self._count: float = 0
        self._group = group
        self._outcome: str | None = None
        self._lock = threading.Lock()
        self._task_id: TaskID | None = None

    @property
    def count(self) -> float:
        """Amount completed so far."""
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
        """Total work expected, or None for an indeterminate spinner."""
        with self._lock:
            return self._total

    @total.setter
    def total(self, value: float | None) -> None:
        """Set total work expected."""
        with self._lock:
            self._total = value
        self._sync()

    @property
    def caption(self) -> str:
        """Trailing note shown after the count."""
        with self._lock:
            return self._caption

    @caption.setter
    def caption(self, value: str) -> None:
        """Set trailing note shown after the count."""
        with self._lock:
            self._caption = value
        self._sync()

    @property
    def outcome(self) -> str | None:
        """Final state (`"ok"` or `"error"`), or None while running."""
        with self._lock:
            return self._outcome

    @property
    def finished(self) -> bool:
        """Whether the row has ended."""
        with self._lock:
            return self._outcome is not None

    def advance(self, n: float = 1) -> None:
        """Add `n` to `count` safely across threads."""
        with self._lock:
            self._count = max(self._count + n, 0)
        self._sync()

    def part(self) -> Callable[[int, int], None]:
        """Return a thread-safe `progress(done, total)` callback for this row."""
        def update(done: int, total: int) -> None:
            with self._lock:
                self._total = total
                self._count = max(done, 0)
            self._sync()

        return update

    def finish(self) -> None:
        """Mark the row as `"ok"` and clear its caption."""
        self._end("ok")

    def fail(self) -> None:
        """Mark the row as `"error"` and keep its caption."""
        self._end("error")

    def _end(self, outcome: str) -> None:
        with self._lock:
            if self._outcome is not None:
                return
            self._outcome = outcome
            if outcome == "ok":
                self._caption = ""
        self._sync()

    def _sync(self) -> None:
        """Push updated state to the Rich progress task if attached."""
        if self._task_id is not None:
            self._group._ui._update_task(self._task_id, self._count, self._total)

    def _final(self, owner: UI) -> tuple[str, Text]:
        """Return `(level, rendered Text)` for the row's final scrollback line."""
        with self._lock:
            level = "info" if self._outcome is None else self._outcome
            return level, self._build_text(owner, level)

    def _render(self, owner: UI) -> Text:
        """Render the row for the live display."""
        with self._lock:
            level = "info" if self._outcome is None else self._outcome
            return self._build_text(owner, level)

    def _build_text(self, owner: UI, level: str) -> Text:
        """Assemble Rich `Text` for this progress row."""
        role = level
        if level == "info":
            role = "wait" if self.wait else "accent"
        glyph = owner._glyph(LOG_GLYPHS[level])
        text = Text()
        text.append(glyph, style=role)
        text.append(" ")
        if self._total is None and self._count <= 0:
            text.append(self.label, style="text")
            if self._outcome is None:
                text.append(" " + owner._spinner(), style=role)
            if self._caption:
                text.append(" " + _sanitize(self._caption), style="muted")
            return text
        text.append_text(_render_bar(owner, self._count, self._total, level, role))
        text.append(" " + self.label, style="text")
        count_str = _format_count(self._count, self._total, level, self._show)
        if count_str:
            text.append(" " + count_str, style="size")
        if self._caption:
            text.append(" " + _sanitize(self._caption), style="muted")
        return text


class StatusRow:
    """One live status line inside `ui.group` rendered via a custom Rich column."""

    def __init__(self, status: Status, group: Group) -> None:
        self.slots: int | None = None
        self._status = status
        self._group = group
        self._outcome: str | None = None
        self._lock = threading.Lock()
        self._created = clock.now()
        self._ended = self._created
        self._task_id: TaskID | None = None

    @property
    def status(self) -> Status:
        """Current `Status` snapshot for this row."""
        with self._lock:
            return self._status

    @status.setter
    def status(self, value: Status) -> None:
        """Update the `Status` snapshot and refresh the live row."""
        with self._lock:
            self._status = value
        if self._task_id is not None:
            self._group._ui._refresh_task(self._task_id)

    @property
    def outcome(self) -> str | None:
        """Final state (`"ok"` or `"error"`), or None while running."""
        with self._lock:
            return self._outcome

    @property
    def finished(self) -> bool:
        """Whether the status row has ended."""
        with self._lock:
            return self._outcome is not None

    def finish(self) -> None:
        """End the status row as `"ok"` and retire it to scrollback."""
        self._end("ok")

    def fail(self) -> None:
        """End the status row as `"error"` and retire it to scrollback."""
        self._end("error")

    def _end(self, outcome: str) -> None:
        with self._lock:
            if self._outcome is not None:
                return
            self._outcome = outcome
            self._ended = clock.now()
        self._group._retire(self)

    def _final(self, owner: UI) -> tuple[str, Text]:
        """Return `(level, rendered Text)` for the final scrollback line."""
        with self._lock:
            level = self._outcome or "info"
            snap = replace(
                self._status, outcome=self._outcome, elapsed=self._ended - self._created, width=0,
            )
        return level, _render_status(owner, snap)

    def _render(self, owner: UI) -> Text:
        """Render the status row for the live display."""
        with self._lock:
            snap = replace(
                self._status,
                outcome=self._outcome,
                elapsed=clock.now() - self._created,
                width=owner.console.width,
            )
        return _render_status(owner, snap)


class Group:
    """Collection of `Row` and `StatusRow` items managed under one `ui.group` block."""

    def __init__(self, owner: UI, title: str) -> None:
        self._ui = owner
        self.title = title
        self._rows: list[Row | StatusRow] = []
        self._outcome: str | None = None
        self._lock = threading.Lock()

    def add(
        self,
        label: str,
        *,
        total: float | None = None,
        show: Callable[[Any], str] = str,
        wait: bool = False,
    ) -> Row:
        """Add a progress `Row` to this group and return it."""
        row = Row(label, total, show, self, wait=wait)
        with self._lock:
            self._rows.append(row)
        self._ui._attach_row(row)
        return row

    def add_status(self, status: Status) -> StatusRow:
        """Add a `StatusRow` to this group and return it."""
        row = StatusRow(status, self)
        with self._lock:
            self._rows.append(row)
        self._ui._attach_status(row)
        return row

    def _retire(self, row: StatusRow) -> None:
        """Emit a finished `StatusRow` to scrollback and remove it from live tasks."""
        level, text = row._final(self._ui)
        with self._lock:
            if row in self._rows:
                self._rows.remove(row)
        self._ui._detach_task(row._task_id)
        self._ui._emit_text(level, text)

    def _close(self, outcome: str) -> None:
        """Finish remaining rows and emit their final lines."""
        with self._lock:
            rows = list(self._rows)
            self._outcome = outcome
        for row in rows:
            row._end(outcome)
            self._ui._detach_task(row._task_id)
        if self.title:
            title_text = Text(f"{self._ui._glyph(LOG_GLYPHS[outcome])} {self.title}", style=outcome)
            self._ui._emit_text(outcome, title_text)
        for row in rows:
            level, text = row._final(self._ui)
            self._ui._emit_text(level, text)


class UI:
    """Rich-backed terminal UI: logging, progress, tables, trees, panels and recording."""

    Error = Error

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._prompt_lock = threading.RLock()
        self._console = Console()
        self._level: Level = "info"
        self._records: list[_Record] = []
        self._groups: list[Group] = []
        self._progress: RichProgress | None = None
        self._paused = 0
        self._held: list[tuple[str, Text]] = []
        self._unicode: bool | None = None

    @property
    def console(self) -> Console:
        """Active `Console` wrapper."""
        return self._console

    @property
    def level(self) -> Level:
        """Minimum log level printed (`debug`, `info`, `ok`, `warn`, `error`, or `quiet`)."""
        return self._level

    @level.setter
    def level(self, value: Level) -> None:
        """Set the minimum log level printed."""
        if value not in LEVELS:
            raise ValueError(f"ui.level must be one of {', '.join(LEVELS)}, not {value!r}")
        self._level = value

    def debug(self, message: str = "", /, *args: Any, **fields: Any) -> None:
        """Log a debug line."""
        self._log("debug", message, args, fields)

    def info(self, message: str = "", /, *args: Any, **fields: Any) -> None:
        """Log an info line."""
        self._log("info", message, args, fields)

    def ok(self, message: str = "", /, *args: Any, **fields: Any) -> None:
        """Log a success line."""
        self._log("ok", message, args, fields)

    def warn(self, message: str = "", /, *args: Any, **fields: Any) -> None:
        """Log a warning line."""
        self._log("warn", message, args, fields)

    def error(self, message: str = "", /, *args: Any, **fields: Any) -> None:
        """Log an error line."""
        self._log("error", message, args, fields)

    def event(self, kind: str, message: str, /, *args: Any, **fields: Any) -> None:
        """Log a debug event line using `kind`'s glyph and palette color."""
        if kind not in EVENTS:
            raise ValueError(f"ui.event kind must be one of {', '.join(EVENTS)}, not {kind!r}")
        self._log("debug", message, args, fields, kind=kind)

    def table(
        self,
        columns: Sequence[str] = (),
        *,
        rows: Iterable[Sequence[Any]] = (),
        title: str | None = None,
        show_header: bool = True,
    ) -> Table:
        """Build a styled Rich `Table`, printing it when `rows` are provided."""
        tbl = Table(
            title=title,
            show_header=show_header,
            header_style="title",
            border_style="faint",
        )
        for col in columns:
            tbl.add_column(col)
        row_list = list(rows)
        for row in row_list:
            tbl.add_row(*(str(cell) for cell in row))
        if row_list:
            self._print_renderable(tbl)
        return tbl

    def tree(self, label: Any, *, style: str = "title", guide_style: str = "faint") -> Tree:
        """Return a styled Rich `Tree`."""
        return Tree(label, style=style, guide_style=guide_style)

    def panel(self, content: Any, *, title: str | None = None, style: str = "faint") -> Panel:
        """Print and return a styled Rich `Panel`."""
        pnl = Panel(content, title=title, border_style=style)
        self._print_renderable(pnl)
        return pnl

    def rule(self, title: str = "", *, style: str = "faint") -> Rule:
        """Print and return a styled Rich `Rule`."""
        rl = Rule(title, style=style)
        self._print_renderable(rl)
        return rl

    def style(self, spec: str | Style) -> Style:
        """Parse a style specification into a `rich.style.Style`."""
        if isinstance(spec, Style):
            return spec
        try:
            return Style.parse(spec)
        except (StyleError, StyleSyntaxError) as err:
            raise Error(f"invalid style {spec!r}: {err}") from err

    def strip(self, text: str) -> str:
        """Remove ANSI escape sequences and control codes from `text`."""
        return strip_control_codes(Text.from_ansi(text).plain)

    @contextmanager
    def progress(
        self,
        label: str,
        *,
        total: float | None = None,
        show: Callable[[Any], str] = str,
        wait: bool = False,
    ) -> Iterator[Row]:
        """Context manager displaying a single `Row` that finishes on exit or fails on error."""
        with self.group("") as grp:
            yield grp.add(label, total=total, show=show, wait=wait)

    @contextmanager
    def group(self, title: str = "") -> Iterator[Group]:
        """Context manager managing a group of progress and status rows."""
        grp = Group(self, title)
        self._open_group(grp)
        try:
            yield grp
        except BaseException:
            self._close_group(grp, "error")
            raise
        self._close_group(grp, "ok")

    @contextmanager
    def record(self, path: Path) -> Iterator[None]:
        """Mirror all log lines and finished rows to `path` with timestamps and scrubbed URLs."""
        target = Path(path)
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            file = open(target, "a", encoding="utf-8")
        except OSError as err:
            raise Error(f"cannot open log file {target}: {err}") from err
        sink = _Record(file)
        with self._lock:
            self._records.append(sink)
        try:
            yield
        finally:
            with self._lock:
                if sink in self._records:
                    self._records.remove(sink)
            sink.close()

    @contextmanager
    def scope(
        self,
        *,
        color: str | bool | None = None,
        dest: IO[str] | None = None,
        width: int | None = None,
        level: Level | None = None,
    ) -> Iterator[UI]:
        """Temporarily override console destination, color, width or level for tests."""
        with self._lock:
            prev_raw = self._console.raw
            prev_level = self._level
            prev_unicode = self._unicode
            if level is not None:
                self.level = level
            self._console.raw = _scoped_console(prev_raw, color=color, dest=dest, width=width)
            self._unicode = _detect_unicode(self._console.raw.file)
        try:
            yield self
        finally:
            with self._lock:
                self._console.raw = prev_raw
                self._level = prev_level
                self._unicode = prev_unicode

    @contextmanager
    def pause_live(self) -> Iterator[None]:
        """Pause the live progress area while interactive prompts run."""
        with self._prompt_lock:
            with self._lock:
                self._paused += 1
                if self._paused == 1 and self._progress is not None:
                    self._progress.stop()
            try:
                yield
            finally:
                with self._lock:
                    self._paused -= 1
                    if self._paused == 0:
                        held, self._held = self._held, []
                        for lvl, text in held:
                            self._write_screen(lvl, text)
                        if self._progress is not None and self._groups and self._level != "quiet":
                            self._progress.start()

    def _log(
        self,
        level: str,
        message: str,
        args: tuple[Any, ...],
        fields: dict[str, Any],
        *,
        kind: str = "",
    ) -> None:
        """Validate fields, format, sanitize and emit a log line."""
        unused = set(fields) - _field_names(message) if fields else ()
        if unused:
            raise TypeError(f"ui message {message!r} has no field {', '.join(sorted(unused))}")
        shown = LEVEL_RANKS[level] >= LEVEL_RANKS[self._level]
        with self._lock:
            has_records = bool(self._records)
        if not shown and not has_records:
            return
        formatted = (
            _FORMATTER.vformat(message, args, fields)
            if (args or fields)
            else _sanitize(message)
        )
        text = self._build_log_text(level, formatted, kind=kind)
        self._emit_text(level, text)

    def _build_log_text(self, level: str, message: str, *, kind: str = "") -> Text:
        if kind:
            return Text(f"{self._glyph(kind)} {message}", style=kind)
        glyph = self._glyph(LOG_GLYPHS[level])
        if not message or level == "debug":
            return Text(f"{glyph} {message}" if message else glyph, style=level)
        head, colon, detail = message.partition(": ")
        if level == "info":
            return Text.assemble((glyph, "info"), " ", (head, "text"), (colon + detail, "info"))
        dim_style = f"dim {level}"
        return Text.assemble(
            (f"{glyph} {head}", level),
            (colon + detail, dim_style) if colon else ("", ""),
        )

    def _emit_text(self, level: str, text: Text) -> None:
        self._write_records(text.plain)
        if LEVEL_RANKS[level] < LEVEL_RANKS[self._level]:
            return
        with self._lock:
            if self._paused > 0:
                self._held.append((level, text))
                return
            self._write_screen(level, text)

    def _write_screen(self, level: str, text: Text) -> None:
        if self._progress is not None and self._groups and self._level != "quiet":
            self._progress.console.print(text, highlight=False)
        else:
            self._console.raw.print(text, highlight=False)

    def _print_renderable(self, renderable: RenderableType) -> None:
        with self._lock:
            if self._progress is not None and self._groups and self._level != "quiet":
                self._progress.console.print(renderable, highlight=False)
            else:
                self._console.raw.print(renderable, highlight=False)

    def _write_records(self, plain: str) -> None:
        with self._lock:
            records = list(self._records)
        if not records:
            return
        stamp = datetime.fromtimestamp(clock.wall()).strftime("%H:%M:%S.%f")[:-3]
        scrubbed = _URL_IN_LINE.sub(lambda m: _scrub_url(m.group(0)), plain)
        payload = "".join(f"{stamp} {line}\n" for line in scrubbed.splitlines() or [""])
        for sink in records:
            sink.write(payload)

    def _open_group(self, grp: Group) -> None:
        with self._lock:
            self._groups.append(grp)
            if self._progress is None and self._level != "quiet":
                self._progress = RichProgress(
                    _RowColumn(self),
                    console=self._console.raw,
                    transient=True,
                    disable=not self._console.is_terminal,
                )
                if self._paused == 0:
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
                    row.label, total=row.total, kind="row", row=row,
                )

    def _attach_status(self, row: StatusRow) -> None:
        with self._lock:
            if self._progress is not None:
                row._task_id = self._progress.add_task(
                    row.status.subject, total=None, kind="status", row=row,
                )

    def _update_task(self, task_id: TaskID, completed: float, total: float | None) -> None:
        with self._lock:
            if self._progress is not None:
                self._progress.update(task_id, completed=completed, total=total)

    def _refresh_task(self, task_id: TaskID) -> None:
        with self._lock:
            if self._progress is not None:
                self._progress.update(task_id, refresh=True)

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
        return _detect_unicode(self._console.raw.file)

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


def _format_count(
    count: float, total: float | None, level: str, show: Callable[[Any], str],
) -> str:
    """Format the count/total readout for a `Row`."""
    if level == "ok" and total is not None:
        return show(total)
    if total is not None:
        return f"{show(count)}/{show(total)}"
    return show(count) if count > 0 else ""


def _render_status(owner: UI, status: Status) -> Text:
    """Render a `Status` snapshot into Rich `Text`."""
    text = Text()
    if status.outcome == "ok":
        glyph = owner._glyph("tick")
    elif status.outcome == "error":
        glyph = owner._glyph("cross")
    else:
        glyph = owner._spinner()
    text.append(glyph, style=status.outcome or "accent")
    text.append(" " + status.subject.ljust(status.subject_width), style="target")
    if status.label:
        text.append(" " + status.label.ljust(status.label_width), style="category")
    for c in status.counts:
        role = c.role if c.number > 0 else "muted"
        text.append(" " + f"{c.number:,}".rjust(COUNT_WIDTH), style=role)
        text.append(" " + c.word, style="muted")
    for item in status.tail:
        text.append(" " + item, style="size")
    if status.elapsed is not None:
        text.append(f" {int(status.elapsed)}s", style="size")
    return text


ui = UI()
