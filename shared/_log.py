"""Five log levels and progress rows, drawn through `theme.screen`.

A line (`log.info`, `log.warn`, …) is formatted, dropped when it's below `log.level`, drawn by
the theme and written above the live area: the open rows at the bottom of the screen. A
`log.progress` or `log.group` block opens rows there. Changing a row only marks it: one drawing
thread per screen, alive while a block is open, redraws the rows that changed (at most every
0.1 s, one write inside synchronized output, over the old rows rather than after erasing them),
and the lines written meanwhile go out above the area in that same frame. A row is drawn once it
has run for 0.5 s, so a burst of small files never flickers. When the block ends each row is
written once more as a final line. Off a terminal there is no live area and no thread: only the
final lines are written. How a line or row looks is the theme's (`shared.theme`); this module
decides when and where.

    log.level = "debug"
    log.ok("{n} saved", n=3)
    with log.progress("photos", total=len(links)) as bar:
        for link in links:
            bar.advance()
    with log.group("download") as rows:
        row = rows.add("ins:example")
        row.total = 12

Every function and property here may be called from several threads at once: `advance` and the
row setters take no screen lock (a download thread may call them on every chunk), lines go
through the screen's lock, and `prompt` holds other threads' lines while it asks.
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
from shared._screen import SYNC_BEGIN, SYNC_END, hide_cursor, show_cursor
from shared._style import strip
from shared._theme import Bar, LogRecord, Screen, Theme, _bar_cells, _count, fit

# `shared.log` is the `log` object: its methods, `Error` among them, are the module's names, and the
# classes are what `log.progress` and `log.group` return.
__all__ = ["Error", "Group", "Progress", "log"]

Level = Literal["debug", "info", "ok", "warn", "error", "quiet"]
LEVELS: tuple[Level, ...] = ("debug", "info", "ok", "warn", "error", "quiet")
# quiet shares error's rank, so errors are still shown under quiet.
LEVEL_RANKS = {"debug": 0, "info": 1, "ok": 2, "warn": 3, "error": 4, "quiet": 4}
# The drawing thread draws at most this often, in seconds; a spinner row keeps it at this pace.
REDRAW_INTERVAL = 0.1
# A row is drawn once it has run this long, in seconds: a file that finishes sooner writes only its
# final line, so a burst of small photos shows no rows coming and going.
SHOW_AFTER = 0.5
# The clock rows and frames are timed with; tests replace it to run seconds in no time.
clock = time.monotonic
# The row `log.under` names on this thread, which `log.progress` nests its row under.
_nest = threading.local()


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
        """Set the lowest level written; a value that is not in `LEVELS` raises ValueError."""
        if value not in LEVELS:
            raise ValueError(f"log.level must be one of {', '.join(LEVELS)}, not {value!r}")
        self._level = value

    def debug(self, message: str = "", /, *args: Any, **fields: Any) -> None:
        """Write a debug line, shown under `log.level = "debug"` only.

            log.debug("{path}: {size} bytes", path=path, size=size)

        Args:
            message: a `str.format` template; "" writes the level's glyph alone.
            *args: positional values for the template's numbered fields.
            **fields: values for its named fields. A name the template does not use raises
                TypeError, even when the level drops the line.

        Raises:
            TypeError: a keyword has no field in `message`.
        """
        self._line("debug", message, args, fields)

    def info(self, message: str = "", /, *args: Any, **fields: Any) -> None:
        """Write an info line.

            log.info("{n} files in {folder}", n=count, folder=folder)

        Args:
            message: a `str.format` template; "" writes the level's glyph alone.
            *args: positional values for the template's numbered fields.
            **fields: values for its named fields. A name the template does not use raises
                TypeError, even when the level drops the line.

        Raises:
            TypeError: a keyword has no field in `message`.
        """
        self._line("info", message, args, fields)

    def ok(self, message: str = "", /, *args: Any, **fields: Any) -> None:
        """Write a line saying something succeeded.

            log.ok("{n} saved", n=3)

        Args:
            message: a `str.format` template; "" writes the level's glyph alone.
            *args: positional values for the template's numbered fields.
            **fields: values for its named fields. A name the template does not use raises
                TypeError, even when the level drops the line.

        Raises:
            TypeError: a keyword has no field in `message`.
        """
        self._line("ok", message, args, fields)

    def warn(self, message: str = "", /, *args: Any, **fields: Any) -> None:
        """Write a warning line.

            log.warn("{source}: {error}", source="ins", error=error)

        Args:
            message: a `str.format` template; "" writes the level's glyph alone.
            *args: positional values for the template's numbered fields.
            **fields: values for its named fields. A name the template does not use raises
                TypeError, even when the level drops the line.

        Raises:
            TypeError: a keyword has no field in `message`.
        """
        self._line("warn", message, args, fields)

    def error(self, message: str = "", /, *args: Any, **fields: Any) -> None:
        """Write an error line, shown at every level, `quiet` included.

            log.error("{url} failed: {reason}", url=url, reason=reason)

        Args:
            message: a `str.format` template; "" writes the level's glyph alone.
            *args: positional values for the template's numbered fields.
            **fields: values for its named fields. A name the template does not use raises
                TypeError, even when the level drops the line.

        Raises:
            TypeError: a keyword has no field in `message`.
        """
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
        group = Group(_theme.theme, title="", parent=getattr(_nest, "row", None))
        bar = group.add(label, total=total, show=show)
        with _ending(group):
            yield bar

    @contextmanager
    def under(self, row: Progress | None) -> Iterator[None]:
        """Nest the rows `log.progress` opens on this thread under `row`, for the length of the block.

            with log.under(target_row):
                download(url)       # its bar is drawn indented under `target_row`

        Args:
            row: the row to nest under, one of the live area's; None nests nothing. The row's
                `slots` says how many of its nested rows are drawn at once. Only this thread's
                `log.progress` calls are nested; other threads' are not.
        """
        previous = getattr(_nest, "row", None)
        _nest.row = row
        try:
            yield
        finally:
            _nest.row = previous

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
        # The check above runs before the level filter, so a misspelled field is caught even on a
        # line that is then dropped.
        if LEVEL_RANKS[level] < LEVEL_RANKS[self._level]:
            return
        # Without values the message is written as it is, so braces in a plain message stay braces.
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
    are its transitions. Every member may be used from several threads at once. None of them
    draws or takes the screen's lock: each changes the row and marks the live area, whose one
    drawing thread shows the change in its next frame. Two threads advancing one row lose no count.
    """

    def __init__(self, label: str, total: float | None, show: Callable[[Any], str], _group: Group):
        """Make a row that is not yet drawn; `Group.add` and `log.progress` are the ways to open one.

        Args:
            label: the row's text.
            total: how much there is to do; None draws a spinner.
            show: how a count and the total are written.
            _group: the group whose live area redraws when this row changes.
        """
        self.label = label
        # How many rows nested under this one (`log.under`) are drawn at once; None draws them all.
        self.slots: int | None = None
        self._show = show
        self._total = total
        self._caption = ""
        self._count: float = 0
        self._group = _group
        self._outcome: str | None = None
        # Guards `_count` and `_outcome` against two threads; it is this row's own, never the screen's.
        self._lock = threading.Lock()
        self._created = clock()
        # Whether the row has been drawn: a row that ends before it is old enough never is.
        self._seen = False
        # What the row's last drawn line showed, so an unchanged row is neither rendered nor cut again.
        self._drawn: tuple = ()
        self._line = ""

    @property
    def count(self) -> float:
        """How much is done, in the unit of `total`; setting it below 0 sets 0."""
        return self._count

    @count.setter
    def count(self, value: float) -> None:
        """Set how much is done; a value below 0 is stored as 0, and the live area is marked to redraw.

        This is a plain assignment with no lock, so setting the count while another thread calls
        `advance` can drop that advance. Use `advance` when several threads move one row.
        """
        self._count = max(value, 0)
        self._group._live.mark()

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
        """Set how much there is to do; None draws a spinner. The live area is marked to redraw."""
        self._total = value
        self._group._live.mark()

    @property
    def caption(self) -> str:
        """A note drawn after the count, such as "part 3"; `finish` clears it and `fail` keeps it."""
        return self._caption

    @caption.setter
    def caption(self, value: str) -> None:
        """Set the note drawn after the count; the live area is marked to redraw."""
        self._caption = value
        self._group._live.mark()

    def advance(self, n: float = 1) -> None:
        """Add `n` to `count`, at once, so threads advancing one row lose no count.

        Args:
            n: how much more is done, in the unit of `total`; a negative `n` takes back, down to 0.
        """
        with self._lock:
            self._count = max(self._count + n, 0)
        self._group._live.mark()

    def finish(self) -> None:
        """End this row at ok and clear its caption; a row already ended is left as it is."""
        self._end("ok")

    def fail(self) -> None:
        """End this row at error and keep its caption; a row already ended is left as it is."""
        self._end("error")

    def _end(self, outcome: str) -> None:
        with self._lock:
            if self._outcome is not None:
                return
            self._outcome = outcome
        # A finished row drops its caption, but a failed one keeps it, so the reason stays on screen.
        if outcome == "ok":
            self._caption = ""
        self._group._live.mark()

    def _record(self) -> LogRecord:
        level = "info" if self._outcome is None else self._outcome
        return LogRecord(level, self.label, self._view(None))

    def _view(self, now: float | None) -> Bar:
        """What the theme draws; `now` fixes the spinner's frame, None leaves it to the theme's clock."""
        return Bar(self.label, self._count, self._total, self._caption, self.finished, self._show,
                   None if now is None else int(now * 10))

    def _area_line(self, now: float, width: int) -> str | None:
        """The row as drawn in the live area, cut to `width`; None while it is too young or ended unseen."""
        # A row is shown only after SHOW_AFTER, so a fast file never flickers in and out. Once shown
        # it stays shown, even if it finishes later.
        if not self._seen:
            if self.finished or now - self._created < SHOW_AFTER:
                return None
            self._seen = True
        theme = self._group._theme
        bar = self._view(now)
        drawn = (self._outcome, bar.label, bar.caption, _bar_cells(theme, bar), _count(bar), width, theme.color)
        # Re-rendering is skipped when nothing the line shows has changed since the last frame.
        if drawn != self._drawn:
            self._drawn = drawn
            record = LogRecord("info" if self._outcome is None else self._outcome, self.label, bar)
            self._line = fit(render(theme, record), width)
        return self._line

    def _waiting(self, now: float) -> bool:
        """Whether the row changes with the clock alone: a spinner, or a row not yet old enough to draw."""
        if self.finished:
            return False
        return self._total is None or (not self._seen and now - self._created < SHOW_AFTER)


