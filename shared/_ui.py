"""Terminal output and progress bars backed by Rich."""

from __future__ import annotations

import os
import re
import string
import threading
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from functools import lru_cache
from typing import IO, Any, Literal
from urllib.parse import urlsplit

from rich.console import Console as RichConsole
from rich.control import strip_control_codes
from rich.progress import (
    Progress as RichProgress,
    ProgressColumn,
    Task,
    TaskID,
)
from rich.theme import Theme
from rich.text import Text

import shared
from shared import _clock as clock
from shared._text import fmt

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
    "info": ("•", "*"),
    "ok": ("✓", "+"),
    "warn": ("!", "!"),
    "error": ("✗", "x"),
    "debug": ("·", "."),
    "bar_full": ("━", "="),
    "bar_empty": ("─", "-"),
}
LOG_GLYPHS: dict[str, str] = {
    "info": "info", "ok": "ok", "warn": "warn", "error": "error", "debug": "debug",
}
SPIN_FRAMES: tuple[tuple[str, ...], tuple[str, ...]] = (
    ("⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏"),
    ("|", "/", "-", "\\"),
)
BAR_CELLS = 20
_ESCAPE = re.compile(
    r"\x1b\[[0-?]*[ -/]*[@-~]|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)|\x1b[@-Z\\-_]"
)
_URL = re.compile(r"https?://[^\s:]+(?::\d+)?[^\s]*")


class Error(shared.Error):
    """Base error raised by `shared.ui`."""


class _SafeFormatter(string.Formatter):
    def format_field(self, value: Any, format_spec: str) -> str:
        if isinstance(value, str):
            value = _sanitize(value)
        return _sanitize(super().format_field(value, format_spec))


_FORMATTER = _SafeFormatter()


def _sanitize(value: str) -> str:
    return strip_control_codes(_ESCAPE.sub("", value))


@lru_cache(maxsize=256)
def _field_names(message: str) -> frozenset[str]:
    """Extract root field names from `message`, including nested format specs."""
    names: set[str] = set()
    for _, field_name, format_spec, _ in string.Formatter().parse(message):
        if field_name:
            names.add(field_name.split(".")[0].split("[")[0])
        if format_spec and "{" in format_spec:
            names.update(_field_names(format_spec))
    return frozenset(names)


def _color_settings(
    color: str | bool | None, base: RichConsole | None,
) -> tuple[Any, bool | None, bool | None]:
    """Map a color setting to `(color_system, no_color, force_terminal)`."""
    if color is None and base is not None:
        return base.color_system, base.no_color, base._force_terminal
    table: dict[Any, tuple[Any, bool | None, bool | None]] = {
        None: ("auto", None, None),
        False: (None, True, False),
        "none": (None, True, False),
        True: ("truecolor", False, True),
        "16m": ("truecolor", False, True),
        "truecolor": ("truecolor", False, True),
        "256": ("256", False, True),
        "8": ("standard", False, True),
        "standard": ("standard", False, True),
    }
    if color not in table:
        raise ValueError(f"unknown color setting {color!r}")
    return table[color]


def _detect_unicode(stream: Any) -> bool:
    """Return whether `stream` can render Unicode marks."""
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


class Row:
    """A single progress row inside a `Group` or `ui.progress`."""

    def __init__(
        self, label: str, total: float | None, group: Group, unit: str | None = None,
    ) -> None:
        self.label = label
        self._total = total
        self._count = 0.0
        self._group = group
        self._unit = unit
        self._reason: str | None = None
        self._outcome: Level | None = None
        self._task: TaskID = group._add_task(label, total)

    @property
    def raw(self) -> TaskID:
        """Underlying Rich `TaskID`."""
        return self._task

    @property
    def count(self) -> float:
        """Current completed units."""
        return self._count

    @count.setter
    def count(self, value: float) -> None:
        self._count = float(value)
        self._group._update(self._task, completed=self._count)

    @property
    def total(self) -> float | None:
        """Expected total units, or `None` if indeterminate."""
        return self._total

    @total.setter
    def total(self, value: float | None) -> None:
        self._total = value
        self._group._update(self._task, total=value)

    def advance(self, n: float = 1) -> None:
        """Increment progress by `n`."""
        self._count += n
        self._group._update(self._task, advance=n)

    def finish(self) -> None:
        """Mark this row completed."""
        self._outcome = "ok"
        if self._total is not None and self._count < self._total:
            self._count = self._total
            self._group._update(self._task, completed=self._total)
        self._group._stop_task(self._task)

    def fail(self, reason: str | None = None) -> None:
        """Mark this row failed, showing `reason` after it."""
        self._outcome = "error"
        self._reason = reason
        self._group._stop_task(self._task)

    def part(self) -> Callable[[int, int | None], None]:
        """Return a `(done, total)` callback that updates this row."""
        def _callback(done: int, total: int | None) -> None:
            if total is not None and total != self._total:
                self.total = total
            self.count = done

        return _callback

    def _render(self, owner: UI) -> Text:
        level = "info" if self._outcome is None else self._outcome
        role = "accent" if level == "info" else level
        text = Text()
        if self._total is None:
            mark = owner._spinner() if self._outcome is None else owner._glyph(LOG_GLYPHS[level])
            text.append(mark, style=role)
            text.append(" ")
            text.append_text(_label_text(self.label))
        else:
            text.append(owner._glyph(LOG_GLYPHS[level]), style=role)
            text.append(" ")
            text.append_text(_render_bar(owner, self._count, self._total, role))
            text.append(" ")
            text.append_text(_label_text(self.label))
            if count_str := _format_count(self._count, self._total, level, self._unit):
                text.append(" " + count_str, style="size")
        if self._reason:
            text.append(" — " + self._reason, style="dim error")
        return text


