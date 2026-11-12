"""Five log levels and progress rows, drawn through `theme.screen`.

A line (`log.info`, `log.warn`, …) is formatted, dropped when it's below `log.level`, drawn by
the theme and written above the live area: the open rows at the bottom of the screen. A
`log.progress` or `log.group` block opens rows there; on a terminal they're redrawn in place
(at most every 0.1 s, or at once when a row starts or ends), and when the block ends each row
is written once more as a final line. Off a terminal only those final lines are written. How a
line or row looks is the theme's (`shared.theme`); this module decides when and where.

    log.level = "debug"
    log.ok("{n} saved", n=3)
    with log.progress("photos", total=len(links)) as bar:
        for link in links:
            bar.advance()
    with log.group("download") as rows:
        row = rows.add("ins:example")
        row.total = 12

Every function and property here may be called from several threads at once: lines and row
changes go through the screen's lock, and `prompt` holds other threads' lines while it asks.
"""

from __future__ import annotations

import string
import threading
import time
import weakref
from contextlib import contextmanager
from typing import Any, Callable, Iterator, Literal

import shared
from shared import _theme
from shared._style import strip
from shared._theme import Bar, LogRecord, Screen, Theme, fit

# `shared.log` is the `log` object: its methods, `Error` among them, are the module's names, and the
# classes are what `log.progress` and `log.group` return.
__all__ = ["Error", "Group", "Progress", "log"]

Level = Literal["debug", "info", "ok", "warn", "error", "quiet"]
LEVELS: tuple[Level, ...] = ("debug", "info", "ok", "warn", "error", "quiet")
# quiet shares error's rank, so errors are still shown under quiet.
LEVEL_RANKS = {"debug": 0, "info": 1, "ok": 2, "warn": 3, "error": 4, "quiet": 4}
REDRAW_INTERVAL = 0.1


class Error(shared.Error):
    """The base of every error `shared.log` raises."""


class Log:
    """The `log` object: five levels, the level shown, and progress rows.

    Each level's method takes a `str.format` message and its values:

        log.warn("{source}: {error}", source="ins", error=error)

    A keyword the message has no field for raises `TypeError`, so a misspelled field is caught
    rather than dropped. Every method may be called from several threads.
    """

    # `shared.log` is this object, not a module, so the module's error is reached through it.
    Error = Error

    def __init__(self) -> None:
        self._level: Level = "info"

    @property
    def level(self) -> Level:
        """The lowest level written: `debug`, `info` (the default), `ok`, `warn`, `error` or `quiet`.

        `quiet` writes errors only, and draws no running rows; a row that ends at error is still
        written. Setting anything else raises `ValueError`.
        """
        return self._level

    @level.setter
    def level(self, value: Level) -> None:
        if value not in LEVELS:
            raise ValueError(f"log.level must be one of {', '.join(LEVELS)}, not {value!r}")
        self._level = value

    def debug(self, message: str = "", /, *args: Any, **fields: Any) -> None:
        """Write a debug line, shown under `log.level = "debug"` only."""
        self._line("debug", message, args, fields)

    def info(self, message: str = "", /, *args: Any, **fields: Any) -> None:
        """Write an info line."""
        self._line("info", message, args, fields)

    def ok(self, message: str = "", /, *args: Any, **fields: Any) -> None:
        """Write a line saying something succeeded."""
        self._line("ok", message, args, fields)

    def warn(self, message: str = "", /, *args: Any, **fields: Any) -> None:
        """Write a warning line."""
        self._line("warn", message, args, fields)

    def error(self, message: str = "", /, *args: Any, **fields: Any) -> None:
        """Write an error line, shown at every level, `quiet` included."""
        self._line("error", message, args, fields)

    @contextmanager
    def progress(self, label: str, *, total: float | None = None,
                 show: Callable[[Any], str] = str) -> Iterator[Progress]:
        """One row for the length of the block, ending at ok on a clean exit and at error on an exception.

            with log.progress("clip.mp4", total=size, show=fmt.size) as bar:
                bar.advance(len(chunk))

        Args:
            label: the row's text.
            total: how much there is to do, in the caller's unit; None draws a spinner and the count.
            show: how a count and the total are written, such as `fmt.size`.

        Returns:
            The row, a `Progress`. An exception, `KeyboardInterrupt` included, is re-raised after
            the row ends at error. The block may be used from one thread while others advance the row.
        """
        group = Group(_theme.theme, title="")
        bar = group.add(label, total=total, show=show)
        with _ending(group):
            yield bar

    @contextmanager
    def group(self, title: str = "") -> Iterator[Group]:
        """Rows added for the length of the block under one title row, which ends as `progress` does.

            with log.group("download") as rows:
                row = rows.add("ins:example")
                row.finish()

        Args:
            title: the text of the title row; "" draws no title row.

        Returns:
            The `Group`, whose `add` opens a row. Unfinished rows end with the block, at ok on a
            clean exit and at error on an exception. Rows may be added and advanced from several threads.
        """
        group = Group(_theme.theme, title=title)
        with _ending(group):
            yield group

    def _line(self, level: str, message: str, args: tuple, fields: dict) -> None:
        """Format a line, drop it below `level`, and write it above the live area."""
        # str.format ignores unused keywords, so a misspelled field would vanish without a word.
        unused = set(fields) - _field_names(message) if fields else ()
        if unused:
            raise TypeError(f"log message {message!r} has no field {', '.join(sorted(unused))}")
        if LEVEL_RANKS[level] < LEVEL_RANKS[self._level]:
            return
        if args or fields:
            message = message.format(*args, **fields)
        theme = _theme.theme
        live_area(theme.screen).write_lines([render(theme, LogRecord(level, message))])


