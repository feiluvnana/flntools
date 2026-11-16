"""Terminal output: log lines and progress rows, on a Rich console, safe for site text.

A library module: it imports only the stdlib, Rich and other library modules (`_time`,
`_text`), so any program can log through it. The questions a program asks are `_prompt`'s.

What it offers, through the one shared `ui` object:
1. The log: `ui.info("saved {name}", name=…)` and its siblings `debug`, `ok`, `warn` and
   `error` print one line each, with a mark (`✓`, `!`, …) and the level's color. Each field is
   formatted by `_SafeFormatter`, which strips escape and control codes, so a file name from a
   site can't move the cursor or rewrite the screen. A field the message doesn't name is a
   `TypeError`, so a typo can't silently drop a value.
2. Progress rows: `ui.group(title)` holds rows made with `Group.add`; `ui.progress(label)` is a
   group of one row. On a terminal they are live Rich bars that vanish when the group ends and
   leave one finished line per row; elsewhere each row is logged once at the end. A group made
   with `drawn=False` only counts and shows nothing, for a caller that draws progress its own
   way (flnd's CLI draws a run from its events).
3. `ui.level` (what is shown, `quiet` hides even progress) and `ui.scope(…)`, which swaps the
   console's destination, color, width or level inside a block, as tests and `--color` do.

Who calls it: every flnd module that tells a person something (the HTTP client's retries, the
browser, cookies, the home, the downloader), and the CLI, which sets the level.

How a log line is made (`UI._log`):
1. Check every keyword field is named in the message (`_field_names`, cached per message).
2. Format the fields safely, or just sanitise a message without fields.
3. Drop the line if its level is below `ui.level`.
4. Build the line: the mark, the text up to the first `: ` bright, the detail after it dim.
5. Print it under the lock.

Rules a change must keep:
- `_log` holds the lock while it prints, so lines of two threads never interleave; the lock is an
  `RLock`, so code that logs while it already holds the lock can't deadlock.
- A `Group`'s own lock guards its Rich task table: download threads update rows at once.
- Nothing else may print to the console a group draws on while its bars are live except
  through `ui`, or Rich can't keep the bars below the lines.
"""

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

from flnd import _time as clock
from flnd._text import size

__all__ = ["Group", "Row", "UI", "ui"]

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


class _SafeFormatter(string.Formatter):
    """`str.format` that strips terminal escape and control codes from each field it formats.

    Both the value (when it is text) and the formatted field are cleaned, so neither a hostile
    name nor a format spec that pads it can smuggle a code through.
    """

    def format_field(self, value: Any, format_spec: str) -> str:
        if isinstance(value, str):
            value = _sanitize(value)
        return _sanitize(super().format_field(value, format_spec))


_FORMATTER = _SafeFormatter()


def _sanitize(value: str) -> str:
    return strip_control_codes(_ESCAPE.sub("", value))


@lru_cache(maxsize=256)
def _field_names(message: str) -> frozenset[str]:
    """The top-level field names a log message uses, nested format specs included.

    `{a.b}` and `{a[0]}` count as `a`, and `{n:{width}}` names both `n` and `width`. Cached,
    since a program logs the same few messages many times.

    Args:
        message: a `str.format` template.

    Returns:
        The names.
    """
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
    """What Rich's `Console` needs for one of `ui.scope`'s color settings.

    Args:
        color: None keeps `base`'s settings (or auto-detects without one); False or "none"
            turns color off; True, "16m" or "truecolor", "256", and "8" or "standard" force
            that many colors, even when the output isn't a terminal.
        base: the console being replaced, whose settings None keeps; None for a fresh one.

    Returns:
        `(color_system, no_color, force_terminal)`, as `Console` takes them.

    Raises:
        ValueError: `color` is none of those.
    """
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
    """Whether a stream can show the Unicode marks (`✓`, `━`, the braille spinner).

    Each check rules out a case seen to print boxes or question marks: an encoding that isn't
    UTF-8; a CJK locale, whose fonts draw the marks double width and break the bars; and an old
    Windows console (no Windows Terminal, no ConEmu with ANSI on). Otherwise the ASCII
    fallbacks in `GLYPHS` are used.

    Args:
        stream: the console's file; one without an `encoding` counts as not UTF-8.

    Returns:
        True when the Unicode marks are safe to print.
    """
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
    """One progress row of a `Group`: a label, a count, and a total or a spinner.

    A row is made by `Group.add` or `ui.progress`, never directly. Its count may be moved from
    any thread; it ends as finished (`finish`), failed (`fail`), or with its group's outcome.

    Args:
        label: what the row is about; a `folder/name` label shows the folders dim.
        total: the units expected; None shows a spinner instead of a bar.
        group: the group that draws it.
        unit: "bytes" to show counts as sizes (`1.5 MB/3.0 MB`); None for plain numbers.
    """

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
        """A `(done, total)` callback that moves this row, for code that reports progress so.

        A downloader calls it with the bytes so far and the full size; a total that changes
        (a server that sends its length late) replaces the row's.

        Returns:
            The callback.
        """
        def _callback(done: int, total: int | None) -> None:
            if total is not None and total != self._total:
                self.total = total
            self.count = done

        return _callback

    def _render(self, owner: UI) -> Text:
        """The row as one line: its mark, its bar or spinner, its label, its count, its reason.

        Called by Rich's refresh thread while the bars are live, and once more at the end.

        Args:
            owner: the `UI` whose glyphs and spinner it uses.

        Returns:
            The styled line.
        """
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
    """The one Rich progress column: it draws each task as its `Row`, in the Charm Huh look.

    Args:
        owner: the `UI` whose glyphs the rows use.
        lookup: the row of a Rich task id, or None for a task that has no row.
    """

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
    """A row's count as text: `3/10`, just the total once finished, or the count alone.

    Args:
        count: the units done.
        total: the units expected; None for a spinner row, which shows the count once above 0.
        level: the row's outcome; "ok" shows only the total.
        unit: "bytes" to show sizes; None for numbers.

    Returns:
        The text; "" for a spinner row at 0.
    """

    def one(value: float) -> str:
        if unit == "bytes":
            return size(value)
        return str(int(value) if float(value).is_integer() else value)

    if level == "ok" and total is not None:
        return one(total)
    if total is not None:
        return f"{one(count)}/{one(total)}"
    return one(count) if count > 0 else ""


