"""The look of everything `shared` prints: glyphs, templates, and color depth."""

from __future__ import annotations

import os
import re
import sys
import time
import unicodedata
from contextlib import contextmanager
from dataclasses import replace
from typing import Any, Callable, Iterator, Literal, NamedTuple

from shared._screen import Screen, isatty
from shared._style import ESCAPE, ESCAPE_PATTERN, LINK_CLOSE, RESET, pieces_of, style

ColorDepth = Literal["none", "8", "256", "16m"]
COLOR_DEPTHS: tuple[ColorDepth, ...] = ("none", "8", "256", "16m")
BAR_CELLS = 20
HELP_LABEL_WIDTH = 20

# Each glyph is (unicode, ascii). A nested dict is a group of glyphs.
GLYPHS: dict[str, Any] = {
    "bullet": ("•", "*"), "tick": ("✓", "+"), "bang": ("!", "!"),
    "cross": ("✗", "x"), "dot": ("·", "."), "pointer": ("›", ">"),
    "select": {"yes": ("●", "(*)"), "no": ("○", "( )")},
    "check": {"yes": ("✔", "[x]"), "no": ("·", "[ ]")},
    "bar": {"full": ("━", "="), "empty": ("─", "-")},
    "spin": (("⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏"), ("|", "/", "-", "\\")),
}


class LogRecord(NamedTuple):
    level: str
    message: str
    fields: str = ""
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
    kind: str
    message: str
    value: str = ""
    options: tuple = ()
    position: int = 0
    checked: frozenset = frozenset()
    error: str | None = None
    keys: tuple = ()
    done: bool = False
    placeholder: str = ""


