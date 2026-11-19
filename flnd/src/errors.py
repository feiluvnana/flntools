"""flnd's face of the one error hierarchy: flnkit's classes, `Hidden`, `PluginError`, `classify`.

src/docs/errors explains it. The classes flnkit raises are defined in `flnkit.errors` and are
the same objects here (`flnd.LoginError is flnkit.errors.LoginError`); `Hidden`, `PluginError`
and the hints that name flnd's commands and flags are flnd's own.
"""

from __future__ import annotations

import errno

from flnkit.errors import (
    Blocked,
    Changed,
    DiskError,
    Error,
    LoginError,
    NetworkError,
    NotFound,
    SiteError,
    Stopped,
    ToolError,
    UsageError,
    disk,
)

__all__ = [
    "Blocked",
    "Changed",
    "DiskError",
    "Error",
    "Hidden",
    "LoginError",
    "NetworkError",
    "NotFound",
    "SiteError",
    "PluginError",
    "Stopped",
    "ToolError",
    "UsageError",
    "classify",
    "disk",
]


class Hidden(SiteError):
    """It exists, but this account can't see it: a private profile, a chat the account left."""


class PluginError(Error):
    """A plugin is broken: a mistake in its declaration, or an unexpected error in a listing."""


# flnd's hints, which `classify` gives an error that has none. A disk error's depends on its
# cause's error number; a login error's names the source, read from the target's `<code>:`.
DISK_HINTS = {
    errno.ENOSPC: "free some space on that disk, or use another --home",
    errno.EDQUOT: "free some space on that disk, or use another --home",
    errno.EROFS: "that disk is read-only; use another --home",
    errno.ENAMETOOLONG: "use a shorter --home or folder template",
}
LOGIN_HINT = "run flnd auth login {code}"
BUG_HINT = "this is a bug; run with -v and report it"


def classify(error: BaseException, *, target: str | None = None) -> Error | BaseException:
    """`error` as an `Error`, for a failure that escaped a listing or a download.

    An `Error` comes back as it is, with `target` set when it had none and flnd's hint when it
    had none. Any other exception becomes a `NetworkError`, a `DiskError` or, as a bug, a
    `PluginError`, with the original as its `__cause__`.

    Args:
        error: what was raised.
        target: the target it happened in, as written; None when it isn't about one.

    Returns:
        The `Error`, or the `KeyboardInterrupt` or `SystemExit` itself.
    """
    if isinstance(error, (KeyboardInterrupt, SystemExit)):
        return error
    if isinstance(error, Error):
        if error.target is None and target is not None:
            error.target = target
        if not error.hint:
            error.hint = _hint(error)
        return error
    made: Error
    if isinstance(error, (ConnectionError, TimeoutError)):
        made = NetworkError(f"{type(error).__name__}: {error}", retryable=True)
    elif isinstance(error, OSError):
        made = disk(error)
        made.hint = made.hint or _hint(made)
    else:
        # A ValueError too: `flnkit.dig` raises one for a malformed path, the plugin's mistake.
        made = PluginError(f"unexpected {type(error).__name__}: {error}", hint=BUG_HINT)
    made.target = target
    made.__cause__ = error
    return made


def _hint(error: Error) -> str:
    """flnd's hint for an error that has none, from `DISK_HINTS` and `LOGIN_HINT`; or ""."""
    if isinstance(error, DiskError) and isinstance(error.__cause__, OSError):
        return DISK_HINTS.get(error.__cause__.errno or 0, "")
    if isinstance(error, LoginError) and error.target and ":" in error.target:
        return LOGIN_HINT.format(code=error.target.split(":", 1)[0])
    return ""
