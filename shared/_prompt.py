"""Questions in the terminal. Each function returns the answer."""

from __future__ import annotations

from collections.abc import Mapping
from contextlib import contextmanager
from typing import Any, Callable, Iterator, Sequence

from shared import _theme
from shared._log import live_area
from shared._style import strip
from shared._theme import PromptView, Theme, fit

Validate = Callable[[Any], "str | None"]

# The actions a question lists in its key guide, in the order they are listed.
GUIDE_ACTIONS = {
    "input": ("accept",),
    "secret": ("accept",),
    "confirm": ("up", "down", "yes", "no", "accept"),
    "choice": ("up", "down", "accept"),
    "choices": ("up", "down", "toggle", "accept"),
}


class PromptError(Exception):
    """A question that cannot be asked: no default to fall back on, or bad options."""


class Keymap:
    """The keys for each action. Assigning a tuple replaces that action's keys."""

    ACTIONS = ("up", "down", "left", "right", "accept", "toggle", "backspace", "yes", "no", "interrupt")

    def __init__(self) -> None:
        self.up = ("up", "k")
        self.down = ("down", "j")
        self.left = ("left", "h")
        self.right = ("right", "l")
        self.accept = ("enter",)
        self.toggle = ("space",)
        self.backspace = ("backspace",)
        self.yes = ("y",)
        self.no = ("n",)
        self.interrupt = ("ctrl+c",)

    def __setattr__(self, name: str, keys: tuple) -> None:
        if name not in self.ACTIONS:
            raise AttributeError(f"unknown key action {name!r}")
        object.__setattr__(self, name, tuple(keys))


keymap = Keymap()


def input(message: str, *, default: str | None = None, placeholder: str = "", keys: bool = False,
          validate: Validate | None = None, theme: Theme | None = None) -> str:
    question = _Question("input", message, default=default, validate=validate, keys=keys)
    question.buffer = default or ""
    question.placeholder = placeholder
    return _ask(question, theme)


def secret(message: str, *, default: str | None = None, mask: str | None = None, keys: bool = False,
           validate: Validate | None = None, theme: Theme | None = None) -> str:
    question = _Question("secret", message, default=default, validate=validate, keys=keys)
    question.buffer = default or ""
    question.mask = mask
    return _ask(question, theme)


def confirm(message: str, *, default: bool = False, yes: str = "Yes", no: str = "No", keys: bool = False,
            validate: Validate | None = None, theme: Theme | None = None) -> bool:
    question = _Question("confirm", message, default=default, validate=validate, keys=keys)
    question.labels, question.values = [yes, no], [True, False]
    question.index = 0 if default else 1
    return _ask(question, theme)


def choice(message: str, options: Sequence | Mapping, *, default: Any = None, filter: bool = False,
           keys: bool = False, validate: Validate | None = None, theme: Theme | None = None) -> Any:
    question = _Question("choice", message, default=default, validate=validate, keys=keys)
    question.set_options(options, filter)
    if default is not None:
        question.index = question.position_of(default)
    return _ask(question, theme)


def choices(message: str, options: Sequence | Mapping, *, default: Sequence | None = None, min: int = 0,
            max: int | None = None, filter: bool = False, keys: bool = False, validate: Validate | None = None,
            theme: Theme | None = None) -> list:
    if min < 0 or (max is not None and max < min):
        raise ValueError(f"choices needs 0 <= min <= max, got min={min} max={max}")
    question = _Question("choices", message, default=None if default is None else list(default),
                         validate=validate, keys=keys)
    question.set_options(options, filter)
    question.checked = {question.position_of(value) for value in default or ()}
    question.least, question.most = min, max
    return _ask(question, theme)


# Process-wide on purpose: worker threads started inside the block also get defaults.
_answering_with_defaults = 0


@contextmanager
def defaults() -> Iterator[None]:
    """Answer each question with its default, reading no keys."""
    global _answering_with_defaults
    _answering_with_defaults += 1
    try:
        yield
    finally:
        _answering_with_defaults -= 1


