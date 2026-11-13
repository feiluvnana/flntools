"""The look of everything `shared` prints: glyphs, the palette, drawers, and color depth.

`shared.theme` is one process-wide `Theme`. `log`, `prompt` and `cli` build a record (a
`LogRecord`, a `PromptView`, a `HelpModel`) and hand it to `theme.draw(kind, record)`, which calls
that kind's drawer and paints the result with `shared.style` for `theme.color`. The text goes to
`theme.screen`, a `Screen` that writes to its `dest` (stderr unless a test gives another) and reads
keys from the terminal. Color depth and Unicode support are detected from stderr once, at import.
Stdlib only: nothing is wrapped, so there is no `.raw`.

Restyling means changing `theme.palette` or `theme.glyphs`. A script that wants something
stranger replaces a drawer, a plain function of (theme, value) that returns the drawn text:

    theme.drawers.log = lambda theme, record: f"[{record.level}] {record.message}"
    with theme.scope(color="none", screen=Screen(dest=io.StringIO())):   # plain text, captured
        log.info("hello")
"""

from __future__ import annotations

import math
import os
import re
import sys
import textwrap
import unicodedata
from contextlib import contextmanager
from dataclasses import dataclass, replace
from typing import Any, Callable, Iterator, Literal, NamedTuple

import shared
from shared import _clock as clock
from shared import _fmt as fmt
from shared._screen import Screen, isatty
from shared._style import (ESCAPE, ESCAPE_PATTERN, LINK_CLOSE, RESET, Chain, color_code, color_rgb,
                           style)

# `shared.theme` is the `theme` object: its members, `Error` among them, are the module's names, and
# the rest are the records the drawers take and the types of `theme.screen`, `.palette` and `.glyphs`.
__all__ = ["Bar", "Count", "Error", "HelpModel", "LogRecord", "Namespace", "PromptView", "Screen",
           "Status", "draw_help", "draw_log", "draw_progress", "draw_prompt", "draw_status",
           "theme"]

# The depths `Theme.color` accepts: "none" (plain text), "8" (the 16 ANSI colors), "256", and "16m"
# (24-bit RGB).
ColorDepth = Literal["none", "8", "256", "16m"]
COLOR_DEPTHS: tuple[ColorDepth, ...] = ("none", "8", "256", "16m")
# Cells in a progress bar, and the column width of a help screen's option labels.
BAR_CELLS = 20
HELP_LABEL_WIDTH = 20
# A running bar's gradient is one soft wave this many cells long, which moves one cell every
# `DRIFT` ticks (tenths of a second): slow enough to read as calm rather than busy.
WAVE = 20
DRIFT = 3
# The spinner steps through the accent's shades over this many ticks.
SPIN_SHADES = 10
# A status line's numbers are right-aligned in slots this wide, so many targets' lines line up.
COUNT_WIDTH = 4
# The kinds of debug line `log.event` writes, each drawn with its own glyph and palette color.
EVENTS = ("skip", "link", "shrink")

# Each glyph is (unicode, ascii). A nested dict is a group of glyphs. The ASCII half is what a
# terminal that can't show the Unicode set gets (see `detect_unicode`).
GLYPHS: dict[str, Any] = {
    "bullet": ("•", "*"), "tick": ("✓", "+"), "bang": ("!", "!"),
    "cross": ("✗", "x"), "dot": ("·", "."), "pointer": (">", ">"),
    "border": ("┃", "|"), "keysep": ("•", "|"),
    "select": {"yes": ("●", "(*)"), "no": ("○", "( )")},
    "check": {"yes": ("✓", "[x]"), "no": ("•", "[ ]")},
    "keys": {"up": ("↑", "up"), "down": ("↓", "down"), "left": ("←", "left"), "right": ("→", "right")},
    "bar": {"full": ("━", "="), "empty": ("─", "-")},
    # The spinner's Braille frames, and the ASCII frames used when Unicode is off.
    "spin": (("⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏"), ("|", "/", "-", "\\")),
    # A status line's counts and `log.event`'s lines. ↓ and ⛓ are ambiguous-width (and ⛓ an
    # emoji in some fonts), so ⇣ and ⤷ stand for downloading and linked.
    "found": ("◦", "#"), "download": ("⇣", "v"), "shrink": ("⟳", "~"), "skip": ("↷", ">"),
    "link": ("⤷", "="), "arrow": ("→", "->"),
}
# Each value is anything `style.color` takes, or style names and colors such as "bold red".
# The prompt colors are huh's ThemeCharm; ok and error match them, so log lines and prompts agree.
# The numbers are 256-color indices. From 16 up they are fixed values rather than the terminal's own
# palette, so the greys (237 to 252) look the same on every terminal.
# Most text is toned down; plain `text` is kept for what a person must read, a name or a message.
# `accent` and `glow` are the two neighbouring shades a running bar's gradient and the spinner move
# between. A status line's numbers take `download`, `shrink`, `ok`, `link`, `skip` and `error`.
PALETTE: dict[str, Any] = {
    "info": 243, "ok": "#02BF87", "warn": "yellow", "error": "#ED567A", "debug": 242,
    "title": "bold #7571F9", "cursor": "#F780E2", "chosen": "#02BF87",
    "text": 252, "muted": 243, "faint": 238, "key": 241, "hint": 239,
    "label": "magenta", "cream": "#FFFDF5", "button": 237,
    "accent": "#7D84D6", "glow": "#A9AEF0", "target": "bold 252", "category": "#9A96C9",
    "size": 245, "path": 245, "skip": 241, "link": "#6FA3D8", "download": "#5FBDC9",
    "shrink": "#B86FB8", "wait": "#D7B65F",
}
# The glyph each log level draws before its message.
LOG_GLYPHS = {"info": "bullet", "ok": "tick", "warn": "bang", "error": "cross", "debug": "dot"}