class Group:
    """The rows of one `log.progress` or `log.group` block, under an optional title row.

        with log.group("download") as rows:
            row = rows.add("ins:example", total=12)

    `add` may be called from several threads at once; rows are drawn in the order they were added.
    """

    def __init__(self, theme: Theme, title: str, parent: Progress | None = None):
        self._theme = theme
        self._title = title
        # The row this group's rows are drawn under, indented, while that row's group is open.
        self.parent = parent
        self._bars: list[Progress] = []
        self._outcome: str | None = None
        self._live = live_area(theme.screen)
        self._title_drawn: tuple = ()
        self._title_line = ""

    def add(self, label: str, *, total: float | None = None, show: Callable[[Any], str] = str) -> Progress:
        """Open a row at the bottom of the group, and return it.

        Args:
            label: the row's text.
            total: how much there is to do, in the caller's unit; None draws a spinner and the count.
            show: how a count and the total are written, such as `fmt.size`.
        """
        bar = Progress(label, total, show, self)
        # The drawing thread reads `_bars` under the screen lock, so the append takes it too.
        with self._live.screen.lock:
            self._bars.append(bar)
        self._live.mark()
        return bar

    def _rows(self) -> list[tuple[str, str]]:
        """(level, drawn line) for the title row and each bar row: the final lines."""
        records = [bar._record() for bar in self._bars]
        if self._title:
            records.insert(0, LogRecord("info" if self._outcome is None else self._outcome, self._title))
        return [(record.level, render(self._theme, record)) for record in records]

    def _area_rows(self, now: float, width: int) -> list[tuple[Progress | None, str]]:
        """The rows to draw now, each with its bar (None for the title), reusing the lines of unchanged rows."""
        rows: list[tuple[Progress | None, str]] = []
        if self._title:
            drawn = (self._outcome, width, self._theme.color)
            if drawn != self._title_drawn:
                self._title_drawn = drawn
                level = "info" if self._outcome is None else self._outcome
                self._title_line = fit(render(self._theme, LogRecord(level, self._title)), width)
            rows.append((None, self._title_line))
        for bar in list(self._bars):
            line = bar._area_line(now, width)
            if line is not None:
                rows.append((bar, line))
        return rows

    def _end(self, outcome: str) -> None:
        """End every unfinished row and the title at this outcome."""
        for bar in list(self._bars):
            bar._end(outcome)
        self._outcome = outcome
        self._live.mark()


