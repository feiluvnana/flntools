"""Atomic file writes, file locks, and stale part cleanup."""

from __future__ import annotations

import os
import secrets
from collections.abc import Iterator
from contextlib import AbstractContextManager, contextmanager
from pathlib import Path
from typing import Union, overload

import filelock

import shared
from shared import _clock as clock

__all__ = ["Error", "ExistsError", "lock", "sweep", "write"]

PathLike = Union[str, "os.PathLike[str]"]

# Windows opens files in text mode unless told otherwise, which turns "\n" into "\r\n".
BINARY = getattr(os, "O_BINARY", 0)
# On Windows an antivirus or indexer may hold `dest` briefly, so rename retries for ~1 s.
WINDOWS = os.name == "nt"
REPLACE_TRIES, REPLACE_PAUSE = 20, 0.05
KEEP_AGE = 7 * 86_400
SETTLE_AGE = 3_600
SIDE_SUFFIXES = (".json", ".info")


class Error(shared.Error):
    """The base of every error `shared.fs` raises."""


class ExistsError(Error, FileExistsError):
    """Raised when `dest` already exists and `replace` is False."""


@overload
def write(
    dest: PathLike, *, data: bytes | str, replace: bool = True, sync: bool = False
) -> Path: ...


@overload
def write(
    dest: PathLike, *, data: None = None, replace: bool = True, sync: bool = False
) -> AbstractContextManager[Path]: ...


def write(
    dest: PathLike,
    *,
    data: bytes | str | None = None,
    replace: bool = True,
    sync: bool = False,
) -> Path | AbstractContextManager[Path]:
    """Atomically write `data` to `dest`, or yield a temp `Path` to write and rename on exit."""
    if data is None:
        return _write_context(dest, replace=replace, sync=sync)
    raw = data.encode("utf-8") if isinstance(data, str) else data
    with _write_context(dest, replace=replace, sync=sync) as part:
        _write_bytes(part, raw)
    return Path(dest)


@contextmanager
def _write_context(
    dest: PathLike, *, replace: bool = True, sync: bool = False
) -> Iterator[Path]:
    """Yield a hidden part path beside `dest` and atomically move it into place on exit."""
    target = Path(dest)
    try:
        if target.exists() and not replace:
            raise ExistsError(f"{target} already exists")
    except OSError as error:
        if isinstance(error, ExistsError):
            raise
        raise Error(str(error)) from error
    # Random suffix keeps concurrent writers of the same destination apart.
    part = target.with_name(f".{target.name}.{secrets.token_hex(4)}.part")
    try:
        yield part
        _commit(part, target, sync=sync)
    finally:
        try:
            part.unlink(missing_ok=True)
        except OSError:
            pass


def _write_bytes(part: Path, data: bytes) -> None:
    """Write `data` to `part` in binary mode."""
    try:
        handle = os.open(part, os.O_WRONLY | os.O_CREAT | os.O_EXCL | BINARY, 0o666)
    except OSError as error:
        raise Error(str(error)) from error
    try:
        file = os.fdopen(handle, "wb")
    except BaseException:
        os.close(handle)
        raise
    try:
        with file:
            file.write(data)
    except OSError as error:
        raise Error(str(error)) from error


@contextmanager
def lock(path: PathLike, *, timeout: float | None = None) -> Iterator[filelock.FileLock]:
    """Acquire an advisory lock at `path` (or `<path>/.lock` for a folder) and yield it."""
    target = Path(path)
    try:
        if target.is_dir():
            target = target / ".lock"
        else:
            target.parent.mkdir(parents=True, exist_ok=True)
        wait = -1.0 if timeout is None else timeout
        flock = filelock.FileLock(target, timeout=wait)
        with flock:
            yield flock
    except (filelock.Timeout, OSError) as error:
        raise Error(str(error)) from error