class Error(shared.Error):
    """The base of every error `shared.theme` raises."""


class LogRecord(NamedTuple):
    """One log line, as `draw_log` or `draw_progress` takes it."""

    # The level name, one of the keys of `LOG_GLYPHS`; "error" is drawn with the failure glyph.
    level: str
    # The text after the glyph; empty draws the glyph alone.
    message: str
    # The bar `draw_progress` draws; None for a plain line. `draw_log` ignores it.
    bar: Bar | None = None
    # One of `EVENTS`, whose glyph and color draw the whole line; empty for a plain line.
    kind: str = ""


# A dataclass, not a NamedTuple: a tuple's own `count` method would clash with the `count` field.
@dataclass(frozen=True)
class Bar:
    """The state of one progress bar, as `draw_progress` draws it."""

    # The text before the bar, such as the item's name.
    label: str
    # How far along the bar is, in the same units as `total`.
    count: float
    # The amount that means done; None makes the bar a spinner with no end.
    total: float | None
    # The text after the count.
    caption: str
    # True once the work is done; the line's glyph turns to ok, and a spinner stops drawing.
    finished: bool
    # How a count is written, such as a byte formatter; the default is `str`.
    show: Callable[[Any], str] = str
    # The spinner's frame number, which the live area fixes for a whole frame; None reads the clock.
    tick: int | None = None
    # True for a wait (a rate limit, a block): drawn steady in the wait color, never moving.
    wait: bool = False


class Count(NamedTuple):
    """One number of a status line: `150 saved`, or `✓150` on a narrow screen."""

    number: int
    word: str
    # A key of the palette, which paints the number when it is above zero.
    role: str
    # A key of `glyphs`, which stands in for the word when the line is too narrow for words.
    glyph: str


@dataclass(frozen=True)
class Status:
    """The state of one status line, as `draw_status` draws it."""

    # Drawn bold and padded to `subject_width`, such as a target.
    subject: str
    # Drawn in the category color and padded to `label_width`; empty draws none.
    label: str = ""
    # The first line's numbers, each in its slot; a zero one is dim.
    counts: tuple[Count, ...] = ()
    # The second line's numbers while the line runs; a zero one is left out.
    running: tuple[Count, ...] = ()
    # Dim text after the counts, such as the bytes saved.
    tail: tuple[str, ...] = ()
    # A wait on the second line while the line runs, steady in the wait color.
    note: str = ""
    subject_width: int = 0
    label_width: int = 0
    # Set by the live area: how the line ended (None while it runs), how long it has run (None
    # draws no time), the spinner's frame (None reads the clock), and its room (0 for any).
    outcome: str | None = None
    elapsed: float | None = None
    tick: int | None = None
    width: int = 0


class HelpModel(NamedTuple):
    """Everything a `--help` screen shows, as `draw_help` takes it."""

    # The program's name. `draw_help` does not print it; the caller puts it into `usage`.
    prog: str
    # A paragraph shown under the usage line; empty shows none.
    description: str
    # The usage line, drawn after "Usage:".
    usage: str
    # (label, text) rows under "Arguments:".
    arguments: tuple = ()
    # (label, text) rows under "Options:".
    options: tuple = ()
    # (label, text) rows under "Commands:".
    commands: tuple = ()
    # Text shown at the bottom; empty shows none.
    epilog: str = ""
    # A message shown before the help, in the error style, for a usage mistake; empty shows none.
    error: str = ""