def _brief(error: BaseException, *, limit: int = 100) -> str:
    """One line naming `error`, with each URL cut to its host."""
    first = (str(error).strip().splitlines() or [""])[0]
    first = _URL.sub(lambda match: urlsplit(match.group()).hostname or match.group(), first)
    message = f"{type(error).__name__}: {first}" if first else type(error).__name__
    return message if len(message) <= limit else message[: limit - 1] + "…"


class Group:
    """A Rich progress group holding one or more `Row` bars.

    Args:
        owner: the `UI` it draws on.
        title: a line logged above the rows; "" for none.
        drawn: whether the rows show; False keeps their counts and shows nothing.
    """

    def __init__(self, owner: UI, title: str, *, drawn: bool = True) -> None:
        self._ui = owner
        self.title = title
        # Whether the rows show at all: an undrawn group only counts (the engine's rows, which
        # the CLI draws from events instead).
        self.drawn = drawn
        self._lock = threading.Lock()
        self._rows: list[Row] = []
        self._tasks: dict[TaskID, Row] = {}
        self._progress: RichProgress | None = None
        if drawn and owner._progress_enabled():
            self._progress = RichProgress(
                _RowColumn(owner, self._tasks.get),
                console=owner.raw,
                transient=True,
            )

    def _start(self) -> None:
        if self.title and self.drawn and self._ui._allows("info"):
            self._ui.info(self.title)
        if self._progress is not None:
            self._progress.start()

    def _stop(self, outcome: Level = "ok") -> None:
        """End the group: give each open row `outcome`, and leave one line per row.

        Live bars are stopped (Rich removes them) and each row is printed once, finished or
        failed. Without live bars, a drawn group logs a line per row instead; an undrawn one
        prints nothing.

        Args:
            outcome: "ok" for a block that ended cleanly, "error" for one that raised.
        """
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
        if not self.drawn:
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
        """Add a row to this group; any thread may call it.

        Args:
            label: what the row is about, such as a file's name.
            total: the units expected; None shows a spinner.
            unit: "bytes" to show its counts as sizes; None for plain numbers.

        Returns:
            The new row.
        """
        row = Row(label, total, self, unit)
        with self._lock:
            self._rows.append(row)
            self._tasks[row._task] = row
        return row


class UI:
    """The log and the progress rows of one console; the module's `ui` is the shared one.

    It writes to stderr by default, so a program's stdout stays free for its real output.
    Tests and the CLI swap its console inside `scope`.
    """

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
        """A Rich console with flnd's theme, built from `scope`'s settings.

        Each setting left as None is taken from `base`; with no base either, Rich detects it.
        Soft wrap is on, so a long line is never cut, and highlighting off, so Rich doesn't
        color numbers and paths in a site's text.

        Args:
            color: the color setting, as `_color_settings` reads it.
            dest: where to write; None keeps `base`'s file, or stderr without one.
            width: the width in columns; None keeps `base`'s, or detects it.
            base: the console being replaced.

        Returns:
            The console.
        """
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
        """Format and print one log line (the module docstring gives its steps).

        Args:
            _kind: the level; named with an underscore so a field called `kind` stays free.
            message: the text, a `str.format` template when fields are given.
            *args: positional fields for the template.
            **fields: named fields; each must appear in `message`.

        Raises:
            TypeError: a named field doesn't appear in `message`.
        """
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
        """Inside the block, write to another destination, or with other colors, width or level.

        The console and level before it come back when the block ends, even on an error. Scopes
        nest.

        Args:
            color: the color setting (see `_color_settings`); None keeps the current one.
            dest: a text stream to write to, such as `io.StringIO()` in a test; None keeps the
                current one.
            width: the width in columns; None keeps the current one.
            level: the least level shown; None keeps the current one.

        Yields:
            This `UI`.
        """
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
    def group(self, title: str = "", *, drawn: bool = True) -> Iterator[Group]:
        """Context manager yielding a `Group` of progress rows.

        Args:
            title: a line logged above the rows; "" for none.
            drawn: whether the rows show: as live bars on a terminal, else as a line each when
                the group ends. False keeps the counts and shows nothing.

        Yields:
            The group, whose `add` makes a row.
        """
        grp = Group(self, title, drawn=drawn)
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
        drawn: bool = True,
    ) -> Iterator[Row]:
        """Context manager yielding a single `Row` progress bar.

        `drawn=False` makes a row that only counts, as `group` does: a file a run downloads,
        whose progress the CLI shows from the run's events.

        Args:
            label: what the row is about, such as a file's name.
            total: the units expected; None when unknown (a spinner).
            unit: "bytes" to show the counts as sizes; None for plain numbers.
            drawn: whether the row shows.

        Yields:
            The row; it ends failed when the block raises, else finished.
        """
        with self.group(drawn=drawn) as grp:
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
