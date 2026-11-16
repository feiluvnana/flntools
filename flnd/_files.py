"""Atomic writes, staged files, locks, stale stage sweeps and fast folder walks.

    fs.write("out.json", data)
    with fs.stage("big.mp4") as tmp:            # .big.mp4.stage, moved onto big.mp4 on success
        sh.run("ffmpeg", "-i", src, tmp)
"""

from __future__ import annotations

import hashlib
import os
from collections.abc import Iterator
from contextlib import contextmanager, suppress
from pathlib import Path
from typing import Union

import filelock

from flnd import _time as clock
from flnd.errors import DiskError, disk

__all__ = ["BusyError", "ExistsError", "lock", "stage", "sweep", "touch", "walk", "write"]

PathLike = Union[str, "os.PathLike[str]"]

STAGE = ".stage"
LOCK = ".lock"
# `.part` is the name stages had before; `sweep` still clears what old runs left.
TEMPS = (STAGE, ".part")
# A resumable download may keep its offsets beside its stage, as `<stage>.json` or `.info`.
SIDES = (".json", ".info")
# Most filesystems cap a name at 255 bytes; a long name's stage keeps a head and a digest.
NAME_MAX, HEAD_BYTES = 255, 180
# Windows opens files in text mode unless told otherwise, which turns "\n" into "\r\n".
BINARY = getattr(os, "O_BINARY", 0)
# On Windows an antivirus or indexer may hold `dest` briefly, so rename retries for ~1 s.
WINDOWS = os.name == "nt"
REPLACE_TRIES, REPLACE_PAUSE = 20, 0.05
KEEP_AGE = 7 * 86_400
SETTLE_AGE = 3_600


# Every failure here is a `DiskError`: an OSError becomes one through `flnd.errors.disk`, which
# names the path and says what to do. Two conditions a caller acts on get their own class.
class ExistsError(DiskError):
    """Raised when `dest` already exists and `replace` is False."""


class BusyError(DiskError):
    """Raised when another writer holds a lock past `timeout`."""


def write(dest: PathLike, data: bytes | str, *, sync: bool = False, replace: bool = True) -> Path:
    """Atomically write `data` to `dest`; `sync=True` flushes it to disk first."""
    target = Path(dest)
    raw = data.encode("utf-8") if isinstance(data, str) else data
    with stage(target, keep=False, sync=sync) as temp:
        _write_bytes(temp, raw)
        if not replace:
            _link(temp, target, sync=sync)
    return target


@contextmanager
def stage(
    dest: PathLike, *, keep: bool = True, sync: bool = False, timeout: float | None = None
) -> Iterator[Path]:
    """Yield `.<name>.stage` beside `dest`, locked, and move it onto `dest` on a clean exit."""
    target = Path(dest)
    temp = _stage_path(target)
    held = temp.with_name(temp.name + LOCK)
    with lock(held, timeout=timeout):
        try:
            yield temp
            _commit(temp, target, sync=sync)
        except BaseException:
            if not keep:
                _unlink(os.fspath(temp))
            raise
        finally:
            # Unlinked while still held: filelock retries a lock whose file is gone.
            with suppress(OSError):
                held.unlink(missing_ok=True)


@contextmanager
def lock(path: PathLike, *, timeout: float | None = None) -> Iterator[filelock.FileLock]:
    """Acquire an advisory lock at `path` (or `<path>/.lock` for a folder) and yield it."""
    target = Path(path)
    try:
        if target.is_dir():
            target = target / ".lock"
        else:
            target.parent.mkdir(parents=True, exist_ok=True)
        flock = filelock.FileLock(target, timeout=-1.0 if timeout is None else timeout)
        flock.acquire()
    except filelock.Timeout as error:
        raise BusyError(f"{target}: held by another writer",
                        hint="wait for the other flnd run to end, then try again") from error
    except OSError as error:
        raise disk(error) from error
    try:
        yield flock
    finally:
        flock.release()


def touch(path: PathLike, *, mtime: float) -> None:
    """Set the access and modification times of `path` to `mtime` epoch seconds."""
    try:
        os.utime(path, (mtime, mtime))
    except OSError as error:
        raise disk(error) from error


def walk(folder: PathLike, *, suffix: tuple[str, ...] = ()) -> Iterator[os.DirEntry[str]]:
    """Yield the files under `folder` whose lowercase name ends in `suffix`, minus stages."""
    for entry in _scan(folder):
        name = entry.name
        if _temporary(name) or (suffix and not name.lower().endswith(suffix)):
            continue
        yield entry


def sweep(folder: PathLike, *, keep: float = KEEP_AGE, settle: float = SETTLE_AGE) -> int:
    """Remove stale stages, their sidecars and locks under `folder`, returning the count."""
    root = Path(folder)
    if not _exists(root):
        return 0
    stages: list[os.DirEntry[str]] = []
    extras: dict[str, os.DirEntry[str]] = {}
    for entry in _scan(root):
        if entry.name.endswith(TEMPS) and entry.name.startswith("."):
            stages.append(entry)
        elif _temporary(entry.name):
            extras[entry.path] = entry
    now = clock.wall()
    removed = 0
    for entry in stages:
        sides = [entry.path + side for side in SIDES if entry.path + side in extras]
        # A stage with offsets beside it can resume, so it waits `keep`, not `settle`.
        if _older_than(entry, now, keep if sides else settle):
            removed += _remove(entry.path, sides)
        for path in (*sides, entry.path + LOCK):
            extras.pop(path, None)
    for path, entry in extras.items():
        if _older_than(entry, now, settle):
            removed += _remove(_unsuffixed(path), [path])
    return removed


