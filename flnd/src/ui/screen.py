"""The run's screen: a live block drawn from the run's `State` snapshot, and the end summary.

src/docs/cli explains it.
"""

from __future__ import annotations

import queue
import sys
import threading
from collections.abc import Callable
from pathlib import Path
from typing import IO, Any

from rich.cells import cell_len
from rich.text import Text

from flnd.engine.events import Failed, Phase, Report, State, TargetState
from flnd.ui import out, theme
from flnkit import clock
from flnkit.text import count, duration, size

__all__ = ["Screen", "frame", "summary"]

# Seconds between two looks at the snapshot.
TICK = 0.1
# The most cells a target, its categories and a file's path take, and a file's bar.
TARGET_CELLS, CATEGORY_CELLS, PATH_CELLS, BAR_CELLS = 28, 14, 24, 30
# The lines the block has beside its target and file rows: the header, the column titles, the
# `now` line, the foot, and the blank lines between them.
FRAME_LINES = 8
# The failures the end summary names; `flnd status` has them all.
FAILURES_SHOWN = 10
# The count columns: title and width, right-aligned.
# The speed column is 2 cells wider than its longest value (`999.9 KB/s`), so it never
# touches the size.
COLUMNS = (("saved", 7), ("skipped", 9), ("failed", 8), ("size", 10), ("speed", 12))
KEYS = "ctrl+c stop · twice: stop now"
INDENT = "  "


# ---------------------------------------------------------------------------------------------
# Cells: every width here is counted in terminal cells, not characters (a CJK character is two)
# ---------------------------------------------------------------------------------------------

def _head(text: str, cells: int) -> str:
    """The longest start of `text` that fits in `cells`."""
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
    return _head(text[::-1], cells)[::-1]