def sweep(
    folder: PathLike, *, keep: float = KEEP_AGE, settle: float = SETTLE_AGE
) -> int:
    """Remove stale `.part` files and orphan sidecars under `folder`, returning count removed."""
    root = Path(folder)
    try:
        if not root.exists():
            return 0
    except OSError as error:
        raise Error(str(error)) from error
    files = _scan_files(root)
    parts, sides = _classify_parts(files)
    now = clock.wall()
    removed = 0
    for entry in parts:
        has_side = any(f"{entry.path}{ext}" in sides for ext in SIDE_SUFFIXES)
        cutoff = keep if has_side else settle
        if _older_than(entry, now, cutoff):
            removed += _unlink(entry.path)
            for ext in SIDE_SUFFIXES:
                if f"{entry.path}{ext}" in sides:
                    removed += _unlink(f"{entry.path}{ext}")
    part_paths = {entry.path for entry in parts}
    for side in sides.values():
        stem = _part_stem(side.path)
        if stem not in part_paths and _older_than(side, now, settle):
            removed += _unlink(side.path)
    return removed


def _scan_files(folder: PathLike) -> list[os.DirEntry[str]]:
    """Walk `folder` recursively in one `os.scandir` pass, returning file entries."""
    files: list[os.DirEntry[str]] = []
    stack = [os.fspath(folder)]
    while stack:
        current = stack.pop()
        try:
            with os.scandir(current) as entries:
                for entry in entries:
                    if entry.is_dir(follow_symlinks=False):
                        stack.append(entry.path)
                    elif entry.is_file(follow_symlinks=False):
                        files.append(entry)
        except OSError as error:
            raise Error(str(error)) from error
    return files


def _commit(part: Path, dest: Path, *, sync: bool) -> None:
    """Flush `part` if requested and replace `dest`."""
    try:
        if not part.exists():
            return
        if sync:
            _fsync_file(part)
        _replace(part, dest)
        if sync and not WINDOWS:
            _fsync_dir(dest.parent)
    except OSError as error:
        raise Error(str(error)) from error


def _replace(part: Path, dest: Path) -> None:
    """`os.replace(part, dest)`, retrying briefly while Windows holds `dest`."""
    for attempt in range(1, REPLACE_TRIES + 1):
        try:
            os.replace(part, dest)
            return
        except PermissionError:
            if not WINDOWS or attempt == REPLACE_TRIES:
                raise
            clock.sleep(REPLACE_PAUSE)


def _fsync_file(path: Path) -> None:
    """Flush `path` to disk."""
    # Windows FlushFileBuffers requires write access on the descriptor.
    flags = os.O_RDWR | BINARY if WINDOWS else os.O_RDONLY
    fd = os.open(path, flags)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _fsync_dir(folder: Path) -> None:
    """Flush directory entries in `folder` on POSIX."""
    fd = os.open(folder, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _classify_parts(
    files: list[os.DirEntry[str]],
) -> tuple[list[os.DirEntry[str]], dict[str, os.DirEntry[str]]]:
    """Split scanned files into `.part` files and `.part.json`/`.part.info` sidecars."""
    parts: list[os.DirEntry[str]] = []
    sides: dict[str, os.DirEntry[str]] = {}
    for entry in files:
        name = entry.name
        if name.endswith(".part"):
            parts.append(entry)
        elif any(name.endswith(f".part{ext}") for ext in SIDE_SUFFIXES):
            sides[entry.path] = entry
    return parts, sides


def _part_stem(side_path: str) -> str:
    """Strip the sidecar extension from a `.part.json` or `.part.info` path."""
    for ext in SIDE_SUFFIXES:
        if side_path.endswith(ext):
            return side_path[: -len(ext)]
    return side_path


def _older_than(entry: os.DirEntry[str], now: float, age: float) -> bool:
    """Return True when `entry` was modified at least `age` seconds before `now`."""
    try:
        mtime = entry.stat(follow_symlinks=False).st_mtime
    except FileNotFoundError:
        return False
    except OSError as error:
        raise Error(str(error)) from error
    return now - mtime >= age


def _unlink(path: str) -> int:
    """Remove `path`, returning 1 if removed or 0 if already gone."""
    try:
        os.unlink(path)
        return 1
    except FileNotFoundError:
        return 0
    except OSError as error:
        raise Error(str(error)) from error