class PromptView(NamedTuple):
    """What a question shows. `options` are the visible labels; a confirm's are its buttons."""

    # What is asked: "input" or "secret" (typed), "confirm" (buttons), "choices" (checkboxes); any
    # other kind draws a list of options with one highlighted.
    kind: str
    # The question itself, drawn in the title style.
    message: str
    # Lines of help under the question; empty shows none.
    description: str = ""
    # The typed text, or the answer once a choice is made.
    value: str = ""
    # Greyed text shown while the typed text is empty; never the answer.
    placeholder: str = ""
    # The visible labels: a list's options, or a confirm's two buttons.
    options: tuple = ()
    # The highlighted option's index into `options`.
    position: int = 0
    # The indices of the checked options of a "choices" question.
    checked: frozenset = frozenset()
    # A validation message, shown in the error style under the question; None shows none.
    error: str | None = None
    # (action, key names) pairs for the help line
    keys: tuple = ()
    # True once answered; the frame then collapses to one done line.
    done: bool = False
    # Where the typed text's cursor is, as a character index; None is its end.
    caret: int | None = None


class Theme:
    """The process's look: its glyphs, palette, drawers, color depth and screen.

    Changing `color`, `unicode` or `screen` affects every later draw. `scope` changes them for one
    block only.
    """

    _color: ColorDepth

    # `shared.theme` is this object, not a module, so the module's error, the status line's records
    # and its event kinds are reached through it.
    Error = Error
    Status = Status
    Count = Count
    EVENTS = EVENTS

    def __init__(self, *, color: ColorDepth | None = None, unicode: bool | None = None,
                 screen: Screen | None = None):
        self.screen = screen or Screen()
        # Detected from stderr, since that is where `log` and `prompt` write by default.
        self.color = detect_color(sys.stderr) if color is None else color
        self.unicode = detect_unicode(sys.stderr) if unicode is None else unicode
        self.glyphs = Namespace("glyph", GLYPHS, self._pick_glyph)
        self.palette = Namespace("palette color", PALETTE, _same, check=chain_of)
        self.drawers = Namespace("drawer", DRAWERS, _same)

    @property
    def color(self) -> ColorDepth:
        """The color depth every paint uses: "none", "8", "256" or "16m"."""
        return self._color

    @color.setter
    def color(self, value: ColorDepth) -> None:
        """Set the color depth; any other value raises ValueError.

        The check runs here, at the assignment, so a typo fails where it is made and not at the
        first paint.
        """
        if value not in COLOR_DEPTHS:
            raise ValueError(f"theme.color must be one of {', '.join(COLOR_DEPTHS)}, not {value!r}")
        self._color = value

    @contextmanager
    def scope(self, **changes: Any) -> Iterator[Theme]:
        """Apply changes for the length of the block, then restore the old values.

        Args:
            **changes: any of `color`, `unicode` or `screen`, with the value to use inside the block.

        Raises:
            TypeError: a name in `changes` is none of those three.
        """
        unknown = set(changes) - {"color", "unicode", "screen"}
        if unknown:
            raise TypeError(f"unknown theme settings: {', '.join(sorted(unknown))}")
        previous = {name: getattr(self, name) for name in changes}
        try:
            for name, value in changes.items():
                setattr(self, name, value)
            yield self
        finally:
            # Restored in `finally`, so an exception inside the block still puts the old values back.
            for name, value in previous.items():
                setattr(self, name, value)

    def paint(self, name: str, text: str, *, on: str | None = None) -> str:
        """Paint text with a palette color, on another palette color's background when given.

        Args:
            name: a key of `palette`, such as "ok" or "muted".
            text: the text to paint; empty text returns "" with no codes around it.
            on: another key of `palette` whose text color becomes this text's background; None
                paints the text on the terminal's own background.

        Returns:
            The painted text, for `self.color`.
        """
        if not text:
            return ""
        chain = chain_of(getattr(self.palette, name))
        if on is not None:
            chain = replace(chain, bg=chain_of(getattr(self.palette, on)).fg)
        return chain.paint(text, self.color)

    def draw(self, kind: str, value: Any) -> str:
        """Draw a value with this theme's drawer for its kind: log, progress, status, prompt, help.

        Args:
            kind: a key of `drawers`; an unknown kind raises AttributeError.
            value: the record for that kind, such as a `LogRecord` or a `PromptView`.

        Returns:
            The drawn text. A log line and a prompt frame have no trailing newline; a help screen
            ends with one.
        """
        return getattr(self.drawers, kind)(self, value)

    @property
    def motion(self) -> bool:
        """Whether running rows may move: only with 256 or 24-bit color, never flat or plain."""
        return self.color in ("256", "16m")

    def gradient(self, text: str, *, phase: int = 0) -> str:
        """Paint text with a soft wave between `accent` and `glow`, moved `phase` cells along.

            theme.gradient("━" * 12, phase=tick // DRIFT)

        Without `motion` the text is painted flat in `accent`, and plain without color.
        """
        shades = self._shades()
        if not text or shades is None:
            return self.paint("accent", text)
        runs: list[tuple[str, str]] = []
        for cell, char in enumerate(text):
            code = shades(_wave(cell - phase, WAVE))
            if runs and runs[-1][0] == code:
                runs[-1] = (code, runs[-1][1] + char)
            else:
                runs.append((code, char))
        return "".join(f"\x1b[{code}m{chars}" for code, chars in runs) + RESET

    def _shades(self) -> Callable[[float], str] | None:
        """A color code for each point from `accent` (0) to `glow` (1); None without motion."""
        low, high = chain_of(self.palette.accent).fg, chain_of(self.palette.glow).fg
        if not self.motion or low is None or high is None:
            return None
        start, end = color_rgb(low), color_rgb(high)
        depth = self.color

        def code(weight: float) -> str:
            mixed = tuple(round(a + (b - a) * weight) for a, b in zip(start, end))
            return color_code(("rgb", mixed), depth, "fg")

        return code

    def _pick_glyph(self, pair: tuple) -> Any:
        return pair[0] if self.unicode else pair[1]


