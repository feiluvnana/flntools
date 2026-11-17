"""Every error flnd raises: one hierarchy under `flnd.Error`, plus `flnd.Stopped` for Ctrl-C."""

from __future__ import annotations

from flncore.errors import (  # noqa: F401
    Blocked,
    Changed,
    DiskError,
    Error,
    NetworkError,
    NotFound,
    SiteError,
    Stopped,
    ToolError,
    UsageError,
)

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
