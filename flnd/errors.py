"""Every error flnd raises: one hierarchy under `flnd.Error`, plus `flnd.Stopped` for Ctrl-C.

Who calls it: every module of flnd raises these, `flnd` and `flnd.source` re-export them, and the
CLI maps them to exit codes (see `flnddocs/errors`). What it calls: only the stdlib, so importing
it costs nothing and any module may import it.

The hierarchy:

    Error                   the root: str(e) is the one-line message, e.hint what to do
    ├── UsageError          a bad target, flag or config value
    ├── LoginError          not logged in, expired, or refused
    ├── SiteError           the site answered, but not usefully (status, response)
    │   ├── Blocked         rate limited or blocked for a while (wait)
    │   ├── NotFound        deleted, private, or never there
    │   └── Changed         the answer no longer has the shape the source expects
    ├── NetworkError        DNS, TLS, a timeout, a reset, a transfer cut short
    ├── DiskError           no space, no permission, a name too long, a damaged store
    ├── ToolError           ffmpeg, 7zz, a browser: missing, or failed
    └── SourceError         a source is broken: its declaration, or a bug in its listing
    Stopped                 Ctrl-C or a stop: a KeyboardInterrupt, never an Error

Rules a change must keep:
1. The message is `str(e)`, one line, with no secret in it: a URL in a message goes through
   `flnd._text.redact` first, and a cookie or token never appears at all.
2. `hint` says what to do, as a command when there is one (`flnd login ins`); "" when
   there is nothing to suggest.
3. `retryable` is set by whoever raises, from the policy in `flnddocs/errors`, and never implied
   by the class: a `NetworkError` for a name that doesn't resolve won't heal, and a 403 may be a
   login wall or a block.
4. `Stopped` derives from `KeyboardInterrupt`, so `except flnd.Error` (or `except Exception`)
   never swallows a Ctrl-C.
5. No class here has a builtin base besides `Exception`, so `except ValueError` never catches
   an `Error` by accident.

Two helpers turn a stdlib or unknown exception into one of these, for the places that meet
them: `disk` reads an `OSError`, and `classify` takes anything a listing let escape.
"""

from __future__ import annotations

import errno
from typing import Any

__all__ = [
    "Blocked", "Changed", "DiskError", "Error", "LoginError", "NetworkError", "NotFound",
    "SiteError", "SourceError", "Stopped", "ToolError", "UsageError",
]


class Error(Exception):
    """Every error flnd raises: `str(e)` says what went wrong, `e.hint` what to do about it.

    Catch it to handle any failure of flnd at once; catch a subclass to handle one kind. A
    Ctrl-C is `Stopped`, which is not an `Error`, so this never swallows it.

    Args:
        message: what went wrong, in one line, without secrets.
        hint: what to do about it, a command when there is one; "" (the default) for nothing.
        target: the target the error is about, as written (`ins:example_user`); None when it
            isn't about one.
        retryable: whether trying again later may succeed. The raiser sets it from the policy;
            False by default.
    """

    def __init__(self, message: str, *, hint: str = "", target: str | None = None,
                 retryable: bool = False) -> None:
        super().__init__(message)
        self.hint = hint
        self.target = target
        self.retryable = retryable


class UsageError(Error):
    """A bad target, flag or config value. The CLI prints it without a traceback and exits 2."""


class LoginError(Error):
    """A login is missing, expired, or refused. That source stops, the others go on; exit 3."""


class SiteError(Error):
    """The site answered, but not usefully: a refusal, an error page, a web page for a photo.

    A 401 or 403 that ends a listing on the source's own domains becomes a `LoginError`; a 5xx
    is retryable.

    Args:
        message: what the site answered, with the redacted URL.
        status: the HTTP status of the answer; None when there was none (a browser page, an SDK).
        response: the answer itself, so a source can read the site's reason from its body; None
            when there was none. It is `flnd.source`'s response type.
        hint, target, retryable: as `Error`.
    """

    def __init__(self, message: str, *, status: int | None = None, response: Any = None,
                 hint: str = "", target: str | None = None, retryable: bool = False) -> None:
        super().__init__(message, hint=hint, target=target, retryable=retryable)
        self.status = status
        self.response = response


