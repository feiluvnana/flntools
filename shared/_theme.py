"""The look of everything `shared` prints: glyphs, the palette, drawers, and color depth.

Restyling means changing `theme.palette` or `theme.glyphs`. A script that wants something
stranger replaces a drawer, a plain function of (theme, value) that returns the drawn text:
`theme.drawers.log = lambda theme, record: f"[{record.level}] {record.message}"`.
"""

from __future__ import annotations

import os
import re
import sys
import textwrap
import time
import unicodedata
from contextlib import contextmanager
from dataclasses import replace
from typing import Any, Callable, Iterator, Literal, NamedTuple

import shared
from shared._screen import Screen, isatty
from shared._style import ESCAPE, ESCAPE_PATTERN, LINK_CLOSE, RESET, Chain, style

# `shared.theme` is the `theme` object: its members, `Error` among them, are the module's names, and
# the rest are the records the drawers take and the types of `theme.screen`, `.palette` and `.glyphs`.
__all__ = ["Bar", "Error", "HelpModel", "LogRecord", "Namespace", "PromptView", "Screen", "draw_help",
           "draw_log", "draw_progress", "draw_prompt", "theme"]

ColorDepth = Literal["none", "8", "256", "16m"]
COLOR_DEPTHS: tuple[ColorDepth, ...] = ("none", "8", "256", "16m")
BAR_CELLS = 20
HELP_LABEL_WIDTH = 20

# Each glyph is (unicode, ascii). A nested dict is a group of glyphs.
GLYPHS: dict[str, Any] = {
    "bullet": ("•", "*"), "tick": ("✓", "+"), "bang": ("!", "!"),
    "cross": ("✗", "x"), "dot": ("·", "."), "pointer": (">", ">"),
    "border": ("┃", "|"), "keysep": ("•", "|"),
    "select": {"yes": ("●", "(*)"), "no": ("○", "( )")},
    "check": {"yes": ("✓", "[x]"), "no": ("•", "[ ]")},
    "keys": {"up": ("↑", "up"), "down": ("↓", "down"), "left": ("←", "left"), "right": ("→", "right")},
    "bar": {"full": ("━", "="), "empty": ("─", "-")},
    "spin": (("⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏"), ("|", "/", "-", "\\")),
}
# Each value is anything `style.color` takes, or style names and colors such as "bold red".
# The prompt colors are huh's ThemeCharm; ok and error match them, so log lines and prompts agree.
PALETTE: dict[str, Any] = {
    "info": "cyan", "ok": "#02BF87", "warn": "yellow", "error": "#ED567A", "debug": "dim",
    "title": "bold #7571F9", "cursor": "#F780E2", "chosen": "#02BF87",
    "text": 252, "muted": 243, "faint": 238, "key": 241, "hint": 239,
    "label": "magenta", "cream": "#FFFDF5", "button": 237,
}
LOG_GLYPHS = {"info": "bullet", "ok": "tick", "warn": "bang", "error": "cross", "debug": "dot"}


class Error(shared.Error):
    """The base of every error `shared.theme` raises."""


class LogRecord(NamedTuple):
    level: str
    message: str
    bar: Bar | None = None


class Bar(NamedTuple):
    label: str
    done: float
    total: float | None
    caption: str
    finished: bool
    show: Callable[[Any], str] = str


class HelpModel(NamedTuple):
    prog: str
    description: str
    usage: str
    arguments: tuple = ()
    options: tuple = ()
    commands: tuple = ()
    epilog: str = ""
    error: str = ""


class PromptView(NamedTuple):
    """What a question shows. `options` are the visible labels; a confirm's are its buttons."""

    kind: str
    message: str
    description: str = ""
    value: str = ""
    placeholder: str = ""
    options: tuple = ()
    position: int = 0
    checked: frozenset = frozenset()
    error: str | None = None
    keys: tuple = ()  # (action, key names) pairs for the help line
    done: bool = False