class _RowColumn(ProgressColumn):
    """Single Rich progress column rendering a `Row` with the Charm Huh theme."""

    def __init__(self, owner: UI, lookup: Callable[[TaskID], Row | None]) -> None:
        super().__init__()
        self._owner = owner
        self._lookup = lookup

    def render(self, task: Task) -> Text:
        row = self._lookup(task.id)
        if row is None:
            return Text("")
        return row._render(self._owner)


def _render_bar(owner: UI, count: float, total: float, role: str) -> Text:
    ratio = 1.0 if total <= 0 else min(max(count, 0.0), total) / total
    filled = round(BAR_CELLS * ratio)
    full = owner._glyph("bar_full") * filled
    empty = owner._glyph("bar_empty") * (BAR_CELLS - filled)
    return Text.assemble((full, role), (empty, "faint"))


def _label_text(label: str) -> Text:
    """A path label with its folders dim and its file name in the normal color."""
    folder, slash, name = label.rpartition("/")
    if not slash or " | " in label:
        return Text(label, style="text")
    return Text.assemble((folder + slash, "info"), (name, "text"))


def _format_count(
    count: float, total: float | None, level: str, unit: str | None = None,
) -> str:
    def one(value: float) -> str:
        if unit == "bytes":
            return fmt.size(value)
        return str(int(value) if float(value).is_integer() else value)

    if level == "ok" and total is not None:
        return one(total)
    if total is not None:
        return f"{one(count)}/{one(total)}"
    return one(count) if count > 0 else ""


def _brief(error: BaseException, *, limit: int = 100) -> str:
    """One line naming `error`, with each URL cut to its host."""
    first = (str(error).strip().splitlines() or [""])[0]
    first = _URL.sub(lambda match: urlsplit(match.group()).netloc or match.group(), first)
    message = f"{type(error).__name__}: {first}" if first else type(error).__name__
    return message if len(message) <= limit else message[: limit - 1] + "…"


class Group:
    """A Rich progress group holding one or more `Row` bars."""

    def __init__(self, owner: UI, title: str) -> None:
        self._ui = owner
        self.title = title
        self._lock = threading.Lock()
        self._rows: list[Row] = []
        self._tasks: dict[TaskID, Row] = {}
        self._progress: RichProgress | None = None
        if owner._progress_enabled():
            self._progress = RichProgress(
                _RowColumn(owner, self._tasks.get),
                console=owner.raw,
                transient=True,
            )

    def _start(self) -> None:
        if self.title and self._ui._allows("info"):
            self._ui.info(self.title)
        if self._progress is not None:
            self._progress.start()

    def _stop(self, outcome: Level = "ok") -> None:
        for row in self._rows:
            if row._outcome is None:
                row._outcome = outcome
        if self._progress is not None:
            self._progress.stop()
            if self._ui._allows("ok"):
                for row in self._rows:
                    if row._outcome is not None and self._ui._allows(row._outcome):
                        self._ui._console.print(row._render(self._ui))
            return
        for row in self._rows:
            self._ui._log(row._outcome or outcome, row.label)

    def _add_task(self, label: str, total: float | None) -> TaskID:
        if self._progress is None:
            return TaskID(0)
        with self._lock:
            return self._progress.add_task(_sanitize(label), total=total)

    def _update(self, task_id: TaskID, **kwargs: Any) -> None:
        if self._progress is None:
            return
        with self._lock:
            self._progress.update(task_id, **kwargs)

    def _stop_task(self, task_id: TaskID) -> None:
        if self._progress is None:
            return
        with self._lock:
            self._progress.stop_task(task_id)

    def add(
        self, label: str, *, total: float | None = None, unit: str | None = None,
    ) -> Row:
        """Add a new `Row` to this group; `unit="bytes"` shows its counts as sizes."""
        row = Row(label, total, self, unit)
        with self._lock:
            self._rows.append(row)
            self._tasks[row._task] = row
        return row


