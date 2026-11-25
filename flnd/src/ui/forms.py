"""Questions in the terminal, in Huh's look: a pick, a line of text, a yes or no.

SPEC.md#commands-and-flags explains it.
"""

from __future__ import annotations

import sys
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from typing import Any, NamedTuple

from flnd.errors import Stopped, UsageError
from flnd.ui import theme

__all__ = ["Option", "ask", "choose", "confirm", "interactive", "select", "text"]


class Option(NamedTuple):
    """One answer `select` offers.

    Args:
        label: what is shown and, without a `value`, what is returned.
        value: what `select` returns for it; None returns `label`.
        note: a dim note shown beside the label.
    """

    label: str
    value: Any = None
    note: str = ""


def interactive() -> bool:
    """True when standard input is a terminal."""
    stdin = sys.stdin
    return bool(stdin and stdin.isatty())


def select(title: str, options: Sequence[Option | str] | Mapping[str, Any], *,
           default: Any = None) -> Any:
    """Ask to pick one of `options` with ↑↓ and enter.

    Args:
        options: the answers: labels, `Option`s, or a dict of label to value.
        default: the value the cursor starts on; None starts on the first.

    Returns:
        The value picked, or None when esc goes back.

    Raises:
        UsageError: there are no options, or no terminal to ask in.
        Stopped: Ctrl-C.
    """
    picks = _options(options)
    if not picks:
        raise UsageError(f"{title}: there is nothing to choose from")
    if not interactive():
        raise _needed(title)
    values = [pick.value for pick in picks]
    start = values.index(default) if default in values else 0
    return run(_Select(title, picks, start), keep=True)


def text(title: str, *, secret: bool = False, default: str | None = None) -> str | None:
    """Ask for one line of text.

    Args:
        secret: hide what is typed, showing a bullet per character.
        default: the answer when enter is pressed on an empty line, shown dim; None for "".

    Returns:
        The text, or None when esc goes back.

    Raises:
        UsageError: there is no terminal to ask in.
        Stopped: Ctrl-C.
    """
    if not interactive():
        raise _needed(title)
    return run(_Text(title, secret=secret, default=default or ""), keep=True)


def confirm(title: str, *, default: bool = True) -> bool:
    """Ask a yes-or-no question. Without a terminal nothing is asked and `default` is the answer.

    Args:
        default: the answer the cursor starts on, and the answer without a terminal, so a
            command such as `target clean` proceeds on a pipe.

    Returns:
        True for yes; False for no, or when esc goes back.

    Raises:
        Stopped: Ctrl-C.
    """
    if not interactive():
        return default
    return bool(run(_Confirm(title, default=default), keep=True))


def ask(message: str, *, secret: bool = False, default: str | None = None) -> str:
    """`text` as today's callers use it: off a terminal it returns `default`, when one is set.

    Raises:
        UsageError: esc went back, or there is no terminal and no default.
    """
    if not interactive() and default is not None:
        return default
    answer = text(message, secret=secret, default=default)
    if answer is None:
        raise UsageError("the input ended before an answer")
    return answer


def choose(message: str, choices: Sequence[Any] | Mapping[str, Any], *,
           default: Any = None) -> Any:
    """`select` as today's callers use it: off a terminal it returns `default`, when one is set.

    Raises:
        UsageError: no choices, a `default` that isn't one, esc, or no terminal and no default.
    """
    picks = _options(choices)
    if default is not None and default not in [pick.value for pick in picks]:
        raise UsageError(f"{default!r} is not a choice for {message!r}")
    if picks and not interactive() and default is not None:
        return default
    answer = select(message, picks, default=default)
    if answer is None:
        raise UsageError("the input ended before an answer")
    return answer


def _options(options: Sequence[Option | str] | Mapping[str, Any]) -> list[Option]:
    if isinstance(options, Mapping):
        return [Option(str(label), value) for label, value in options.items()]
    picks = [pick if isinstance(pick, Option) else Option(str(pick), pick) for pick in options]
    return [pick if pick.value is not None else pick._replace(value=pick.label) for pick in picks]


def _needed(title: str) -> UsageError:
    return UsageError(f"{title!r} needs an answer, and there is no terminal to ask in",
                      hint="run it in a terminal")


# ---------------------------------------------------------------------------------------------
# The fields: each draws itself and takes keys. `press` returns PENDING until it has an answer.
# ---------------------------------------------------------------------------------------------

PENDING = object()
_STOP = object()


