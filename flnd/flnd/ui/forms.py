"""Questions in the terminal (questionary, Charm style).

flnd/docs/cli explains it.
"""

from __future__ import annotations

import sys
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from typing import Any

import questionary

from flnd.errors import UsageError
from flnd.ui import theme
from flnd.ui.log import log

__all__ = ["ask", "choose", "confirm", "interactive"]

_COLOR = theme.TOKENS
CHARM_STYLE = questionary.Style([
    ("qmark", f"fg:{_COLOR['focus']} bold"),
    ("question", f"fg:{_COLOR['text']} bold"),
    ("answer", f"fg:{_COLOR['good']} bold"),
    ("pointer", f"fg:{_COLOR['focus']} bold"),
    ("highlighted", f"fg:{_COLOR['focus']} bold"),
    ("selected", f"fg:{_COLOR['good']}"),
    ("separator", f"fg:{_COLOR['dim']}"),
    ("instruction", f"fg:{_COLOR['dim']}"),
    ("text", f"fg:{_COLOR['text']}"),
])
QMARK = theme.glyph("bullet", True)
POINTER = theme.glyph("cursor", True)


def interactive() -> bool:
    """True when standard input is a terminal."""
    stdin = sys.stdin
    return bool(stdin and stdin.isatty())


def ask(message: str, *, secret: bool = False, default: str | None = None) -> str:
    """Ask for one line of text; off a terminal, return `default` (required when set).

    Args:
        message: the question to show.
        secret: True to hide what is typed.
        default: the answer when nothing is typed, and the answer off a terminal. None means
            there is no default, so off a terminal the call fails.

    Returns:
        The text typed, or `default` (or "") when nothing was typed.

    Raises:
        UsageError: the input ended before an answer, or there is no terminal and no default.

    Example:
        >>> import io
        >>> from unittest import mock
        >>> with mock.patch("sys.stdin", io.StringIO()):
        ...     ask("Account name?", default="example_user")
        'example_user'
    """
    if not interactive():
        return _required(message, default)
    with _paused():
        fn = questionary.password if secret else questionary.text
        try:
            answer = fn(
                message,
                default=default or "",
                qmark=QMARK,
                style=CHARM_STYLE,
            ).unsafe_ask()
        except KeyboardInterrupt:
            raise
        except EOFError as error:
            raise UsageError("the input ended before an answer") from error
        if answer is None:
            raise UsageError("the input ended before an answer")
        return answer or default or ""


def confirm(message: str, *, default: bool = True) -> bool:
    """Ask a yes-or-no question; off a terminal, return `default`.

    Args:
        message: the question to show.
        default: the answer when there is no terminal, and the one shown first.

    Returns:
        True for a yes, False for a no.

    Raises:
        UsageError: the input ended before an answer.

    Example:
        >>> import io
        >>> from unittest import mock
        >>> with mock.patch("sys.stdin", io.StringIO()):
        ...     confirm("Download again?", default=False)
        False
    """
    if not interactive():
        return default
    with _paused():
        try:
            answer = questionary.confirm(
                message,
                default=default,
                qmark=QMARK,
                style=CHARM_STYLE,
            ).unsafe_ask()
        except KeyboardInterrupt:
            raise
        except EOFError as error:
            raise UsageError("the input ended before an answer") from error
        if answer is None:
            raise UsageError("the input ended before an answer")
        return bool(answer)


def choose(message: str, choices: Sequence[Any] | Mapping[str, Any], *,
           default: Any = None) -> Any:
    """Ask to pick one of `choices` by number or label.

    Args:
        message: the question to show.
        choices: the values to pick from, or a dict of label to value to show the labels.
        default: the value picked first, and the answer when there is no terminal. It must be
            one of the values; None means there is no default.

    Returns:
        The value picked.

    Raises:
        UsageError: there are no choices, `default` is not one of them, the input ended
            before an answer, or there is no terminal and no default.

    Example:
        >>> import io
        >>> from unittest import mock
        >>> with mock.patch("sys.stdin", io.StringIO()):
        ...     choose("Quality?", {"high": 2, "low": 1}, default=2)
        2
    """
    pairs = list(choices.items()) if isinstance(choices, Mapping) else [(c, c) for c in choices]
    if not pairs:
        raise UsageError(f"{message}: there is nothing to choose from")
    values = [value for _, value in pairs]
    if default is not None and default not in values:
        raise UsageError(f"{default!r} is not a choice for {message!r}")
    if not interactive():
        return _required(message, default)
    with _paused():
        items = [questionary.Choice(title=str(label), value=val) for label, val in pairs]
        initial = next((item for item in items if item.value == default), None)
        try:
            answer = questionary.select(
                message,
                choices=items,
                default=initial,
                qmark=QMARK,
                pointer=POINTER,
                style=CHARM_STYLE,
            ).unsafe_ask()
        except KeyboardInterrupt:
            raise
        except EOFError as error:
            raise UsageError("the input ended before an answer") from error
        if answer is None:
            raise UsageError("the input ended before an answer")
        return answer


def _required(message: str, default: Any) -> Any:
    """The default off a terminal, or a UsageError when there is none.

    Args:
        message: the question, named in the error.
        default: the answer to use; None means there is none.

    Returns:
        `default`, when it is not None.

    Raises:
        UsageError: `default` is None.
    """
    if default is None:
        raise UsageError(
            f"{message!r} needs an answer, and there is no terminal to ask in",
            hint="run it in a terminal",
        )
    return default


@contextmanager
def _paused() -> Iterator[None]:
    with log.hold():
        yield