"""The run's screen: a live block drawn from the run's `State` snapshot, and the end summary.

SPEC.md#commands-and-flags explains it.
"""

from __future__ import annotations

import queue
import sys
import threading
import time
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from pathlib import Path
from typing import IO, Any

from rich.cells import cell_len
from rich.text import Text

from flnd.engine.events import Failed, Phase, Report, Resources, State, TargetState
from flnd.ui import out, theme
from flnkit import clock
from flnkit.text import count, duration, size

__all__ = ["Screen", "frame", "paused", "summary"]

# Seconds between two screen redraws (10 fps).
TICK = 1 / 10
# The most cells a target, its categories and a file's path take, and a file's bar.
TARGET_CELLS, CATEGORY_CELLS, PATH_CELLS, BAR_CELLS = 28, 14, 24, 30
# The lines the block has beside its target and file rows: the header, the column titles, the
# `now` line, the foot, and the blank lines between them.
FRAME_LINES = 8
# The failures the end summary names; `flnd status` has them all.
FAILURES_SHOWN = 10
# The count columns: title and width, right-aligned.
# Each column leaves at least 2 cells before its widest value (`999,999`, `1023.9 MB`,
# `1023.9 KB/s`), so columns never touch.
COLUMNS = (("saved", 8), ("skipped", 9), ("failed", 9), ("size", 11), ("speed", 13))
KEYS = "ctrl+c stop · twice: stop now"
INDENT = "  "


# ---------------------------------------------------------------------------------------------
# Cells: every width here is counted in terminal cells, not characters (a CJK character is two)
# ---------------------------------------------------------------------------------------------

def _cells(text: str) -> int:
    """Terminal cell width of `text`, fast-pathed for printable ASCII."""
    return len(text) if text.isascii() and text.isprintable() else cell_len(text)


def _head(text: str, cells: int) -> str:
    """The longest start of `text` that fits in `cells`."""
    if text.isascii() and text.isprintable():
        return text[:cells]
    taken, used = [], 0
    for char in text:
        width = cell_len(char)
        if used + width > cells:
            break
        taken.append(char)
        used += width
    return "".join(taken)


def _tail(text: str, cells: int) -> str:
    """The longest end of `text` that fits in `cells`."""
    if text.isascii() and text.isprintable():
        return text[-cells:] if cells > 0 else ""
    return _head(text[::-1], cells)[::-1]


