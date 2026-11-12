"""Ask a person a question in the terminal, in the look of Charm's huh: `ask`, `confirm` and `choose`.

How it works: each function builds one question and returns its answer. In a terminal the question
is drawn by `shared.theme`'s prompt drawer and redrawn after every key `theme.screen` reads, until
the answer is submitted, then replaced by one done line. While it's open, `shared.log` holds the
other threads' lines and live rows, and asks from other threads wait their turn. Without a
terminal, or inside `with prompt.defaults():`, nothing is read: each question answers its default
at once, and one without a default raises `Error`. Which keys do what is `prompt.keymap`, and
whether the help line of keys is shown is `prompt.keys_shown`. It wraps no library and has no `.raw`.

    from shared import prompt

    name = prompt.ask("Name", placeholder="ada")
    token = prompt.ask("Token", secret=True)
    if prompt.confirm("Deploy?", default=True):
        source = prompt.choose("Source", {"Instagram": "ins", "TikTok": "tik"})
        tabs = prompt.choose("Tabs", ["posts", "reels", "tagged"], many=True, min=1)
"""

from __future__ import annotations

from collections.abc import Mapping
from contextlib import contextmanager
from typing import Any, Callable, Iterator, Literal, Sequence, TypeVar, overload

import shared
from shared import _theme
from shared._log import live_area
from shared._screen import Paste, hide_cursor, show_cursor
from shared._style import strip
from shared._theme import PromptView, Theme, fit

__all__ = ["Error", "Keymap", "ask", "choose", "confirm", "defaults", "keymap", "keys_shown"]

# A validator takes the answer and returns the message to show, or None to accept it.
Validate = Callable[[Any], "str | None"]

# The actions a question lists in its help line, in the order they are listed. The keys are the
# question kinds `shared.theme`'s prompt drawer knows: a typed text, a hidden one, yes or no, one
# option, several options.
GUIDE_ACTIONS = {
    "input": ("submit",),
    "secret": ("submit",),
    "confirm": ("left", "right", "yes", "no", "submit"),
    "choice": ("up", "down", "submit"),
    "choices": ("up", "down", "toggle", "submit"),
}
# Old action names, kept so that a rename raises a message that names the new one.
RENAMED = {"accept": "submit"}

# Whether questions show the help line of keys; `keys=` overrides it for one question.
keys_shown = True


class Error(shared.Error):
    """A question that cannot be asked: no default to fall back on, or bad options."""


class Keymap:
    """The keys for each action. Assigning a tuple replaces that action's keys.

        prompt.keymap.submit = ("enter", "tab")
        prompt.keymap.no = ()          # n no longer answers no

    An unknown action, read or assigned, raises `AttributeError`.
    """

    ACTIONS = ("up", "down", "left", "right", "submit", "toggle", "backspace", "yes", "no", "interrupt")

    def __init__(self) -> None:
        """Set the default keys: the arrows and the vim letters, enter to submit, space to toggle, and so on."""
        # The vim letters move the cursor too, which is why a typed question treats them as text instead.
        self.up = ("up", "k")
        self.down = ("down", "j")
        self.left = ("left", "h")
        self.right = ("right", "l")
        self.submit = ("enter",)
        self.toggle = ("space",)
        self.backspace = ("backspace",)
        self.yes = ("y",)
        self.no = ("n",)
        self.interrupt = ("ctrl+c",)

    def __getattr__(self, name: str) -> tuple:
        raise _unknown_action(name)

    def __setattr__(self, name: str, keys: tuple) -> None:
        # Only the known actions can be assigned, so a typo in a key action fails at once.
        if name not in self.ACTIONS:
            raise _unknown_action(name)
        object.__setattr__(self, name, tuple(keys))


keymap = Keymap()


