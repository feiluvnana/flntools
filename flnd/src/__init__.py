"""flnd: a media downloader, as a library.

COOKBOOK.md#python explains it."""

from __future__ import annotations

import importlib
from typing import TYPE_CHECKING, Any

__version__ = "0.2.0"

# Where each public name is defined. A name is imported from its module on first use, then
# kept in this module's globals, so `__getattr__` runs once per name.
_HOMES = {
    "Flnd": "flnd._library",
    "open": "flnd._library",
    "Run": "flnd.engine.run",
    "Account": "flnd.plugin",
    "Checked": "flnd.engine.events",
    "Event": "flnd.engine.events",
    "Failed": "flnd.engine.events",
    "Failure": "flnd.engine.events",
    "FileState": "flnd.engine.events",
    "HoldState": "flnd.engine.events",
    "Installed": "flnd.engine.events",
    "Phase": "flnd.engine.events",
    "Planned": "flnd.engine.events",
    "Record": "flnd.engine.events",
    "Report": "flnd.engine.events",
    "Saved": "flnd.engine.events",
    "Skipped": "flnd.engine.events",
    "State": "flnd.engine.events",
    "Status": "flnd.engine.events",
    "TargetState": "flnd.engine.events",
    "Tracked": "flnd.engine.events",
    "Target": "flnd.plugin",
    "Blocked": "flnd.errors",
    "Changed": "flnd.errors",
    "DiskError": "flnd.errors",
    "Error": "flnd.errors",
    "Hidden": "flnd.errors",
    "LoginError": "flnd.errors",
    "NetworkError": "flnd.errors",
    "NotFound": "flnd.errors",
    "PluginError": "flnd.errors",
    "SiteError": "flnd.errors",
    "Stopped": "flnd.errors",
    "ToolError": "flnd.errors",
    "UsageError": "flnd.errors",
}

# Written out, not built from `_HOMES`, so linters and editors can read it; a test checks that
# the two agree.
__all__ = [
    "Account", "Blocked", "Changed", "Checked", "DiskError", "Error", "Event", "Failed",
    "Failure", "FileState", "Flnd", "Hidden", "HoldState", "Installed", "LoginError",
    "NetworkError", "NotFound", "Phase", "Planned", "PluginError", "Record", "Report", "Run",
    "Saved", "SiteError", "Skipped", "State", "Status", "Stopped", "Target", "TargetState",
    "ToolError", "Tracked", "UsageError", "__version__", "open",
]

if TYPE_CHECKING:
    from flnd._library import Flnd, open
    from flnd.engine.events import (
        Checked,
        Event,
        Failed,
        Failure,
        FileState,
        HoldState,
        Installed,
        Phase,
        Planned,
        Record,
        Report,
        Saved,
        Skipped,
        State,
        Status,
        TargetState,
        Tracked,
    )
    from flnd.engine.run import Run
    from flnd.errors import (
        Blocked,
        Changed,
        DiskError,
        Error,
        Hidden,
        LoginError,
        NetworkError,
        NotFound,
        PluginError,
        SiteError,
        Stopped,
        ToolError,
        UsageError,
    )
    from flnd.plugin import Account, Target


def __getattr__(name: str) -> Any:
    """Import a public name from the module that defines it, on its first use.

    Args:
        name: the attribute asked for.

    Returns:
        The object, which is also kept in this module, so the next use is a plain lookup.

    Raises:
        AttributeError: `name` isn't one of flnd's public names (nor a submodule).
    """
    home = _HOMES.get(name)
    if home is None:
        raise AttributeError(f"module 'flnd' has no attribute {name!r}")
    value = getattr(importlib.import_module(home), name)
    globals()[name] = value
    return value


def __dir__() -> list[str]:
    """The public names, with what is already loaded, for `dir(flnd)` and completion."""
    return sorted({*globals(), *_HOMES})