def _stage_path(target: Path) -> Path:
    """`.<name>.stage` beside `target`, shortened when it or its lock would be too long a name."""
    name = target.name
    # The lock and a sidecar (`.json`, `.info`) each add 5 bytes to the stage's name.
    if len(f".{name}{STAGE}{LOCK}".encode()) <= NAME_MAX:
        return target.with_name(f".{name}{STAGE}")
    head = name.encode()[:HEAD_BYTES].decode("utf-8", "ignore")
    digest = hashlib.sha256(name.encode()).hexdigest()[:8]
    return target.with_name(f".{head}.{digest}{STAGE}")


def _exists(path: Path) -> bool:
    """Whether `path` exists, as a `DiskError` when the check itself fails."""
    try:
        return path.exists()
    except OSError as error:
        raise disk(error) from error


def _write_bytes(temp: Path, data: bytes) -> None:
    """Write `data` to `temp` in binary mode, truncating what a crashed writer left."""
    try:
        handle = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | BINARY, 0o666)
        with os.fdopen(handle, "wb") as file:
            file.write(data)
    except OSError as error:
        raise disk(error, temp) from error


def _link(temp: Path, dest: Path, *, sync: bool) -> None:
    """Land `temp` on `dest` only if `dest` is free, leaving no window for another writer."""
    if sync:
        _fsync_file(temp)
    try:
        os.link(temp, dest)
    except FileExistsError as error:
        raise ExistsError(f"{dest}: already exists") from error
    except OSError:
        # No hard links here (FAT, some network shares): check, then let `stage` replace.
        if _exists(dest):
            raise ExistsError(f"{dest}: already exists") from None
        return
    _unlink(os.fspath(temp))
    if sync and not WINDOWS:
        _fsync_dir(dest.parent)


def _commit(temp: Path, dest: Path, *, sync: bool) -> None:
    """Flush `temp` if asked and move it onto `dest`; a block that moved it away wins."""
    try:
        if not temp.exists():
            return
        if sync:
            _fsync_file(temp)
        _replace(temp, dest)
        if sync and not WINDOWS:
            _fsync_dir(dest.parent)
    except OSError as error:
        raise disk(error) from error


def _replace(temp: Path, dest: Path) -> None:
    """`os.replace(temp, dest)`, retrying briefly while Windows holds `dest`."""
    for attempt in range(1, REPLACE_TRIES + 1):
        try:
            os.replace(temp, dest)
            return
        except PermissionError:
            if not WINDOWS or attempt == REPLACE_TRIES:
                raise
            clock.sleep(REPLACE_PAUSE)


def _fsync_file(path: Path) -> None:
    """Flush `path` to disk."""
    # Windows FlushFileBuffers requires write access on the descriptor.
    flags = os.O_RDWR | BINARY if WINDOWS else os.O_RDONLY
    try:
        fd = os.open(path, flags)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
    except OSError as error:
        raise disk(error) from error


def _fsync_dir(folder: Path) -> None:
    """Flush directory entries in `folder` on POSIX."""
    fd = os.open(folder, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _unlink(path: str) -> int:
    """Remove `path`, returning 1 if removed or 0 if already gone."""
    try:
        os.unlink(path)
        return 1
    except FileNotFoundError:
        return 0
    except OSError as error:
        raise disk(error) from error


def _scan(folder: PathLike) -> Iterator[os.DirEntry[str]]:
    """Yield every file under `folder`, depth first, from one `os.scandir` per folder."""
    stack = [os.fspath(folder)]
    while stack:
        try:
            with os.scandir(stack.pop()) as entries:
                for entry in entries:
                    if entry.is_dir(follow_symlinks=False):
                        stack.append(entry.path)
                    elif entry.is_file(follow_symlinks=False):
                        yield entry
        except OSError as error:
            raise disk(error) from error


def _temporary(name: str) -> bool:
    """Whether `name` is a stage, or a stage's sidecar or lock."""
    return name.startswith(".") and _unsuffixed(name).endswith(TEMPS)


def _unsuffixed(path: str) -> str:
    """`path` without a sidecar or lock suffix."""
    for suffix in (*SIDES, LOCK):
        if path.endswith(suffix):
            return path[: -len(suffix)]
    return path


def _older_than(entry: os.DirEntry[str], now: float, age: float) -> bool:
    """Whether `entry` was modified at least `age` seconds before `now`."""
    try:
        mtime = entry.stat(follow_symlinks=False).st_mtime
    except FileNotFoundError:
        return False
    except OSError as error:
        raise disk(error) from error
    return now - mtime >= age


def _remove(path: str, others: list[str]) -> int:
    """Remove the stage `path` and `others` unless a writer holds its lock; returns the count."""
    held = path + LOCK
    try:
        with lock(held, timeout=0):
            removed = sum(_unlink(other) for other in (path, *others))
            with suppress(OSError):
                os.unlink(held)
    except BusyError:
        return 0
    return removed