class Namespace:
    """A fixed set of names, some of them nested groups. Unknown names raise AttributeError."""

    _kind: str
    _defaults: dict
    _groups: dict[str, Namespace]
    _pick: Callable[[Any], Any]
    _check: Callable[[Any], Any]
    _assigned: dict

    def __init__(self, kind: str, defaults: dict, pick: Callable[[Any], Any], *,
                 check: Callable[[Any], Any] = lambda value: value):
        groups = {name: Namespace(kind, value, pick) for name, value in defaults.items() if isinstance(value, dict)}
        # `object.__setattr__` because `__setattr__` below refuses names that are not defaults.
        object.__setattr__(self, "_kind", kind)
        object.__setattr__(self, "_defaults", defaults)
        object.__setattr__(self, "_groups", groups)
        object.__setattr__(self, "_pick", pick)
        object.__setattr__(self, "_check", check)
        object.__setattr__(self, "_assigned", {})

    def __getattr__(self, name: str) -> Any:
        # Looked up in order: a group, then a value the caller assigned, then the default.
        if name in self._groups:
            return self._groups[name]
        if name in self._assigned:
            return self._assigned[name]
        if name in self._defaults:
            return self._pick(self._defaults[name])
        raise AttributeError(f"unknown {self._kind} {name!r}")

    def __setattr__(self, name: str, value: Any) -> None:
        if name not in self._defaults or name in self._groups:
            raise AttributeError(f"unknown {self._kind} {name!r}")
        self._check(value)
        self._assigned[name] = value


def chain_of(value: str | int) -> Chain:
    """The style for a palette value: a color, or style names and colors such as "bold #7571F9".

    Args:
        value: a color `style.color` takes, or words separated by spaces. A word that starts with
            "#" or a digit is a color; any other word is a style name such as "bold".

    Returns:
        A `Chain` with every word applied.

    Raises:
        ValueError: a word is neither a color nor a style name.
    """
    try:
        return style.color(value)
    except ValueError:
        # A color as a whole (a hex, an rgb() or an index) is tried first; anything else is read as words.
        pass
    chain = style
    for word in str(value).split():
        # A word starting with "#" or a digit is a color (a digit is a 256-color index); any other
        # word is a style name, which `getattr` resolves on the chain.
        try:
            chain = chain.color(int(word) if word.isdigit() else word) if word[0] in "#0123456789" \
                else getattr(chain, word)
        except AttributeError:
            raise ValueError(f"not a style: {value!r}") from None
    return chain


def draw_log(theme: Theme, record: LogRecord) -> str:
    """A log line: glyph and subject (up to the first ": ") in the level's color, the rest dimmer.

        ✓ Complete. 159 files                      (ok: green, wholly)
        • fbk:example.page: photos: 131 saved      (info: muted, the subject in normal text)
        ↷ posts/a.jpg → posts/2026-11-11_a.jpg     (an event: wholly in its kind's color)

    A debug line is faint grey throughout; a part of the message with its own style keeps it.

    Args:
        theme: the theme whose glyphs, palette and color depth draw the line.
        record: the level and message to draw; with a bar, the bar is not used here.

    Returns:
        One line of text, with no trailing newline.
    """
    if record.kind:
        return theme.paint(record.kind, f"{getattr(theme.glyphs, record.kind)} {record.message}")
    glyph = getattr(theme.glyphs, LOG_GLYPHS[record.level])
    if not record.message or record.level == "debug":
        return theme.paint(record.level, f"{glyph} {record.message}" if record.message else glyph)
    head, colon, detail = record.message.partition(": ")
    if record.level == "info":
        # Only the subject is in normal text; the glyph and the detail sit back.
        line = theme.paint("info", glyph) + " " + theme.paint("text", head)
        return line + theme.paint("info", colon + detail)
    dimmer = chain_of(getattr(theme.palette, record.level)).dim.paint(colon + detail, theme.color)
    return theme.paint(record.level, f"{glyph} {head}") + (dimmer if colon else "")


