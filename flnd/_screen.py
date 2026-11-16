"""The full-screen view of a run: Rich on the terminal's alternate screen, read from its events.

    with Screen("download ins:example_user", run=run) as screen, ui.scope(dest=screen.log):
        for event in run:
            screen.show(event)
    screen.close()                      # the failures, on the normal screen

Who calls it: the CLI (`flnd/cli.py`, `--ui full`, the default on a terminal). What it calls:
Rich (`Live` on the alternate screen), the run's public verbs (`stop`, `suspend`, `resume`) for
the keys, `flnd._prompt.covering` so a question asked mid-run gives the terminal back, and
`flnd._text` for sizes and times. It reads events by their attributes only, so it never looks
inside a run: what it draws, a script can read from the same `flnd.Stats`.

What a frame shows, top to bottom (`Screen.frame`):
1. a header: the run, its state (running, paused, stopping) and elapsed time; then the totals:
   saved, skipped, failed, bytes, the speed and the ETA once it is known;
2. targets: one row per target and category, the busy ones first, with their state (listing,
   finishing, done, caught up, cut, stopped, failed), pages, posts and files;
3. downloads: each file in flight with its bar, size, speed and host, and the hosts' caps;
4. the system: browser tabs and RAM, flnd's RAM and CPU, ffmpeg and Pillow jobs, free disk;
5. a log: each wait with its reason first, then the latest warnings and failures;
6. a footer: the keys, and the stop stage when there is one.

How it costs nothing per event: `show` only stores (a counter, a dict entry, the latest
`Stats`), and Rich redraws at `FPS` from what is stored, on its own thread. Rows are picked and
sorted once per `Stats` (once a second), and a frame renders only the rows that fit, so a run
with a thousand targets draws as fast as one with ten.

Rules a change must keep:
- The terminal always comes back: leaving the `with` (normally, on Ctrl-C, or on a crash)
  restores the key reader's terminal mode first, then leaves the alternate screen. `Keys`
  restores what it changed even when its thread fails.
- Text from a site (a target, a file name, a log line) is shown as plain `Text`, never as
  markup, with control codes stripped, so it can't style or move anything.
- A frame never raises: a bug in drawing shows one line instead of ending the run.
"""

from __future__ import annotations

import os
import re
import sys
import threading
import time
from collections import deque
from collections.abc import Callable, Iterator
from contextlib import ExitStack
from typing import IO, Any, NamedTuple

from rich.console import Console, ConsoleOptions, Group
from rich.control import strip_control_codes
from rich.text import Text
from rich.theme import Theme

from flnd import _prompt, _text
from flnd._ui import PALETTE

__all__ = ["Keys", "Screen"]

# Frames a second. Each frame redraws from the stored state; events between two cost nothing.
FPS = 10
# Lines the log keeps; it shows the newest that fit.
LOG = 200
# Cells of a target's bar and a download's bar.
BAR = 10
TRANSFER_BAR = 12
# Width from which a frame adds the wide columns (pages, early stop, host).
WIDE = 110
# The order rows show in: busy first, then what needs a look, then what is done.
ORDER = {"listing": 0, "finishing": 1, "failed": 2, "stopped": 3, "caught up": 4, "cut": 4,
         "done": 5}
# Each target state's mark and colour.
MARKS = {"finishing": ("↓", "accent"), "done": ("✓", "ok"), "caught up": ("✓", "ok"),
         "cut": ("✓", "ok"), "failed": ("✗", "error"), "stopped": ("■", "warn")}
SPIN = ("⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏")
# How a log line's first mark (from `flnd._ui`'s glyphs) colours it.
LOG_STYLES = {"!": "warn", "✗": "error", "x": "error", "✓": "ok", "+": "ok", "·": "debug"}
# The speed shown is smoothed over samples, so a burst doesn't make it jump: this much of the
# new sample, the rest of the old.
SMOOTH = 0.5
_ESCAPE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)|\x1b[@-Z\\-_]")