class LiveArea:
    """The open rows at the bottom of one screen, in the order their groups were opened.

    Rows change without drawing; `tick` draws what changed, on the drawing thread, as one frame
    that overwrites the rows already there. `groups`, `shown` and `pending` are touched under the
    screen's lock. While a question is open, every line is held and written after the question's
    done line.
    """

    def __init__(self, screen: Screen):
        """Make the area for `screen`, with no rows; `live_area` makes one per screen and reuses it.

        Args:
            screen: the screen whose bottom rows this area owns.
        """
        self.screen = screen
        self.groups: list[Group] = []
        # The rows the terminal shows now, which a frame is compared against; the cursor sits on
        # the line below them.
        self.shown: list[str] = []
        # Lines to write above the area in the next frame.
        self.pending: list[str] = []
        # Lines written while a question is open; they are written after its done line.
        self.held_lines: list[str] = []
        # The thread whose question is open, or None. Set under the screen lock.
        self.asking_thread: int | None = None
        # Held for a whole question, so two questions never interleave. It is its own lock, not the
        # screen's, because the screen lock must be free while the question waits for keys. It is
        # re-entrant so that a question asked from inside another on the same thread does not deadlock.
        self.question_lock = threading.RLock()
        # Set by every change, from any thread; the drawing thread sleeps on `wake` while it is clear.
        # `dirty` lets a thread skip the set when it is already set, which keeps `mark` cheap.
        self.dirty = False
        self.wake = threading.Event()
        # Set when the last group closes, so the drawing thread leaves at once.
        self.closing = threading.Event()
        self.thread: threading.Thread | None = None
        # Tests turn the thread off and call `tick` themselves.
        self.threaded = True
        # A slow write (an SSH session) skips the next frame instead of queueing it.
        self.skip = False
        self.frames = 0
        # When the last frame was written, by `time.perf_counter`, which spaces the drawing thread's frames.
        self.drawn_at = 0.0

    def mark(self) -> None:
        """Say a row or line changed. No lock, no drawing: the drawing thread does that."""
        if not self.dirty:
            self.dirty = True
            self.wake.set()

    def open(self, group: Group) -> None:
        """Add a group's rows to the area, and start the drawing thread if it is not running.

        The cursor is hidden on a terminal unless `log.level` is "quiet".

        Args:
            group: the group that has just opened.
        """
        with self.screen.lock:
            self.groups.append(group)
            if self.screen.tty:
                # Under "quiet" nothing is drawn, so there is no cursor to hide.
                if log.level != "quiet":
                    hide_cursor(self.screen)
                self._start()
            self.mark()

    def close(self, group: Group) -> None:
        """Remove a group's rows and write its final lines above the live area.

        Under log.level "quiet", only the final lines that ended at error are written. The last
        group to close draws the final frame at once; any other leaves it to the next tick.
        """
        with self.screen.lock:
            self.groups = [open_group for open_group in self.groups if open_group is not group]
            quiet = log.level == "quiet"
            self.write_lines([line for level, line in group._rows() if not quiet or level == "error"])
            if self.groups:
                self.mark()
            else:
                self._finish()

    def write_lines(self, lines: list[str]) -> None:
        """Write lines above the area: at once when no area is open, else in the next frame."""
        with self.screen.lock:
            # A question's frame owns the screen, so every line waits for its done line; the check
            # does not tell threads apart, so the asking thread's own lines wait too.
            if self.asking_thread is not None:
                self.held_lines.extend(lines)
                return
            if not self.screen.tty or (not self.groups and not self.shown and not self.pending):
                # No area to keep below the lines: plain text, one write.
                self.screen.write("".join(line + "\n" for line in lines))
                return
            self.pending.extend(lines)
            if self.groups:
                self.mark()
            else:
                self._draw([])

    @contextmanager
    def question(self) -> Iterator[None]:
        """Clear the rows for a question, hold other threads' output, then flush and redraw."""
        with self.question_lock:
            # `previous_asker` is set only by a question nested on this same thread, which keeps the
            # outer question's hold; the lines are flushed only when the outermost one ends.
            with self.screen.lock:
                previous_asker = self.asking_thread
                if previous_asker is None and self.screen.tty:
                    self._draw([])
                self.asking_thread = threading.get_ident()
            try:
                yield
            finally:
                with self.screen.lock:
                    self.asking_thread = previous_asker
                    if previous_asker is None:
                        # Swapped out under the lock, so lines that arrive later start a new list.
                        held, self.held_lines = self.held_lines, []
                        if self.screen.tty and self.groups:
                            # The prompt's frame showed the cursor again when it ended.
                            hide_cursor(self.screen)
                        self.write_lines(held)
                        self.mark()

    def tick(self) -> None:
        """Draw one frame of what changed since the last, unless a question is open or the last write was slow."""
        with self.screen.lock:
            if not self.screen.tty or self.asking_thread is not None:
                return
            # A frame that took too long skips the next one, so the terminal can catch up instead of
            # queueing frames behind it.
            if self.skip:
                self.skip = False
                return
            now = clock()
            # Cleared before the rows are read, so a change made while drawing marks the next frame.
            self.dirty = False
            self.wake.clear()
            started = time.perf_counter()
            self._draw(self._rows(now))
            self.skip = time.perf_counter() - started > REDRAW_INTERVAL

    def _start(self) -> None:
        """Start the drawing thread if it isn't running; called under the lock when a group opens."""
        self.closing.clear()
        if self.threaded and self.thread is None:
            self.thread = threading.Thread(target=self._run, name="shared-log-draw", daemon=True)
            self.thread.start()

    def _run(self) -> None:
        """The drawing thread: sleep until something changes, wait out the interval since the last frame, draw."""
        while True:
            with self.screen.lock:
                if not self.groups:
                    self.thread = None
                    return
                # Rows that only the clock changes (a spinner, a row about to be old enough) need a
                # wake-up of their own; with none, the thread sleeps until a change marks it.
                now = clock()
                timeout = REDRAW_INTERVAL if any(
                    bar._waiting(now) for group in self.groups for bar in group._bars) else None
            self.wake.wait(timeout)
            # At most one frame per interval: a change that comes right after a frame waits for the rest of it.
            remaining = REDRAW_INTERVAL - (time.perf_counter() - self.drawn_at)
            if remaining > 0:
                # `closing` ends this wait at once, so the last group's close is not held for the interval.
                self.closing.wait(remaining)
            self.tick()

    def _finish(self) -> None:
        """Called under the lock when the last group has closed: the final frame at once, then the cursor back."""
        self._draw([])
        self.closing.set()
        self.wake.set()
        if self.asking_thread is None:
            show_cursor(self.screen)

    def _rows(self, now: float) -> list[str]:
        """The rows to show now: groups in the order they opened, a nested group's rows under its row."""
        # Under log.level "quiet", running bars are not drawn at all.
        if log.level == "quiet":
            return []
        width = self.screen.width
        open_ids = {id(group) for group in self.groups}
        nested: dict[int, list[Group]] = {}
        top: list[Group] = []
        for group in self.groups:
            parent = group.parent
            if parent is not None and id(parent._group) in open_ids:
                nested.setdefault(id(parent), []).append(group)
            else:
                top.append(group)
        rows: list[str] = []

        def emit(group: Group, indent: int) -> None:
            for bar, line in group._area_rows(now, width - indent):
                rows.append(" " * indent + line)
                if bar is None:
                    continue
                # A bar's `slots` caps how many nested rows show; the surplus is cut from the end.
                room = bar.slots
                for child in nested.get(id(bar), ()):
                    start = len(rows)
                    emit(child, indent + 2)
                    if room is not None:
                        # Each running file is one row; the rest is the target's row counting them.
                        room -= len(rows) - start
                        if room < 0:
                            del rows[room:]
                            room = 0

        for group in top:
            emit(group, 0)
        return _cap_rows(rows, self.screen.height)

    def _draw(self, rows: list[str]) -> None:
        """Write the frame that turns the shown rows into `rows`, with the pending lines above them."""
        frame = self._frame(rows)
        if frame:
            self.frames += 1
            self.screen.write(frame)
            self.drawn_at = time.perf_counter()

    def _frame(self, rows: list[str]) -> str:
        """The text that makes the terminal show `rows` under the pending lines; "" when nothing changed.

        Rows are overwritten, each followed by `\\x1b[K` (clear to the end of that line), never
        erased first, so no moment shows an empty area; `\\x1b[J` (clear below) comes last, and
        only when the new frame has fewer rows. Only the rows that differ are written, each after
        a cursor move, and the whole frame sits inside synchronized output.
        """
        old, pending = self.shown, self.pending
        if not pending and rows == old:
            return ""
        self.shown, self.pending = rows, []
        # Between SYNC_BEGIN and SYNC_END the terminal shows the frame all at once, so the redraw
        # does not show a half-written area.
        out = [SYNC_BEGIN]
        if pending:
            # Lines above the area push it down, so every row moves: all are written again. The
            # cursor starts on the line below the old rows, and goes back to the area's top.
            # CR returns to column 0; CUU (`ESC [ n A`) moves up over the old rows.
            out.append("\r" + (f"\x1b[{len(old)}A" if old else ""))
            for line in pending + rows:
                out.append(line + "\x1b[K\n")
            # ED 0 (`ESC [ J`) clears below, but only when the new frame is shorter than the old one.
            if len(old) > len(pending) + len(rows):
                out.append("\x1b[J")
        else:
            # `cursor` is the row the terminal's cursor is on, counted from the old rows' top.
            cursor = len(old)

            def goto(row: int) -> None:
                # CUD / CUU (`ESC [ n B` / `ESC [ n A`) move the cursor by n rows without writing.
                nonlocal cursor
                if row > cursor:
                    out.append(f"\x1b[{row - cursor}B")
                elif row < cursor:
                    out.append(f"\x1b[{cursor - row}A")
                cursor = row

            for number, row in enumerate(rows[:len(old)]):
                if row != old[number]:
                    goto(number)
                    # EL 0 (`ESC [ K`) clears the rest of the line, so a shorter row leaves no tail of the old one.
                    out.append("\r" + row + "\x1b[K")
            if len(rows) > len(old):
                # New rows go below the old ones with a newline each, which scrolls at the screen's bottom.
                goto(len(old))
                out.append("\r")
                for row in rows[len(old):]:
                    out.append(row + "\x1b[K\n")
            else:
                goto(len(rows))
                out.append("\r")
                # ED 0 clears the old rows that the shorter frame no longer uses.
                if len(rows) < len(old):
                    out.append("\x1b[J")
        out.append(SYNC_END)
        return "".join(out)