def hints(pairs: Sequence[tuple[str, str]], fancy: bool, *, off: frozenset[str] = frozenset()
          ) -> Any:
    """The bottom line of keys: each key, then what it does, a dot between; `off` ones all dim."""
    from rich.text import Text

    line = Text("  ")
    dot = f" {theme.glyph('waiting', fancy)} "
    for n, (key, does) in enumerate(pairs):
        if n:
            line.append(dot, style=theme.style("dim"))
        shown = theme.glyph(key, fancy) if key in theme.GLYPHS else key
        line.append(shown, style=theme.style("dim" if key in off else "text"))
        line.append(f" {does}", style=theme.style("dim"))
    return line


class Owner:
    """What `_plain` draws and feeds: `view` is a Rich renderable, `press` PENDING or the end."""

    # Whether the last frame is an answer worth leaving on the terminal.
    answered = False
    # The terminal's height in lines, set by `_plain` before drawing; None when unknown.
    height: int | None = None

    def view(self, fancy: bool) -> Any:
        raise NotImplementedError

    def press(self, key: str, char: str | None) -> Any:
        raise NotImplementedError

    def paste(self, text: str) -> None:
        """Take pasted text; ignored unless the owner has a place to type."""


class Field(Owner):
    """A question inside the bar `┃`: its title, then its body, then the keys below."""

    keys: tuple[tuple[str, str], ...] = ()

    def __init__(self, title: str) -> None:
        self.title = title
        self.answer: Any = PENDING

    def body(self, fancy: bool) -> list[Any]:
        raise NotImplementedError

    def done(self, fancy: bool) -> list[Any]:
        """The body once answered: the answer, in `good`."""
        from rich.text import Text

        return [Text(self.shown(), style=theme.style("good"))]

    def shown(self) -> str:
        return str(self.answer)

    def press(self, key: str, char: str | None) -> Any:
        raise NotImplementedError

    def view(self, fancy: bool) -> Any:
        from rich.console import Group
        from rich.text import Text

        answered = self.answer is not PENDING
        bar = Text(f"  {theme.glyph('bar', fancy)} ", style=theme.style("dim" if answered
                                                                        else "focus"))
        title = Text(self.title, style=theme.style("bold focus" if not answered else "text"))
        lines = [title, *(self.done(fancy) if answered else self.body(fancy))]
        drawn: list[Any] = [Text.assemble(bar, line) for line in lines]
        if not answered:
            drawn += [Text(""), hints(self.keys, fancy)]
        return Group(*drawn)

    def finish(self, answer: Any) -> Any:
        self.answer = answer
        self.answered = True
        return answer


class _Select(Field):
    keys = (("updown", "choose"), ("enter", "confirm"), ("esc", "back"))

    def __init__(self, title: str, options: list[Option], start: int) -> None:
        super().__init__(title)
        self.options = options
        self.at = start

    def body(self, fancy: bool) -> list[Any]:
        from rich.text import Text

        width = max(len(pick.label) for pick in self.options)
        lines = []
        for n, pick in enumerate(self.options):
            here = n == self.at
            line = Text(f"{theme.glyph('cursor', fancy) if here else ' '} ",
                        style=theme.style("focus"))
            line.append(pick.label.ljust(width), style=theme.style("focus" if here else "text"))
            if pick.note:
                line.append(f"   {pick.note}", style=theme.style("dim"))
            lines.append(line)
        return lines

    def shown(self) -> str:
        return self.options[self.at].label

    def press(self, key: str, char: str | None) -> Any:
        if key in ("up", "k"):
            self.at = (self.at - 1) % len(self.options)
        elif key in ("down", "j", "tab"):
            self.at = (self.at + 1) % len(self.options)
        elif key == "enter":
            return self.finish(self.options[self.at].value)
        elif key == "escape":
            return None
        return PENDING


