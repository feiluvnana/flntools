"""flncore: a kit for any program that fetches things from the web."""

from __future__ import annotations

import importlib
from typing import Any

_HOMES = {
    "Blocked": "flncore.errors",
    "Changed": "flncore.errors",
    "DiskError": "flncore.errors",
    "Error": "flncore.errors",
    "Fake": "flncore.clock",
    "Loop": "flncore.loop",
    "NetworkError": "flncore.errors",
    "NotFound": "flncore.errors",
    "Part": "flncore.files",
    "Run": "flncore.run",
    "SiteError": "flncore.errors",
    "Stop": "flncore.run",
    "Stopped": "flncore.errors",
    "ToolError": "flncore.errors",
    "UsageError": "flncore.errors",
    "App": "flncore.cli",
    "ask": "flncore.prompt",
    "choose": "flncore.prompt",
    "confirm": "flncore.prompt",
    "day": "flncore.clock",
    "dig": "flncore.dig",
    "filename": "flncore.files",
    "log": "flncore.log",
    "now": "flncore.clock",
    "part": "flncore.files",
    "pause": "flncore.clock",
    "sleep": "flncore.clock",
    "use": "flncore.clock",
    "wall": "flncore.clock",
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
