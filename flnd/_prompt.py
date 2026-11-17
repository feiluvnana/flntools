"""Questions in the terminal: a line of text, a secret, yes or no, and numbered choices."""

from __future__ import annotations

from flncore.prompt import (  # noqa: F401
    CHARM_STYLE,
    POINTER,
    QMARK,
    ask,
    choose,
    confirm,
    interactive,
)

__all__ = ["ask", "choose", "confirm", "interactive"]
