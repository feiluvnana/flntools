"""Small helpers for scripts.

Stdlib modules (`clock`, `text`) load at once. The others load on first use,
so `from shared import text` never imports Rich, Playwright, httpx, parsel, cryptography, or Pillow.
A module whose extra is missing raises an ImportError that names the extra to install.

Every error `shared` raises is a `shared.Error`. Each module has its own `Error` under it, with
the module's specific errors below that, and an error about untrusted, damaged or refused input
is also a `shared.UnsafeError`, `shared.BrokenError` or `shared.RefusedError`. So a caller picks
how wide to catch:

    try:
        archive.unpack("photos.zip", "photos/")
    except shared.UnsafeError:
        ...
"""

from __future__ import annotations

import importlib
from typing import TYPE_CHECKING, Any


# The four bases come before the module imports below: each module subclasses them as it loads.
class Error(Exception):
    """The base of every error `shared` raises; each module's own `Error` subclasses it."""


class UnsafeError(Error, ValueError):
    """Input refused because trusting it could do harm: traversal, bomb, or exploit.

    A ValueError, so callers expecting ValueError still catch it.
    """


class BrokenError(Error, ValueError):
    """Input that is damaged or isn't what it claims: a cut-off video, a bad archive.

    A ValueError, so callers expecting ValueError still catch it.
    """


class RefusedError(Error):
    """A remote end or a person said no: an HTTP 4xx, a wrong password."""


class MissingError(Error, RuntimeError):
    """A program, an extra or a browser this machine lacks: ffmpeg, unrar, Chrome."""


from shared import _clock as clock
from shared import _text as text

if TYPE_CHECKING:
    # Checkers see the real modules; at runtime they still load on first use.
    from shared import _archive as archive
    from shared import _browser as browser
    from shared import _bundler as bundler
    from shared import _cli as cli
    from shared import _cookies as cookies
    from shared import _crypto as crypto
    from shared import _fs as fs
    from shared import _http as http
    from shared import _prompt as prompt
    from shared import _selector as selector
    from shared import _sh as sh
    from shared import _store as store
    from shared._ui import ui as ui
    from shared import _worker as worker

LAZY_MODULES = (
    "ui", "prompt", "cli", "fs", "sh", "store", "worker",
    "http", "cookies", "browser", "selector",
    "crypto", "archive", "bundler",
)

__all__ = [
    "Error", "UnsafeError", "BrokenError", "RefusedError", "MissingError",
    "clock", "text",
    "ui", "prompt", "cli", "fs", "sh", "store", "worker",
    "http", "cookies", "browser", "selector",
    "crypto", "archive", "bundler",
]


def __getattr__(name: str) -> Any:
    if name not in LAZY_MODULES:
        raise AttributeError(f"module 'shared' has no attribute {name!r}")
    module = importlib.import_module(f"shared._{name}")
    value = module.ui if name == "ui" else module
    globals()[name] = value
    return value


def __dir__() -> list[str]:
    return sorted(__all__)