class Screen:
    """A run drawn on the terminal's alternate screen at `FPS`, with `q` to stop, `p` to pause.

    Enter it to take the screen, `show` each event, leave it to give the screen back. Its
    `log` is a file for the program's log lines (`ui.scope(dest=screen.log)`), which it shows
    in the log panel instead of over the frame. `close()` then prints, on the normal screen,
    what scrollback should keep: each failure with its hint.

    Args:
        title: what the run does (`download ins:example_user`).
        run: the `flnd.Run`, which `q` stops and `p` suspends; None takes no keys.
        console: where it draws; None draws on standard error.
        keys: whether to read keys from standard input; False leaves the terminal as it is.

    Example:
        >>> import io
        >>> from rich.console import Console
        >>> screen = Screen("download", console=Console(file=io.StringIO(), width=80, height=24))
        >>> "flnd" in screen.text()
        True
    """

    def __init__(self, title: str, *, run: Any = None, console: Console | None = None,
                 keys: bool = True) -> None:
        self.title = _clean(title)
        self.run = run
        self.console = console or Console(stderr=True, theme=Theme(PALETTE), highlight=False)
        self.log = _Log()
        self._began = time.monotonic()
        self._live: Any = None
        self._keys = Keys(self.key) if keys and run is not None else None
        self._stack = ExitStack()
        # What `show` stores, for the frames.
        self._stats: Any = None
        self._progress: dict[tuple[str, str], Any] = {}
        self._saved = self._skipped = self._failed = self._planned = self._bytes = 0
        self._speed = 0.0
        self._failures: list[Any] = []
        self._plans: list[str] = []
        # The rows picked from the latest `Stats` (or `Progress` events), sorted once each.
        self._targets: list[_Target] | None = None
        # Whether `q` was pressed, so the footer says so before the next `Stats` does; and
        # whether `p` suspended the run, so the next `p` resumes it.
        self._quit = False
        self._held = False
        self._unicode = (getattr(self.console, "encoding", "") or "").lower().startswith("utf")

    # -- the terminal --------------------------------------------------------------------------

    def __enter__(self) -> Screen:
        from rich.live import Live

        self._live = Live(self, console=self.console, screen=True, refresh_per_second=FPS,
                          transient=False, redirect_stdout=False, redirect_stderr=False)
        try:
            self.start()
            self._stack.enter_context(_prompt.covering(self))
        except BaseException:
            self.stop()
            raise
        return self

    def __exit__(self, *exc: object) -> None:
        try:
            self._stack.close()
        finally:
            self.stop()

    def start(self) -> None:
        """Take the screen and the keys (again, after a question gave them back)."""
        self._live.start()
        if self._keys is not None:
            self._keys.start()

    def stop(self) -> None:
        """Give back the keys, then the screen: the terminal is left as `start` found it."""
        try:
            if self._keys is not None:
                self._keys.stop()
        finally:
            if self._live is not None and self._live.is_started:
                self._live.stop()

    def close(self) -> None:
        """Print what the normal screen's scrollback should keep: the plan of a dry run, then
        each failure with its hint. Call it after leaving the `with`."""
        write = sys.stdout.write
        for line in self._plans:
            write(line + "\n")
        for event in self._failures:
            where = f"{event.target} {event.post}" if event.post else str(event.target)
            print(f"failed {where}: {event.error}", file=sys.stderr)
            hint = getattr(event.error, "hint", "")
            if hint:
                print(f"  {hint}", file=sys.stderr)

    # -- events and keys -----------------------------------------------------------------------

    def show(self, event: Any) -> None:
        """Store what an event says for the next frame; nothing is drawn here.

        Args:
            event: one event of the run, of any kind.
        """
        name = type(event).__name__
        if name == "Stats":
            if self._stats is None:
                self._speed = event.speed
            else:
                self._speed = SMOOTH * event.speed + (1 - SMOOTH) * self._speed
            self._stats, self._targets = event, None
        elif name == "Progress":
            self._progress[(str(event.target), event.category)] = event
            if self._stats is None:
                self._targets = None
        elif name == "Saved":
            self._saved += 1
            self._bytes += event.size
        elif name == "Skipped":
            self._skipped += 1
        elif name == "Failed":
            self._failed += 1
            self._failures.append(event)
            where = f"{event.target} {event.post}" if event.post else str(event.target)
            self.log.add(f"✗ failed {where}: {event.error}")
        elif name == "Planned":
            self._planned += 1
            self._plans.append(f"{event.state} {event.path}" if event.path is not None
                               else f"found {event.target}")

    def key(self, key: str) -> None:
        """Act on one key: `q` stops gracefully (the first Ctrl-C stage), `p` pauses or resumes.

        Args:
            key: the character typed; any other is ignored.
        """
        if self.run is None:
            return
        if key in ("q", "Q"):
            self._quit = True
            self.run.stop()
        elif key in ("p", "P", " "):
            if self._held:
                self.run.resume()
            else:
                self.run.suspend()
            self._held = not self._held

    # -- drawing -------------------------------------------------------------------------------

    def text(self, width: int | None = None, height: int | None = None) -> str:
        """The frame as plain text, the way a test or a doc reads it.

        Args:
            width: the columns; None takes the console's.
            height: the lines; None takes the console's.

        Returns:
            The frame's lines, joined.
        """
        width = width or self.console.width
        height = height or self.console.height
        return "\n".join(line.plain.rstrip() for line in self.frame(width, height))

    def __rich_console__(self, console: Console, options: ConsoleOptions) -> Iterator[Any]:
        """Rich's hook: one frame, as tall as the screen."""
        height = options.height or console.height
        try:
            lines = self.frame(options.max_width, height)
        except Exception as error:  # noqa: BLE001 - a drawing bug must never end the run
            lines = [Text(f"flnd: can't draw the screen: {type(error).__name__}: {error}")]
        yield Group(*lines)

    def frame(self, width: int, height: int) -> list[Text]:
        """Every line of one frame, fitted to `width` × `height`.

        The header, the system line and the footer always show; the targets, downloads and
        log share the lines left, each taking what it needs up to a fair share, so a short
        list gives its lines to the others.

        Args:
            width: the columns.
            height: the lines.

        Returns:
            At most `height` lines, each at most `width` cells.
        """
        width, height = max(width, 20), max(height, 4)
        stats = self._stats
        targets = self._picked()
        files = list(stats.transfers.files) if stats is not None else []
        pauses = list(stats.pauses) if stats is not None else []
        logged = self.log.lines()
        top = [self._head(width), self._totals(width), self._rule(width)]
        bottom = [self._rule(width), self._footer(width)]
        system = self._system(width, two=height >= 20)
        # The three panels: each a title line, then rows. What fits is shared fairly.
        room = height - len(top) - len(bottom) - len(system) - 3 - 2
        log_rows = min(len(pauses) + len(logged), max(2, room // 5)) if room > 4 else 0
        rest = max(0, room - log_rows)
        target_rows = min(len(targets) or 1, max(rest // 2, rest - len(files)))
        file_rows = max(0, rest - target_rows)
        # A panel with fewer rows than its share gives the rest to the log.
        spare = file_rows - len(files) if file_rows > len(files) else 0
        file_rows -= spare
        log_rows = min(len(pauses) + len(logged), log_rows + spare) if room > 4 else 0
        lines = list(top)
        lines += self._targets_panel(width, targets, target_rows)
        lines.append(Text())
        lines += self._downloads_panel(width, files, file_rows)
        lines.append(Text())
        lines += system
        if log_rows:
            lines += self._log_panel(width, pauses, logged, log_rows + 1)
        lines = lines[:max(0, height - len(bottom))]
        lines += [Text()] * (height - len(bottom) - len(lines))
        lines += bottom
        for line in lines:
            line.truncate(width, overflow="ellipsis")
        return lines

    # The parts of a frame, top to bottom. Each takes the frame's `width` in cells.

    def _head(self, width: int) -> Text:
        """The first line: flnd, the run, its state and elapsed time.

        Args:
            width: the frame's columns.

        Returns:
            The line, the state and time flush right.
        """
        stats = self._stats
        if stats is not None and stats.stopping == "now":
            state = ("■ stopping now", "error")
        elif (stats is not None and stats.stopping) or self._quit:
            state = ("■ stopping", "warn")
        elif stats is not None and stats.suspended:
            state = ("⏸ paused", "warn")
        else:
            state = (f"{self._spinner()} running", "ok")
        elapsed = _clock(time.monotonic() - self._began)
        right = Text.assemble(state, ("  " + elapsed, "text"))
        left = Text.assemble((" flnd ", "bold reverse accent"), "  ", (self.title, "text"))
        return _spread(left, right, width)

    def _totals(self, width: int) -> Text:
        """The totals: files saved, skipped and failed, bytes, then the speed and the ETA.

        The counts come from the events as they happen; the speed and the ETA from the latest
        `Stats`, so they show from the first second on.

        Args:
            width: the frame's columns.

        Returns:
            The line.
        """
        line = Text.assemble((self._mark("✓", "+"), "ok"), f" {self._saved:,} saved",
                             ("   " + _text.size(self._bytes), "size"))
        if self._skipped:
            line.append(f"   {self._skipped:,} already saved", style="info")
        if self._failed:
            line.append(f"   {self._mark('✗', 'x')} {self._failed:,} failed", style="error")
        if self._planned:
            line.append(f"   {self._planned:,} planned", style="accent")
        stats = self._stats
        right = Text()
        if stats is not None:
            right.append(f"{self._mark('↓', 'v')} {_text.size(self._speed)}/s", style="accent")
            if stats.eta is not None:
                right.append(f"   ETA {_text.duration(stats.eta)}", style="text")
        return _spread(line, right, width)

    def _rule(self, width: int) -> Text:
        return Text(self._mark("─", "-") * width, style="faint")

    def _targets_panel(self, width: int, rows: list[_Target], room: int) -> list[Text]:
        """The targets: a title with the count of each state, then the rows that fit.

        Args:
            width: the frame's columns.
            rows: every row, busy first (`_picked`).
            room: the lines for rows; when they don't all fit, the last says how many more.

        Returns:
            The title and the rows.
        """
        states: dict[str, int] = {}
        for row in rows:
            states[row.state] = states.get(row.state, 0) + 1
        summary = " · ".join(f"{count} {state}" for state, count in
                             sorted(states.items(), key=lambda item: ORDER.get(item[0], 9)))
        lines = [_title("Targets", summary)]
        if not rows:
            lines.append(Text("  waiting for the first listing", style="info"))
            return lines
        shown = rows if len(rows) <= room else rows[:max(0, room - 1)]
        wide = width >= WIDE
        lines += [self._target_row(width, wide, row) for row in shown]
        if len(shown) < len(rows):
            hidden = rows[len(shown):]
            done = sum(row.state == "done" for row in hidden)
            lines.append(Text(f"  … and {len(hidden):,} more ({done:,} done)", style="info"))
        return lines

    def _target_row(self, width: int, wide: bool, row: _Target) -> Text:
        """One target's row: mark, name, state, pages and posts, files with a bar, size.

        Args:
            width: the frame's columns.
            wide: whether the frame has room for the pages and the early-stop streak.
            row: the row's numbers.

        Returns:
            The line.
        """
        if row.state == "listing":
            mark, style = self._spinner(), "accent"
        else:
            mark, style = MARKS.get(row.state, ("·", "info"))
            if not self._unicode:
                mark = {"✓": "+", "✗": "x", "↓": "v", "■": "#"}.get(mark, mark)
        counts = [f"{row.posts:,} posts".rjust(10)]
        if wide:
            counts.insert(0, f"{row.pages:,} pages".rjust(10))
        files = f"{row.ended:,}/{row.found:,}".rjust(10)
        sized = _short(row.size).rjust(9)
        detail = row.state
        if wide and row.state == "listing" and row.streak:
            # Saved posts in a row: at 5, a sync stops early, caught up.
            detail = f"listing, {row.streak}/5 saved"
        column = 18 if wide else 9
        # The mark, the name, the state, the counts, the files, the bar and the size, with one
        # space between each: the name takes what is left.
        fixed = 2 + 1 + column + sum(len(cell) + 1 for cell in counts) + 11 + 1 + BAR + len(sized)
        name = _fit(row.name, max(8, width - fixed))
        line = Text.assemble((mark + " ", style), (name, "text"), " ",
                             (_fit(detail, column), style if row.state != "done" else "info"))
        for cell in counts:
            line.append(" " + cell, style="size")
        line.append(" " + files, style="error" if row.failed else "text")
        line.append(" ")
        line.append_text(_bar(row.ended, row.found, BAR, self._unicode))
        line.append(sized, style="size")
        return line

    def _downloads_panel(self, width: int, files: list[Any], room: int) -> list[Text]:
        """The downloads: counts and the busiest hosts, then each file in flight that fits.

        Args:
            width: the frame's columns.
            files: the `flnd.Stats` transfers in flight, oldest first.
            room: the lines for rows.

        Returns:
            The title and the rows.
        """
        stats = self._stats
        if stats is None:
            return [_title("Downloads", "starting")]
        flows = stats.transfers
        summary = f"{flows.running:,} running · {flows.queued:,} queued"
        if flows.resuming:
            summary += f" · {flows.resuming:,} resuming"
        # A host's busy transfers over its adaptive cap, and how many wait for a place.
        hosts = " · ".join(f"{host.host} {host.busy}/{host.cap}"
                           + (f" +{host.waiting}" if host.waiting else "")
                           for host in flows.hosts[:3] if host.busy or host.waiting)
        lines = [_spread(_title("Downloads", summary), Text(_clean(hosts), style="info"), width)]
        if not files:
            lines.append(Text("  nothing downloading right now", style="info"))
            return lines[:room + 1]
        shown = files if len(files) <= room else files[:max(0, room - 1)]
        wide = width >= WIDE
        lines += [self._file_row(width, wide, flow) for flow in shown]
        if len(shown) < len(files):
            lines.append(Text(f"  … and {len(files) - len(shown):,} more", style="info"))
        return lines

    def _file_row(self, width: int, wide: bool, flow: Any) -> Text:
        """One file in flight: bar and percent, a mark if it resumed, name, size, speed, host.

        Args:
            width: the frame's columns.
            wide: whether the frame has room for the host.
            flow: the file, a `flnd.Stats` transfer.

        Returns:
            The line.
        """
        total = flow.total
        percent = f"{100 * flow.done / total:3.0f}%" if total else "   …"
        sized = f"{_short(flow.done)} / {_short(total)}" if total else _short(flow.done)
        speed = f"{_short(flow.speed)}/s"
        tail = [sized.rjust(17), speed.rjust(10)]
        if wide:
            tail.append(_fit(_clean(flow.host), 26).rjust(27))
        # The indent, the bar, the percent and the resume mark come before the name; the
        # cells of `tail` after it.
        fixed = 2 + TRANSFER_BAR + 5 + 2 + 1 + sum(len(cell) + 1 for cell in tail)
        # A file's name ends in what tells it apart (its post id, its extension): cut its head.
        name = _fit(_clean(flow.name), max(8, width - fixed), head=True)
        line = Text("  ")
        line.append_text(_bar(flow.done, total or 0, TRANSFER_BAR, self._unicode, live=True))
        line.append(" " + percent, style="text")
        line.append(" " + (self._mark("↻", "~") if flow.resumed else " "), style="warn")
        line.append(" " + name, style="text")
        for number, cell in enumerate(tail):
            line.append(" " + cell, style="accent" if number == 1 else "size")
        return line

    def _system(self, width: int, *, two: bool) -> list[Text]:
        """The system line: tabs, the browser's and flnd's RAM, CPU, media jobs, free disk.

        Args:
            width: the frame's columns.
            two: whether it may take two lines when one is too narrow.

        Returns:
            One line, or two.
        """
        stats = self._stats
        if stats is None:
            return [_title("System", "first sample in a second")]
        system = stats.system
        parts = [f"tabs {system.tabs}/{system.cap}"]
        if system.browser:
            parts.append(f"browser {_short(system.browser)}")
        parts += [f"flnd {_short(system.ram)}", f"cpu {system.cpu:.0f}%"]
        jobs = [f"{name} {count}" for count, name in ((system.ffmpeg, "ffmpeg"),
                                                     (system.pillow, "pillow"),
                                                     (system.unpack, "unpack")) if count]
        parts += jobs or ["no media jobs"]
        if system.steps:
            parts.append(f"{system.steps:,} queued for a step")
        if system.disk:
            parts.append(f"disk {_short(system.disk)} free")
        line = _title("System", " · ".join(parts))
        if line.cell_len <= width or not two:
            return [line]
        # Too long for one line: the browser and flnd on the first, the jobs and disk below.
        split = 4 if system.browser else 3
        return [_title("System", " · ".join(parts[:split])),
                Text(" " * len("System  ") + " · ".join(parts[split:]), style="info")]

    def _log_panel(self, width: int, pauses: list[Any], logged: list[str],
                   room: int) -> list[Text]:
        """The log: each wait with its reason first, then the newest lines that fit.

        Args:
            width: the frame's columns (each line is cut to it later).
            pauses: the `flnd.Stats` waits, the longest first.
            logged: the log's lines, oldest first.
            room: the lines for the panel, its title included.

        Returns:
            The title and the lines.
        """
        lines = [_title("Log", f"{len(pauses)} waiting" if pauses else "")]
        rows: list[Text] = []
        for pause in pauses:
            rows.append(Text.assemble(
                ("  " + self._mark("⏸", "|") + " ", "warn"),
                (f"{pause.source} {_clean(pause.host)}", "text"),
                (f"  waits {_text.duration(pause.seconds)}", "warn"),
                (f"  {_clean(pause.reason)}", "info")))
        for text in logged:
            rows.append(Text("  " + text, style=LOG_STYLES.get(text[:1], "info")))
        # The waits always show; of the log, its newest lines.
        if len(rows) > room - 1:
            keep = max(0, room - 1)
            waits = rows[:min(len(pauses), keep)]
            rows = waits + (rows[len(rows) - (keep - len(waits)):] if keep > len(waits) else [])
        return lines + rows

    def _footer(self, width: int) -> Text:
        """The keys, and what a stop or a pause is doing: the Ctrl-C stages show here.

        Args:
            width: the frame's columns.

        Returns:
            The line.
        """
        stats = self._stats
        suspended = stats is not None and stats.suspended
        keys = Text.assemble((" q ", "reverse"), " stop  ", (" p ", "reverse"),
                             " resume" if suspended else " pause", style="info")
        stage = stats.stopping if stats is not None else ""
        if stage == "now":
            say = Text("stopping now: the files in flight are cancelled", style="error")
        elif stage == "soon" or self._quit:
            running = stats.transfers.running if stats is not None else 0
            say = Text(f"stopping: {running} files finishing · Ctrl-C again to stop now",
                       style="warn")
        elif suspended:
            say = Text("paused: nothing new starts · p to go on", style="warn")
        else:
            say = Text("Ctrl-C stops too", style="faint")
        return _spread(keys, say, width)

    # Helpers of the parts.

    def _picked(self) -> list[_Target]:
        """The target rows, busy first, worked out once per change rather than per frame.

        Returns:
            A row per target and category: from the latest `Stats`, or from the `Progress`
            events before the first one; the same state keeps its order of appearance.
        """
        if self._targets is not None:
            return self._targets
        rows: list[_Target]
        if self._stats is not None:
            rows = [_Target(_clean(f"{row.target} {row.category}".strip()), row.state,
                            row.pages, row.posts, row.found,
                            row.saved + row.skipped + row.failed, row.failed, row.bytes,
                            row.streak) for row in self._stats.listing]
        else:
            rows = [_Target(_clean(f"{target} {category}".strip()), _progress_state(step), 0, 0,
                            step.found, step.saved + step.skipped + step.failed, step.failed,
                            step.bytes, 0)
                    for (target, category), step in list(self._progress.items())]
        # `sorted` is stable, so rows of one state keep the order they came in.
        self._targets = sorted(rows, key=lambda row: ORDER.get(row.state, 9))
        return self._targets

    def _spinner(self) -> str:
        if not self._unicode:
            return "|/-\\"[int(time.monotonic() * FPS) % 4]
        return SPIN[int(time.monotonic() * FPS) % len(SPIN)]

    def _mark(self, unicode: str, ascii: str) -> str:
        return unicode if self._unicode else ascii


class _Target(NamedTuple):
    """One target's row of the targets panel: what `_target_row` draws."""

    # The target and its category, cleaned for the screen.
    name: str
    state: str
    pages: int
    posts: int
    found: int
    # Files saved, skipped or failed: those the listing found that are over.
    ended: int
    failed: int
    size: int
    streak: int


class _Log:
    """The log panel's lines: a file the program's log writes to, from any thread.

    Rich writes whole lines (with their end) to it; each becomes one entry, newest last, and
    the oldest go past `LOG`. A deque's append is safe from several threads.
    """

    def __init__(self) -> None:
        self._lines: deque[str] = deque(maxlen=LOG)
        self._partial = ""

    def write(self, text: str) -> int:
        """Keep each whole line of `text`; a part without its end waits for the rest.

        Args:
            text: what the log wrote.

        Returns:
            Its length, as a file's `write` returns.
        """
        self._partial += text
        *whole, self._partial = self._partial.split("\n")
        for line in whole:
            if line.strip():
                self.add(line)
        return len(text)

    def flush(self) -> None:
        """Nothing to flush: lines are kept as they come."""

    def isatty(self) -> bool:
        """Never a terminal, so the log writes plain lines here."""
        return False

    def add(self, line: str) -> None:
        """Keep one line, its control codes stripped."""
        self._lines.append(_clean(line))

    def lines(self) -> list[str]:
        """The lines kept, oldest first."""
        return list(self._lines)


class Keys:
    """Read single keys from the terminal on a thread, and hand each to `handle`.

    On POSIX the terminal goes into cbreak mode (no line buffering, no echo; Ctrl-C still
    interrupts) with `termios`, and `stop` puts back exactly the mode it found; on Windows
    `msvcrt` reads keys with nothing to change. With no terminal on standard input, it reads
    nothing.

    Args:
        handle: called with each key, on the reader's thread.
        stream: where keys come from; None reads standard input.
        terminal: the `termios` module to use; tests pass a fake. None imports the real one.

    Example:
        >>> import io
        >>> keys = Keys(print, stream=io.StringIO(""))   # not a terminal: reads nothing
        >>> keys.start(); keys.stop()
    """

    # How often, in seconds, the reader looks whether it should stop.
    POLL = 0.1

    def __init__(self, handle: Callable[[str], None], *, stream: IO[str] | None = None,
                 terminal: Any = None) -> None:
        self.handle = handle
        self.stream = stream
        self.terminal = terminal
        self._saved: Any = None
        self._fd: int | None = None
        self._thread: threading.Thread | None = None
        self._done = threading.Event()

    def start(self) -> None:
        """Put the terminal in cbreak mode and start reading; nothing off a terminal."""
        stream = self.stream if self.stream is not None else sys.stdin
        if stream is None or not stream.isatty() or self._thread is not None:
            return
        self._done.clear()
        if os.name == "nt":
            target: Callable[[], None] = self._windows
        else:
            termios = self.terminal or __import__("termios")
            self.terminal = termios
            self._fd = stream.fileno()
            self._saved = termios.tcgetattr(self._fd)
            termios.tcsetattr(self._fd, termios.TCSANOW, _cbreak(termios, self._saved))
            target = self._posix
        self._thread = threading.Thread(target=target, name="flnd-keys", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        """Stop reading and put the terminal back as `start` found it; safe to call twice."""
        self._done.set()
        thread, self._thread = self._thread, None
        try:
            if thread is not None and thread is not threading.current_thread():
                thread.join(self.POLL * 5)
        finally:
            saved, self._saved = self._saved, None
            if saved is not None and self._fd is not None:
                self.terminal.tcsetattr(self._fd, self.terminal.TCSADRAIN, saved)

    def _posix(self) -> None:
        """The reader on POSIX: wait for a byte with `select`, so `stop` is seen in `POLL`."""
        import select

        fd = self._fd
        assert fd is not None
        while not self._done.is_set():
            try:
                ready, _, _ = select.select([fd], [], [], self.POLL)
                if not ready:
                    continue
                data = os.read(fd, 1)
            except (OSError, ValueError):
                return
            if not data:
                return
            self._hand(data.decode(errors="ignore"))

    def _windows(self) -> None:  # pragma: no cover - Windows only
        """The reader on Windows: `msvcrt` polls the console for a key."""
        import msvcrt

        while not self._done.wait(self.POLL):
            while msvcrt.kbhit():  # type: ignore[attr-defined]
                self._hand(msvcrt.getwch())  # type: ignore[attr-defined]

    def _hand(self, key: str) -> None:
        """Hand one key on; a failing handler never ends the reader."""
        try:
            self.handle(key)
        except Exception:  # noqa: BLE001 - a key must never end the run
            pass


def _cbreak(termios: Any, mode: list[Any]) -> list[Any]:
    """`mode` with line buffering and echo off, reading one byte at a time; signals stay on.

    Args:
        termios: the module.
        mode: the terminal's mode, as `tcgetattr` gave it.

    Returns:
        A new mode list; `mode` is left as it is, to restore.
    """
    changed = list(mode)
    # The fields of a mode: iflag, oflag, cflag, lflag, ispeed, ospeed, cc.
    changed[3] = changed[3] & ~(termios.ICANON | termios.ECHO)
    cc = list(changed[6])
    cc[termios.VMIN], cc[termios.VTIME] = 1, 0
    changed[6] = cc
    return changed


# ---------------------------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------------------------

def _title(name: str, detail: str) -> Text:
    """A panel's title line: its name, then a detail in the quiet colour."""
    return Text.assemble((name, "bold accent"), ("  " + detail if detail else "", "info"))


def _spread(left: Text, right: Text, width: int) -> Text:
    """`left` and `right` on one line, `right` flush with the right edge when both fit.

    Args:
        left: the start of the line.
        right: its end.
        width: the line's cells.

    Returns:
        A new line; when both don't fit, `right` follows `left` after two spaces, and the
        frame cuts what overflows.
    """
    gap = width - left.cell_len - right.cell_len
    if gap < 2:
        line = left.copy()
        line.append("  ")
        line.append_text(right)
        return line
    line = left.copy()
    line.append(" " * gap)
    line.append_text(right)
    return line


def _bar(done: float, total: float, cells: int, unicode: bool, *, live: bool = False) -> Text:
    """A bar of `cells` cells, filled to `done / total`; empty when the total is unknown.

    Args:
        done: how far.
        total: out of what; 0 or less when unknown.
        cells: the bar's width.
        unicode: draw with box lines, else with `=` and `-`.
        live: a file in flight, always in the accent colour; else a full bar turns green.

    Returns:
        The bar.
    """
    ratio = 0.0 if total <= 0 else min(max(done / total, 0.0), 1.0)
    filled = round(cells * ratio)
    full, empty = ("━", "─") if unicode else ("=", "-")
    style = "accent" if live or ratio < 1 else "ok"
    return Text.assemble((full * filled, style), (empty * (cells - filled), "faint"))


def _fit(text: str, width: int, *, head: bool = False) -> str:
    """`text` padded or cut to `width` cells, with `…` where it was cut.

    Args:
        text: the text.
        width: the cells it gets.
        head: cut its start rather than its end.

    Returns:
        Exactly `width` characters.
    """
    if len(text) <= width:
        return text.ljust(width)
    if head:
        return "…" + text[len(text) - max(0, width - 1):]
    return text[:max(0, width - 1)] + "…"


def _short(amount: float) -> str:
    """Bytes as `_text.size` writes them, without the decimal from 100 of a unit up:
    "412 MB", "7.9 MB", "1.2 GB"."""
    text = _text.size(amount)
    number, _, unit = text.partition(" ")
    return f"{float(number):.0f} {unit}" if float(number) >= 100 else text


def _clock(seconds: float) -> str:
    """Elapsed time as `m:ss`, or `h:mm:ss` past an hour."""
    whole = int(seconds)
    hours, rest = divmod(whole, 3600)
    minutes, secs = divmod(rest, 60)
    return f"{hours}:{minutes:02d}:{secs:02d}" if hours else f"{minutes}:{secs:02d}"


def _progress_state(step: Any) -> str:
    """A `Progress` event's state, before the first `Stats` gives the real one.

    Args:
        step: the event.

    Returns:
        "listing", "finishing" or "done".
    """
    if not step.listed:
        return "listing"
    if step.found > step.saved + step.skipped + step.failed:
        return "finishing"
    return "done"


def _clean(text: str) -> str:
    """Text from a site, safe to show: no escape sequences, no control codes."""
    return strip_control_codes(_ESCAPE.sub("", text))