class _Question:
    """The state of one open question, changed one key at a time."""

    def __init__(self, kind: str, message: str, *, default: Any, validate: Validate | None, keys: bool):
        self.kind, self.message, self.default = kind, message, default
        self.validate, self.show_keys = validate, keys
        self.buffer = ""
        self.placeholder = ""
        self.mask: str | None = None
        self.labels: list[str] = []
        self.values: list = []
        self.index = 0
        self.checked: set[int] = set()
        self.filter = False
        self.least, self.most = 0, None
        self.error: str | None = None

    def set_options(self, options: Sequence | Mapping, filter: bool) -> None:
        if isinstance(options, Mapping):
            self.labels, self.values = [str(label) for label in options], list(options.values())
        else:
            self.labels, self.values = [str(option) for option in options], list(options)
        if not self.values:
            raise PromptError(f"{self.message!r} has no options")
        self.filter = filter

    def position_of(self, value: Any) -> int:
        if value not in self.values:
            raise PromptError(f"{value!r} is not an option of {self.message!r}")
        return self.values.index(value)

    def visible(self) -> list[int]:
        """Positions of the options the filter leaves."""
        typed = self.buffer.casefold() if self.filter else ""
        return [position for position, label in enumerate(self.labels) if label.casefold().startswith(typed)]

    def press(self, key: str) -> bool:
        """Apply one key. Returns True when the key asks to accept."""
        self.error = None
        if key in keymap.accept:
            return True
        typing = self.kind in ("input", "secret") or self.filter
        if typing and key in keymap.backspace:
            self.buffer, self.index = self.buffer[:-1], 0
            return False
        # In a typed question every printable key is text, so "k" types k instead of moving up.
        text = " " if key == "space" else key if len(key) == 1 and key.isprintable() else None
        toggles = self.kind == "choices" and key in keymap.toggle
        if typing and text is not None and not toggles:
            self.buffer, self.index = self.buffer + text, 0
            return False
        visible = self.visible()
        if key in keymap.up:
            self.index = self.index - 1 if self.index > 0 else 0
        elif key in keymap.down:
            self.index = self.index + 1 if self.index < len(visible) - 1 else self.index
        elif self.kind == "confirm" and key in keymap.yes + keymap.no:
            self.index = 0 if key in keymap.yes else 1
            return True
        elif toggles and visible:
            self.checked ^= {visible[self.index]}
        return False

    def problem(self) -> str | None:
        """Why the current answer cannot be accepted, or None."""
        if self.kind == "choice" and not self.visible():
            return "no match"
        if self.kind == "choices" and len(self.checked) < self.least:
            return f"choose at least {self.least}"
        if self.kind == "choices" and self.most is not None and len(self.checked) > self.most:
            return f"choose at most {self.most}"
        return self.validate(self.answer()) if self.validate else None

    def answer(self) -> Any:
        if self.kind in ("input", "secret"):
            return self.buffer
        if self.kind == "choices":
            return [self.values[position] for position in sorted(self.checked)]
        return self.values[self.visible()[self.index]]

    def view(self, done: bool = False) -> PromptView:
        visible = self.visible() if self.labels else []
        if done:
            return PromptView(self.kind, self.message, self._answer_text(), done=True)
        keys = tuple(
            (action, getattr(keymap, action)) for action in GUIDE_ACTIONS[self.kind] if getattr(keymap, action)
        ) if self.show_keys else ()
        no_match = "no match" if self.filter and not visible else None
        shown_value = self.buffer if self.kind != "secret" else (self.mask or "•") * len(self.buffer)
        return PromptView(
            self.kind, self.message, shown_value,
            options=tuple(self.labels[position] for position in visible),
            index=self.index,
            checked=frozenset(row for row, position in enumerate(visible) if position in self.checked),
            error=self.error or no_match, keys=keys, placeholder=self.placeholder,
        )

    def _answer_text(self) -> str:
        if self.kind == "input":
            return self.buffer
        if self.kind == "secret":
            return ""
        if self.kind == "choices":
            return ", ".join(self.labels[position] for position in sorted(self.checked))
        return self.labels[self.visible()[self.index]]


def _ask(question: _Question, theme: Theme | None) -> Any:
    theme = theme or _theme.theme
    screen = theme.screen
    if _answering_with_defaults or not screen.interactive:
        if question.default is None:
            raise PromptError(f"{question.message!r} has no default and cannot be asked here")
        return question.default
    if question.kind == "secret" and question.mask is None:
        question.mask = "•" if theme.unicode else "*"
    frame = _Frame(theme)
    with live_area(screen).question():
        try:
            while True:
                frame.draw(theme._format_prompt(question.view()))
                key = screen.readkey()
                if key in keymap.interrupt:
                    raise KeyboardInterrupt
                if question.press(key):
                    question.error = question.problem()
                    if question.error is None:
                        break
        except KeyboardInterrupt:
            frame.end()
            raise
        frame.draw(theme._format_prompt(question.view(done=True)))
        frame.end()
    return question.answer()


class _Frame:
    """Draws a question's frame, replacing the previous one in place on a TTY."""

    def __init__(self, theme: Theme):
        self.theme = theme
        self.drawn_lines = 0

    def draw(self, frame: str) -> None:
        screen = self.theme.screen
        if self.theme.color == "none":
            frame = strip(frame)
        lines = [fit(line, screen.width) for line in frame.split("\n")]
        if not screen.tty:
            screen.write("\n".join(lines) + "\n")
            return
        erase = ""
        if self.drawn_lines:
            erase = "\r" + (f"\x1b[{self.drawn_lines - 1}A" if self.drawn_lines > 1 else "") + "\x1b[J"
        screen.write(erase + "\n".join(lines))
        self.drawn_lines = len(lines)

    def end(self) -> None:
        if self.theme.screen.tty and self.drawn_lines:
            self.theme.screen.write("\n")
            self.drawn_lines = 0
