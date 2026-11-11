"""Five log levels and progress bars, drawn through `theme.screen`."""

from __future__ import annotations

import string
import threading
import time
import weakref
from contextlib import contextmanager
from typing import Any, Callable, Iterator

from shared import _theme
from shared._style import strip
from shared._theme import Bar, LogRecord, Screen, Theme, fit

LEVELS = ("debug", "info", "ok", "warn", "error", "quiet")
# quiet shares error's rank, so errors are still shown under quiet.
LEVEL_RANKS = {"debug": 0, "info": 1, "ok": 2, "warn": 3, "error": 4, "quiet": 4}
REDRAW_INTERVAL = 0.1


class Log:
    def __init__(self) -> None:
        self._level = "info"

    @property
    def level(self) -> str:
        return self._level

    @level.setter
    def level(self, value: str) -> None:
        if value not in LEVELS:
            raise ValueError(f"log.level must be one of {', '.join(LEVELS)}, not {value!r}")
        self._level = value

    def debug(self, message: str = "", /, *args: Any, theme: Theme | None = None, **fields: Any) -> None:
        self._line("debug", message, args, fields, theme)

    def info(self, message: str = "", /, *args: Any, theme: Theme | None = None, **fields: Any) -> None:
        self._line("info", message, args, fields, theme)

    def ok(self, message: str = "", /, *args: Any, theme: Theme | None = None, **fields: Any) -> None:
        self._line("ok", message, args, fields, theme)

    def warn(self, message: str = "", /, *args: Any, theme: Theme | None = None, **fields: Any) -> None:
        self._line("warn", message, args, fields, theme)

    def error(self, message: str = "", /, *args: Any, theme: Theme | None = None, **fields: Any) -> None:
        self._line("error", message, args, fields, theme)

    @contextmanager
    def progress(self, label: str, *, total: float | None = None, show: Callable[[Any], str] = str,
                 theme: Theme | None = None) -> Iterator[ProgressBar]:
        """One bar. A clean exit ends it at ok; an exception ends it at error and re-raises."""
        theme = theme or _theme.theme
        group = Group(theme, title="")
        bar = group.add(label, total=total, show=show)
        with _ending(group):
            yield bar

    @contextmanager
    def multi(self, title: str = "", *, theme: Theme | None = None) -> Iterator[Group]:
        """A title row with one row per job added under it."""
        theme = theme or _theme.theme
        group = Group(theme, title=title)
        with _ending(group):
            yield group

    def _line(self, level: str, message: str, args: tuple, fields: dict, theme: Theme | None) -> None:
        if LEVEL_RANKS[level] < LEVEL_RANKS[self._level]:
            return
        theme = theme or _theme.theme
        extra = ""
        if args or fields:
            used = _field_names(message)
            message = message.format(*args, **fields)
            extra = "".join(f"  {name}={value}" for name, value in fields.items() if name not in used)
        line = render(theme, LogRecord(level, message, extra))
        live_area(theme.screen).write_lines([line])


class ProgressBar:
    """One row in the live area."""

    def __init__(self, label: str, total: float | None, show: Callable[[Any], str], group: Group):
        self.label = label
        self.show = show
        self._total = total
        self._caption = ""
        self._done: float = 0
        self._group = group
        self.outcome: str | None = None

    @property
    def done(self) -> float:
        return self._done

    @property
    def finished(self) -> bool:
        return self.outcome is not None

    @property
    def total(self) -> float | None:
        return self._total

    @total.setter
    def total(self, value: float | None) -> None:
        self._total = value
        self._group.changed()

    @property
    def caption(self) -> str:
        return self._caption

    @caption.setter
    def caption(self, value: str) -> None:
        self._caption = value
        self._group.changed()

    def advance(self, n: float = 1) -> None:
        self.update(self._done + n)

    def update(self, n: float) -> None:
        self._done = max(n, 0)
        self._group.changed()

    def finish(self) -> None:
        """End this row at ok and clear its caption."""
        self._end("ok")

    def fail(self) -> None:
        """End this row at error and keep its caption."""
        self._end("error")

    def _end(self, outcome: str) -> None:
        if self.finished:
            return
        self.outcome = outcome
        if outcome == "ok":
            self._caption = ""
        self._group.changed(force=True)

    def _record(self) -> LogRecord:
        level = "info" if self.outcome is None else self.outcome
        view = Bar(self.label, self._done, self._total, self._caption, self.finished, self.show)
        return LogRecord(level, self.label, "", view)