class _Text(Field):
    keys = (("enter", "confirm"), ("esc", "back"))

    def __init__(self, title: str, *, secret: bool, default: str) -> None:
        super().__init__(title)
        self.secret = secret
        self.default = default
        self.typed = ""

    def masked(self, fancy: bool) -> str:
        return theme.glyph("bullet", fancy) * len(self.typed) if self.secret else self.typed

    def body(self, fancy: bool) -> list[Any]:
        from rich.text import Text

        line = Text("> ", style=theme.style("focus"))
        line.append(self.masked(fancy), style=theme.style("text"))
        line.append(" ", style="reverse")
        if not self.typed and self.default:
            line.append(self.default, style=theme.style("dim"))
        return [line]

    def done(self, fancy: bool) -> list[Any]:
        from rich.text import Text

        shown = self.masked(fancy) if self.typed else self.answer
        return [Text(f"> {shown}", style=theme.style("good"))]

    def press(self, key: str, char: str | None) -> Any:
        if key == "enter":
            return self.finish(self.typed or self.default)
        if key == "escape":
            return None
        if key == "backspace":
            self.typed = self.typed[:-1]
        elif key == "ctrl+u":
            self.typed = ""
        elif char:
            self.typed += char
        return PENDING

    def paste(self, text: str) -> None:
        self.typed += " ".join(text.splitlines())


class _Confirm(Field):
    keys = (("leftright", "choose"), ("y", "yes"), ("n", "no"), ("enter", "confirm"),
            ("esc", "back"))

    def __init__(self, title: str, *, default: bool) -> None:
        super().__init__(title)
        self.yes = default

    def body(self, fancy: bool) -> list[Any]:
        from rich.text import Text

        line = Text()
        for label, on in (("Yes", self.yes), ("No", not self.yes)):
            style = theme.style("bold text on focus" if on else "dim")
            line.append(f" {label} ", style=style)
            line.append("  ")
        return [line]

    def shown(self) -> str:
        return "Yes" if self.answer else "No"

    def press(self, key: str, char: str | None) -> Any:
        if key in ("left", "right", "up", "down", "tab", "h", "l"):
            self.yes = not self.yes
        elif key == "y":
            return self.finish(True)
        elif key == "n":
            return self.finish(False)
        elif key == "enter":
            return self.finish(self.yes)
        elif key == "escape":
            return False
        return PENDING


def run(owner: Any, *, keep: bool) -> Any:
    """Show `owner` under the prompt until it answers, and return the answer.

    Args:
        keep: leave the answer on the terminal (a form's); esc and Ctrl-C always clear it.

    Raises:
        Stopped: Ctrl-C.
    """
    with _paused():
        result = _plain(owner)
        # Printed while a run's screen is still off the terminal, so it draws again below.
        if result is not _STOP and keep and owner.answered:
            from rich.console import Console

            console = Console(file=sys.stdout, highlight=False,
                              color_system="auto" if theme.color(sys.stdout) else None)
            console.print(owner.view(theme.unicode(sys.stdout)))
    if result is _STOP:
        raise Stopped("stopped by Ctrl-C")
    return result


# ---------------------------------------------------------------------------------------------
# The terminal: an owner drawn with Rich, keys read raw from standard input.
# ---------------------------------------------------------------------------------------------

# What a terminal sends for each key the owners know, beside printable text.
_SEQUENCES = {"\x1b[A": "up", "\x1b[B": "down", "\x1b[C": "right", "\x1b[D": "left",
              "\x1bOA": "up", "\x1bOB": "down", "\x1bOC": "right", "\x1bOD": "left",
              "\x1b[5~": "pageup", "\x1b[6~": "pagedown", "\x1b[H": "home", "\x1b[F": "end"}
_CONTROLS = {"\r": "enter", "\n": "enter", "\x7f": "backspace", "\x08": "backspace",
             "\x03": "ctrl+c", "\x04": "ctrl+d", "\x15": "ctrl+u", "\t": "tab", "\x1b": "escape"}


# Windows sends \x00 or \xe0, then one of these, for a special key.
_WINDOWS = {"H": "up", "P": "down", "K": "left", "M": "right", "G": "home", "O": "end",
            "I": "pageup", "Q": "pagedown"}


def _wkeys() -> list[tuple[str, str | None]]:
    """The next key from the Windows console, as `_keys` names it."""
    import msvcrt

    char = msvcrt.getwch()
    if char in ("\x00", "\xe0"):
        return [(_WINDOWS[code], None)] if (code := msvcrt.getwch()) in _WINDOWS else []
    if char in _CONTROLS:
        return [(_CONTROLS[char], None)]
    return [(char, char if char.isprintable() else None)]


def _terminal_size() -> tuple[int, int]:
    """Live terminal `(cols, rows)`, preferring `os.get_terminal_size` over stale env vars."""
    import os
    import shutil

    for fd in (1, 2, 0):
        try:
            cols, rows = os.get_terminal_size(fd)
        except (AttributeError, ValueError, OSError):
            continue
        if cols > 0 and rows > 0:
            return cols, rows
    cols, rows = shutil.get_terminal_size()
    return max(1, cols), max(1, rows)


