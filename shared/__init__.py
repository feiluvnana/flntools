"""Small helpers for scripts.

The terminal modules are stdlib only and load at once. The others load on first use,
so `from shared import log` never imports Playwright, httpx, parsel, cryptography, or Pillow.
A module whose extra is missing raises an ImportError that names the extra to install.

Every error `shared` raises is a `shared.Error`. Each module has its own `Error` under it, with
the module's specific errors below that, and an error about untrusted, damaged or refused input
is also a `shared.UnsafeError`, `shared.BrokenError` or `shared.RefusedError`. So a caller picks
how wide to catch:

    try:
        archive.unzip("photos.zip", "photos/")
    except shared.UnsafeError:          # this kind of problem, from any module
        ...
    except archive.Error:               # anything from this one module
        ...
"""

from __future__ import annotations

import importlib
from typing import TYPE_CHECKING, Any


# The four bases come before the module imports below: each module subclasses them as it loads.
class Error(Exception):
    """The base of every error `shared` raises; each module's own `Error` subclasses it."""


class UnsafeError(Error, ValueError):
    """Input refused because trusting it could do harm: path traversal, a decompression bomb, a decoder exploit.

    A ValueError, so code written before the error tree still catches it.
    """


class BrokenError(Error, ValueError):
    """Input that is damaged or isn't what it claims: a cut-off video, a bad archive.

    A ValueError, so code written before the error tree still catches it.
    """


class RefusedError(Error):
    """A remote end or a person said no: an HTTP 4xx, a wrong password."""


class MissingError(Error, RuntimeError):
    """A program, an extra or a browser this machine lacks: ffmpeg, unrar, Chrome."""


from shared import _cli as cli
from shared import _clock as clock
from shared import _fmt as fmt
from shared import _prompt as prompt
from shared._log import log
from shared._style import style
from shared._theme import theme

if TYPE_CHECKING:
    # Checkers see the real modules; at runtime they still load on first use.
    from shared import _archive as archive
    from shared import _atomic as atomic
    from shared import _browser as browser
    from shared import _bundler as bundler
    from shared import _cookies as cookies
    from shared import _crypto as crypto
    from shared import _http as http
    from shared import _kv as kv
    from shared import _media as media
    from shared import _selector as selector
    from shared import _worker as worker

LAZY_MODULES = (
    "archive", "atomic", "crypto", "cookies", "kv", "selector", "http", "browser", "worker", "bundler", "media",
)

__all__ = [
    "Error", "UnsafeError", "BrokenError", "RefusedError", "MissingError",
    "cli", "clock", "fmt", "log", "prompt", "style", "theme",
    "archive", "atomic", "crypto", "cookies", "kv", "selector", "http", "browser", "worker", "bundler", "media",
]


def __getattr__(name: str) -> Any:
    if name not in LAZY_MODULES:
        raise AttributeError(f"module 'shared' has no attribute {name!r}")
    module = importlib.import_module(f"shared._{name}")
    globals()[name] = module
    return module


def __dir__() -> list[str]:
    return sorted(__all__)
