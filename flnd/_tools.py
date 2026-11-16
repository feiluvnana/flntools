"""The two outside programs flnd runs: ffmpeg (with ffprobe) and 7-Zip's `7zz`.

Everything else flnd needs is a Python wheel: images are compressed by Pillow and pyoxipng
in-process (`flnd._shrink`). Only video, probes, DASH joins and archives leave Python.

Who calls it:
- `flnd._media` runs `ffmpeg()` and `ffprobe()` for probes and video encodes;
- `flnd._transfer` runs ffmpeg to join a DASH video's two streams;
- `flnd._unpack` runs `sevenzip()` for 7z and RAR archives and for encrypted zips;
- `flnd._shrink` reads `Tool.version` to write the tools string a compressed file records.

How a call goes:
1. `find(name)` looks the program up on PATH once per process and keeps the answer.
2. `Tool.run` runs it with an argument list (never a shell) and returns its `Result`;
   `Tool.stream` yields its stdout lines as they come, which is how ffmpeg's `-progress`
   reaches the caller while a thread drains stderr so the pipe never fills.
3. A non-zero exit raises `RunError` with the last lines of output; a missing program raises
   `MissingError`, whose hint says what to install.

Rules a change must keep:
- every call takes an argument list, and every caller puts `--` before a path that came from a
  site where the tool reads switches (7zz), so a file named `-sdel` is never read as one;
- `fake` answers every run inside its block by tool name, so tests never start a process.

Downloading a pinned ffmpeg or 7zz when neither is on PATH is planned  but
not built: a pin needs a real checksum per platform, so today a missing tool is a `MissingError`.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import threading
from collections import deque
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager, suppress
from dataclasses import dataclass, replace
from pathlib import Path, PurePosixPath
from typing import Union

from flnd import _files
from flnd.errors import DiskError, ToolError

__all__ = [
    "MissingError", "Result", "RunError", "Tool", "fake", "ffmpeg", "ffprobe", "find", "sevenzip",
]

# ffmpeg and its siblings take one dash for every long option, `-version` included.
SINGLE_DASH = frozenset({"ffmpeg", "ffprobe"})
VERSION = re.compile(r"\d+(?:\.\d+)+")
VERSIONS_FILE = "versions.json"
# How much of a streamed tool's stderr a `RunError` keeps.
TAIL_LINES = 20
# How many of a failed tool's last output lines its message quotes, on one line.
QUOTED_LINES = 3


# The two `ToolError`s callers act on get their own class: a missing tool means "leave this
# file as it is", and a failed run's output may say why (a wrong password).
class MissingError(ToolError):
    """The program is not on PATH."""


class RunError(ToolError):
    """A program exited non-zero, or ran past its timeout.

    Args:
        message: the program, its exit status and its last lines of output, on one line.
        code: the exit status; -1 for a timeout.
        out: everything it wrote to stdout.
        err: everything it wrote to stderr (only stderr's tail, when streamed).
    """

    def __init__(self, message: str, *, code: int = 1, out: str = "", err: str = ""):
        super().__init__(message)
        self.code = code
        self.out = out
        self.err = err


@dataclass(frozen=True)
class Result:
    """What a finished program returned: its exit status, its output and its arguments."""

    code: int
    out: str
    err: str
    args: tuple[str, ...] = ()


# A fake answers with a `Result`, or a function of the arguments returning (code, out, err).
Handler = Union[Callable[[tuple[str, ...]], tuple[int, str, str]], Result]

# The handlers of the innermost `fake` block; a list so threads the block starts see it too.
_fakes: list[Mapping[str, Handler]] = []
_found: dict[str, Tool] = {}
_versions_lock = threading.Lock()


@dataclass(frozen=True)
class Tool:
    """A program found on PATH, ready to run.

    Args:
        path: where it is.
        name: what it is called (`ffmpeg`, `7zz`), which decides its version flag.
    """

    path: Path
    name: str

    def run(
        self, *args: str | os.PathLike[str], check: bool = True, timeout: float | None = None,
    ) -> Result:
        """Run the program with `args` and wait for it.

        Args:
            *args: its arguments, each passed as one argument (never through a shell).
            check: raise `RunError` on a non-zero exit; False returns the `Result` anyway.
            timeout: seconds before the program is killed; None waits for ever.

        Returns:
            Its exit status and output.

        Raises:
            MissingError: the program is gone.
            RunError: it exited non-zero (when `check`), or timed out.
        """
        argv = _argv(self.path, *args)
        faked = _faked(argv)
        if faked is not None:
            return _checked(faked, check=check)
        try:
            done = subprocess.run(
                argv, capture_output=True, text=True, encoding="utf-8", errors="replace",
                timeout=timeout, check=False,
            )
        except FileNotFoundError as error:
            raise _missing(argv[0]) from error
        except subprocess.TimeoutExpired as error:
            raise RunError(f"{argv[0]} timed out after {timeout}s", code=-1) from error
        except OSError as error:
            raise ToolError(f"{argv[0]}: {error}") from error
        return _checked(Result(done.returncode, done.stdout or "", done.stderr or "", argv),
                        check=check)

    def stream(self, *args: str | os.PathLike[str], timeout: float | None = None) -> Iterator[str]:
        """Run the program and yield its stdout lines as they come.

        Leaving the loop early kills the program. A thread drains stderr meanwhile, keeping its
        last `TAIL_LINES` lines for the error.

        Args:
            *args: its arguments.
            timeout: seconds before the program is killed; None waits for ever.

        Yields:
            Each stdout line, without its line ending.

        Raises:
            MissingError: the program is gone.
            RunError: it exited non-zero or timed out, raised after its last line.
        """
        argv = _argv(self.path, *args)
        faked = _faked(argv)
        if faked is not None:
            yield from faked.out.splitlines()
            _checked(faked, check=True)
            return
        yield from _stream(argv, timeout=timeout)

    @property
    def version(self) -> str:
        """The first version number `<tool> --version` prints, or "" when it prints none.

        It is cached in `~/.cache/flnd/tools/versions.json` by the binary's path and modified
        time, so asking costs one spawn per upgrade, not one per run.
        """
        try:
            mtime = self.path.stat().st_mtime
        except OSError:
            return _parse_version(self)
        cache = _cache_root() / VERSIONS_FILE
        key = os.fspath(self.path.resolve())
        with _versions_lock:
            known = _read_versions(cache)
            entry = known.get(key)
            if isinstance(entry, dict) and entry.get("mtime") == mtime:
                return str(entry.get("version", ""))
            version = _parse_version(self)
            known[key] = {"mtime": mtime, "version": version}
            # A read-only cache costs a spawn next time, not the answer.
            with suppress(DiskError):
                _files.write(cache, json.dumps(known, indent=1, sort_keys=True))
        return version


def find(name: str) -> Tool:
    """The program `name` on PATH, looked up once per process.

    Args:
        name: the program's name, without `.exe`.

    Returns:
        The program, ready to run.

    Raises:
        MissingError: it is not on PATH (or not in the innermost `fake`).
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
    """ffmpeg, which joins DASH tracks and encodes videos.

    Returns:
        The program, ready to run.

    Raises:
        MissingError: it is not on PATH; the hint says to install it.
    """
    return find("ffmpeg")


def ffprobe() -> Tool:
    """ffprobe, which ships with ffmpeg and reads a video's streams.

    Returns:
        The program, ready to run.

    Raises:
        MissingError: it is not on PATH; the hint says to install ffmpeg.
    """
    return find("ffprobe")


def sevenzip() -> Tool:
    """7-Zip: `7zz` (the current build), else the older `7z` that many Linux packages ship.

    Returns:
        The program, ready to run.

    Raises:
        MissingError: neither is on PATH; the hint says to install 7-Zip.
    """
    for name in ("7zz", "7z"):
        with suppress(MissingError):
            return find(name)
    raise MissingError("7z, RAR and encrypted zip archives need 7-Zip (`7zz`) on PATH",
                       hint="install 7-Zip (7zz), or put it on PATH")


@contextmanager
def fake(handlers: Mapping[str, Handler]) -> Iterator[None]:
    """Answer every program run in the block from `handlers`, by name, without a process.

    Args:
        handlers: by program name, a `Result`, or a function of the arguments returning
            (code, stdout, stderr). A program not named here is missing inside the block.

    Yields:
        Nothing; the fake ends with the block.
    """
    _fakes.append(handlers)
    try:
        yield
    finally:
        _fakes.pop()


def _missing(name: str) -> MissingError:
    """The error for a program that isn't installed, with what to install."""
    package = "ffmpeg" if name in SINGLE_DASH else name
    return MissingError(f"{name}: not found on PATH", hint=f"install {package}, or put it on PATH")


def _argv(*args: str | os.PathLike[str]) -> tuple[str, ...]:
    """`args` as strings, refusing an empty command."""
    if not args:
        raise ValueError("run needs a command")
    return tuple(os.fspath(arg) for arg in args)


def _faked(argv: tuple[str, ...]) -> Result | None:
    """The result the innermost `fake` gives `argv`, or None outside a `fake` block.

    Raises:
        MissingError: a fake is active and doesn't name this program.
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
    """`result`, or a `RunError` when `check` is set and it exited non-zero.

    Raises:
        RunError: `check` and a non-zero exit, quoting the last lines of output.
    """
    if check and result.code != 0:
        detail = _last_lines(result.err or result.out) or f"exit status {result.code}"
        raise RunError(
            f"{result.args[0]} failed ({result.code}): {detail}",
            code=result.code, out=result.out, err=result.err,
        )
    return result


def _last_lines(output: str) -> str:
    """The last `QUOTED_LINES` non-empty lines of a program's output, joined into one line."""
    lines = [line.strip() for line in output.splitlines() if line.strip()]
    return " / ".join(lines[-QUOTED_LINES:])


def _stream(argv: tuple[str, ...], *, timeout: float | None) -> Iterator[str]:
    """Run `argv`, yielding stdout lines while a thread keeps stderr's tail (see `Tool.stream`)."""
    try:
        process = subprocess.Popen(
            argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, encoding="utf-8", errors="replace",
        )
    except FileNotFoundError as error:
        raise _missing(argv[0]) from error
    except OSError as error:
        raise ToolError(f"{argv[0]}: {error}") from error
    tail: deque[str] = deque(maxlen=TAIL_LINES)
    # A full stderr pipe would block the program, so a thread drains it while stdout is read.
    drain = threading.Thread(target=tail.extend, args=(process.stderr,), daemon=True)
    drain.start()
    expired = threading.Event()
    # The lines come from a blocking read, so a timer kills the program when time runs out.
    killer = threading.Timer(timeout or 0, lambda: (expired.set(), process.kill()))
    if timeout is not None:
        killer.start()
    try:
        assert process.stdout is not None
        for line in process.stdout:
            yield line.rstrip("\r\n")
        code = process.wait()
    finally:
        killer.cancel()
        _end(process, drain)
    if expired.is_set():
        raise RunError(f"{argv[0]} timed out after {timeout}s", code=-1, err="".join(tail))
    _checked(Result(code, "", "".join(tail), argv), check=True)


def _end(process: subprocess.Popen[str], drain: threading.Thread) -> None:
    """Kill a program its reader left early, and close its pipes.

    Args:
        process: the running program.
        drain: the thread reading its stderr, joined once the pipe closes.
    """
    if process.poll() is None:
        process.kill()
    process.wait()
    drain.join()
    for pipe in (process.stdout, process.stderr):
        if pipe is not None:
            pipe.close()


def _parse_version(tool: Tool) -> str:
    """Run the program's version flag and pick the first version number it prints."""
    flag = "-version" if tool.name in SINGLE_DASH else "--version"
    result = tool.run(flag, check=False, timeout=30)
    found = VERSION.search(result.out) or VERSION.search(result.err)
    return found.group(0) if found else ""


def _read_versions(cache: Path) -> dict[str, object]:
    """The cached versions by binary path, or none when the file is missing or broken.

    Args:
        cache: the `versions.json` file.

    Returns:
        Each binary's `{"mtime": …, "version": …}` by its resolved path.
    """
    try:
        known = json.loads(cache.read_text("utf-8"))
    except (OSError, ValueError):
        return {}
    return known if isinstance(known, dict) else {}


def _cache_root() -> Path:
    """`$XDG_CACHE_HOME/flnd/tools`, else `~/.cache/flnd/tools`."""
    base = os.environ.get("XDG_CACHE_HOME")
    root = Path(base) if base else Path.home() / ".cache"
    return root / "flnd" / "tools"