def ask(message: str, *, secret: bool = False, default: str | None = None, placeholder: str = "",
        description: str = "", validate: Validate | None = None, mask: str | None = None,
        keys: bool | None = None) -> str:
    """Ask for a line of text, and return what was typed.

        name = prompt.ask("Name", placeholder="ada", validate=lambda text: None if text else "Enter a name.")
        token = prompt.ask("Token", secret=True)

    Args:
        message: the question, drawn as the frame's title and kept in the done line.
        secret: hide what is typed: each character is drawn as `mask`, and the done line shows
            only the message, never the answer.
        default: the text the answer starts with, which can be edited; it is also the answer
            without a terminal or inside `defaults()`. None means there is none.
        placeholder: faint text shown while nothing is typed; it is never the answer.
        description: more lines under the title, wrapped to the screen.
        validate: called with the text on submit; a string it returns is shown as an error and
            the question stays open, None accepts.
        mask: the character drawn for each hidden one, with `secret`; None is • in Unicode and *
            in ASCII. Ignored without `secret`.
        keys: show the help line of keys; None follows `prompt.keys_shown`.

    Returns:
        The text as typed, not stripped.

    Raises:
        Error: there is no terminal, or `defaults()` is on, and `default` is None.
        KeyboardInterrupt: a `keymap.interrupt` key, ctrl+c by default, was pressed.

    It blocks until answered, and holds other threads' log output while it's open.
    """
    question = _Question("secret" if secret else "input", message, description, default=default,
                         validate=validate, keys=keys)
    question.buffer = default or ""
    question.caret = len(question.buffer)
    question.placeholder = placeholder
    if secret:
        question.mask = mask
    return _ask(question)


def confirm(message: str, *, description: str = "", default: bool = False, yes: str = "Yes", no: str = "No",
            keys: bool | None = None, validate: Validate | None = None) -> bool:
    """Ask a yes-or-no question, and return True for yes.

        if prompt.confirm("Overwrite?", yes="Overwrite", no="Keep"):
            ...

    Args:
        message: the question, drawn as the frame's title and kept in the done line.
        description: more lines under the title, wrapped to the screen.
        default: the button selected at first, and the answer without a terminal or inside `defaults()`.
        yes: the label of the yes button.
        no: the label of the no button.
        keys: show the help line of keys; None follows `prompt.keys_shown`.
        validate: called with the answer on submit; a string it returns is shown as an error and
            the question stays open, None accepts.

    Raises:
        KeyboardInterrupt: a `keymap.interrupt` key, ctrl+c by default, was pressed.

    `keymap.yes` and `keymap.no` answer at once; left and right move between the buttons.
    """
    question = _Question("confirm", message, description, default=default, validate=validate, keys=keys)
    question.labels, question.values = [yes, no], [True, False]
    question.index = 0 if default else 1
    return _ask(question)


Value = TypeVar("Value")


@overload
def choose(message: str, options: Mapping[Any, Value], *, many: Literal[False] = False,
           default: Value | None = None, filter: bool = False, description: str = "",
           validate: Validate | None = None, keys: bool | None = None) -> Value: ...
@overload
def choose(message: str, options: Sequence[Value], *, many: Literal[False] = False,
           default: Value | None = None, filter: bool = False, description: str = "",
           validate: Validate | None = None, keys: bool | None = None) -> Value: ...
@overload
def choose(message: str, options: Mapping[Any, Value], *, many: Literal[True],
           default: Sequence[Value] | None = None, filter: bool = False, min: int = 0, max: int | None = None,
           description: str = "", validate: Validate | None = None, keys: bool | None = None) -> list[Value]: ...
@overload
def choose(message: str, options: Sequence[Value], *, many: Literal[True],
           default: Sequence[Value] | None = None, filter: bool = False, min: int = 0, max: int | None = None,
           description: str = "", validate: Validate | None = None, keys: bool | None = None) -> list[Value]: ...