def middle(text: str, cells: int, *, fancy: bool = True) -> str:
    """`text` cut in the middle to fit in `cells`, with the `more` glyph in the cut.

    Example:
        >>> middle("ins:example_user", 10)
        'ins:…_user'
    """
    if _cells(text) <= cells:
        return text
    cut = theme.glyph("more", fancy)
    room = max(0, cells - _cells(cut))
    left = _head(text, room // 2)
    return left + cut + _tail(text, room - _cells(left))


def left(text: str, cells: int, *, fancy: bool = True) -> str:
    """`text` cut at its start to fit in `cells`, the `more` glyph first: a path's end shows.

    Example:
        >>> left("media/2026/1290_2.mp4", 12)
        '…/1290_2.mp4'
    """
    if _cells(text) <= cells:
        return text
    cut = theme.glyph("more", fancy)
    return cut + _tail(text, max(0, cells - _cells(cut)))


def _pad(text: str, cells: int) -> str:
    return text + " " * max(0, cells - _cells(text))


def _right(text: str, cells: int) -> str:
    return " " * max(0, cells - _cells(text)) + text


# ---------------------------------------------------------------------------------------------
# The frame: one draw of the live block
# ---------------------------------------------------------------------------------------------

def frame(state: State, *, command: str, width: int, height: int, fancy: bool = True,
          elapsed: float | None = None) -> Text:
    """The live block for `state`, laid out in `width` by `height` cells.

    Args:
        state: the run's snapshot.
        command: the command's name, for the header (`download`).
        width: the terminal's columns; a longer line is cut.
        height: its lines; targets past the room fold into one `+ <n> targets` line.
        fancy: draw Unicode glyphs; False for ASCII.
        elapsed: seconds since the run started; None takes `state.elapsed`.

    Returns:
        The block, one line of text per row.
    """
    at = clock.now()
    sep = f" {theme.glyph('sep', fancy)} "
    lines: list[Text] = []
    total = len(state.targets)
    if total > 1:
        done = sum(t.phase in (Phase.DONE, Phase.FAILED) for t in state.targets)
        progress = f"{done}/{total} targets{sep}{total - done} left"
    else:
        progress = count(total, "target")
    shown_elapsed = state.elapsed if elapsed is None else elapsed
    title = (f"flnd {command}{sep}{progress}{sep}"
             f"{int(shown_elapsed) // 60:02d}:{int(shown_elapsed) % 60:02d}")
    lines += [_line((title, "focus")), Text()]
    limit = max(1, width - 1)
    widths, cols = _target_layout(state.targets, limit)
    header = _pad("target", 2 + widths[0]) + "  "
    if widths[1]:
        header += _pad("categories", widths[1]) + "  "
    header += "".join(_right(name, cells) for name, cells in cols)
    use_lines = 1 if state.resources is not None else 0
    if height >= 10:
        min_targets = min(total, 2 if height >= 12 else 1)
        max_file_budget = max(0, height - FRAME_LINES - use_lines - min_targets)
        files = _files(state, at, fancy, limit,
                       max_rows=None if max_file_budget >= 99 else max(0, max_file_budget - 1))
        if files and len(files) + 1 > max_file_budget:
            files = []
        room = max(0, height - FRAME_LINES - (len(files) + 1 if files else 0) - use_lines)
        lines.append(_line((header, "dim")))
        shown = _shown_targets(state.targets, room)
        lines += [_target(row, widths, cols, at, fancy) for row in shown]
        if len(shown) < total and room > 0:
            lines.append(_line((f"+ {total - len(shown)} targets", "dim")))
        lines.append(Text())
        if files:
            lines += files + [Text()]
        lines.append(_now(state, fancy))
        if state.resources is not None:
            lines.append(_use(state.resources, sep))
        lines += [Text(), _foot(state, width, fancy)]
    elif height >= 4:
        bottom = [_now(state, fancy), _foot(state, width, fancy)]
        room = height - 2 - len(bottom)
        shown = _shown_targets(state.targets, 2 if room == 1 and total > 1 else room)
        lines = [_line((title, "focus")), _line((header, "dim"))]
        lines += [_target(row, widths, cols, at, fancy) for row in shown]
        if len(shown) < total and len(lines) + len(bottom) < height:
            lines.append(_line((f"+ {total - len(shown)} targets", "dim")))
        lines += bottom
    elif height >= 2:
        shown = _shown_targets(state.targets, height - 2)
        lines = [_line((title, "focus"))]
        lines += [_target(row, widths, cols, at, fancy) for row in shown]
        lines.append(_foot(state, width, fancy))
    else:
        lines = [_line((title, "focus"))]
    if len(lines) > height:
        lines = lines[:height]
    for line in lines:
        line.truncate(limit)
    block = Text("\n").join(lines)
    block.no_wrap = True
    return block


def _shown_targets(targets: tuple[TargetState, ...], room: int) -> list[TargetState]:
    """The target rows that fit in `room`: active first, then waiting, then recent finished."""
    if len(targets) <= room:
        return list(targets)
    keep = max(0, room - 1)
    if keep == 0:
        return []
    active = [i for i, t in enumerate(targets) if t.phase in (Phase.LISTING, Phase.DOWNLOADING)]
    waiting = [i for i, t in enumerate(targets) if t.phase is Phase.WAITING]
    ended = [i for i, t in enumerate(targets) if t.phase in (Phase.DONE, Phase.FAILED)]
    picked = sorted((active + waiting + list(reversed(ended)))[:keep])
    return [targets[i] for i in picked]


def _line(*parts: tuple[str, str]) -> Text:
    text = Text(INDENT)
    for words, spec in parts:
        text.append(words, style=theme.style(spec))
    return text


def _mark(phase: Phase, at: float, fancy: bool) -> tuple[str, str]:
    """A target's glyph and its token, by its phase."""
    if phase is Phase.WAITING:
        return theme.glyph("waiting", fancy), "dim"
    if phase is Phase.DONE:
        return theme.glyph("done", fancy), "good"
    if phase is Phase.FAILED:
        return theme.glyph("failed", fancy), "bad"
    return theme.spinner(at, fancy), "focus"


def _target_layout(targets: Sequence[TargetState],
                   limit: int) -> tuple[tuple[int, int], tuple[tuple[str, int], ...]]:
    tw = min(TARGET_CELLS, max([4, *(_cells(t.text) for t in targets)]))
    cw = min(CATEGORY_CELLS, max([10, *(_cells(" ".join(t.categories)) for t in targets)]))
    cols = list(COLUMNS)

    def needed(w0: int, w1: int, active: list[tuple[str, int]]) -> int:
        return 6 + w0 + (w1 + 2 if w1 else 0) + sum(w for _, w in active)

    if needed(tw, cw, cols) <= limit:
        return (tw, cw), tuple(cols)
    if needed(tw, cw, cols) > limit:
        cw = max(min(cw, 10), cw - (needed(tw, cw, cols) - limit))
    if needed(tw, cw, cols) > limit:
        tw = max(min(tw, 16), tw - (needed(tw, cw, cols) - limit))
    if needed(tw, cw, cols) > limit:
        cw = 0
    for drop in ("skipped", "failed", "size", "speed"):
        if needed(tw, cw, cols) <= limit:
            break
        cols = [c for c in cols if c[0] != drop]
    if needed(tw, cw, cols) > limit:
        tw = max(8, min(tw, limit - 6 - sum(w for _, w in cols)))
    return (tw, cw), tuple(cols)


def _target(row: TargetState, widths: tuple[int, int], cols: tuple[tuple[str, int], ...],
            at: float, fancy: bool) -> Text:
    glyph, token = _mark(row.phase, at, fancy)
    none = theme.glyph("none", fancy)
    counted = row.found or row.saved or row.skipped or row.failed
    going = row.phase in (Phase.LISTING, Phase.DOWNLOADING)
    values = {
        "saved": f"{row.saved:,}" if counted else none,
        "skipped": f"{row.skipped:,}" if counted else none,
        "failed": f"{row.failed:,}" if counted else none,
        "size": size(row.bytes) if row.bytes else none,
        "speed": f"{size(row.speed)}/s" if going and row.speed else none,
    }
    parts: list[tuple[str, str]] = [
        (glyph + " ", token),
        (_pad(middle(row.text, widths[0], fancy=fancy), widths[0]) + "  ", "text"),
    ]
    if widths[1]:
        parts.append((_pad(middle(" ".join(row.categories), widths[1], fancy=fancy),
                           widths[1]) + "  ", "dim"))
    parts.append(("".join(_right(values[name], cells) for name, cells in cols), "text"))
    return _line(*parts)


def _files(state: State, at: float, fancy: bool, limit: int = 99,
           max_rows: int | None = None) -> list[Text]:
    """The rows of the files in flight, then `+ <n> more`."""
    if limit >= 90:
        path_cells, bar_cells = PATH_CELLS, BAR_CELLS
        show_pct, show_size, show_speed = True, True, True
    else:
        show_speed = limit >= 42
        show_size = limit >= 30
        tail = (11 if show_size else 0) + (13 if show_speed else 0)
        path_cells = PATH_CELLS if limit >= 72 else max(12, min(PATH_CELLS, limit - 20 - tail))
        avail = limit - 4 - path_cells - 6 - tail
        bar_cells = max(8, min(BAR_CELLS, avail)) if limit >= 56 and avail >= 8 else 0
        show_pct = limit >= 36
    total_rows = len(state.files) + (1 if state.more else 0)
    if max_rows is not None and max_rows <= 0:
        return []
    if max_rows is not None and total_rows > max_rows:
        shown_files = state.files[:max(0, max_rows - 1)]
    else:
        shown_files = state.files
    hidden = (len(state.files) - len(shown_files)) + state.more
    rows = []
    for file in shown_files:
        line = _line((theme.spinner(at, fancy) + " ", "focus"),
                     (_pad(left(file.path, path_cells, fancy=fancy), path_cells) + "  ", "text"))
        if file.total:
            share = max(0.0, min(1.0, file.done / file.total))
            if bar_cells:
                filled = int(share * bar_cells)
                line.append(theme.glyph("full", fancy) * filled, style=theme.style("focus"))
                if filled < bar_cells:
                    rest = theme.glyph("head", fancy) + theme.glyph("rest", fancy) * (
                        bar_cells - filled - 1)
                    line.append(rest, style=theme.style("dim"))
            if show_pct:
                line.append(_right(f"{int(share * 100)}%", 6), style=theme.style("text"))
            shown = size(file.total)
        else:
            if bar_cells or show_pct:
                line.append(" " * (bar_cells + (6 if show_pct else 0)))
            shown = size(file.done)
        speed = f"{size(file.speed)}/s" if file.speed else theme.glyph("none", fancy)
        tail_text = (_right(shown, 11) if show_size else "") + (
            _right(speed, 13) if show_speed else "")
        if tail_text:
            line.append(tail_text, style=theme.style("text"))
        rows.append(line)
    if hidden:
        rows.append(_line((f"  + {hidden} more", "dim")))
    return rows


def _now(state: State, fancy: bool) -> Text:
    """`now  <State.now>`, in `warn` while a wait goes on."""
    text = " ".join(state.now.split())
    if state.holds and state.holds[-1].count > 1:
        text += f" ({theme.glyph('times', fancy)}{state.holds[-1].count})"
    return _line(("now  ", "dim"), (text, "warn" if state.holds else "text"))


def _use(use: Resources, sep: str) -> Text:
    """`use  flnd 5 processes · 312.0 MB · 14% · browser 3 processes · 200.0 MB · 9%`."""
    def part(name: str, procs: int, rss: int, cpu: float) -> str:
        found = count(procs, "process", plural="processes")
        return sep.join([f"{name} {found}", size(rss), f"{cpu:.0f}%"])

    both = part("flnd", use.procs, use.rss, use.cpu)
    if use.browser:
        both += sep + part("browser", use.browser, use.browser_rss, use.browser_cpu)
    return _line(("use  ", "dim"), (both, "text"))


def _foot(state: State, width: int, fancy: bool) -> Text:
    """The run's speed and counts, with the keys on the right."""
    sep = f" {theme.glyph('sep', fancy)} "
    # Idle (nothing in flight), the speed of the last 3 s is no speed.
    flying = state.files or state.more
    speed = f"{size(state.speed)}/s" if flying and state.speed else theme.glyph("none", fancy)
    counts = f"{speed}{sep}{state.saved:,} saved{sep}{state.failed:,} failed"
    keys = KEYS.replace(" · ", sep)
    if width >= 100 or _cells(INDENT + counts + keys) + 4 <= width:
        gap = max(2, width - _cells(INDENT + counts + keys) - 2)
        return _line((counts, "text"), (" " * gap + keys, "dim"))
    short_keys = "ctrl+c stop"
    gap = width - _cells(INDENT + counts + short_keys) - 2
    if gap >= 2:
        return _line((counts, "text"), (" " * gap + short_keys, "dim"))
    return _line((counts, "text"))


def _reflowed_rows(widths: Sequence[int], width: int) -> int:
    step = max(1, width)
    return sum(max(1, (w - 1) // step + 1) if w > 0 else 1 for w in widths)


def _clear_block(console: Any, up: int) -> None:
    """Erase `up + 1` rows upward from the current cursor row, leaving cursor at column 0."""
    from rich.control import Control
    from rich.segment import ControlType

    if console.legacy_windows:
        steps: list[Any] = [ControlType.CARRIAGE_RETURN, (ControlType.ERASE_IN_LINE, 2)]
        for _ in range(up):
            steps.extend([(ControlType.CURSOR_UP, 1), (ControlType.ERASE_IN_LINE, 2)])
        console.control(Control(*steps))
        return
    steps = [ControlType.CARRIAGE_RETURN]
    if up:
        steps.append((ControlType.CURSOR_UP, up))
    console.control(Control(*steps))
    console.file.write("\x1b[J")
    console.file.flush()


# ---------------------------------------------------------------------------------------------
# The screen: the `flnd-screen` thread
# ---------------------------------------------------------------------------------------------

class Screen:
    """The run's live block on a terminal, redrawn at `TICK` fps while the run is active.

    On a terminal, a `flnd-screen` thread redraws the block (Rich `Live`, never refreshed on its
    own) every `TICK` seconds so spinners and the elapsed clock keep moving even while waiting.
    The lines other threads print on standard error (warnings) go through its queue and print
    above the block. Anywhere else (a pipe, a file) it draws nothing.

    Args:
        read: gives the run's newest snapshot (`run.state`).
        command: the command's name, for the header.
        stream: where the block goes; None for standard error.
        width: the columns; None asks the terminal.
        height: the lines; None asks the terminal.
    """

    def __init__(self, read: Callable[[], State], *, command: str, stream: IO[str] | None = None,
                 width: int | None = None, height: int | None = None) -> None:
        self._read, self.command = read, command
        self._stream = stream or sys.stderr
        self._size = (width, height)
        self.live = theme.tty(self._stream)
        self._lines: queue.SimpleQueue[Any] = queue.SimpleQueue()
        self._done = threading.Event()
        self._thread: threading.Thread | None = None
        self._block: Any = None
        self._console: Any = None
        self._drawn = -1
        self._at = clock.now()
        self._last_size: tuple[int, int] | None = None
        self._last_widths: list[int] = []
        # Held while a draw goes on, and while a question has the terminal (`paused`).
        self._turn = threading.Lock()

    def __enter__(self) -> Screen:
        if not self.live:
            return self
        from rich.console import Console

        width, height = self._size
        self._console = Console(file=self._stream, force_terminal=True, width=width,
                                height=height,
                                color_system="truecolor" if theme.color(self._stream) else None,
                                highlight=False, markup=False, emoji=False)
        self._block = self._fresh()
        global _drawing
        _drawing = self
        out.above(self._lines.put)
        self._thread = threading.Thread(target=self._run, name="flnd-screen", daemon=True)
        self._thread.start()
        return self

    def __exit__(self, *exc: object) -> None:
        if self._block is None:
            return
        self._done.set()
        if self._thread is not None:
            self._thread.join()
        try:
            self.tick()
        finally:
            global _drawing
            _drawing = None
            out.above(None)
            self._block.stop()
            self._block = None

    def _fresh(self) -> Any:
        """A new live block, started: it knows no earlier block's height, so it erases nothing."""
        from rich.live import Live

        block = Live(console=self._console, auto_refresh=False, transient=False,
                     redirect_stdout=False, redirect_stderr=False, vertical_overflow="crop")
        block.start()
        return block

    def _run(self) -> None:
        next_at = time.monotonic() + TICK
        while not self._done.wait(max(0.0, next_at - time.monotonic())):
            self.tick()
            next_at += TICK
            now = time.monotonic()
            if next_at < now:
                next_at = now + TICK

    def tick(self) -> None:
        """Print the lines waiting above the block, and redraw the live block."""
        with self._turn:
            self._tick()

    def _tick(self) -> None:
        block = self._block
        if block is None or not block.is_started:
            return
        console = block.console
        live_render = getattr(block, "_live_render", None)

        def measure() -> tuple[int, int]:
            w, h = console.size
            return max(1, w), max(1, h)

        def clear_if_changed(w: int, h: int) -> None:
            if self._last_size is not None and (w, h) != self._last_size:
                if self._last_widths:
                    cols = w + int(bool(console.legacy_windows))
                    up = max(0, min(_reflowed_rows(self._last_widths, cols), h) - 1)
                    _clear_block(console, up)
                if live_render is not None:
                    live_render._shape = None
                self._last_widths = []
                self._last_size = (w, h)

        width, height = measure()
        clear_if_changed(width, height)

        queued: list[Any] = []
        while True:
            try:
                queued.append(self._lines.get_nowait())
            except queue.Empty:
                break
        if queued:
            if live_render is not None and live_render._shape is not None:
                _clear_block(console, max(0, live_render._shape[1] - 1))
                live_render._shape = None
                self._last_widths = []
            hooks = getattr(console, "_render_hooks", None)
            popped = bool(hooks and hooks[-1] is block)
            if popped:
                console.pop_render_hook()
            try:
                for line in queued:
                    console.print(line)
            finally:
                if popped:
                    console.push_render_hook(block)
            width, height = measure()
            self._last_size = (width, height)

        state = self._read()
        now = clock.now()
        if state.version != self._drawn:
            self._drawn = state.version
            self._at = now
        elapsed = state.elapsed + max(0.0, now - self._at)

        drawn: Text | None = None
        for _ in range(3):
            try:
                drawn = frame(state, command=self.command, width=width,
                              height=max(1, height - 1),
                              fancy=theme.unicode(self._stream), elapsed=elapsed)
            except Exception as error:  # a bug in a frame must not end the run, nor tear its screen
                from flnd.ui.log import ui

                ui.record("error", f"the screen could not draw: {type(error).__name__}: {error}")
                return
            now_w, now_h = measure()
            if (now_w, now_h) == (width, height):
                break
            width, height = now_w, now_h
            clear_if_changed(width, height)
        assert drawn is not None

        orig_w, orig_h = console._width, console._height
        try:
            if orig_w is None:
                console._width = width + int(bool(console.legacy_windows))
            if orig_h is None:
                console._height = height
            block.update(drawn, refresh=True)
        finally:
            console._width, console._height = orig_w, orig_h
        self._last_size = (width, height)
        self._last_widths = [_cells(line) for line in drawn.plain.split("\n")]


# The screen being drawn, for `paused`; None when there is none.
_drawing: Screen | None = None


@contextmanager
def paused() -> Iterator[None]:
    """Take the run's screen off the terminal for the block (a question), then draw it again."""
    shown = _drawing
    if shown is None:
        yield
        return
    with shown._turn:
        block = shown._block
        if block is None:
            yield
            return
        # Cleared, not left drawn: the question prints where the block was, and stays.
        block.transient = True
        block.stop()
        try:
            yield
        finally:
            shown._last_size = None
            shown._last_widths = []
            shown._block = shown._fresh()
            shown._drawn = -1


# ---------------------------------------------------------------------------------------------
# The end summary
# ---------------------------------------------------------------------------------------------

def summary(report: Report, state: State) -> None:
    """Print the run's end: the counts on standard output, then the failures on standard error.

    Args:
        report: the run's report.
        state: its last snapshot, for the time it took and its targets.
    """
    fancy = theme.unicode(sys.stdout)
    sep = f" {theme.glyph('sep', fancy)} "
    saved = f"{report.saved:,} saved" + (f" ({size(report.bytes)})" if report.bytes else "")
    counts = sep.join([f"done in {duration(state.elapsed)}", count(len(state.targets), "target"),
                       saved, f"{report.skipped:,} already saved",
                       f"{len(report.failed):,} failed"])
    out.line(*_marked("error" if report.failed else "ok", fancy), counts)
    if not report.failed:
        return
    for failed in report.failed[:FAILURES_SHOWN]:
        out.plain("", err=True)
        _failure(failed, sep, theme.unicode(sys.stderr))
    out.plain("", err=True)
    out.line((INDENT + "details  ", "dim"), ("flnd status", "bold focus"), err=True)


def _marked(kind: str, fancy: bool) -> tuple[tuple[str, str], tuple[str, str]]:
    """The indent and a kind's glyph (`✓`, `✗`), as the parts that start a summary line."""
    name, token = theme.MARKS[kind]
    return (INDENT, "text"), (theme.glyph(name, fancy) + " ", f"bold {token}")


def _failure(failed: Failed, sep: str, fancy: bool) -> None:
    """One failure: `✗ <target> · <path>`, then its message and its hint."""
    where = str(failed.target)
    if failed.path is not None:
        where += sep + under(failed.path, failed.target)
    out.line(*_marked("error", fancy), where, err=True)
    out.line((INDENT * 2, "text"), (str(failed.error), "text"), err=True)
    hint = str(getattr(failed.error, "hint", "") or "")
    if hint:
        out.line((INDENT * 2 + "hint  ", "dim"), hint, err=True)


def under(path: Path | str, target: Any) -> str:
    """A file's path under its target's folder (`<home>/media/<code>/<value>/`), else its name.

    Example:
        >>> from flnd.plugin import Target
        >>> user = Target("ins:example_user", kind="user")
        >>> under("home/media/ins/example_user/posts/1.jpg", user)
        'posts/1.jpg'
    """
    parts = Path(path).parts
    for at in range(len(parts) - 3):
        if parts[at] == "media" and parts[at + 1] == target.plugin:
            return "/".join(parts[at + 3:])
    return Path(path).name