def _cap_rows(rows: list[str], height: int) -> list[str]:
    """Cut the rows to fit the screen's height, with a last line that counts the rows cut.

    Args:
        rows: the area's rows, top first.
        height: the screen's height in lines.

    Returns:
        The rows unchanged when they fit; otherwise the first rows and a "… N more" line.
    """
    # Cursor-up can't reach rows that scrolled above the screen, so a taller area would leave
    # a copy in the scrollback on every redraw. One line stays free for the cursor.
    room = max(height - 1, 1)
    if len(rows) <= room:
        return rows
    return rows[:room - 1] + [f"… {len(rows) - room + 1} more"]


_live_areas: weakref.WeakKeyDictionary = weakref.WeakKeyDictionary()
_live_areas_lock = threading.Lock()


def live_area(screen: Screen) -> LiveArea:
    """The live area of `screen`, made on first use and then shared by every group on that screen.

    Args:
        screen: the screen the rows are drawn on.

    Returns:
        The one `LiveArea` for `screen`.
    """
    # Under the lock, so two threads opening the first group at once cannot each make an area.
    # The dictionary holds screens weakly, so an area goes when its screen does.
    with _live_areas_lock:
        if screen not in _live_areas:
            _live_areas[screen] = LiveArea(screen)
        return _live_areas[screen]


