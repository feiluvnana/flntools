"""Every error flncore raises: one hierarchy under `flncore.Error`, plus `Stopped`.

The hierarchy:

    Error                   the root: str(e) is the one-line message, e.hint what to do
    ├── UsageError          a bad target, flag or config value
    ├── SiteError           the site answered, but not usefully (status, response)
    │   ├── Blocked         rate limited or blocked for a while (wait)
    │   ├── NotFound        deleted, private, or never there
    │   └── Changed         the answer no longer has the shape expected
    ├── NetworkError        DNS, TLS, a timeout, a reset, a transfer cut short
    ├── DiskError           no space, no permission, a name too long, a damaged store
    └── ToolError           ffmpeg, 7zz, a browser: missing, or failed
    Stopped                 Ctrl-C or a stop: a KeyboardInterrupt, never an Error
"""

from __future__ import annotations

import errno
from typing import Any

__all__ = [
    "Blocked",
    "Changed",
    "DiskError",
    "Error",
    "NetworkError",
    "NotFound",
    "SiteError",
    "Stopped",
    "ToolError",
    "UsageError",
]


class Error(Exception):
    """Base class for errors with a message and hint."""

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

    @classmethod
    def of(cls, error: BaseException, *, target: str | None = None) -> Error | BaseException:
        """Convert any exception into an Error without mutating the original."""
        if isinstance(error, (KeyboardInterrupt, SystemExit)):
            return error
        if isinstance(error, Error):
            if error.target is None and target is not None:
                error.target = target
            return error
        if isinstance(error, (ConnectionError, TimeoutError)):
            made = NetworkError(f"{type(error).__name__}: {error}", retryable=True)
        elif isinstance(error, OSError):
            made = DiskError.of(error)
        else:
            made = Error(
                f"unexpected {type(error).__name__}: {error}",
                hint="this is a bug; run with -v and report it",
            )
        made.target = target
        made.__cause__ = error
        return made


class UsageError(Error):
    """A bad argument, target or setting."""


class SiteError(Error):
    """The site answered, but not usefully."""

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
    """Rate limited or blocked for a while."""

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


_DISK_HINTS = {
    errno.ENOSPC: "free some space on that disk, or use another --home",
    errno.EDQUOT: "free some space on that disk, or use another --home",
    errno.EACCES: "check that you may write to that folder",
    errno.EPERM: "check that you may write to that folder",
    errno.EROFS: "that disk is read-only; use another --home",
    errno.ENAMETOOLONG: "use a shorter --home or folder template",
}


class DiskError(Error):
    """A file system or storage error."""

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

    @classmethod
    def of(cls, error: OSError, *, path: object = None) -> DiskError:
        """Create a DiskError from an OSError."""
        where = path if path is not None else error.filename
        reason = error.strerror or str(error) or type(error).__name__
        message = f"{where}: {reason}" if where is not None else reason
        hint = _DISK_HINTS.get(error.errno or 0, "")
        return cls(message, path=where, hint=hint)


class ToolError(Error):
    """An external tool (ffmpeg, 7zz, browser) failed or is missing."""

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
