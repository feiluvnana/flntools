"""flnd: a media downloader for Instagram, TikTok, X, Facebook, Telegram and more, as a library.

src/docs/library explains it.

    import flnd

    with flnd.open("~/Media/flnd") as fl:
        for event in fl.download("ins:example_user", only=["reels"]):
            if isinstance(event, flnd.Saved):
                print(event.path)

What it holds: the public names a script uses, and nothing else. `flnd.open(home)` gives a
`Flnd`, whose methods are the CLI's commands; a download returns a `Run`, whose events
(`Saved`, `Skipped`, `Failed`, `Planned`) are the only output, and whose `state`
(a `State`) says what it is doing now; `Report`, `Login`, `Record`, `Tracked`, `Status`,
`Failure`, `Checked` and `Installed` are what the reads return; the errors are the one
hierarchy of `flnd.errors`. A
source is written against `flnd.source` instead (see the docs' guide to writing a source), and
tested with `flnd.testing`.

How it loads: every name is lazy. `import flnd` imports nothing but this file, and the first
use of a name (`flnd.open`, `flnd.Saved`) imports the one module that defines it. The engine,
curl_cffi, Playwright and Rich load only when a command needs them, so a script, the CLI's
`--help` and `python -c "import flnd"` stay fast. `tests/test_packaging.py` guards this.
"""

from __future__ import annotations

import importlib
from typing import TYPE_CHECKING, Any

__version__ = "0.1.0"

# Where each public name is defined. A name is imported from its module on first use, then
# kept in this module's globals, so `__getattr__` runs once per name.
_HOMES = {
    "Flnd": "flnd._library",
    "open": "flnd._library",
    "Run": "flnd.engine.run",
    "Checked": "flnd.engine.events",
    "Event": "flnd.engine.events",
    "Failed": "flnd.engine.events",
    "Failure": "flnd.engine.events",
    "FileState": "flnd.engine.events",
    "HoldState": "flnd.engine.events",
    "Installed": "flnd.engine.events",
    "Login": "flnd.engine.events",
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
    "Target": "flnd.source",
    "Blocked": "flnd.errors",
    "Changed": "flnd.errors",
    "DiskError": "flnd.errors",
    "Error": "flnd.errors",
    "LoginError": "flnd.errors",
    "NetworkError": "flnd.errors",
    "NotFound": "flnd.errors",
    "SiteError": "flnd.errors",
    "SourceError": "flnd.errors",
    "Stopped": "flnd.errors",
    "ToolError": "flnd.errors",
    "UsageError": "flnd.errors",
}

# Written out, not built from `_HOMES`, so linters and editors can read it; a test checks that
# the two agree.
__all__ = [
    "Blocked", "Changed", "Checked", "DiskError", "Error", "Event", "Failed", "Failure",
    "FileState", "Flnd", "HoldState", "Installed", "Login", "LoginError", "NetworkError",
    "NotFound", "Phase", "Planned", "Record", "Report", "Run", "Saved", "SiteError",
    "Skipped", "SourceError", "State", "Status", "Stopped", "Target", "TargetState",
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
        Login,
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
        LoginError,
        NetworkError,
        NotFound,
        SiteError,
        SourceError,
        Stopped,
        ToolError,
        UsageError,
    )
    from flnd.source import Target


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
