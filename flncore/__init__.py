"""flncore: a kit for any program that fetches things from the web."""

from __future__ import annotations

import importlib
from typing import Any

_HOMES = {
    "Blocked": "flnd.errors",
    "Changed": "flnd.errors",
    "DiskError": "flnd.errors",
    "Error": "flnd.errors",
    "Fake": "flnd.kit.clock",
    "Loop": "flnd.kit.loop",
    "NetworkError": "flnd.errors",
    "NotFound": "flnd.errors",
    "Part": "flnd.kit.files",
    "Run": "flncore.run",
    "SiteError": "flnd.errors",
    "Stop": "flncore.run",
    "Stopped": "flnd.errors",
    "ToolError": "flnd.errors",
    "UsageError": "flnd.errors",
    "App": "flncore.cli",
    "ask": "flnd.ui.forms",
    "choose": "flnd.ui.forms",
    "confirm": "flnd.ui.forms",
    "day": "flnd.kit.clock",
    "dig": "flnd.kit.dig",
    "filename": "flnd.kit.files",
    "log": "flnd.ui.log",
    "now": "flnd.kit.clock",
    "part": "flnd.kit.files",
    "pause": "flnd.kit.clock",
    "sleep": "flnd.kit.clock",
    "use": "flnd.kit.clock",
    "wall": "flnd.kit.clock",
}

__all__ = sorted(_HOMES.keys())


def __getattr__(name: str) -> Any:
    if name not in _HOMES:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    module = importlib.import_module(_HOMES[name])
    if name == _HOMES[name].split(".")[-1]:
        value = module
    else:
        value = getattr(module, name)
    globals()[name] = value
    return value
