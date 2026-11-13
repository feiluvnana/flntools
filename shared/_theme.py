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
from shared._screen import Screen, isatty
from shared._style import ESCAPE, ESCAPE_PATTERN, LINK_CLOSE, RESET, Chain, style

# `shared.theme` is the `theme` object: its members, `Error` among them, are the module's names, and
# the rest are the records the drawers take and the types of `theme.screen`, `.palette` and `.glyphs`.
__all__ = ["Bar", "Error", "HelpModel", "LogRecord", "Namespace", "PromptView", "Screen", "draw_help",
           "draw_log", "draw_progress", "draw_prompt", "theme"]

# The depths `Theme.color` accepts: "none" (plain text), "8" (the 16 ANSI colors), "256", and "16m"
# (24-bit RGB).
ColorDepth = Literal["none", "8", "256", "16m"]
COLOR_DEPTHS: tuple[ColorDepth, ...] = ("none", "8", "256", "16m")
# Cells in a progress bar, and the column width of a help screen's option labels.
BAR_CELLS = 20
HELP_LABEL_WIDTH = 20

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
}
# Each value is anything `style.color` takes, or style names and colors such as "bold red".
# The prompt colors are huh's ThemeCharm; ok and error match them, so log lines and prompts agree.
# The numbers are 256-color indices. From 16 up they are fixed values rather than the terminal's own
# palette, so the greys (237 to 252) look the same on every terminal.
PALETTE: dict[str, Any] = {
    "info": "cyan", "ok": "#02BF87", "warn": "yellow", "error": "#ED567A", "debug": "dim",
    "title": "bold #7571F9", "cursor": "#F780E2", "chosen": "#02BF87",
    "text": 252, "muted": 243, "faint": 238, "key": 241, "hint": 239,
    "label": "magenta", "cream": "#FFFDF5", "button": 237,
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

    # `shared.theme` is this object, not a module, so the module's error is reached through it.
    Error = Error

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
        """Draw a value with this theme's drawer for its kind: log, progress, prompt, or help.

        Args:
            kind: a key of `drawers`; an unknown kind raises AttributeError.
            value: the record for that kind, such as a `LogRecord` or a `PromptView`.

        Returns:
            The drawn text. A log line and a prompt frame have no trailing newline; a help screen
            ends with one.
        """
        return getattr(self.drawers, kind)(self, value)

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
    """A log line, wholly in its level's color; a part of the message with its own style keeps it.

    Args:
        theme: the theme whose glyphs, palette and color depth draw the line.
        record: the level and message to draw; with a bar, the bar is not used here.

    Returns:
        One line of text, with no trailing newline.
    """
    glyph = getattr(theme.glyphs, LOG_GLYPHS[record.level])
    line = f"{glyph} {record.message}" if record.message else glyph
    # Paint a marker to learn the level's opening and closing sequences, whatever the color depth.
    opening, _, closing = theme.paint(record.level, "\0").partition("\0")
    if not opening:
        return line
    # A reset inside the message ends the level's color too, so it is started again after each one.
    body = line.replace(RESET, RESET + opening)
    if body.endswith(opening):
        # The message ended in a reset, which already closes the line.
        return opening + body[:-len(opening)]
    return opening + body + closing


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
    level = "error" if record.level == "error" else "ok" if bar.finished else "info"
    glyph = theme.paint(level, getattr(theme.glyphs, LOG_GLYPHS[level]))
    if bar.total is None and bar.count <= 0:
        # Nothing counted and nothing to count toward: a bar's columns would only be empty space.
        return " ".join(part for part in (glyph, bar.label, _bar_cells(theme, bar), bar.caption) if part)
    if bar.total is not None:
        cells = _bar_cells(theme, bar)
        count = bar.show(bar.total) if level == "ok" else _count(bar)
    else:
        # A spinner's frame sits in the cells' column, padded to its width, so the count and label stay put;
        # once ended, the cells say how it ended, as a bar with a total does.
        cells = (theme.glyphs.bar.full * BAR_CELLS if level == "ok" else theme.glyphs.bar.empty * BAR_CELLS) \
            if bar.finished else _bar_cells(theme, bar).ljust(BAR_CELLS)
        count = _count(bar)
    parts = [glyph, cells, bar.label, count, bar.caption]
    return " ".join(part for part in parts if part)


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
    done = max(bar.count, 0)
    # A total of zero or less would divide by zero, so it draws as full.
    filled = BAR_CELLS if bar.total <= 0 else round(BAR_CELLS * min(done, bar.total) / bar.total)
    return theme.glyphs.bar.full * filled + theme.glyphs.bar.empty * (BAR_CELLS - filled)


def _count(bar: Bar) -> str:
    """The count as `count/total`, or the count alone for a spinner; empty before any progress."""
    if bar.total is not None:
        return f"{bar.show(bar.count)}/{bar.show(bar.total)}"
    return bar.show(bar.count) if bar.count > 0 else ""


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
DRAWERS: dict[str, Any] = {"log": draw_log, "progress": draw_progress, "prompt": draw_prompt, "help": draw_help}

# The process theme, built once at import, so the color and Unicode detection runs once.
theme = Theme()
