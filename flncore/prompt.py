"""Questions in the terminal (questionary, Charm style)."""

from __future__ import annotations

import sys
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from typing import Any

import questionary

from flncore.errors import UsageError
from flncore.log import log

__all__ = ["ask", "choose", "confirm", "interactive"]

CHARM_STYLE = questionary.Style([
    ("qmark", "fg:#7D84D6 bold"),
    ("question", "fg:#D0D0D0 bold"),
    ("answer", "fg:#02BF87 bold"),
    ("pointer", "fg:#7D84D6 bold"),
    ("highlighted", "fg:#7D84D6 bold"),
    ("selected", "fg:#02BF87"),
    ("separator", "fg:#767676"),
    ("instruction", "fg:#767676"),
    ("text", "fg:#D0D0D0"),
])
QMARK = "•"
POINTER = "❯"


def interactive() -> bool:
    """True when standard input is a terminal."""
    stdin = sys.stdin
    return bool(stdin and stdin.isatty())


def ask(message: str, *, secret: bool = False, default: str | None = None) -> str:
    """Ask for one line of text; off a terminal, return `default` (required when set)."""
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
    """Ask a yes-or-no question; off a terminal, return `default`."""
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
    """Ask to pick one of `choices` by number or label."""
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