class Blocked(SiteError):
    """Rate limited (a 429) or blocked for a while: waited out, then reported.

    Args:
        message: what the site said, with the redacted URL.
        wait: the seconds the site asked to wait (`Retry-After`, or its reset time); None when
            it didn't say.
        status, response, hint, target, retryable: as `SiteError`.
    """

    def __init__(self, message: str, *, wait: float | None = None, status: int | None = None,
                 response: Any = None, hint: str = "", target: str | None = None,
                 retryable: bool = True) -> None:
        super().__init__(message, status=status, response=response, hint=hint, target=target,
                         retryable=retryable)
        self.wait = wait


class NotFound(SiteError):
    """Deleted, private, or never there (a 404 or 410): recorded as gone, and never retried."""


class Changed(SiteError):
    """The answer no longer has the shape the source expects: the site changed, or a source bug."""


class NetworkError(Error):
    """DNS, TLS, a timeout, a reset, or a transfer cut short: retried with backoff."""


class DiskError(Error):
    """No space, no permission, a name too long, or a damaged store: the run stops; exit 4."""


class ToolError(Error):
    """An outside tool (ffmpeg, 7zz, a browser) is missing, or it failed; exit 4."""


class SourceError(Error):
    """A source is broken: a mistake in its declaration, or an unexpected error in a listing."""


class Stopped(KeyboardInterrupt):
    """Ctrl-C, or a run told to stop: not a failure and not an `Error`. The CLI exits 130.

    It is a `KeyboardInterrupt`, so code that already handles Ctrl-C handles it too, and
    `except flnd.Error` never swallows it.
    """


# ---------------------------------------------------------------------------------------------
# Turning other exceptions into these
# ---------------------------------------------------------------------------------------------

# What to do about an OSError, by its errno. The message itself comes from the OS.
_DISK_HINTS = {
    errno.ENOSPC: "free some space on that disk, or use another --home",
    errno.EDQUOT: "free some space on that disk, or use another --home",
    errno.EACCES: "check that you may write to that folder",
    errno.EPERM: "check that you may write to that folder",
    errno.EROFS: "that disk is read-only; use another --home",
    errno.ENAMETOOLONG: "use a shorter --home or folder template",
}


def disk(error: OSError, path: object = None) -> DiskError:
    """The `DiskError` an `OSError` from the file system means, with a hint for its errno.

    Args:
        error: what the OS raised.
        path: the file or folder it was about, named in the message; None to use the error's own
            file name, if it has one.

    Returns:
        A `DiskError` whose message names the path and the OS's reason. It isn't retryable:
        a full disk or a refused folder stays that way until someone acts.

    Example:
        >>> import errno
        >>> e = disk(OSError(errno.ENOSPC, "No space left on device"), "media/a.jpg")
        >>> str(e)
        'media/a.jpg: No space left on device'
        >>> e.hint
        'free some space on that disk, or use another --home'
    """
    where = path if path is not None else error.filename
    reason = error.strerror or str(error) or type(error).__name__
    message = f"{where}: {reason}" if where is not None else reason
    return DiskError(message, hint=_DISK_HINTS.get(error.errno or 0, ""))


def classify(error: BaseException, *, target: str | None = None) -> Error | BaseException:
    """`error` as an `Error`, for a failure that escaped a listing or a download.

    1. An `Error` is kept as it is, with `target` filled in when it had none.
    2. A `KeyboardInterrupt` (`Stopped` too) or a `SystemExit` is returned unchanged: it isn't a
       failure, and the caller re-raises it.
    3. A `ConnectionError` or `TimeoutError` is a retryable `NetworkError`.
    4. Any other `OSError` is a `DiskError` (see `disk`).
    5. Anything else is a bug where it was raised, so a `SourceError` that names its type.

    The original stays the new error's `__cause__`, for a crash file or `-v`.

    Args:
        error: what was raised.
        target: the target it happened in, as written; None when it isn't about one.

    Returns:
        The `Error`, or the `KeyboardInterrupt` or `SystemExit` itself.

    Example:
        >>> e = classify(KeyError("id"), target="tik:example_user")
        >>> type(e).__name__, str(e), e.target
        ('SourceError', "unexpected KeyError: 'id'", 'tik:example_user')
    """
    if isinstance(error, (KeyboardInterrupt, SystemExit)):
        return error
    if isinstance(error, Error):
        if error.target is None:
            error.target = target
        return error
    made: Error
    if isinstance(error, (ConnectionError, TimeoutError)):
        made = NetworkError(f"{type(error).__name__}: {error}", retryable=True)
    elif isinstance(error, OSError):
        made = disk(error)
    else:
        made = SourceError(f"unexpected {type(error).__name__}: {error}",
                           hint="this is a bug; run with -v and report it")
    made.target = target
    made.__cause__ = error
    return made
