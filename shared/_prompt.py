"""Terminal prompts backed by `questionary`, with `ContextVar`-isolated defaults.

    name = prompt.ask("Name", default="ada")
    ok = prompt.confirm("Continue?", default=True)
"""

from __future__ import annotations

import sys
from collections.abc import Iterable, Iterator, Mapping, Sequence
from contextlib import AbstractContextManager, contextmanager, nullcontext
from contextvars import ContextVar
from typing import Any, Callable, TypeVar, cast, overload

import questionary

import shared

__all__ = ["Error", "ask", "choose", "choose_many", "confirm", "defaults"]

T = TypeVar("T")
Validate = Callable[[Any], "str | bool | None"]
_defaults_depth: ContextVar[int] = ContextVar("prompt_defaults", default=0)


class Error(shared.Error):
    """A prompt that cannot be asked, such as missing choices or no default off-TTY."""


def ask(
    message: str,
    *,
    secret: bool = False,
    default: str | None = None,
    validate: Validate | None = None,
    description: str = "",
) -> str:
    """Ask for a line of text, hiding characters when `secret` is true."""
    if _use_defaults():
        return _require_default(message, default)
    fn = questionary.password if secret else questionary.text
    kwargs: dict[str, Any] = {
        "default": default or "",
        "validate": _validator(validate),
    }
    if description:
        kwargs["instruction"] = description
    return _run(fn(message, **kwargs))


def confirm(message: str, *, default: bool = True) -> bool:
    """Ask a yes-or-no question and return the boolean answer."""
    if _use_defaults():
        return default
    return bool(_run(questionary.confirm(message, default=default)))


@overload
def choose(
    message: str, choices: Mapping[Any, T], *, default: T | None = None
) -> T: ...
@overload
def choose(
    message: str, choices: Sequence[T], *, default: T | None = None
) -> T: ...
def choose(
    message: str, choices: Sequence[Any] | Mapping[Any, Any], *, default: Any = None
) -> Any:
    """Ask to pick one item from `choices`, returning its value."""
    pairs = _choice_pairs(message, choices)
    if default is not None and not any(value == default for _, value in pairs):
        raise Error(f"{default!r} is not a choice for {message!r}")
    if _use_defaults():
        return _require_default(message, default)
    items = [questionary.Choice(title=str(label), value=value) for label, value in pairs]
    initial = next((item for item in items if item.value == default), None)
    return _run(questionary.select(message, choices=items, default=initial))


@overload
def choose_many(
    message: str, choices: Mapping[Any, T], *, defaults: Iterable[T] = ()
) -> list[T]: ...
@overload
def choose_many(
    message: str, choices: Sequence[T], *, defaults: Iterable[T] = ()
) -> list[T]: ...
def choose_many(
    message: str,
    choices: Sequence[Any] | Mapping[Any, Any],
    *,
    defaults: Iterable[Any] = (),
) -> list[Any]:
    """Ask to pick zero or more items from `choices`, returning selected values."""
    pairs = _choice_pairs(message, choices)
    selected = list(defaults)
    values = [value for _, value in pairs]
    for item in selected:
        if item not in values:
            raise Error(f"{item!r} is not a choice for {message!r}")
    if _use_defaults():
        return selected
    items = [
        questionary.Choice(title=str(label), value=value, checked=value in selected)
        for label, value in pairs
    ]
    return list(_run(questionary.checkbox(message, choices=items)))


@contextmanager
def defaults() -> Iterator[None]:
    """Answer every prompt inside the block with its default without reading input."""
    token = _defaults_depth.set(_defaults_depth.get() + 1)
    try:
        yield
    finally:
        _defaults_depth.reset(token)


def _use_defaults() -> bool:
    """Whether prompts should return their default immediately."""
    return _defaults_depth.get() > 0 or not _interactive()


def _interactive() -> bool:
    """Whether standard input and output are both attached to a terminal."""
    stdin, stdout = sys.stdin, sys.stdout
    return bool(stdin and stdin.isatty() and stdout and stdout.isatty())


def _require_default(message: str, default: T | None) -> T:
    """Return `default` or raise `Error` when a non-interactive prompt has none."""
    if default is None:
        raise Error(f"{message!r} has no default and cannot be asked here")
    return default


def _choice_pairs(
    message: str, choices: Sequence[Any] | Mapping[Any, Any]
) -> list[tuple[Any, Any]]:
    """Normalize a sequence or mapping of choices into `(label, value)` pairs."""
    pairs = list(choices.items()) if isinstance(choices, Mapping) else [(c, c) for c in choices]
    if not pairs:
        raise Error(f"{message!r} has no choices")
    return pairs


def _validator(validate: Validate | None) -> Callable[[str], bool | str] | None:
    """Adapt a `str | None` validator into `questionary`'s `True | False | str` contract."""
    if validate is None:
        return None

    def check(text: str) -> bool | str:
        verdict = validate(text)
        if verdict is None or verdict is True:
            return True
        if verdict is False:
            return False
        return str(verdict)

    return check


def _live_paused() -> AbstractContextManager[Any]:
    """Pause `shared._ui`'s live display if `_ui` has already been imported."""
    ui_mod = sys.modules.get("shared._ui")
    pause = getattr(getattr(ui_mod, "ui", None), "pause_live", None)
    return cast(AbstractContextManager[Any], pause()) if callable(pause) else nullcontext()


def _run(question: questionary.Question) -> Any:
    """Run a `questionary` prompt while any active live UI is paused."""
    with _live_paused():
        try:
            return question.unsafe_ask()
        except EOFError as error:
            raise Error("unexpected end of input") from error