class Group:
    """The rows of one `progress` or `multi` block. `multi` yields it as `jobs`."""

    def __init__(self, theme: Theme, title: str):
        self.theme = theme
        self.title = title
        self.bars: list[ProgressBar] = []
        self.outcome: str | None = None
        self.live = live_area(theme.screen)

    def add(self, label: str, *, total: float | None = None, show: Callable[[Any], str] = str) -> ProgressBar:
        bar = ProgressBar(label, total, show, self)
        with self.live.screen.lock:
            self.bars.append(bar)
        self.changed(force=True)
        self.live.start_spinner()
        return bar

    def changed(self, force: bool = False) -> None:
        self.live.changed(force)

    def rows(self) -> list[tuple[str, str]]:
        """(level, drawn line) for the title row and each bar row."""
        records = [bar._record() for bar in self.bars]
        if self.title:
            records.insert(0, LogRecord("info" if self.outcome is None else self.outcome, self.title))
        return [(record.level, render(self.theme, record)) for record in records]

    def end(self, outcome: str) -> None:
        """End every unfinished row and the title at this outcome."""
        for bar in self.bars:
            bar._end(outcome)
        self.outcome = outcome


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
        self.changed(force=True)

    def close(self, group: Group) -> None:
        """Remove a group's rows and write its final lines above the live area.

        Under log.level "quiet", only the final lines that ended at error are written.
        """
        with self.screen.lock:
            self.groups = [open_group for open_group in self.groups if open_group is not group]
            quiet = log.level == "quiet"
            self.write_lines([line for level, line in group.rows() if not quiet or level == "error"])

    def write_lines(self, lines: list[str]) -> None:
        with self.screen.lock:
            if self.asking_thread not in (None, threading.get_ident()):
                self.held_lines.extend(lines)
                return
            text = "".join(line + "\n" for line in lines)
            if self.screen.tty:
                text = self._erase() + text + self._draw_rows()
            self.screen.write(text)

    def changed(self, force: bool = False) -> None:
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
                self.changed(force=True)

    def _has_spinner(self) -> bool:
        return any(bar.total is None and not bar.finished for group in self.groups for bar in group.bars)

    def _erase(self) -> str:
        erase = f"\r\x1b[{self.drawn_rows}A\x1b[J" if self.drawn_rows else ""
        self.drawn_rows = 0
        return erase

    def _draw_rows(self) -> str:
        width = self.screen.width
        # Under log.level "quiet", running bars are not drawn at all.
        groups = [] if log.level == "quiet" else self.groups
        rows = [fit(row, width) for group in groups for _, row in group.rows()]
        self.drawn_rows = len(rows)
        self.last_draw = time.monotonic()
        return "".join(row + "\n" for row in rows)


_live_areas: weakref.WeakKeyDictionary = weakref.WeakKeyDictionary()
_live_areas_lock = threading.Lock()


def live_area(screen: Screen) -> LiveArea:
    with _live_areas_lock:
        if screen not in _live_areas:
            _live_areas[screen] = LiveArea(screen)
        return _live_areas[screen]


def render(theme: Theme, record: LogRecord) -> str:
    line = theme._format_log(record)
    return strip(line) if theme.color == "none" else line


@contextmanager
def _ending(group: Group) -> Iterator[None]:
    group.live.open(group)
    group.live.start_spinner()
    try:
        yield
    except BaseException:
        group.end("error")
        group.live.close(group)
        raise
    group.end("ok")
    group.live.close(group)


def _field_names(message: str) -> set[str]:
    names = set()
    for _, field_name, _, _ in string.Formatter().parse(message):
        if field_name:
            names.add(field_name.split(".")[0].split("[")[0])
    return names


log = Log()
