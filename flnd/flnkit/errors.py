"""The errors flnkit raises: one hierarchy under `Error`, plus `Stopped` for Ctrl-C.

COOKBOOK.md#when-it-goes-wrong explains it, and flnd's own classes (`flnd.errors`).
"""

from __future__ import annotations

import errno
from typing import Any

__all__ = [
    "Blocked",
    "Changed",
    "DiskError",
    "Error",
    "LoginError",
    "NetworkError",
    "NotFound",
    "SiteError",
    "Stopped",
    "ToolError",
    "UsageError",
    "disk",
]


class Error(Exception):
    """Base class for errors with a message and hint.

    Args:
        message: the one-line description; it is what `str(e)` gives.
        hint: what the person can do about it; empty when there is nothing to add.
        target: the target the error happened in, as written; None when it isn't about one.
        retryable: whether trying again later may work.
    """

    def __init__(
        self,
        message: str,
        *,
        hint: str = "",
        target: str | None = None,
        retryable: bool = False,
    ) -> None:
        super().__init__(message)
        self.hint = hint
        self.target = target
        self.retryable = retryable


class UsageError(Error):
    """A bad argument, target or setting."""


class LoginError(Error):
    """A login is missing, expired, or refused."""


class SiteError(Error):
    """The site answered, but not usefully.

    Args:
        message: the one-line description.
        status: the HTTP status the site sent; None when it sent none.
        response: the response, or its body, kept for a log to show; None when not kept.
        hint: what the person can do about it; empty when there is nothing to add.
        target: the target the error happened in, as written; None when it isn't about one.
        retryable: whether trying again later may work.
    """

    def __init__(
        self,
        message: str,
        *,
        status: int | None = None,
        response: Any = None,
        hint: str = "",
        target: str | None = None,
        retryable: bool = False,
    ) -> None:
        super().__init__(message, hint=hint, target=target, retryable=retryable)
        self.status = status
        self.response = response


class Blocked(SiteError):
    """Rate limited or blocked for a while.

    Args:
        message: the one-line description.
        wait: the seconds to wait before trying again; None when the site gave no time.
        status: the HTTP status the site sent; None when it sent none.
        response: the response, or its body, kept for a log to show; None when not kept.
        hint: what the person can do about it; empty when there is nothing to add.
        target: the target the error happened in, as written; None when it isn't about one.
        retryable: whether trying again later may work; True by default.
    """

    def __init__(
        self,
        message: str,
        *,
        wait: float | None = None,
        status: int | None = None,
        response: Any = None,
        hint: str = "",
        target: str | None = None,
        retryable: bool = True,
    ) -> None:
        super().__init__(
            message,
            status=status,
            response=response,
            hint=hint,
            target=target,
            retryable=retryable,
        )
        self.wait = wait


class NotFound(SiteError):
    """Deleted, private, or never there."""


class Changed(SiteError):
    """The site changed its response shape or layout."""


class NetworkError(Error):
    """A network transport, DNS, TLS, timeout or connection failure."""


class DiskError(Error):
    """A file system or storage error.

    Args:
        message: the one-line description.
        path: the file or folder the error is about; None when it isn't known.
        hint: what the person can do about it; empty when there is nothing to add.
        target: the target the error happened in, as written; None when it isn't about one.
        retryable: whether trying again later may work.
    """

    def __init__(
        self,
        message: str,
        *,
        path: object = None,
        hint: str = "",
        target: str | None = None,
        retryable: bool = False,
    ) -> None:
        super().__init__(message, hint=hint, target=target, retryable=retryable)
        self.path = path


class ToolError(Error):
    """An external tool (ffmpeg, 7zz, browser) failed or is missing.

    Args:
        message: the one-line description.
        command: the command that was run, as a tuple; empty when none ran.
        tail: the last lines of the tool's output, or None when there were none.
        hint: what the person can do about it; empty when there is nothing to add.
        target: the target the error happened in, as written; None when it isn't about one.
        retryable: whether trying again later may work.
    """

    def __init__(
        self,
        message: str,
        *,
        command: tuple[str, ...] = (),
        tail: str | None = None,
        hint: str = "",
        target: str | None = None,
        retryable: bool = False,
    ) -> None:
        super().__init__(message, hint=hint, target=target, retryable=retryable)
        self.command = command
        self.tail = tail


class Stopped(KeyboardInterrupt):
    """The run was stopped or interrupted (Ctrl-C)."""


# The advice that needs nothing of the caller. A full disk, a read-only one and a name that is
# too long get none here: what to do there depends on the program (flnd names `--home`).
_DISK_HINTS = {
    errno.EACCES: "check that you may write to that folder",
    errno.EPERM: "check that you may write to that folder",
}


def disk(error: OSError, path: object = None) -> DiskError:
    """The `DiskError` an `OSError` from the file system means, with `error` as its cause.

    Args:
        error: the OSError the file system raised.
        path: the path to show in the message; None uses the error's own file name.

    Returns:
        The DiskError, with `path` set to the path it shows.
    """
    where = path if path is not None else error.filename
    reason = error.strerror or str(error) or type(error).__name__
    message = f"{where}: {reason}" if where is not None else reason
    made = DiskError(message, path=where, hint=_DISK_HINTS.get(error.errno or 0, ""))
    made.__cause__ = error
    return made