def middle(text: str, cells: int, *, fancy: bool = True) -> str:
    """`text` cut in the middle to fit in `cells`, with the `more` glyph in the cut.

    Example:
        >>> middle("ins:example_user", 10)
        'ins:…_user'
    """
    if cell_len(text) <= cells:
        return text
    cut = theme.glyph("more", fancy)
    room = max(0, cells - cell_len(cut))
    left = _head(text, room // 2)
    return left + cut + _tail(text, room - cell_len(left))


def left(text: str, cells: int, *, fancy: bool = True) -> str:
    """`text` cut at its start to fit in `cells`, the `more` glyph first: a path's end shows.

    Example:
        >>> left("media/2026/1290_2.mp4", 12)
        '…/1290_2.mp4'
    """
    if cell_len(text) <= cells:
        return text
    cut = theme.glyph("more", fancy)
    return cut + _tail(text, max(0, cells - cell_len(cut)))


def _pad(text: str, cells: int) -> str:
    return text + " " * max(0, cells - cell_len(text))


def _right(text: str, cells: int) -> str:
    return " " * max(0, cells - cell_len(text)) + text


# ---------------------------------------------------------------------------------------------
# The frame: one draw of the live block
# ---------------------------------------------------------------------------------------------

def frame(state: State, *, command: str, width: int, height: int, fancy: bool = True) -> Text:
    """The live block for `state`, laid out in `width` by `height` cells.

    Args:
        state: the run's snapshot.
        command: the command's name, for the header (`download`).
        width: the terminal's columns; a longer line is cut.
        height: its lines; targets past the room fold into one `+ <n> targets` line.
        fancy: draw Unicode glyphs; False for ASCII.

    Returns:
        The block, one line of text per row.
    """
    at = clock.now()
    sep = f" {theme.glyph('sep', fancy)} "
    lines: list[Text] = []
    title = (f"flnd {command}{sep}{count(len(state.targets), 'target')}{sep}"
             f"{int(state.elapsed) // 60:02d}:{int(state.elapsed) % 60:02d}")
    lines += [_line((title, "focus")), Text()]
    # Each column as wide as its widest cell, up to its most.
    widths = (
        min(TARGET_CELLS, max([4, *(cell_len(t.text) for t in state.targets)])),
        min(CATEGORY_CELLS, max([10, *(cell_len(" ".join(t.categories)) for t in state.targets)])),
    )
    lines.append(_line((_pad("target", 2 + widths[0]) + " " + _pad("categories", widths[1])
                        + "".join(_right(name, cells) for name, cells in COLUMNS), "dim")))
    files = _files(state, at, fancy)
    room = height - FRAME_LINES - len(files)
    shown = state.targets
    if len(shown) > room:
        shown = shown[:max(0, room - 1)]
    lines += [_target(row, widths, at, fancy) for row in shown]
    if len(shown) < len(state.targets):
        lines.append(_line((f"+ {len(state.targets) - len(shown)} targets", "dim")))
    lines.append(Text())
    if files:
        lines += files + [Text()]
    lines += [_now(state, fancy), Text(), _foot(state, width, fancy)]
    for line in lines:
        line.truncate(width)
    block = Text("\n").join(lines)
    block.no_wrap = True
    return block


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


def _target(row: TargetState, widths: tuple[int, int], at: float, fancy: bool) -> Text:
    glyph, token = _mark(row.phase, at, fancy)
    none = theme.glyph("none", fancy)
    counted = row.found or row.saved or row.skipped or row.failed
    cells = [f"{n:,}" if counted else none for n in (row.saved, row.skipped, row.failed)]
    cells.append(size(row.bytes) if row.bytes else none)
    going = row.phase in (Phase.LISTING, Phase.DOWNLOADING)
    cells.append(f"{size(row.speed)}/s" if going and row.speed else none)
    return _line(
        (glyph + " ", token),
        (_pad(middle(row.text, widths[0], fancy=fancy), widths[0]) + " ", "text"),
        (_pad(middle(" ".join(row.categories), widths[1], fancy=fancy), widths[1]), "dim"),
        ("".join(_right(cell, cells) for cell, (_, cells) in zip(cells, COLUMNS)), "text"),
    )


def _files(state: State, at: float, fancy: bool) -> list[Text]:
    """The rows of the files in flight, then `+ <n> more`."""
    rows = []
    for file in state.files:
        line = _line((theme.spinner(at, fancy) + " ", "focus"),
                     (_pad(left(file.path, PATH_CELLS, fancy=fancy), PATH_CELLS) + "  ", "text"))
        if file.total:
            share = max(0.0, min(1.0, file.done / file.total))
            filled = int(share * BAR_CELLS)
            line.append(theme.glyph("full", fancy) * filled, style=theme.style("focus"))
            if filled < BAR_CELLS:
                rest = theme.glyph("head", fancy) + theme.glyph("rest", fancy) * (
                    BAR_CELLS - filled - 1)
                line.append(rest, style=theme.style("dim"))
            line.append(_right(f"{int(share * 100)}%", 5), style=theme.style("text"))
            shown = size(file.total)
        else:
            line.append(" " * (BAR_CELLS + 5))
            shown = size(file.done)
        speed = f"{size(file.speed)}/s" if file.speed else theme.glyph("none", fancy)
        line.append(_right(shown, 9) + _right(speed, 12), style=theme.style("text"))
        rows.append(line)
    if state.more:
        rows.append(_line((f"  + {state.more} more", "dim")))
    return rows


def _now(state: State, fancy: bool) -> Text:
    """`now  <State.now>`, in `warn` while a wait goes on."""
    text = state.now
    if state.holds and state.holds[-1].count > 1:
        text += f" ({theme.glyph('times', fancy)}{state.holds[-1].count})"
    return _line(("now  ", "dim"), (text, "warn" if state.holds else "text"))


def _foot(state: State, width: int, fancy: bool) -> Text:
    """The run's speed and counts, with the keys on the right."""
    sep = f" {theme.glyph('sep', fancy)} "
    # Idle (nothing in flight), the speed of the last 3 s is no speed.
    flying = state.files or state.more
    speed = f"{size(state.speed)}/s" if flying and state.speed else theme.glyph("none", fancy)
    counts = f"{speed}{sep}{state.saved:,} saved{sep}{state.failed:,} failed"
    keys = KEYS.replace(" · ", sep)
    gap = max(2, width - cell_len(INDENT + counts + keys) - 2)
    return _line((counts, "text"), (" " * gap + keys, "dim"))


# ---------------------------------------------------------------------------------------------
# The screen: the `flnd-screen` thread
# ---------------------------------------------------------------------------------------------

class Screen:
    """The run's live block on a terminal, redrawn only when the run's snapshot changed.

    On a terminal, a `flnd-screen` thread looks at `read()` every `TICK` seconds and redraws
    the block (Rich `Live`, never refreshed on its own) when the snapshot's `version` moved.
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
        self._drawn = -1

    def __enter__(self) -> Screen:
        if not self.live:
            return self
        from rich.console import Console
        from rich.live import Live

        width, height = self._size
        console = Console(file=self._stream, force_terminal=True, width=width, height=height,
                          color_system="truecolor" if theme.color(self._stream) else None,
                          highlight=False, markup=False, emoji=False)
        self._block = Live(console=console, auto_refresh=False, transient=False,
                           redirect_stdout=False, redirect_stderr=False)
        self._block.start()
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
            out.above(None)
            self._block.stop()
            self._block = None

    def _run(self) -> None:
        while not self._done.wait(TICK):
            self.tick()

    def tick(self) -> None:
        """Print the lines waiting above the block, and redraw it if the snapshot moved."""
        block = self._block
        if block is None:
            return
        while True:
            try:
                block.console.print(self._lines.get_nowait())
            except queue.Empty:
                break
        state = self._read()
        if state.version == self._drawn:
            return
        self._drawn = state.version
        console = block.console
        try:
            drawn = frame(state, command=self.command, width=console.width,
                          height=console.height, fancy=theme.unicode(self._stream))
        except Exception as error:  # a bug in a frame must not end the run, nor tear its screen
            from flnd.ui.log import ui

            ui.record("error", f"the screen could not draw: {type(error).__name__}: {error}")
            return
        block.update(drawn, refresh=True)


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
        >>> from flnd.source import Target
        >>> user = Target("ins:example_user", kind="user")
        >>> under("home/media/ins/example_user/posts/1.jpg", user)
        'posts/1.jpg'
    """
    parts = Path(path).parts
    for at in range(len(parts) - 3):
        if parts[at] == "media" and parts[at + 1] == target.source:
            return "/".join(parts[at + 3:])
    return Path(path).name