class Theme:
    _color: ColorDepth

    # `shared.theme` is this object, not a module, so the module's error is reached through it.
    Error = Error

    def __init__(self, *, color: ColorDepth | None = None, unicode: bool | None = None,
                 screen: Screen | None = None):
        self.screen = screen or Screen()
        self.color = detect_color(sys.stderr) if color is None else color
        self.unicode = detect_unicode(sys.stderr) if unicode is None else unicode
        self.glyphs = Namespace("glyph", GLYPHS, self._pick_glyph)
        self.palette = Namespace("palette color", PALETTE, _same, check=chain_of)
        self.drawers = Namespace("drawer", DRAWERS, _same)

    @property
    def color(self) -> ColorDepth:
        return self._color

    @color.setter
    def color(self, value: ColorDepth) -> None:
        if value not in COLOR_DEPTHS:
            raise ValueError(f"theme.color must be one of {', '.join(COLOR_DEPTHS)}, not {value!r}")
        self._color = value

    @contextmanager
    def scope(self, **changes: Any) -> Iterator[Theme]:
        """Apply changes for the length of the block, then restore the old values."""
        unknown = set(changes) - {"color", "unicode", "screen"}
        if unknown:
            raise TypeError(f"unknown theme settings: {', '.join(sorted(unknown))}")
        previous = {name: getattr(self, name) for name in changes}
        try:
            for name, value in changes.items():
                setattr(self, name, value)
            yield self
        finally:
            for name, value in previous.items():
                setattr(self, name, value)

    def paint(self, name: str, text: str, *, on: str | None = None) -> str:
        """Paint text with a palette color, on another palette color's background when given."""
        if not text:
            return ""
        chain = chain_of(getattr(self.palette, name))
        if on is not None:
            chain = replace(chain, bg=chain_of(getattr(self.palette, on)).fg)
        return chain.paint(text, self.color)

    def draw(self, kind: str, value: Any) -> str:
        """Draw a value with this theme's drawer for its kind: log, progress, prompt, or help."""
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
        object.__setattr__(self, "_kind", kind)
        object.__setattr__(self, "_defaults", defaults)
        object.__setattr__(self, "_groups", groups)
        object.__setattr__(self, "_pick", pick)
        object.__setattr__(self, "_check", check)
        object.__setattr__(self, "_assigned", {})

    def __getattr__(self, name: str) -> Any:
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
    """The style for a palette value: a color, or style names and colors such as "bold #7571F9"."""
    try:
        return style.color(value)
    except ValueError:
        pass
    chain = style
    for word in str(value).split():
        try:
            chain = chain.color(int(word) if word.isdigit() else word) if word[0] in "#0123456789" \
                else getattr(chain, word)
        except AttributeError:
            raise ValueError(f"not a style: {value!r}") from None
    return chain


def draw_log(theme: Theme, record: LogRecord) -> str:
    glyph = theme.paint(record.level, getattr(theme.glyphs, LOG_GLYPHS[record.level]))
    return f"{glyph} {record.message}" if record.message else glyph


def draw_progress(theme: Theme, record: LogRecord) -> str:
    bar = record.bar
    assert bar is not None
    level = "error" if record.level == "error" else "ok" if bar.finished else "info"
    parts = [theme.paint(level, getattr(theme.glyphs, LOG_GLYPHS[level])), bar.label, _bar_cells(theme, bar),
             _count(bar), bar.caption]
    return " ".join(part for part in parts if part)


def draw_help(theme: Theme, model: HelpModel) -> str:
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
                padded = ("  " + label).ljust(HELP_LABEL_WIDTH)
                lines.append(theme.paint("label", padded) + "  " + theme.paint("muted", text) if text
                             else theme.paint("label", "  " + label))
    if model.epilog:
        lines += [""] + [theme.paint("muted", line) for line in model.epilog.splitlines()]
    return "\n".join(lines) + "\n"


