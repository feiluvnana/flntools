"""Small helpers for scripts.

The terminal modules are stdlib only and load at once. The others load on first use,
so `from shared import log` never imports Playwright, parsel, or cryptography.
A module whose extra is missing raises an ImportError that names the extra to install.
"""

from __future__ import annotations

import importlib
from typing import TYPE_CHECKING, Any

from shared import _cli as cli
from shared import _fmt as fmt
from shared import _prompt as prompt
from shared._log import log
from shared._style import style
from shared._theme import theme

if TYPE_CHECKING:
    # Checkers see the real modules; at runtime they still load on first use.
    from shared import _archive as archive
    from shared import _browser as browser
    from shared import _cookies as cookies
    from shared import _crypto as crypto
    from shared import _kv as kv
    from shared import _selector as selector
    from shared import _worker as worker

LAZY_MODULES = ("archive", "crypto", "cookies", "kv", "selector", "browser", "worker")

__all__ = [
    "cli", "fmt", "log", "prompt", "style", "theme",
    "archive", "crypto", "cookies", "kv", "selector", "browser", "worker",
]


def __getattr__(name: str) -> Any:
    if name not in LAZY_MODULES:
        raise AttributeError(f"module 'shared' has no attribute {name!r}")
    module = importlib.import_module(f"shared._{name}")
    globals()[name] = module
    return module


def __dir__() -> list[str]:
    return sorted(__all__)