class Progress:
    """One row in the live area, from `log.progress` or `Group.add`.

        bar.total = 10      # state is set
        bar.advance()       # and moved on
        bar.count, bar.finished

    Its state is the settable `count`, `total` and `caption`; `advance`, `finish` and `fail`
    are its transitions. Every member may be used from several threads at once: changes are
    made under the screen's lock, so two threads advancing one row lose no count.
    """

    def __init__(self, label: str, total: float | None, show: Callable[[Any], str], _group: Group):
        self.label = label
        self._show = show
        self._total = total
        self._caption = ""
        self._count: float = 0
        self._group = _group
        self._outcome: str | None = None

    @property
    def count(self) -> float:
        """How much is done, in the unit of `total`; setting it below 0 sets 0."""
        return self._count

    @count.setter
    def count(self, value: float) -> None:
        with self._group._live.screen.lock:
            self._count = max(value, 0)
            self._group._redraw()

    @property
    def outcome(self) -> str | None:
        """How the row ended, "ok" or "error"; None while it runs."""
        return self._outcome

    @property
    def finished(self) -> bool:
        """Whether the row has ended, at ok or at error."""
        return self._outcome is not None

    @property
    def total(self) -> float | None:
        """How much there is to do; None while it isn't known, which draws a spinner."""
        return self._total

    @total.setter
    def total(self, value: float | None) -> None:
        with self._group._live.screen.lock:
            self._total = value
            self._group._redraw()

    @property
    def caption(self) -> str:
        """A note drawn after the count, such as "part 3"; `finish` clears it and `fail` keeps it."""
        return self._caption

    @caption.setter
    def caption(self, value: str) -> None:
        with self._group._live.screen.lock:
            self._caption = value
            self._group._redraw()

    def advance(self, n: float = 1) -> None:
        """Add `n` to `count`, at once, so threads advancing one row lose no count.

        Args:
            n: how much more is done, in the unit of `total`; a negative `n` takes back, down to 0.
        """
        with self._group._live.screen.lock:
            self.count = self._count + n

    def finish(self) -> None:
        """End this row at ok and clear its caption; a row already ended is left as it is."""
        self._end("ok")

    def fail(self) -> None:
        """End this row at error and keep its caption; a row already ended is left as it is."""
        self._end("error")

    def _end(self, outcome: str) -> None:
        with self._group._live.screen.lock:
            if self.finished:
                return
            self._outcome = outcome
            if outcome == "ok":
                self._caption = ""
            self._group._redraw(force=True)

    def _record(self) -> LogRecord:
        level = "info" if self._outcome is None else self._outcome
        view = Bar(self.label, self._count, self._total, self._caption, self.finished, self._show)
        return LogRecord(level, self.label, view)


class Group:
    """The rows of one `log.progress` or `log.group` block, under an optional title row.

        with log.group("download") as rows:
            row = rows.add("ins:example", total=12)

    `add` may be called from several threads at once; rows are drawn in the order they were added.
    """

    def __init__(self, theme: Theme, title: str):
        self._theme = theme
        self._title = title
        self._bars: list[Progress] = []
        self._outcome: str | None = None
        self._live = live_area(theme.screen)

    def add(self, label: str, *, total: float | None = None, show: Callable[[Any], str] = str) -> Progress:
        """Open a row at the bottom of the group, and return it.

        Args:
            label: the row's text.
            total: how much there is to do, in the caller's unit; None draws a spinner and the count.
            show: how a count and the total are written, such as `fmt.size`.
        """
        bar = Progress(label, total, show, self)
        with self._live.screen.lock:
            self._bars.append(bar)
        self._redraw(force=True)
        self._live.start_spinner()
        return bar

    def _redraw(self, force: bool = False) -> None:
        """Redraw the live area, at most every 0.1 s unless `force`."""
        self._live.redraw(force)

    def _rows(self) -> list[tuple[str, str]]:
        """(level, drawn line) for the title row and each bar row."""
        records = [bar._record() for bar in self._bars]
        if self._title:
            records.insert(0, LogRecord("info" if self._outcome is None else self._outcome, self._title))
        return [(record.level, render(self._theme, record)) for record in records]

    def _end(self, outcome: str) -> None:
        """End every unfinished row and the title at this outcome."""
        with self._live.screen.lock:
            for bar in self._bars:
                bar._end(outcome)
            self._outcome = outcome


