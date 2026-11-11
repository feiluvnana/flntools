"""Small helpers for scripts.

Phase 1 (the terminal) is stdlib only and loads at once. The Phase 3 modules load on
first use, so `from shared import log` never imports httpx, Playwright, or cryptography.
A module whose extra is missing raises an ImportError that names the extra to install.
"""

from __future__ import annotations

import importlib
from typing import Any

from shared import _cli as cli
from shared import _fmt as fmt
from shared import _prompt as prompt
from shared._log import log
from shared._style import style
from shared._theme import theme

LAZY_MODULES = ("path", "archive", "crypto", "store", "worker", "scrape", "http", "browser")

__all__ = ["cli", "fmt", "log", "prompt", "style", "theme", *LAZY_MODULES]


def __getattr__(name: str) -> Any:
    if name not in LAZY_MODULES:
        raise AttributeError(f"module 'shared' has no attribute {name!r}")
    module = importlib.import_module(f"shared._{name}")
    globals()[name] = module
    return module


def __dir__() -> list[str]:
    return sorted(__all__)