def render(theme: Theme, record: LogRecord) -> str:
    """Draw a record with the theme, as a line or as a progress row, and strip escapes when colour is off.

    Args:
        theme: the theme that draws it.
        record: the record to draw; a record with a bar is drawn as a progress row.

    Returns:
        The drawn text, with any escape sequences removed when `theme.color` is "none".
    """
    line = theme.draw("log" if record.bar is None else "progress", record)
    return strip(line) if theme.color == "none" else line


@contextmanager
def _ending(group: Group) -> Iterator[None]:
    """Open a group's rows around a block, and end them: at ok on a clean exit, else at error.

    The exception is re-raised after the rows end, so the caller still sees it.
    """
    group._live.open(group)
    try:
        yield
    # BaseException, not Exception: a KeyboardInterrupt also ends the rows at error before it
    # propagates, so the screen is not left with rows still running.
    except BaseException:
        group._end("error")
        group._live.close(group)
        raise
    group._end("ok")
    group._live.close(group)


def _field_names(message: str) -> set[str]:
    """The names of the fields a `str.format` template uses, such as "x" for "{x.y}" and "{x[0]}"."""
    names = set()
    for _, field_name, _, _ in string.Formatter().parse(message):
        if field_name:
            names.add(field_name.split(".")[0].split("[")[0])
    return names


log = Log()