def draw_progress(theme: Theme, record: LogRecord) -> str:
    """One progress line drawn from `record.bar`: its glyph and cells in fixed columns, then its label, count and caption.

        ✓ ━━━━━━━━━━━━━━━━━━━━ posts/a.jpg 45.4 KB
        • ━━━━━━━━━━────────── videos/b.mp4 7.3 MB/14.6 MB
        • ⠋                    posts/c.jpg 12.0 KB
        • scroll posts ⠋

    Every line with a bar starts the same way, so the labels of many lines start at one column,
    and the count follows its label with one space, so no line has a wide gap. A row with no total
    that has counted nothing (a target's row, a scroll) has no bar to draw: it stays one short line,
    its spinner after its label. A bar with no total
    (a download whose size the server didn't send) draws its spinner in the cells' column while it
    runs, and full cells once it ends at ok, as a bar with a total does. A line that ended at ok
    shows its total alone (the count, when there was no total), and a failed one keeps
    `count/total`, so how far it got stays on screen.

    Args:
        theme: the theme whose glyphs and palette draw the line.
        record: the level and the bar; an error level draws as a failed line, a finished bar as ok.

    Returns:
        One line of text, with no trailing newline.
    """
    bar = record.bar
    assert bar is not None
    level = _bar_level(record.level, bar)
    glyph = theme.paint(_bar_role(level, bar), getattr(theme.glyphs, LOG_GLYPHS[level]))
    label, caption = _path(theme, bar.label), theme.paint("muted", bar.caption)
    if bar.total is None and bar.count <= 0:
        # Nothing counted and nothing to count toward: a bar's columns would only be empty space.
        spinner = _spinner(theme, bar.tick) if not bar.finished else ""
        return " ".join(part for part in (glyph, label, spinner, caption) if part)
    count = bar.show(bar.total) if level == "ok" and bar.total is not None else _count(bar)
    parts = [glyph, _painted_cells(theme, bar, level), label, theme.paint("size", count), caption]
    return " ".join(part for part in parts if part)


def draw_status(theme: Theme, status: Status) -> str:
    """A status line: state, subject and label, the counts in fixed slots, then tail and time.

        ⠹ fbk:example.page photos  140 found    9 saved  131 skipped    0 failed 3.4 MB 12s
                                   4 downloading  1 shrinking

    The second line is drawn only while the line runs and something on it is above zero or
    waits. When the line is wider than `status.width`, glyphs stand in for the words (`✓150`).

    Args:
        theme: the theme whose glyphs and palette draw the line.
        status: the line's state.

    Returns:
        One line, or two joined by a newline, with no trailing newline.
    """
    head = " ".join(part for part in (
        _state_glyph(theme, status),
        theme.paint("target", status.subject.ljust(status.subject_width)),
        theme.paint("category", status.label.ljust(status.label_width))) if part)
    tail = [*status.tail] + ([_clock_text(status.elapsed)] if status.elapsed is not None else [])
    painted_tail = [theme.paint("size", text) for text in tail]
    words = True
    line = " ".join([head, *(_slot(theme, count, words, COUNT_WIDTH) for count in status.counts),
                     *painted_tail])
    if status.width and display_width(line) > status.width:
        words = False
        line = " ".join([head, *(_slot(theme, count, words, 0) for count in status.counts),
                         *painted_tail])
    second = [_slot(theme, count, words, 0) for count in status.running if count.number > 0]
    if status.note:
        second.append(theme.paint("wait", status.note))
    if status.outcome is not None or not second:
        return line
    return line + "\n" + " " * (display_width(head) + 1) + "  ".join(second)


def _state_glyph(theme: Theme, status: Status) -> str:
    """A status line's first cell: the spinner while it runs, then how it ended."""
    if status.outcome == "ok":
        return theme.paint("ok", theme.glyphs.tick)
    if status.outcome == "error":
        return theme.paint("error", theme.glyphs.cross)
    return _spinner(theme, status.tick)


def _slot(theme: Theme, count: Count, words: bool, width: int) -> str:
    """One count: its number right-aligned in `width`, colored above zero, then word or glyph."""
    role = count.role if count.number > 0 else "muted"
    number = f"{count.number:,}"
    if not words:
        return theme.paint(role, getattr(theme.glyphs, count.glyph) + number)
    return theme.paint(role, number.rjust(width)) + " " + theme.paint("muted", count.word)


def _clock_text(seconds: float) -> str:
    """How long a status line has run, in whole seconds, so the text changes once a second."""
    whole = int(seconds)
    return f"{whole}s" if whole < 60 else fmt.duration(whole)


