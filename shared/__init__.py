"""Small helpers for scripts.

Phase 1 (the terminal) is stdlib only and loads at once. The Phase 3 modules load on
first use, so `from shared import log` never imports httpx, Playwright, or cryptography.
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
    from shared import _crypto as crypto
    from shared import _http as http
    from shared import _path as path
    from shared import _scrape as scrape
    from shared import _store as store
    from shared import _worker as worker

LAZY_MODULES = ("path", "archive", "crypto", "store", "worker", "scrape", "http", "browser")

__all__ = [
    "cli", "fmt", "log", "prompt", "style", "theme",
    "path", "archive", "crypto", "store", "worker", "scrape", "http", "browser",
]


def __getattr__(name: str) -> Any:
    if name not in LAZY_MODULES:
        raise AttributeError(f"module 'shared' has no attribute {name!r}")
    module = importlib.import_module(f"shared._{name}")
    globals()[name] = module
    return module


def __dir__() -> list[str]:
    return sorted(__all__)
