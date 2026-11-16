"""Questions in the terminal: a line of text, a secret, yes or no, and numbered choices.

    name = _prompt.ask("Name", default="example_user")
    if _prompt.confirm("Download Chromium now", default=False): ...
    how = _prompt.choose("Log in with", {"cookies from chrome": "chrome", "a cookie file": "file"})

A library module: it imports only the stdlib and `flnd.errors`, so any program can ask through
it. Who calls it in flnd: the CLI (`flnd login`'s way), a site's login questions (tlg's phone and
code), and the browser (whether to download Chromium).

How a question goes:
1. Off a terminal (standard input isn't one), nobody can answer: the question returns its
   default, or raises `UsageError` when it has none, so a cron job never hangs on a question.
2. On a terminal, the question goes to standard error, so standard output stays clean for what
   a command prints (a pipe, `--json`); the answer is one line of standard input, or a hidden
   one (`getpass`) for a secret.
3. A choice lists its options numbered from 1 and takes a number or a label; a wrong answer asks
   again. Input that ends (Ctrl-D) is a `UsageError`.

Rule a change must keep: a question never runs under a live display (`_paused`), which would
draw over it. A display that covers the whole terminal (a full-screen view, which also reads
keys) says so with `covering`, and is stopped for the question too.
"""

from __future__ import annotations

import getpass
import sys
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from typing import Any

from flnd.errors import UsageError

__all__ = ["ask", "choose", "confirm", "covering", "interactive"]

# The displays covering the terminal now (`covering`), innermost last: each has `stop()` and
# `start()`, and a question stops them first.
_covers: list[Any] = []


@contextmanager
def covering(display: Any) -> Iterator[None]:
    """Say that `display` covers the terminal for the block, so a question stops it first.

    A full-screen view takes the screen and the keys: a question asked under it (from any
    thread) would be neither seen nor typed. `display.stop()` gives the terminal back before
    the question, and `display.start()` takes it again after.

    Args:
        display: anything with `stop()` and `start()`.

    Yields:
        Nothing; the display is known for the block.
    """
    _covers.append(display)
    try:
        yield
    finally:
        _covers.remove(display)

def interactive() -> bool:
    """Whether a person can answer a prompt: standard input is a terminal.

    Returns:
        True when standard input is a terminal.

    Example:
        >>> import io, unittest.mock
        >>> with unittest.mock.patch("sys.stdin", io.StringIO("")):
        ...     interactive()
        False
    """
    stdin = sys.stdin
    return bool(stdin and stdin.isatty())


def ask(message: str, *, secret: bool = False, default: str | None = None) -> str:
    """Ask for one line of text, with the characters hidden when `secret` is true.

    Off a terminal it answers with `default`, which must then be given.

    Args:
        message: the question, without a trailing colon.
        secret: hide what is typed (a password, a code), with `getpass`.
        default: the answer when the line is left empty, or when nobody can be asked; None
            for no default.

    Returns:
        What was typed, without its line end; `default` for an empty line.

    Raises:
        UsageError: no terminal and no default, or the input ended (Ctrl-D).
    """
    if not interactive():
        return _required(message, default)
    shown = f"{message} [{default}]: " if default else f"{message}: "
    with _paused():
        try:
            answer = getpass.getpass(shown) if secret else _line(shown)
        except EOFError:
            raise UsageError("the input ended before an answer") from None
    return answer or default or ""


def confirm(message: str, *, default: bool = True) -> bool:
    """Ask a yes-or-no question; Enter takes `default`, and off a terminal so does everything.

    Args:
        message: the question, without a question mark.
        default: the answer for an empty line, or when nobody can be asked.

    Returns:
        True for yes.

    Raises:
        UsageError: the input ended.
    """
    if not interactive():
        return default
    shown = f"{message}? [{'Y/n' if default else 'y/N'}] "
    while True:
        with _paused():
            try:
                answer = _line(shown).strip().lower()
            except EOFError:
                raise UsageError("the input ended before an answer") from None
        if not answer:
            return default
        if answer in ("y", "yes", "n", "no"):
            return answer.startswith("y")