def draw_help(theme: Theme, model: HelpModel) -> str:
    """A `--help` screen: the error if any, the description, the usage, then the argument, option and command rows.

    Args:
        theme: the theme that draws the screen.
        model: the program's help text, as a `HelpModel`.

    Returns:
        The whole screen, ending with a newline.
    """
    lines = []
    if model.error:
        lines += [f"{theme.paint('error', theme.glyphs.cross)} {model.error}", ""]
    if model.description:
        lines += [model.description, ""]
    lines += [theme.paint("title", "Usage:"), theme.paint("label", "  " + model.usage)]
    for title, rows in (("Arguments:", model.arguments), ("Options:", model.options), ("Commands:", model.commands)):
        if rows:
            lines += ["", theme.paint("title", title)]
            for label, text in rows:
                # Labels are padded to HELP_LABEL_WIDTH, so every description starts in one column.
                padded = ("  " + label).ljust(HELP_LABEL_WIDTH)
                lines.append(theme.paint("label", padded) + "  " + theme.paint("muted", text) if text
                             else theme.paint("label", "  " + label))
    if model.epilog:
        lines += [""] + [theme.paint("muted", line) for line in model.epilog.splitlines()]
    return "\n".join(lines) + "\n"


def draw_prompt(theme: Theme, view: PromptView) -> str:
    """A question's frame: a bar down every line, then the help line; once answered, one done line.

    Args:
        theme: the theme that draws the frame; its screen's size limits the frame's height.
        view: the question's state, as a `PromptView`.

    Returns:
        The frame, with newlines between its lines and no trailing newline.
    """
    if view.done:
        return f"{theme.paint('ok', theme.glyphs.tick)} {_done_text(view)}"
    head = [theme.paint("title", view.message) + (theme.paint("error", " *") if view.error else "")]
    width = max(theme.screen.width - 2, 1)
    for line in view.description.splitlines():
        # Wrapped to the screen width less the border and the space after it.
        head += [theme.paint("muted", part) for part in textwrap.wrap(line, width)] or [""]
    tail = [theme.paint("error", f"* {view.error}")] if view.error else []
    keys = [_keys_line(theme, view.keys)] if view.keys else []
    # A frame never outgrows the screen, or its cursor-up erase would miss the top rows.
    room = theme.screen.height - 1 - len(head) - len(tail) - len(keys)
    if view.kind in ("input", "secret"):
        body = [_input_line(theme, view)]
    elif view.kind == "confirm":
        buttons = [_button(theme, label, index == view.position) for index, label in enumerate(view.options)]
        body = ["", "  ".join(buttons)]
    else:
        body = _option_rows(theme, view, room)
    border = theme.paint("faint", theme.glyphs.border)
    return "\n".join([f"{border} {line}" if line else border for line in head + body + tail] + keys)


def _input_line(theme: Theme, view: PromptView) -> str:
    """The pointer, then the text with the cursor drawn where the next character goes."""
    pointer = theme.paint("cursor", theme.glyphs.pointer)
    if not view.value:
        # The cursor sits before the placeholder, which is never the answer.
        return f"{pointer} {_cursor_cell(theme)}{theme.paint('faint', view.placeholder)}"
    caret = len(view.value) if view.caret is None else min(max(view.caret, 0), len(view.value))
    before, after = view.value[:caret], view.value[caret:]
    if theme.color == "none":
        return f"{pointer} {before}{_cursor_cell(theme)}{after}"
    # In color the cell is reverse video over the character under it, so nothing shifts.
    return f"{pointer} {theme.paint('text', before)}{_cursor_cell(theme, after[:1] or ' ')}{theme.paint('text', after[1:])}"


def _cursor_cell(theme: Theme, char: str = " ") -> str:
    """A reverse-video cell as huh draws it, or `_` without color."""
    return "_" if theme.color == "none" else theme.paint("cream", char, on="cursor")


