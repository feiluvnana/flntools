"""Every error flnd raises: one hierarchy under `flnd.Error`, plus `flnd.Stopped` for Ctrl-C.

src/docs/errors explains it.

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
    "LoginError",
    "NetworkError",
    "NotFound",
    "SiteError",
    "SourceError",
    "Stopped",
    "ToolError",
    "UsageError",
    "classify",
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

    @classmethod
    def of(cls, error: BaseException, *, target: str | None = None) -> Error | BaseException:
        """Convert any exception into an Error.

        An `Error` comes back as it is, with `target` set when it had none. A `KeyboardInterrupt`
        or `SystemExit` comes back unchanged. Any other exception becomes a `NetworkError`, a
        `DiskError` or a plain `Error`, with the original as its `__cause__`.

        Args:
            error: what was raised.
            target: the target it happened in, as written; None when it isn't about one.

        Returns:
            The `Error`, or the `KeyboardInterrupt` or `SystemExit` itself.

        Example:
            >>> made = Error.of(ValueError("bad value"))
            >>> print(made)
            unexpected ValueError: bad value
            >>> made.hint
            'this is a bug; run with -v and report it'
        """
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


_DISK_HINTS = {
    errno.ENOSPC: "free some space on that disk, or use another --home",
    errno.EDQUOT: "free some space on that disk, or use another --home",
    errno.EACCES: "check that you may write to that folder",
    errno.EPERM: "check that you may write to that folder",
    errno.EROFS: "that disk is read-only; use another --home",
    errno.ENAMETOOLONG: "use a shorter --home or folder template",
}


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

    @classmethod
    def of(cls, error: OSError, *, path: object = None) -> DiskError:
        """Create a DiskError from an OSError.

        The hint depends on the error number: a full disk, a missing permission, a read-only
        disk and a name that is too long each get their own.

        Args:
            error: the OSError the file system raised.
            path: the path to show in the message; None uses the error's own file name.

        Returns:
            The DiskError, with `path` set to the path it shows.

        Example:
            >>> import errno
            >>> made = DiskError.of(OSError(errno.ENOSPC, "No space left", "example/file.bin"))
            >>> print(made)
            example/file.bin: No space left
            >>> made.hint
            'free some space on that disk, or use another --home'
        """
        where = path if path is not None else error.filename
        reason = error.strerror or str(error) or type(error).__name__
        message = f"{where}: {reason}" if where is not None else reason
        hint = _DISK_HINTS.get(error.errno or 0, "")
        return cls(message, path=where, hint=hint)


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


class LoginError(Error):
    """A login is missing, expired, or refused. That source stops, the others go on; exit 3."""


class SourceError(Error):
    """A source is broken: a mistake in its declaration, or an unexpected error in a listing."""


def disk(error: OSError, path: object = None) -> DiskError:
    """The `DiskError` an `OSError` from the file system means."""
    return DiskError.of(error, path=path)


def classify(error: BaseException, *, target: str | None = None) -> Error | BaseException:
    """`error` as an `Error`, for a failure that escaped a listing or a download.

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
        return error
    made: Error
    if isinstance(error, (ConnectionError, TimeoutError)):
        made = NetworkError(f"{type(error).__name__}: {error}", retryable=True)
    elif isinstance(error, OSError):
        made = disk(error)
    else:
        made = SourceError(
            f"unexpected {type(error).__name__}: {error}",
            hint="this is a bug; run with -v and report it",
        )
    made.target = target
    made.__cause__ = error
    return made