def draw_prompt(theme: Theme, view: PromptView) -> str:
    """A question's frame: a bar down every line, then the help line; once answered, one done line."""
    if view.done:
        return f"{theme.paint('ok', theme.glyphs.tick)} {_done_text(view)}"
    head = [theme.paint("title", view.message) + (theme.paint("error", " *") if view.error else "")]
    width = max(theme.screen.width - 2, 1)
    for line in view.description.splitlines():
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
    pointer = theme.paint("cursor", theme.glyphs.pointer)
    if view.value:
        return f"{pointer} {theme.paint('text', view.value)}"
    return f"{pointer} {theme.paint('faint', view.placeholder)}" if view.placeholder else pointer


def _option_rows(theme: Theme, view: PromptView, room: int) -> list[str]:
    """One row per visible option, scrolled to a window around the cursor when they don't fit."""
    count = len(view.options)
    start, end = 0, count
    if count > room:
        shown = max(room - 2, 1)
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
    # Without color, brackets show which button is active; both shapes are the same width.
    if theme.color == "none":
        return f"[ {label} ]" if active else f"  {label}  "
    padded = f"  {label}  "
    return theme.paint("cream", padded, on="cursor") if active else theme.paint("text", padded, on="button")


def _keys_line(theme: Theme, keys: tuple) -> str:
    separator = f" {theme.paint('hint', theme.glyphs.keysep)} "
    return separator.join(
        theme.paint("key", "/".join(getattr(theme.glyphs.keys, key) if key in GLYPHS["keys"] else key for key in bound))
        + " " + theme.paint("hint", action)
        for action, bound in keys
    )


def _done_text(view: PromptView) -> str:
    if view.kind == "secret":
        return view.message
    if view.kind == "confirm":
        return f"{view.message} {view.value}"
    return f"{view.message}: {view.value}"


def _bar_cells(theme: Theme, bar: Bar) -> str:
    if bar.total is None:
        if bar.finished:
            return ""
        frames = theme.glyphs.spin
        return frames[int(time.monotonic() * 10) % len(frames)]
    done = max(bar.done, 0)
    filled = BAR_CELLS if bar.total <= 0 else round(BAR_CELLS * min(done, bar.total) / bar.total)
    return theme.glyphs.bar.full * filled + theme.glyphs.bar.empty * (BAR_CELLS - filled)


def _count(bar: Bar) -> str:
    if bar.total is not None:
        return f"{bar.show(bar.done)}/{bar.show(bar.total)}"
    return bar.show(bar.done) if bar.done > 0 else ""


def detect_color(stream) -> ColorDepth:
    """The color depth a stream supports, from NO_COLOR, TERM, COLORTERM, and isatty."""
    if os.environ.get("NO_COLOR") or os.environ.get("TERM") == "dumb" or not isatty(stream):
        return "none"
    if os.environ.get("COLORTERM", "").lower() in ("truecolor", "24bit"):
        return "16m"
    return "256"


def detect_unicode(stream) -> bool:
    encoding = getattr(stream, "encoding", None) or ""
    return encoding.lower().replace("-", "").replace("_", "") == "utf8"


def display_width(text: str) -> int:
    return sum(_char_width(char) for char in ESCAPE.sub("", text))


def fit(text: str, width: int) -> str:
    """Cut text to a display width, keeping escape sequences intact."""
    kept, used = [], 0
    for unit in _UNIT.findall(text):
        if not unit.startswith("\x1b"):
            used += _char_width(unit)
            if used > width:
                return "".join(kept) + (RESET + LINK_CLOSE if "\x1b" in text else "")
        kept.append(unit)
    return text


_UNIT = re.compile(ESCAPE_PATTERN + "|.", re.S)


def _char_width(char: str) -> int:
    if unicodedata.combining(char):
        return 0
    return 2 if unicodedata.east_asian_width(char) in "WF" else 1


def _same(value: Any) -> Any:
    return value


DRAWERS: dict[str, Any] = {"log": draw_log, "progress": draw_progress, "prompt": draw_prompt, "help": draw_help}

theme = Theme()
