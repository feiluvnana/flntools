"""Run ffmpeg and 7-Zip from PATH with argument lists, never a shell.

Tests use `fake` to answer runs without a process.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, replace
from pathlib import Path, PurePosixPath

from flnd.errors import ToolError

__all__ = [
    "MissingError", "Result", "RunError", "Tool", "fake", "ffmpeg", "ffprobe", "find", "sevenzip",
]

SINGLE_DASH = frozenset({"ffmpeg", "ffprobe"})
QUOTED_LINES = 3
# Merges and large archives; callers may pass a smaller `timeout` to `run`.
DEFAULT_TIMEOUT = 6 * 3600.0


class MissingError(ToolError):
    """The program is not on PATH."""


class RunError(ToolError):
    """A program exited non-zero or ran past its timeout."""

    def __init__(self, message: str, *, code: int = 1, out: str = "", err: str = ""):
        """Attach output from the failed run.

        Args:
            message: the program, status and last output line.
            code: exit status, or -1 after a timeout.
            out: stdout, when captured.
            err: stderr, when captured.
        """
        super().__init__(message)
        self.code = code
        self.out = out
        self.err = err


@dataclass(frozen=True)
class Result:
    """Exit status, output and argv of a finished run."""

    code: int
    out: str
    err: str
    args: tuple[str, ...] = ()


Handler = Callable[[tuple[str, ...]], tuple[int, str, str]] | Result

_fakes: list[Mapping[str, Handler]] = []
_found: dict[str, Tool] = {}


@dataclass(frozen=True)
class Tool:
    """A program on PATH."""

    path: Path
    name: str

    def run(
        self,
        *args: str | os.PathLike[str],
        check: bool = True,
        timeout: float | None = DEFAULT_TIMEOUT,
        input: str | bytes | None = None,
    ) -> Result:
        """Run with `args`; raise `RunError` when `check` and the exit is non-zero.

        Args:
            *args: argv tail after the program path.
            check: raise on a non-zero exit when True.
            timeout: seconds before kill; None waits forever.
            input: bytes or text for standard input; None sends none.

        Returns:
            Exit status and output.

        Raises:
            MissingError: the program path no longer exists.
            RunError: non-zero exit with `check`, or a timeout.
        """
        argv = _argv(self.path, *args)
        faked = _faked(argv)
        if faked is not None:
            return _checked(faked, check=check)
        try:
            done = subprocess.run(
                argv, capture_output=True, text=True, encoding="utf-8", errors="replace",
                timeout=timeout, check=False, input=input,
            )
        except FileNotFoundError as error:
            raise _missing(argv[0]) from error
        except subprocess.TimeoutExpired as error:
            raise RunError(f"{argv[0]} timed out after {timeout}s", code=-1) from error
        except OSError as error:
            raise ToolError(f"{argv[0]}: {error}") from error
        return _checked(Result(done.returncode, done.stdout or "", done.stderr or "", argv),
                        check=check)


def find(name: str) -> Tool:
    """`name` on PATH, cached for the process.

    Args:
        name: the executable name without `.exe`.

    Returns:
        A `Tool` ready to `run`.

    Raises:
        MissingError: not on PATH and not in the active `fake`.
    """
    if _fakes:
        if name in _fakes[-1]:
            return Tool(Path(name), name)
        raise MissingError(f"{name}: not found; not in the fake")
    if name in _found:
        return _found[name]
    on_path = shutil.which(name)
    if on_path is None:
        raise _missing(name)
    tool = _found[name] = Tool(Path(on_path), name)
    return tool


def ffmpeg() -> Tool:
    """ffmpeg for DASH merges."""
    return find("ffmpeg")


def ffprobe() -> Tool:
    """ffprobe, shipped with ffmpeg."""
    return find("ffprobe")


def sevenzip() -> Tool:
    """`7zz`, else distro `7z`.

    Returns:
        The 7-Zip binary on PATH.

    Raises:
        MissingError: neither name is on PATH.
    """
    for name in ("7zz", "7z"):
        try:
            return find(name)
        except MissingError:
            continue
    raise MissingError("7z, RAR and encrypted zip archives need 7-Zip (`7zz`) on PATH",
                       hint="install 7-Zip (7zz), or put it on PATH")


@contextmanager
def fake(handlers: Mapping[str, Handler]) -> Iterator[None]:
    """Answer every tool run in the block from `handlers`, by program name.

    Args:
        handlers: by program name, a `Result` or a handler of argv.

    Yields:
        Nothing; the fake ends with the block.
    """
    _fakes.append(handlers)
    try:
        yield
    finally:
        _fakes.pop()


def _missing(name: str) -> MissingError:
    package = "ffmpeg" if name in SINGLE_DASH else name
    return MissingError(f"{name}: not found on PATH", hint=f"install {package}, or put it on PATH")


def _argv(*args: str | os.PathLike[str]) -> tuple[str, ...]:
    if not args:
        raise ValueError("run needs a command")
    return tuple(os.fspath(arg) for arg in args)


def _faked(argv: tuple[str, ...]) -> Result | None:
    """The fake handler's `Result` for `argv`, or None outside a fake block.

    Args:
        argv: full command, program first.

    Returns:
        The handler's result with `argv` as `args`, or None.

    Raises:
        MissingError: a fake is active and does not name this program.
    """
    if not _fakes:
        return None
    path = PurePosixPath(argv[0].replace("\\", "/"))
    name = path.stem if path.suffix.lower() == ".exe" else path.name
    handler = _fakes[-1].get(name)
    if handler is None:
        raise MissingError(f"{name}: not found; not in the fake")
    if isinstance(handler, Result):
        return replace(handler, args=argv)
    code, out, err = handler(argv)
    return Result(code, out, err, argv)


def _checked(result: Result, *, check: bool) -> Result:
    """Return `result`, or raise `RunError` when `check` and the exit is non-zero.

    Args:
        result: a finished run.
        check: whether a non-zero exit is an error.

    Returns:
        `result` when the exit is zero or `check` is false.

    Raises:
        RunError: `check` and a non-zero exit.
    """
    if check and result.code != 0:
        detail = _last_lines(result.err or result.out) or f"exit status {result.code}"
        raise RunError(
            f"{result.args[0]} failed ({result.code}): {detail}",
            code=result.code, out=result.out, err=result.err,
        )
    return result


def _last_lines(output: str) -> str:
    lines = [line.strip() for line in output.splitlines() if line.strip()]
    return " / ".join(lines[-QUOTED_LINES:])