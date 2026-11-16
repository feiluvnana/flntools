"""`Sink`: what a pool or a transfer reports progress to; `ui.Row` is one.

    def copy(src, dest, progress: Sink | None = None):
        progress.total = src.stat().st_size
        progress.advance(len(chunk))
"""

from __future__ import annotations

from typing import Any, Protocol

__all__ = ["Sink"]


class Sink(Protocol):
    """A progress sink: a settable `total`, and `advance(n)` for each step done."""

    @property
    def total(self) -> float | None: ...

    @total.setter
    def total(self, value: float | None) -> None: ...

    def advance(self, n: float) -> Any: ...