def _option_rows(theme: Theme, view: PromptView, room: int) -> list[str]:
    """One row per visible option, scrolled to a window around the cursor when they don't fit."""
    count = len(view.options)
    start, end = 0, count
    if count > room:
        # Two rows of the room are kept for the "more" lines above and below the window.
        shown = max(room - 2, 1)
        # The window is centred on the cursor, then clamped so it never runs past either end.
        start = min(max(view.position - shown // 2, 0), count - shown)
        end = start + shown
    # Rows without the cursor are indented by the pointer's width, so the labels line up.
    blank = " " * display_width(theme.glyphs.pointer)
    rows = [f"{blank} {theme.paint('faint', f'{theme.glyphs.keys.up} {start} more')}"] if start else []
    for row in range(start, end):
        label = view.options[row]
        cursor = theme.paint("cursor", theme.glyphs.pointer) if row == view.position else blank
        if view.kind == "choices":
            # Color follows the checked state; the cursor only adds the pointer.
            checked = row in view.checked
            mark = theme.paint("chosen", theme.glyphs.check.yes) if checked \
                else theme.paint("muted", theme.glyphs.check.no)
            rows.append(f"{cursor} {mark} {theme.paint('chosen' if checked else 'text', label)}")
        else:
            rows.append(f"{cursor} {theme.paint('chosen' if row == view.position else 'text', label)}")
    if end < count:
        rows.append(f"{blank} {theme.paint('faint', f'{theme.glyphs.keys.down} {count - end} more')}")
    return rows


def _button(theme: Theme, label: str, active: bool) -> str:
    """One confirm button: bracketed without color, and a cursor-colored block when it is active.

    The bracketed and the plain forms are the same width, so the buttons never shift when moved.
    """
    # Without color, brackets show which button is active; both shapes are the same width.
    if theme.color == "none":
        return f"[ {label} ]" if active else f"  {label}  "
    padded = f"  {label}  "
    return theme.paint("cream", padded, on="cursor") if active else theme.paint("text", padded, on="button")


def _keys_line(theme: Theme, keys: tuple) -> str:
    """The help line: each action with its keys, the keys joined by "/" and the actions by a separator.

    Arrow key names are drawn as their glyphs; any other key name is drawn as its word.
    """
    separator = f" {theme.paint('hint', theme.glyphs.keysep)} "
    return separator.join(
        theme.paint("key", "/".join(getattr(theme.glyphs.keys, key) if key in GLYPHS["keys"] else key for key in bound))
        + " " + theme.paint("hint", action)
        for action, bound in keys
    )


def _done_text(view: PromptView) -> str:
    """The one line a finished question leaves: the answer, shown after the question, except for a secret."""
    if view.kind == "secret":
        # A secret's answer is never printed, so only the question stays on the line.
        return view.message
    if view.kind == "confirm":
        return f"{view.message} {view.value}"
    return f"{view.message}: {view.value}"


def _bar_cells(theme: Theme, bar: Bar) -> str:
    """The bar's cells: a spinner frame when there is no total, else filled and empty cells.

    A spinner advances ten frames a second unless the bar gives a `tick`, which the live area
    sets so that a whole frame shows one frame.
    """
    if bar.total is None:
        if bar.finished:
            return ""
        frames = theme.glyphs.spin
        return frames[(int(clock.now() * 10) if bar.tick is None else bar.tick) % len(frames)]
    filled = _filled(bar.count, bar.total)
    return theme.glyphs.bar.full * filled + theme.glyphs.bar.empty * (BAR_CELLS - filled)


def _filled(count: float, total: float) -> int:
    """How many of the bar's cells are filled; a total of zero or less fills them all."""
    return BAR_CELLS if total <= 0 else round(BAR_CELLS * min(max(count, 0), total) / total)


def _count(bar: Bar) -> str:
    """The count as `count/total`, or the count alone for a spinner; empty before any progress."""
    if bar.total is not None:
        return f"{bar.show(bar.count)}/{bar.show(bar.total)}"
    return bar.show(bar.count) if bar.count > 0 else ""


def _bar_level(level: str, bar: Bar) -> str:
    """The level a row is drawn at: error, ok once finished, else info."""
    if level == "error":
        return "error"
    return "ok" if bar.finished else "info"


def _bar_role(level: str, bar: Bar) -> str:
    """The palette color of a row's glyph and filled cells."""
    if level != "info":
        return level
    return "wait" if bar.wait else "accent"


def _painted_cells(theme: Theme, bar: Bar, level: str) -> str:
    """The bar's cells in color: a running bar's fill drifts; an ended or waiting one is flat."""
    if bar.total is None and not bar.finished:
        # A spinner's frame sits in the cells' column, padded to its width, so the label stays put.
        return _spinner(theme, bar.tick) + " " * (BAR_CELLS - 1)
    if bar.total is None:
        # Once ended, the cells say how it ended, as a bar with a total does.
        filled = BAR_CELLS if level == "ok" else 0
    else:
        filled = _filled(bar.count, bar.total)
    full, empty = theme.glyphs.bar.full * filled, theme.glyphs.bar.empty * (BAR_CELLS - filled)
    role = _bar_role(level, bar)
    if role == "accent":
        tick = int(clock.now() * 10) if bar.tick is None else bar.tick
        full = theme.gradient(full, phase=tick // DRIFT)
    else:
        full = theme.paint(role, full)
    return full + theme.paint("faint", empty)


def _spinner(theme: Theme, tick: int | None) -> str:
    """The spinner's frame for `tick` (None reads the clock), stepping through the accent shades."""
    tick = int(clock.now() * 10) if tick is None else tick
    frames = theme.glyphs.spin
    frame = frames[tick % len(frames)]
    shades = theme._shades()
    if shades is None:
        return theme.paint("accent", frame)
    return f"\x1b[{shades(_wave(tick, SPIN_SHADES))}m{frame}{RESET}"


def _wave(step: int, length: int) -> float:
    """A point on a smooth wave from 0 to 1 and back, `length` steps long."""
    return (1 - math.cos(2 * math.pi * step / length)) / 2


def _path(theme: Theme, label: str) -> str:
    """A row's label: a path's folder dim and its name in normal text; any other label normal."""
    folder, slash, name = label.rpartition("/")
    if not slash:
        return theme.paint("text", label)
    return theme.paint("path", folder + slash) + theme.paint("text", name)


def detect_color(stream) -> ColorDepth:
    """The color depth a stream supports, from NO_COLOR, TERM, COLORTERM, and isatty.

    Args:
        stream: the stream to test; a stream that is not a terminal gets "none".

    Returns:
        "none", "16m" for 24-bit, or "256". It never returns "8": detection chooses only among
        those three, so the 16-color depth has to be set by hand.
    """
    # A non-empty NO_COLOR is the convention that turns colour off; TERM=dumb is a terminal that
    # can't take escapes; a pipe or file is never a terminal.
    if os.environ.get("NO_COLOR") or os.environ.get("TERM") == "dumb" or not isatty(stream):
        return "none"
    # COLORTERM is how terminals advertise 24-bit colour; without it the 256 palette is assumed.
    if os.environ.get("COLORTERM", "").lower() in ("truecolor", "24bit"):
        return "16m"
    return "256"


def detect_unicode(stream) -> bool:
    """Whether to draw Unicode glyphs: the stream is UTF-8, and no width is ambiguous.

    `•`, `●`, `○`, `✓`, `✗` and `┃` are two cells wide in a CJK locale and in some Windows fonts,
    which `display_width` counts as one, so those terminals get the ASCII glyphs.
    """
    encoding = getattr(stream, "encoding", None) or ""
    return encoding.lower().replace("-", "").replace("_", "") == "utf8" and not ambiguous_width()


def ambiguous_width() -> bool:
    """Whether ambiguous-width characters probably show two cells wide: a CJK locale, or Windows' legacy console."""
    locale = os.environ.get("LC_ALL") or os.environ.get("LANG") or ""
    if locale.lower().startswith(("ja", "ko", "zh")):
        return True
    # Windows Terminal, ConEmu, ANSICON and terminals that set TERM (mintty, VS Code) aren't the legacy console.
    # The legacy console is the one assumed to draw these glyphs two cells wide; the variables below
    # are set by the terminals that do not.
    modern = ("WT_SESSION", "TERM", "ANSICON", "TERM_PROGRAM")
    return os.name == "nt" and not any(os.environ.get(name) for name in modern) \
        and os.environ.get("ConEmuANSI") != "ON"


def display_width(text: str) -> int:
    """How many terminal cells `text` takes: escapes take none, wide characters take two.

    Args:
        text: the text to measure, which may contain escape sequences.

    Returns:
        The number of cells, counting ambiguous-width characters as one (see `_char_width`).
    """
    return sum(_char_width(char) for char in ESCAPE.sub("", text))


def fit(text: str, width: int) -> str:
    """Cut text to a display width, keeping escape sequences intact.

    Args:
        text: the text to cut, which may contain escape sequences.
        width: the most cells to keep, as `display_width` counts them.

    Returns:
        The text unchanged when it fits. When it is cut and had any escapes, a reset and a link
        close are appended, so a color or link opened before the cut does not run on.
    """
    kept, used = [], 0
    for unit in _UNIT.findall(text):
        if not unit.startswith("\x1b"):
            used += _char_width(unit)
            # The first character past the width ends the cut; escapes already kept stay in place.
            if used > width:
                return "".join(kept) + (RESET + LINK_CLOSE if "\x1b" in text else "")
        kept.append(unit)
    return text


# One unit is an escape sequence or any single character, so `fit` never cuts an escape in half.
_UNIT = re.compile(ESCAPE_PATTERN + "|.", re.S)


def _char_width(char: str) -> int:
    """The cells one character takes: 0 for a combining mark, 2 for a wide one, else 1.

    Ambiguous-width characters count as 1 here, which is why `ambiguous_width` exists.
    """
    if unicodedata.combining(char):
        return 0
    return 2 if unicodedata.east_asian_width(char) in "WF" else 1


def _same(value: Any) -> Any:
    return value


# The four kinds `Theme.draw` knows. A script can replace one (see the module docstring).
DRAWERS: dict[str, Any] = {"log": draw_log, "progress": draw_progress, "prompt": draw_prompt,
                           "help": draw_help, "status": draw_status}

# The process theme, built once at import, so the color and Unicode detection runs once.
theme = Theme()
