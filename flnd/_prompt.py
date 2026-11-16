"""Terminal prompts backed by `questionary`, with `ContextVar`-isolated autofill.

    name = prompt.ask("Name", default="ada")
    ok = prompt.confirm("Continue?", default=True)
"""

from __future__ import annotations

import sys
from collections.abc import Iterable, Iterator, Mapping, Sequence
from contextlib import AbstractContextManager, contextmanager, nullcontext
from contextvars import ContextVar
from types import ModuleType
from typing import Any, Callable, TypeVar, cast

import questionary

from flnd import _base

__all__ = ["Error", "ask", "autofill", "choose", "confirm", "interactive", "raw"]

T = TypeVar("T")
Validate = Callable[[str], bool | str]
_autofill_depth: ContextVar[int] = ContextVar("prompt_autofill", default=0)

raw: ModuleType = questionary
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


class Error(_base.Error):
    """A prompt that cannot be asked, such as missing choices or no default off-TTY."""


def ask(
    message: str,
    *,
    secret: bool = False,
    default: str | None = None,
    validate: Validate | None = None,
) -> str:
    """Ask for a line of text, hiding characters when `secret` is true."""
    if _use_autofill():
        return _require_default(message, default)
    fn = questionary.password if secret else questionary.text
    return _run(fn(
        message, default=default or "", validate=_validator(validate),
        qmark=QMARK, style=CHARM_STYLE,
    ))


def confirm(message: str, *, default: bool = True) -> bool:
    """Ask a yes-or-no question and return the boolean answer."""
    if _use_autofill():
        return default
    return bool(_run(questionary.confirm(
        message, default=default, qmark=QMARK, style=CHARM_STYLE,
    )))


def choose(
    message: str,
    choices: Sequence[Any] | Mapping[str, Any],
    *,
    many: bool = False,
    default: Any = None,
) -> Any:
    """Ask to pick one or more items from `choices`, returning selected value(s)."""
    pairs = _choice_pairs(message, choices)
    if many:
        return _choose_many(message, pairs, () if default is None else default)
    if default is not None and not any(value == default for _, value in pairs):
        raise Error(f"{default!r} is not a choice for {message!r}")
    if _use_autofill():
        return _require_default(message, default)
    items = [questionary.Choice(title=str(label), value=value) for label, value in pairs]
    initial = next((item for item in items if item.value == default), None)
    return _run(questionary.select(
        message, choices=items, default=initial,
        qmark=QMARK, pointer=POINTER, style=CHARM_STYLE,
    ))


def _choose_many(
    message: str, pairs: list[tuple[Any, Any]], default: Iterable[Any]
) -> list[Any]:
    """Ask to pick zero or more items from `pairs`, returning selected values."""
    selected = list(default)
    values = [value for _, value in pairs]
    for item in selected:
        if item not in values:
            raise Error(f"{item!r} is not a choice for {message!r}")
    if _use_autofill():
        return selected
    items = [
        questionary.Choice(title=str(label), value=value, checked=value in selected)
        for label, value in pairs
    ]
    return list(_run(questionary.checkbox(
        message, choices=items, qmark=QMARK, pointer=POINTER, style=CHARM_STYLE,
    )))


@contextmanager
def autofill() -> Iterator[None]:
    """Answer every prompt inside the block with its default without reading input."""
    token = _autofill_depth.set(_autofill_depth.get() + 1)
    try:
        yield
    finally:
        _autofill_depth.reset(token)


def _use_autofill() -> bool:
    """Whether prompts should return their default immediately."""
    return _autofill_depth.get() > 0 or not interactive()


def interactive() -> bool:
    """Whether standard input is a terminal, so a person can answer a prompt."""
    stdin = sys.stdin
    return bool(stdin and stdin.isatty())


def _require_default(message: str, default: T | None) -> T:
    """Return `default` or raise `Error` when a non-interactive prompt has none."""
    if default is None:
        raise Error(f"{message!r} has no default and cannot be asked here")
    return default


def _choice_pairs(
    message: str, choices: Sequence[Any] | Mapping[str, Any]
) -> list[tuple[Any, Any]]:
    """Normalize a sequence or mapping of choices into `(label, value)` pairs."""
    pairs = list(choices.items()) if isinstance(choices, Mapping) else [(c, c) for c in choices]
    if not pairs:
        raise Error(f"{message!r} has no choices")
    return pairs


def _validator(validate: Validate | None) -> Callable[[str], bool | str] | None:
    """Adapt a validator into `questionary`'s `True | False | str` contract."""
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
    """Pause `flnd._ui`'s live display if `_ui` has already been imported."""
    ui_mod = sys.modules.get("flnd._ui")
    ui_obj = getattr(ui_mod, "ui", None)
    pause = getattr(ui_obj, "pause_live", None)
    if callable(pause):
        return cast(AbstractContextManager[Any], pause())
    live = getattr(getattr(ui_obj, "raw", None), "_live", None)
    if live is not None and getattr(live, "is_started", False):
        return _pause_live(live)
    return nullcontext()


@contextmanager
def _pause_live(live: Any) -> Iterator[None]:
    """Temporarily stop and restart a Rich `Live` instance."""
    try:
        live.stop()
    except Exception:
        yield
        return
    try:
        yield
    finally:
        try:
            live.start()
        except Exception:
            pass


def _run(question: questionary.Question) -> Any:
    """Run a `questionary` prompt while any active live UI is paused."""
    with _live_paused():
        try:
            return question.unsafe_ask()
        except EOFError as error:
            raise Error("unexpected end of input") from error