class UI:
    """Terminal logging and progress helper."""

    Error = Error
    Group = Group
    Row = Row

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._level: Level = "info"
        self._console = self._make_console()

    @staticmethod
    def _make_console(
        *,
        color: str | bool | None = None,
        dest: IO[str] | None = None,
        width: int | None = None,
        base: RichConsole | None = None,
    ) -> RichConsole:
        color_system, no_color, force_terminal = _color_settings(color, base)
        file = dest if dest is not None else (base.file if base is not None else None)
        return RichConsole(
            file=file,
            stderr=file is None,
            width=width if width is not None else (base.width if base is not None else None),
            color_system=color_system,
            no_color=no_color,
            force_terminal=force_terminal,
            theme=Theme(PALETTE),
            highlight=False,
            soft_wrap=True,
        )

    def _use_unicode(self) -> bool:
        return _detect_unicode(self._console.file)

    def _glyph(self, name: str) -> str:
        unicode, ascii_ = GLYPHS[name]
        return unicode if self._use_unicode() else ascii_

    def _spinner(self) -> str:
        frames = SPIN_FRAMES[0] if self._use_unicode() else SPIN_FRAMES[1]
        return frames[int(clock.now() * 10) % len(frames)]

    @property
    def raw(self) -> RichConsole:
        """Underlying Rich `Console`."""
        return self._console

    @property
    def level(self) -> Level:
        """Current minimum log level."""
        return self._level

    @level.setter
    def level(self, value: Level) -> None:
        if value not in LEVELS:
            raise ValueError(f"unknown ui level {value!r}; choose from {', '.join(LEVELS)}")
        self._level = value

    def _allows(self, kind: Level) -> bool:
        return LEVEL_RANKS[kind] >= LEVEL_RANKS[self._level]

    def _progress_enabled(self) -> bool:
        return self._level != "quiet" and self._console.is_terminal

    def _log(self, _kind: Level, message: str, *args: Any, **fields: Any) -> None:
        unused = set(fields) - _field_names(message) if fields else set()
        if unused:
            raise TypeError(
                f"ui message {message!r} has no field {', '.join(sorted(unused))}"
            )
        text = (
            _FORMATTER.format(message, *args, **fields)
            if (args or fields) else _sanitize(message)
        )
        if not self._allows(_kind):
            return
        mark = self._glyph(LOG_GLYPHS.get(_kind, "info"))
        with self._lock:
            if not text or _kind == "debug":
                line = Text(f"{mark} {text}" if text else mark, style=_kind)
            elif _kind == "info":
                head, colon, detail = text.partition(": ")
                line = Text.assemble(
                    (mark, "info"), " ", (head, "text"), (colon + detail, "info"),
                )
            else:
                head, colon, detail = text.partition(": ")
                line = Text.assemble(
                    (f"{mark} {head}", _kind),
                    (colon + detail, f"dim {_kind}") if colon else ("", ""),
                )
            self._console.print(line)

    def debug(self, message: str = "", /, *args: Any, **fields: Any) -> None:
        """Log a debug message."""
        self._log("debug", message, *args, **fields)

    def info(self, message: str = "", /, *args: Any, **fields: Any) -> None:
        """Log an informational message."""
        self._log("info", message, *args, **fields)

    def ok(self, message: str = "", /, *args: Any, **fields: Any) -> None:
        """Log a success message."""
        self._log("ok", message, *args, **fields)

    def warn(self, message: str = "", /, *args: Any, **fields: Any) -> None:
        """Log a warning message."""
        self._log("warn", message, *args, **fields)

    def error(self, message: str = "", /, *args: Any, **fields: Any) -> None:
        """Log an error message."""
        self._log("error", message, *args, **fields)

    @contextmanager
    def scope(
        self,
        *,
        color: str | bool | None = None,
        dest: IO[str] | None = None,
        width: int | None = None,
        level: Level | None = None,
    ) -> Iterator[UI]:
        """Temporarily override console output target, color, width, or log level."""
        old_console, old_level = self._console, self._level
        try:
            self._console = self._make_console(
                color=color, dest=dest, width=width, base=self._console,
            )
            if level is not None:
                self.level = level
            yield self
        finally:
            self._console, self._level = old_console, old_level

    @contextmanager
    def group(self, title: str = "") -> Iterator[Group]:
        """Context manager yielding a `Group` of progress rows."""
        grp = Group(self, title)
        grp._start()
        try:
            yield grp
        except BaseException:
            grp._stop("error")
            raise
        grp._stop("ok")

    @contextmanager
    def progress(
        self, label: str, *, total: float | None = None, unit: str | None = None,
    ) -> Iterator[Row]:
        """Context manager yielding a single `Row` progress bar; `unit="bytes"` shows sizes."""
        with self.group() as grp:
            row = grp.add(label, total=total, unit=unit)
            try:
                yield row
                if row._outcome is None:
                    row.finish()
            except BaseException as error:
                if isinstance(error, KeyboardInterrupt):
                    row.fail("stopped")
                else:
                    row.fail(_brief(error))
                raise


ui = UI()