def choose(message: str, options: Sequence | Mapping, *, many: bool = False, default: Any = None,
           filter: bool = False, min: int = 0, max: int | None = None, description: str = "",
           validate: Validate | None = None, keys: bool | None = None) -> Any:
    """Ask to pick one of the options, or several with `many=True`, and return the value or values.

        source = prompt.choose("Source", {"Instagram": "ins", "TikTok": "tik"}, default="ins")
        tabs = prompt.choose("Tabs", ["posts", "reels", "tagged"], many=True, min=1)

    Args:
        message: the question, drawn as the frame's title and kept in the done line.
        options: the values, each shown as `str(value)`; or a mapping of label to value, which
            shows the labels and returns the values.
        many: a checklist: the toggle key checks and unchecks the option under the cursor, and the
            answer is the checked values, in the options' order.
        default: the value the cursor starts on, or with `many` the values checked at first; it is
            also the answer without a terminal or inside `defaults()`. None means there is none.
        filter: typed letters narrow the options to those whose label starts with them, ignoring
            case, so letters are text and not keys.
        min: with `many`, the fewest options that can be checked on submit.
        max: with `many`, the most options that can be checked on submit; None is no limit.
        description: more lines under the title, wrapped to the screen.
        validate: called with the answer on submit; a string it returns is shown as an error and
            the question stays open, None accepts.
        keys: show the help line of keys; None follows `prompt.keys_shown`.

    Returns:
        One value, or with `many` a list of values: `str` and `list[str]` to a type checker when
        the options are strings.

    Raises:
        Error: `options` is empty, `default` holds a value that isn't an option, or there is no
            terminal (or `defaults()` is on) and `default` is None.
        ValueError: `min` and `max` aren't 0 <= min <= max.
        TypeError: `min` or `max` is given without `many`.
        KeyboardInterrupt: a `keymap.interrupt` key, ctrl+c by default, was pressed.

    A long list scrolls with the cursor, so the frame never outgrows the screen.
    """
    if not many:
        if min != 0 or max is not None:
            raise TypeError("choose takes min and max only with many=True")
        question = _Question("choice", message, description, default=default, validate=validate, keys=keys)
        question.set_options(options, filter)
        if default is not None:
            question.index = question.position_of(default)
        return _ask(question)
    if min < 0 or (max is not None and max < min):
        raise ValueError(f"choose needs 0 <= min <= max, got min={min} max={max}")
    question = _Question("choices", message, description, default=None if default is None else list(default),
                         validate=validate, keys=keys)
    question.set_options(options, filter)
    question.checked = {question.position_of(value) for value in default or ()}
    question.least, question.most = min, max
    return _ask(question)


# Process-wide on purpose: worker threads started inside the block also get defaults.
_answering_with_defaults = 0


@contextmanager
def defaults() -> Iterator[None]:
    """Answer each question with its default, reading no keys, inside the block.

        with prompt.defaults():
            prompt.confirm("Deploy?", default=True)    # True, at once

    It is process-wide: questions asked from other threads inside the block answer their default
    too. A question with no default raises `Error`.
    """
    global _answering_with_defaults
    _answering_with_defaults += 1
    try:
        yield
    finally:
        _answering_with_defaults -= 1