def choose(message: str, choices: Sequence[Any] | Mapping[str, Any], *,
           default: Any = None) -> Any:
    """Ask to pick one of `choices` by its number (or its label), and return its value.

    Each choice is printed on its own line as `  1. label`. An empty line takes `default`, and
    so does every prompt off a terminal. A wrong answer asks again.

    Args:
        message: the question.
        choices: labels mapped to the values they stand for, or values that are their own
            labels.
        default: the value an empty line (or no terminal) picks; None for no default.

    Returns:
        The value of the choice picked.

    Raises:
        UsageError: there are no choices, `default` isn't one, nobody can be asked and there
            is no default, or the input ended.
    """
    pairs = list(choices.items()) if isinstance(choices, Mapping) else [(c, c) for c in choices]
    if not pairs:
        raise UsageError(f"{message}: there is nothing to choose from")
    values = [value for _, value in pairs]
    if default is not None and default not in values:
        raise UsageError(f"{default!r} is not a choice for {message!r}")
    if not interactive():
        return _required(message, default)
    labels = [str(label) for label, _ in pairs]
    numbered = "\n".join(f"  {number}. {label}" for number, label in enumerate(labels, 1))
    shown = f"{message} [1-{len(pairs)}]: "
    while True:
        with _paused():
            print(numbered, file=sys.stderr)
            try:
                answer = _line(shown).strip()
            except EOFError:
                raise UsageError("the input ended before an answer") from None
        if not answer and default is not None:
            return default
        if answer.isdigit() and 1 <= int(answer) <= len(pairs):
            return values[int(answer) - 1]
        if answer in labels:
            return values[labels.index(answer)]
        print(f"pick a number from 1 to {len(pairs)}", file=sys.stderr)


def _line(shown: str) -> str:
    """Read one line after showing `shown` on standard error, which keeps standard output for
    what a command prints (a pipe or `--json` reads it).

    Args:
        shown: the question, as it is shown before the cursor.

    Returns:
        The line typed, without its line end.

    Raises:
        EOFError: the input ended.
    """
    sys.stderr.write(shown)
    sys.stderr.flush()
    line = sys.stdin.readline()
    if not line:
        raise EOFError
    return line.rstrip("\r\n")


def _required(message: str, default: Any) -> Any:
    """The default of a prompt nobody can answer; a usage error when there is none."""
    if default is None:
        raise UsageError(f"{message!r} needs an answer, and there is no terminal to ask in",
                         hint="run it in a terminal")
    return default


@contextmanager
def _paused() -> Iterator[None]:
    """Stop the live displays on `_ui`'s console while a prompt asks, and start them after.

    A live view and a progress row draw through `_ui.ui.raw`, and Rich nests one live display in
    another (`Console._live_stack`); a question typed under a redrawing display would be
    overwritten. A display `covering` the terminal counts as the outermost. They stop innermost
    first and start again outermost first. `_ui` is read only
    when something already imported it, so a prompt never loads Rich. A display that won't stop
    or start only costs a redraw, so its errors are ignored.

    Yields:
        Nothing; the displays are stopped for the block.
    """
    module = sys.modules.get("flnd._ui")
    console = getattr(getattr(module, "ui", None), "raw", None)
    lives = [*_covers, *(live for live in getattr(console, "_live_stack", ())
                         if getattr(live, "is_started", False))]
    stopped = []
    for live in reversed(lives):
        try:
            live.stop()
        except Exception:  # noqa: BLE001 - see above: never let a display stop a question
            continue
        stopped.append(live)
    try:
        yield
    finally:
        for live in reversed(stopped):
            try:
                live.start()
            except Exception:  # noqa: BLE001
                pass