class LiveArea:
    """The open rows at the bottom of one screen, in the order their groups were opened.

    Every change and write happens under the screen's lock. While a question is open,
    lines from other threads are held and written after the question's done line.
    """

    def __init__(self, screen: Screen):
        self.screen = screen
        self.groups: list[Group] = []
        self.drawn_rows = 0
        self.last_draw = 0.0
        self.held_lines: list[str] = []
        self.asking_thread: int | None = None
        self.question_lock = threading.RLock()
        self.spinner: threading.Thread | None = None

    def open(self, group: Group) -> None:
        with self.screen.lock:
            self.groups.append(group)
        self.redraw(force=True)

    def close(self, group: Group) -> None:
        """Remove a group's rows and write its final lines above the live area.

        Under log.level "quiet", only the final lines that ended at error are written.
        """
        with self.screen.lock:
            self.groups = [open_group for open_group in self.groups if open_group is not group]
            quiet = log.level == "quiet"
            self.write_lines([line for level, line in group._rows() if not quiet or level == "error"])

    def write_lines(self, lines: list[str]) -> None:
        with self.screen.lock:
            if self.asking_thread not in (None, threading.get_ident()):
                self.held_lines.extend(lines)
                return
            text = "".join(line + "\n" for line in lines)
            if self.screen.tty:
                text = self._erase() + text + self._draw_rows()
            self.screen.write(text)

    def redraw(self, force: bool = False) -> None:
        with self.screen.lock:
            if not self.screen.tty or self.asking_thread is not None:
                return
            if force or time.monotonic() - self.last_draw >= REDRAW_INTERVAL:
                self.screen.write(self._erase() + self._draw_rows())

    @contextmanager
    def question(self) -> Iterator[None]:
        """Clear the rows for a question, hold other threads' output, then flush and redraw."""
        with self.question_lock:
            with self.screen.lock:
                previous_asker = self.asking_thread
                if self.screen.tty:
                    self.screen.write(self._erase())
                self.asking_thread = threading.get_ident()
            try:
                yield
            finally:
                with self.screen.lock:
                    self.asking_thread = previous_asker
                    if previous_asker is None:
                        held, self.held_lines = self.held_lines, []
                        self.write_lines(held)

    def start_spinner(self) -> None:
        with self.screen.lock:
            if self.screen.tty and self.spinner is None and self._has_spinner():
                self.spinner = threading.Thread(target=self._spin, name="shared-log-spinner", daemon=True)
                self.spinner.start()

    def _spin(self) -> None:
        while True:
            time.sleep(REDRAW_INTERVAL)
            with self.screen.lock:
                if not self._has_spinner():
                    self.spinner = None
                    return
                self.redraw(force=True)

    def _has_spinner(self) -> bool:
        return any(bar.total is None and not bar.finished for group in self.groups for bar in group._bars)

    def _erase(self) -> str:
        erase = f"\r\x1b[{self.drawn_rows}A\x1b[J" if self.drawn_rows else ""
        self.drawn_rows = 0
        return erase

    def _draw_rows(self) -> str:
        width = self.screen.width
        # Under log.level "quiet", running bars are not drawn at all.
        groups = [] if log.level == "quiet" else self.groups
        rows = _cap_rows([fit(row, width) for group in groups for _, row in group._rows()], self.screen.height)
        self.drawn_rows = len(rows)
        self.last_draw = time.monotonic()
        return "".join(row + "\n" for row in rows)


def _cap_rows(rows: list[str], height: int) -> list[str]:
    # Cursor-up can't reach rows that scrolled above the screen, so a taller area would leave
    # a copy in the scrollback on every redraw. One line stays free for the cursor.
    room = max(height - 1, 1)
    if len(rows) <= room:
        return rows
    return rows[:room - 1] + [f"… {len(rows) - room + 1} more"]


_live_areas: weakref.WeakKeyDictionary = weakref.WeakKeyDictionary()
_live_areas_lock = threading.Lock()


def live_area(screen: Screen) -> LiveArea:
    with _live_areas_lock:
        if screen not in _live_areas:
            _live_areas[screen] = LiveArea(screen)
        return _live_areas[screen]


def render(theme: Theme, record: LogRecord) -> str:
    line = theme.draw("log" if record.bar is None else "progress", record)
    return strip(line) if theme.color == "none" else line


@contextmanager
def _ending(group: Group) -> Iterator[None]:
    group._live.open(group)
    group._live.start_spinner()
    try:
        yield
    except BaseException:
        group._end("error")
        group._live.close(group)
        raise
    group._end("ok")
    group._live.close(group)


def _field_names(message: str) -> set[str]:
    names = set()
    for _, field_name, _, _ in string.Formatter().parse(message):
        if field_name:
            names.add(field_name.split(".")[0].split("[")[0])
    return names


log = Log()