class _Question:
    """The state of one open question, changed one key at a time."""

    def __init__(self, kind: str, message: str, description: str, *, default: Any, validate: Validate | None,
                 keys: bool | None):
        """Start a question of the given kind, with its text, its default and its validator, and nothing answered."""
        self.kind, self.message, self.description, self.default = kind, message, description, default
        self.validate, self.show_keys = validate, keys
        self.buffer = ""
        self.caret = 0  # where typed characters go in `buffer`, for input and secret
        self.placeholder = ""
        self.mask: str | None = None
        self.labels: list[str] = []
        self.values: list = []
        self.index = 0
        self.checked: set[int] = set()
        self.filter = False
        self.least = 0
        self.most: int | None = None
        self.error: str | None = None

    @property
    def typing(self) -> bool:
        """Whether letters are typed as text, so they can't be keys."""
        return self.kind in ("input", "secret") or self.filter

    def set_options(self, options: Sequence | Mapping, filter: bool) -> None:
        """Set the options from a sequence, or from a mapping of label to value, and whether they filter.

        Raises:
            Error: there are no options at all, which would leave nothing to answer with.
        """
        # A mapping shows its keys as labels and answers with its values; a sequence does both with the item.
        if isinstance(options, Mapping):
            self.labels, self.values = [str(label) for label in options], list(options.values())
        else:
            self.labels, self.values = [str(option) for option in options], list(options)
        if not self.values:
            raise Error(f"{self.message!r} has no options")
        self.filter = filter

    def position_of(self, value: Any) -> int:
        """The index of value among the options, for a default or a checked value."""
        if value not in self.values:
            raise Error(f"{value!r} is not an option of {self.message!r}")
        return self.values.index(value)

    def visible(self) -> list[int]:
        """Positions of the options the filter leaves."""
        typed = self.buffer.casefold() if self.filter else ""
        return [position for position, label in enumerate(self.labels) if label.casefold().startswith(typed)]

    def press(self, key: str) -> bool:
        """Apply one key. Returns True when the key asks to submit.

        A key is handled in this order: a paste, then submit, then the text editing of a typed question,
        and last the list movement of the options. Every key that changes the typed text resets the
        highlighted option to the first, since the filtered list has changed under it.
        """
        self.error = None
        # Step 1: a paste arrives as one value, not as keys, so it is inserted whole.
        if isinstance(key, Paste):
            self._paste(key)
            return False
        # Step 2: submit ends the question; validation happens in the caller, which can keep it open.
        if key in keymap.submit:
            return True
        editing = self.kind in ("input", "secret")
        # Step 3: backspace removes one character before the caret in an input, or the last one in a filter.
        if self.typing and key in keymap.backspace:
            if editing:
                self.buffer = self.buffer[:max(self.caret - 1, 0)] + self.buffer[self.caret:]
                self.caret = max(self.caret - 1, 0)
            else:
                self.buffer = self.buffer[:-1]
            self.index = 0
            return False
        # Step 4: in a typed question every printable key is text, so "k" types k instead of moving up.
        # Space arrives as the key name "space", not as a character, so it is turned back into one here.
        text = " " if key == "space" else key if len(key) == 1 and key.isprintable() else None
        # A checklist's toggle key is not typed text, so it toggles even while a filter is being typed.
        toggles = self.kind == "choices" and key in keymap.toggle
        if self.typing and text is not None and not toggles:
            if editing:
                self.buffer = self.buffer[:self.caret] + text + self.buffer[self.caret:]
                self.caret += 1
            else:
                self.buffer += text
            self.index = 0
            return False
        # Step 5: the arrow keys, not h and l (those are text above), move the typed text's cursor.
        if editing and key in keymap.left + keymap.right:
            self.caret = min(max(self.caret + (-1 if key in keymap.left else 1), 0), len(self.buffer))
            return False
        # Step 6: movement and choice keys act on the options the filter leaves visible.
        visible = self.visible()
        if key in keymap.up:
            self.index = self.index - 1 if self.index > 0 else 0
        elif key in keymap.down:
            self.index = self.index + 1 if self.index < len(visible) - 1 else self.index
        elif self.kind == "confirm" and key in keymap.left + keymap.right:
            self.index = 0 if key in keymap.left else 1
        elif self.kind == "confirm" and key in keymap.yes + keymap.no:
            self.index = 0 if key in keymap.yes else 1
            return True
        elif toggles and visible:
            self.checked ^= {visible[self.index]}
        return False

    def _paste(self, text: str) -> None:
        """Insert a pasted text whole: at the cursor in an input, at the end in a filter."""
        if self.kind in ("input", "secret"):
            self.buffer = self.buffer[:self.caret] + text + self.buffer[self.caret:]
            self.caret += len(text)
        elif self.filter:
            self.buffer += text
        self.index = 0

    def problem(self) -> str | None:
        """Why the current answer cannot be submitted, or None."""
        if self.kind == "choice" and not self.visible():
            return "no match"
        if self.kind == "choices" and len(self.checked) < self.least:
            return f"choose at least {self.least}"
        if self.kind == "choices" and self.most is not None and len(self.checked) > self.most:
            return f"choose at most {self.most}"
        return self.validate(self.answer()) if self.validate else None

    def answer(self) -> Any:
        """The value the question returns: the text, the checked values in order, or the option under the cursor."""
        if self.kind in ("input", "secret"):
            return self.buffer
        if self.kind == "choices":
            return [self.values[position] for position in sorted(self.checked)]
        return self.values[self.visible()[self.index]]

    def view(self, done: bool = False) -> PromptView:
        """The state the theme draws: the live question, or with `done`, the one line kept after it is answered."""
        if done:
            return PromptView(self.kind, self.message, value=self._answer_text(), done=True)
        visible = self.visible() if self.labels else []
        no_match = "no match" if self.filter and not visible else None
        # A secret shows one mask character per typed character, so its length is visible but not its text.
        shown_value = self.buffer if self.kind != "secret" else (self.mask or "•") * len(self.buffer)
        return PromptView(
            self.kind, self.message, self.description, shown_value, self.placeholder,
            options=tuple(self.labels[position] for position in visible),
            position=self.index,
            checked=frozenset(row for row, position in enumerate(visible) if position in self.checked),
            error=self.error or no_match, keys=self._help_keys(),
            caret=self.caret if self.kind in ("input", "secret") else None,
        )

    def _help_keys(self) -> tuple:
        """The (action, keys) pairs the help line lists for this question, or () when keys are hidden."""
        # `keys=` on one question beats the module-wide `keys_shown`; None means "follow the module setting".
        if not (keys_shown if self.show_keys is None else self.show_keys):
            return ()
        listed = []
        for action in GUIDE_ACTIONS[self.kind]:
            # A single character is text in a typed question, so it is not listed as a key to press.
            bound = tuple(key for key in getattr(keymap, action) if not (self.typing and len(key) == 1))
            if bound:
                listed.append((action, bound))
        return tuple(listed)

    def _answer_text(self) -> str:
        """The one line kept after submit: the typed text, the checked labels, or the chosen label.

        A secret keeps nothing, so its done line shows only the question.
        """
        if self.kind == "input":
            return self.buffer
        if self.kind == "secret":
            return ""
        if self.kind == "choices":
            return ", ".join(self.labels[position] for position in sorted(self.checked))
        return self.labels[self.visible()[self.index]]