class Theme:
    _color: ColorDepth

    def __init__(self, *, color: ColorDepth | None = None, unicode: bool | None = None,
                 screen: Screen | None = None):
        self.screen = screen or Screen()
        self.color = detect_color(sys.stderr) if color is None else color
        self.unicode = detect_unicode(sys.stderr) if unicode is None else unicode
        self.glyphs = Namespace("glyph", GLYPHS, self._pick_glyph)
        self.templates = Namespace("template", default_templates(), lambda template: template)

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
        previous = {name: getattr(self, name) for name in _check_changes(changes)}
        try:
            for name, value in changes.items():
                setattr(self, name, value)
            yield self
        finally:
            for name, value in previous.items():
                setattr(self, name, value)

    def branch(self, **changes: Any) -> Theme:
        """A separate copy with the changes applied. The screen is shared unless changed."""
        _check_changes(changes)
        copy = Theme.__new__(Theme)
        copy.screen, copy._color, copy.unicode = self.screen, self._color, self.unicode
        copy.glyphs = self.glyphs._copy(copy._pick_glyph)
        copy.templates = self.templates._copy(None)
        for name, value in changes.items():
            setattr(copy, name, value)
        return copy

    def _format_log(self, record: LogRecord) -> str:
        if record.bar is None:
            template = getattr(self.templates.log, record.level)
            return self._fill(template, message=record.message, fields=record.fields)
        bar = record.bar
        state = "error" if record.level == "error" else "ok" if bar.finished else "run"
        return self._fill(
            getattr(self.templates.progress, state),
            label=bar.label, bar=self._bar_cells(bar), count=_count(bar), caption=bar.caption,
        )

    def _format_help(self, model: HelpModel) -> str:
        help_templates = self.templates.help
        lines = []
        if model.error:
            lines += [self._fill(help_templates.error, message=model.error), ""]
        if model.description:
            lines += [model.description, ""]
        lines += [self._fill(help_templates.section, title="Usage:"), self._fill(help_templates.usage, usage=model.usage)]
        for title, rows in (("Arguments:", model.arguments), ("Options:", model.options), ("Commands:", model.commands)):
            if rows:
                lines += ["", self._fill(help_templates.section, title=title)]
                for label, text in rows:
                    padded = ("  " + label).ljust(HELP_LABEL_WIDTH)
                    lines.append(self._fill(help_templates.row, label=padded, help=text))
        if model.epilog:
            lines.append("")
            lines += [self._epilog_line(line) for line in model.epilog.splitlines()]
        return "\n".join(lines) + "\n"

    def _format_prompt(self, view: PromptView) -> str:
        prompt_templates = self.templates.prompt
        if view.done:
            return self._fill(prompt_templates.done, message=_done_text(view))
        lines = [self._fill(prompt_templates.question, message=view.message)]
        pointer = self.glyphs.pointer
        if view.kind in ("input", "secret"):
            if not view.value and view.placeholder:
                lines.append(self._fill(prompt_templates.placeholder, cursor=pointer, value=view.placeholder))
            else:
                lines.append(self._fill(prompt_templates.input, cursor=pointer, value=view.value))
        else:
            marks = self.glyphs.check if view.kind == "choices" else self.glyphs.select
            # Inactive rows get blanks as wide as the pointer, so the labels line up.
            blank = " " * display_width(pointer)
            for index, label in enumerate(view.options):
                marked = index in view.checked if view.kind == "choices" else index == view.position
                lines.append(self._fill(
                    prompt_templates.choice,
                    cursor=pointer if index == view.position else blank,
                    mark=marks.yes if marked else marks.no,
                    label=label,
                ))
        if view.error:
            lines.append(self._fill(prompt_templates.error, message=view.error))
        if view.keys:
            lines.append("  ".join(
                self._fill(prompt_templates.keys, binding="/".join(keys), action=action) for action, keys in view.keys
            ))
        return "\n".join(lines)

    def _fill(self, template: str, **parts: Any) -> str:
        """Fill the {g-*} and {p-*} slots, paint each styled piece, and tidy the spaces."""
        painted = []
        for raw, chain in pieces_of(template):
            text = self._fill_slots(raw, parts)
            if chain is not None:
                if chain.link:
                    chain = replace(chain, link=self._fill_slots(chain.link, parts).replace(EMPTY_SLOT, ""))
                text = chain.paint(text, self.color)
            painted.append(text)
        return tidy_spaces("".join(painted))

    def _fill_slots(self, raw: str, parts: dict) -> str:
        def slot_value(match: re.Match) -> str:
            kind, name = match.groups()
            value = self._glyph(name) if kind == "g" else parts.get(name)
            return EMPTY_SLOT if value is None or value == "" else str(value)

        return SLOT.sub(slot_value, raw)

    def _glyph(self, dotted_name: str) -> Any:
        value: Any = self.glyphs
        try:
            for name in dotted_name.split("."):
                value = getattr(value, name)
        except AttributeError:
            return None
        return value

    def _pick_glyph(self, pair: tuple) -> Any:
        return pair[0] if self.unicode else pair[1]

    def _bar_cells(self, bar: Bar) -> str:
        if bar.total is None:
            if bar.finished:
                return ""
            frames = self.glyphs.spin
            return frames[int(time.monotonic() * 10) % len(frames)]
        done = max(bar.done, 0)
        filled = BAR_CELLS if bar.total <= 0 else round(BAR_CELLS * min(done, bar.total) / bar.total)
        return self.glyphs.bar.full * filled + self.glyphs.bar.empty * (BAR_CELLS - filled)

    def _epilog_line(self, line: str) -> str:
        """Draw path words with help.path and runs of other words with help.epilog."""
        drawn: list = []
        for gap, word in re.findall(r"(\s*)(\S+)", line):
            if _is_path(word):
                drawn += [gap, self._fill(self.templates.help.path, path=word)]
            elif drawn and isinstance(drawn[-1], list):
                drawn[-1].append(gap + word)
            else:
                drawn += [gap, [word]]
        return "".join(
            self._fill(self.templates.help.epilog, text="".join(piece)) if isinstance(piece, list) else piece
            for piece in drawn
        )