def _plain(owner: Any) -> Any:
    """Show `owner` until it answers; its frame is cleared at the end."""
    stream = sys.stdout
    fancy = theme.unicode(stream)
    drawn: list[int] = []
    result: Any = PENDING
    with _raw() as read:
        try:
            stream.write("\x1b[?25l")
            while result is PENDING:
                owner.height = _terminal_size()[1]
                drawn = _frame(stream, owner.view(fancy), drawn)
                for key, char in read():
                    if key == "ctrl+c":
                        result = _STOP
                    elif key == "paste":
                        owner.paste(char)
                    else:
                        result = owner.press(key, char)
                    if result is not PENDING:
                        break
        finally:
            _frame(stream, None, drawn)
            stream.write("\x1b[?25h")
            stream.flush()
    return result


@contextmanager
def _raw() -> Iterator[Callable[[], list[tuple[str, str | None]]]]:
    """Put the terminal in raw mode (no echo; Ctrl-C arrives as a key, not a signal), and give
    the function that reads the next keys; the terminal's modes are restored at the end."""
    if sys.platform == "win32":
        yield _wkeys
        return
    import select
    import termios
    import tty

    fd = sys.stdin.fileno()
    saved = termios.tcgetattr(fd)
    try:
        # TCSANOW keeps keys typed ahead, which the default TCSAFLUSH would drop.
        tty.setraw(fd, termios.TCSANOW)
        yield lambda: _keys(fd)
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, saved)
        # Back in canonical mode, macOS marks the input PENDIN until it is next polled: poll it
        # now, so the terminal's modes read exactly as they were.
        select.select([fd], [], [], 0)


def _frame(stream: Any, renderable: Any, drawn: list[int] | int = ()) -> list[int]:
    """Draw `renderable` over the `drawn` lines of the last frame (None: erase them)."""
    import io

    from rich.console import Console
    from rich.text import Text

    cols, rows_h = _terminal_size()
    cols, rows_h = max(1, cols), max(1, rows_h)
    if drawn:
        widths = [1] * drawn if isinstance(drawn, int) else list(drawn)
        visual = sum(max(1, (w - 1) // cols + 1) if w > 0 else 1 for w in widths)
        up = max(0, min(visual, rows_h) - 1)
        stream.write(f"\x1b[{up}A\r\x1b[J" if up > 0 else "\r\x1b[J")
    lines: list[str] = []
    if renderable is not None:
        console = Console(file=io.StringIO(), force_terminal=True, highlight=False,
                          width=cols,
                          color_system="auto" if theme.color(stream) else None)
        with console.capture() as captured:
            console.print(renderable)
        lines = captured.get().rstrip("\n").split("\n")
        # Raw mode doesn't turn a line break into a carriage return too.
        stream.write("\r\n".join(lines))
    stream.flush()
    return [Text.from_ansi(line).cell_len for line in lines]


def _keys(fd: int) -> list[tuple[str, str | None]]:
    """The keys in what the terminal sends next; printable text of more than one character is
    one ("paste", text)."""
    import os
    import select

    data = os.read(fd, 1024)
    # A lone escape may be the start of a sequence still on its way.
    while data.endswith(b"\x1b") and select.select([fd], [], [], 0.03)[0]:
        data += os.read(fd, 1024)
    text = data.decode(errors="replace")
    if len(text) > 1 and text.isprintable():
        return [("paste", text)]
    keys: list[tuple[str, str | None]] = []
    at = 0
    while at < len(text):
        sequence = next((s for s in _SEQUENCES if text.startswith(s, at)), None)
        if sequence is not None:
            keys.append((_SEQUENCES[sequence], None))
            at += len(sequence)
        elif text.startswith("\x1b[", at):
            # An unknown sequence: skipped up to its final letter.
            end = at + 2
            while end < len(text) and not text[end].isalpha() and text[end] != "~":
                end += 1
            at = end + 1
        elif text[at] in _CONTROLS:
            keys.append((_CONTROLS[text[at]], None))
            at += 1
        else:
            keys.append((text[at], text[at] if text[at].isprintable() else None))
            at += 1
    return keys


@contextmanager
def _paused() -> Iterator[None]:
    """Hold the run's screen while a question uses the terminal; nothing when none is drawn."""
    screen = sys.modules.get("flnd.ui.screen")
    if screen is None:
        yield
        return
    with screen.paused():
        yield