def _ask(question: _Question) -> Any:
    """Run one question to its answer: read keys and redraw until it submits, or answer its default without a terminal.

    Raises:
        Error: there is no terminal, or defaults() is on, and the question has no default.
        KeyboardInterrupt: the interrupt key was pressed; the frame is ended before it propagates.
    """
    theme = _theme.theme
    screen = theme.screen
    # Step 1: without a terminal, or inside defaults(), nothing is read; the default answers at once.
    if _answering_with_defaults or not screen.interactive:
        if question.default is None:
            raise Error(f"{question.message!r} has no default and cannot be asked here")
        return question.default
    # Step 2: a hidden answer is drawn with a bullet where the theme's Unicode works, else with an asterisk.
    if question.kind == "secret" and question.mask is None:
        question.mask = "•" if theme.unicode else "*"
    frame = _Frame(theme)
    # Step 3: the question holds the screen's live area and key reading, so other threads' log lines wait.
    with live_area(screen).question(), screen.reading():
        try:
            # Step 4: draw, read one key, and apply it, until a submit passes validation.
            while True:
                frame.draw(theme.draw("prompt", question.view()))
                key = screen.readkey()
                # The interrupt key is checked before the question sees the key, so no question can swallow it.
                if key in keymap.interrupt:
                    raise KeyboardInterrupt
                if question.press(key):
                    question.error = question.problem()
                    if question.error is None:
                        break
            # Step 5: the answered question is replaced by its one done line.
            frame.draw(theme.draw("prompt", question.view(done=True)))
        finally:
            # Ctrl-C and any error end the frame too, so the cursor is never left hidden.
            frame.end()
    return question.answer()


class _Frame:
    """Draws a question's frame, replacing the previous one in place on a TTY."""

    def __init__(self, theme: Theme):
        """Start with no lines drawn, so the first draw erases nothing."""
        self.theme = theme
        self.drawn_lines = 0

    def draw(self, frame: str) -> None:
        """Draw the frame, erasing the one before it on a terminal, or writing it as plain lines otherwise."""
        screen = self.theme.screen
        if self.theme.color == "none":
            frame = strip(frame)
        lines = [fit(line, screen.width) for line in frame.split("\n")]
        # Without a terminal there is no cursor to move back, so each frame is written as new lines.
        if not screen.tty:
            screen.write("\n".join(lines) + "\n")
            return
        hide_cursor(screen)  # the input line draws its own cursor; the terminal's would sit at the keys line
        # \r returns to column 0; ESC[nA moves up n lines; ESC[J erases from the cursor to the end of the screen.
        erase = ""
        if self.drawn_lines:
            erase = "\r" + (f"\x1b[{self.drawn_lines - 1}A" if self.drawn_lines > 1 else "") + "\x1b[J"
        screen.write(erase + "\n".join(lines))
        self.drawn_lines = len(lines)

    def end(self) -> None:
        """Leave the frame on screen, move past it, and show the cursor again."""
        screen = self.theme.screen
        # The last frame stays on screen, so the next output starts on a new line after it.
        if screen.tty and self.drawn_lines:
            screen.write("\n")
            self.drawn_lines = 0
        show_cursor(screen)


def _unknown_action(name: str) -> AttributeError:
    """The AttributeError for an action name that isn't one of the keymap's, naming the new one for a renamed action."""
    if name in RENAMED:
        return AttributeError(f"prompt.keymap.{name} was renamed to {RENAMED[name]}")
    return AttributeError(f"unknown key action {name!r}")