class Namespace:
    """A fixed set of names, some of them nested groups. Unknown names raise AttributeError."""

    _kind: str
    _defaults: dict
    _groups: dict[str, Namespace]
    _pick: Callable[[Any], Any]
    _assigned: dict

    def __init__(self, kind: str, defaults: dict, pick: Callable[[Any], Any]):
        groups = {name: Namespace(kind, value, pick) for name, value in defaults.items() if isinstance(value, dict)}
        object.__setattr__(self, "_kind", kind)
        object.__setattr__(self, "_defaults", defaults)
        object.__setattr__(self, "_groups", groups)
        object.__setattr__(self, "_pick", pick)
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
        self._assigned[name] = value

    def _copy(self, pick: Callable[[Any], Any] | None) -> Namespace:
        pick = pick or self._pick
        copy = Namespace(self._kind, self._defaults, pick)
        copy._assigned.update(self._assigned)
        for name, group in self._groups.items():
            copy._groups[name] = group._copy(pick)
        return copy


def default_templates() -> dict:
    log_tail = " {p-message}{p-fields}"
    bar_tail = " {p-label} {p-bar} {p-count} {p-caption}"
    return {
        "log": {
            "info": style.cyan("{g-bullet}") + log_tail,
            "ok": style.green("{g-tick}") + log_tail,
            "warn": style.yellow("{g-bang}") + log_tail,
            "error": style.red("{g-cross}") + log_tail,
            "debug": style.dim("{g-dot}") + log_tail,
        },
        "progress": {
            "run": style.cyan("{g-bullet}") + bar_tail,
            "ok": style.green("{g-tick}") + bar_tail,
            "error": style.red("{g-cross}") + bar_tail,
        },
        "prompt": {
            "question": style.white("{p-message}"),
            "input": style.magenta("{p-cursor}") + " {p-value}",
            "placeholder": style.magenta("{p-cursor}") + " " + style.bright_black("{p-value}"),
            "choice": style.magenta("{p-cursor}") + " {p-mark} {p-label}",
            "done": style.green("{g-tick}") + " {p-message}",
            "error": style.red("{g-cross}") + " {p-message}",
            "keys": style.dim("{p-binding} {p-action}"),
        },
        "help": {
            "section": style.bold("{p-title}"),
            "usage": style.magenta("  {p-usage}"),
            "row": style.magenta("{p-label}") + "  " + style.dim("{p-help}"),
            "epilog": style.dim("{p-text}"),
            "path": style.cyan.underline.url("{p-path}")("{p-path}"),
            "error": style.red("{g-cross}") + " {p-message}",
        },
    }


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


SLOT = re.compile(r"\{([gp])-([\w.]+)\}")
EMPTY_SLOT = "\0"
_UNIT = re.compile(ESCAPE_PATTERN + "|.", re.S)
_EMPTY_RUN = re.compile(rf"(?:[ ]|{ESCAPE_PATTERN})*\0(?:[ ]|\0|{ESCAPE_PATTERN})*")
_TRAILING_SPACES = re.compile(rf" +((?:{ESCAPE_PATTERN})*)$", re.M)


def tidy_spaces(text: str) -> str:
    """Collapse the spaces around empty slots to one, and drop trailing spaces."""
    def squeeze(match: re.Match) -> str:
        run = match.group()
        escapes = "".join(ESCAPE.findall(run))
        return (" " if " " in ESCAPE.sub("", run) else "") + escapes

    return _TRAILING_SPACES.sub(r"\1", _EMPTY_RUN.sub(squeeze, text))


def _check_changes(changes: dict) -> dict:
    unknown = set(changes) - {"color", "unicode", "screen"}
    if unknown:
        raise TypeError(f"unknown theme settings: {', '.join(sorted(unknown))}")
    return changes


def _count(bar: Bar) -> str:
    if bar.total is not None:
        return f"{bar.show(bar.done)}/{bar.show(bar.total)}"
    return bar.show(bar.done) if bar.done > 0 else ""


def _done_text(view: PromptView) -> str:
    if view.kind == "secret":
        return view.message
    if view.kind == "confirm":
        return f"{view.message} {view.value}"
    return f"{view.message}: {view.value}"


def _is_path(word: str) -> bool:
    return word.startswith(("./", "~/", "/", ".")) and "/" in word


def _char_width(char: str) -> int:
    if unicodedata.combining(char):
        return 0
    return 2 if unicodedata.east_asian_width(char) in "WF" else 1


theme = Theme()
