"""Safe file writes, tool execution, archives and file helpers.

src/docs/storage explains it.
"""

from __future__ import annotations

import errno
import gzip
import hashlib
import io
import json
import lzma
import mimetypes
import os
import re
import shutil
import subprocess
import tarfile
import threading
import zipfile
import zlib
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager, suppress
from dataclasses import dataclass, replace
from pathlib import Path, PurePosixPath
from typing import IO, Any, Literal

from flnkit import clock
from flnkit.errors import DiskError, SiteError, ToolError, disk

PART = ".part"
SIDE = ".json"
# The longest name most file systems allow, and how much of a long name a short part keeps. The
# reserve covers `.part.json` and the `.part` a small write of that sidecar adds (15 bytes).
NAME_MAX, HEAD_BYTES, RESERVE = 255, 180, len(PART + SIDE + PART)
# Windows opens files in text mode unless told otherwise, which turns "\n" into "\r\n".
BINARY = getattr(os, "O_BINARY", 0)
WINDOWS = os.name == "nt"
# On Windows an antivirus or indexer may hold `dest` briefly, so a rename retries for ~1 s.
REPLACE_TRIES, REPLACE_PAUSE = 20, 0.05
# Windows locks byte ranges, and a locked range refuses writes from every other handle, the
# writer's own included. One byte far past any real file's end is locked instead, so writes
# through another handle (a source's `fetch`, ffmpeg) still land.
LOCK_AT = 1 << 62
# Errnos that mean the file system has no locks at all (some network shares): write unlocked.
NO_LOCKS = {errno.ENOLCK, errno.EOPNOTSUPP, getattr(errno, "ENOTSUP", errno.EOPNOTSUPP)}
# A sweep removes a part untouched this long (it may still resume until then), and an orphan
# sidecar or an old lock file untouched this long.
KEEP_AGE = 7 * 86_400
SETTLE_AGE = 3_600
# A hidden stage file an old flnd may still have left beside a download.
OLD_STAGE = ".stage"
# What may sit beside a part: its sidecar, a hard link being made (`link`), and an old stage's
# `.info` sidecar and lock file.
SIDES = (SIDE, ".link", ".info", ".lock")
# Writes reach the disk this many bytes at a time; a digest reads a file back the same way.
BUFFER = 1 << 20
# Errnos that mean this file system (or this pair of folders) can't hard-link: FAT and exFAT
# (EPERM, ENOTSUP), two volumes (EXDEV), or a file at its file system's link limit (EMLINK).
NO_LINKS = {errno.EPERM, errno.EXDEV, errno.EMLINK, errno.EOPNOTSUPP,
            getattr(errno, "ENOTSUP", errno.EOPNOTSUPP), errno.ENOSYS}

if WINDOWS:
    import msvcrt
else:
    import fcntl


class BusyError(DiskError):
    """Raised when another run holds a part: that file is being saved by someone else."""


@dataclass(frozen=True)
class Saved:
    """Where a part landed, and the size and modified time (ns) it had there.

    The history row takes `size` and `mtime` from here, so nothing stats the file again.
    `hash` is the SHA-256 of its bytes (32 bytes) for a part opened with `digest=True`, else None.
    """

    path: Path
    size: int
    mtime: int
    hash: bytes | None = None


class Part:
    """An open, locked `<dest>.part`, yielded by `part`.

    A caller writes through `write` (or `write_at` from several threads), or hands `path` to a
    tool that writes by name. It may change `dest` (a download whose content calls for another
    extension) and set `mtime` before the block ends.

    Attributes:
        dest: where the part lands on a clean exit.
        path: the part's own path, `<dest>.part` or a shortened name.
        fd: the locked descriptor; it stays open for the whole block.
        size: how many bytes the part holds: what was there when opened, then what `write`,
            `write_at` and `reset` made it. A tool that writes by name doesn't move it.
        mtime: the modified time to give the file, in epoch seconds; None keeps the write's.
        saved: what landed, once the block ended cleanly.
        side: True when a sidecar `<part>.json` may exist and must go when the part lands (a
            source's `fetch` keeps its offsets there).

    Args:
        dest: where the part lands.
        path: the part's own path.
        fd: its open, locked descriptor, which the part now owns.
        size: how many bytes it held when opened.
        resume: whether it is a download's, kept on failure (see `part`).
        digest: whether `saved` gets the SHA-256 of what landed.
    """

    def __init__(self, dest: Path, path: Path, fd: int, size: int, *, resume: bool,
                 digest: bool = False) -> None:
        self.dest, self.path, self.fd, self.size = dest, path, fd, size
        self.mtime: float | None = None
        self.saved: Saved | None = None
        self.side = False
        self._resume = resume
        self._digest = digest
        # The running SHA-256 of what `write` appended, while it covers the part from byte 0:
        # None once the bytes came another way, and `_land` reads the part back instead.
        self._sha: Any = hashlib.sha256() if digest and not size else None
        self._buffer: io.BufferedRandom | None = None
        self._meta: dict[str, Any] | None = None
        self._noted = False
        self._dropped = False
        self._at = threading.Lock()

    @property
    def meta(self) -> dict[str, Any]:
        """What the sidecar `<part>.json` says about the bytes kept: `etag`, `holes`.

        Read once, and only for a part that holds bytes; a fresh part has none, so this costs a
        new download nothing.
        """
        if self._meta is None:
            self._meta = _read_side(self.side_path) if self.size else {}
            self.side = self.side or bool(self._meta)
        return self._meta

    @property
    def side_path(self) -> Path:
        """The sidecar beside the part, `<part>.json`."""
        return self.path.with_name(self.path.name + SIDE)

    def note(self, **fields: Any) -> None:
        """Record facts about the bytes kept, written to the sidecar only if the block fails.

        Args:
            fields: the facts; a value of None removes one.
        """
        meta = self.meta
        for name, value in fields.items():
            if meta.get(name) != value:
                if value is None:
                    meta.pop(name, None)
                else:
                    meta[name] = value
                self._noted = True

    def write(self, data: bytes, *, fed: bool = False) -> None:
        """Append `data` after the part's bytes, through a 1 MiB buffer.

        Args:
            data: the bytes, which reach the file through the buffer, or at once for a piece
                larger than it.
            fed: the caller hashes `data` itself, with `feed`, so the hash and the write can
                run on two threads at once. Every appended byte must reach `feed` in order.
        """
        if self._buffer is None:
            self._buffer = open(self.fd, "r+b", buffering=BUFFER, closefd=False)  # noqa: SIM115
            self._buffer.seek(self.size)
        self._buffer.write(data)
        self.size += len(data)
        if not fed:
            self.feed(data)

    def feed(self, data: bytes) -> None:
        """Add appended bytes to the part's running SHA-256 (see `write`'s `fed`).

        Args:
            data: the bytes just appended, or about to be, in the order they are appended.
        """
        sha = self._sha
        if sha is not None:
            # hashlib lets go of the GIL for a chunk this size, so other downloads go on.
            sha.update(data)

    def write_at(self, offset: int, data: bytes) -> None:
        """Write `data` at `offset`, unbuffered; safe from several threads at once.

        It doesn't move `size`: the caller knows when the file is whole.

        Args:
            offset: where in the file, in bytes from its start.
            data: the bytes.
        """
        with self._at:
            self._sha = None
            os.lseek(self.fd, offset, os.SEEK_SET)
            view = memoryview(data)
            while view:
                view = view[os.write(self.fd, view):]

    def flush(self) -> None:
        """Push what `write` buffered to the file."""
        if self._buffer is not None:
            self._buffer.flush()

    def reset(self) -> None:
        """Empty the part, forget what its sidecar said, and undo a `drop`: start from 0."""
        if self._buffer is not None:
            self._buffer.flush()
            self._buffer.seek(0)
        os.ftruncate(self.fd, 0)
        self.size = 0
        self._meta, self._noted, self._dropped = {}, self.side, False
        self._sha = hashlib.sha256() if self._digest else None

    def cut(self, size: int) -> None:
        """Keep only the first bytes (a segmented download that failed part way).

        Args:
            size: how many bytes to keep.
        """
        self.flush()
        os.ftruncate(self.fd, size)
        self.size = size
        self._sha = None
        if self._buffer is not None:
            self._buffer.seek(size)

    def head(self, count: int = 64) -> bytes:
        """The part's first `count` bytes, read through the descriptor (no second open)."""
        self.flush()
        os.lseek(self.fd, 0, os.SEEK_SET)
        return os.read(self.fd, count)

    def drop(self) -> None:
        """Remove the part when the block ends, whatever happens: its bytes are no good."""
        self._dropped = True


@contextmanager
def part(dest: str | os.PathLike[str], *, resume: bool = False,
         digest: bool = False) -> Iterator[Part]:
    """Open and lock `<dest>.part`, yield it, and rename it onto `dest` on a clean exit.

    The two kinds of caller differ in three ways, all set by `resume`:
    - a download (`resume=True`) keeps the bytes a failed run left and goes on from them, raises
      `BusyError` at once when another run holds the part, and keeps the part when it fails;
    - a small write (`resume=False`) starts from an empty part, waits for another writer of the
      same file, and removes the part when it fails.

    Args:
        dest: the file to write; its folder is made when missing.
        resume: True for a download, as above.
        digest: also give the SHA-256 of what lands, in `Part.saved.hash`.

    Yields:
        The locked `Part`. After a clean exit, `Part.saved` says where it landed.

    Raises:
        BusyError: `resume` is True and another run holds the part.
        DiskError: the part can't be opened, locked or renamed (no space, no permission).
    """
    target = Path(dest)
    path = _part_path(target)
    try:
        fd, size = _open(path, wait=not resume, resume=resume)
        if size and not resume:
            os.ftruncate(fd, 0)
            size = 0
    except OSError as error:
        raise disk(error, path) from error
    held = Part(target, path, fd, size, resume=resume, digest=digest)
    try:
        yield held
    except BaseException:
        _leave(held)
        raise
    if held._dropped:
        _leave(held)
        return
    try:
        _land(held)
    except OSError as error:
        _leave(held)
        raise disk(error, held.dest) from error


def write(dest: str | os.PathLike[str], data: bytes | str) -> Path:
    """Write `data` to `dest` whole, through a part: a reader never sees half a file.

    Args:
        dest: the file; its folder is made when missing.
        data: the content; a str is written as UTF-8.

    Returns:
        `dest`, as a Path.

    Raises:
        DiskError: the file can't be written.
    """
    with part(dest) as held:
        held.write(data.encode("utf-8") if isinstance(data, str) else data)
    return Path(dest)


def walk(folder: str | os.PathLike[str]) -> Iterator[os.DirEntry[str]]:
    """Every file under `folder` except parts and their sidecars, from one `scandir` per folder.

    Args:
        folder: the folder to walk, recursively.

    Yields:
        Each file's `DirEntry`, whose cached `stat` costs no system call on Windows and one on
        POSIX.

    Raises:
        DiskError: a folder can't be read.
    """
    for entry in _scan(folder):
        if not _temporary(entry.name):
            yield entry


def digest(path: str | os.PathLike[str]) -> bytes:
    """The SHA-256 of a file's bytes, read 1 MiB at a time.

    Args:
        path: the file.

    Returns:
        The 32-byte digest.

    Raises:
        DiskError: the file can't be read.

    Example:
        >>> import tempfile
        >>> path = write(Path(tempfile.mkdtemp()) / "a.txt", "abc")
        >>> digest(path).hex()[:16]
        'ba7816bf8f01cfea'
    """
    try:
        fd = os.open(path, os.O_RDONLY | BINARY)
    except OSError as error:
        raise disk(error, Path(path)) from error
    try:
        return _digest_fd(fd)
    except OSError as error:
        raise disk(error, Path(path)) from error
    finally:
        os.close(fd)


def link(source: str | os.PathLike[str], dest: str | os.PathLike[str]) -> bool:
    """Make `dest` a hard link to `source`, replacing whatever `dest` was, atomically.

    The link is made beside `dest` under a temporary name, then renamed onto it, so a reader
    sees the old file or the new one, never neither. Where the file system can't link (FAT,
    exFAT, two volumes, a file at its link limit), nothing changes and False comes back: the
    caller keeps both copies.

    Args:
        source: the file to link to.
        dest: the name to give it; its folder must exist. An existing file there is replaced.

    Returns:
        True when `dest` is now `source`'s file; False when this file system can't link.

    Raises:
        DiskError: the disk refused for another reason (no permission, no space).

    Example:
        >>> import tempfile
        >>> folder = Path(tempfile.mkdtemp())
        >>> one = write(folder / "one.jpg", b"same")
        >>> link(one, folder / "two.jpg"), (folder / "two.jpg").stat().st_nlink
        (True, 2)
    """
    target = Path(dest)
    # `<dest>.part.link`: a sweep's temporary name, so one a crash leaves is cleared like a part.
    temporary = _part_path(target).with_suffix(PART + ".link")
    _unlink(temporary)
    try:
        os.link(source, temporary)
    except OSError as error:
        if error.errno in NO_LINKS:
            return False
        raise disk(error, target) from error
    try:
        _replace(temporary, target)
    except OSError as error:
        _unlink(temporary)
        raise disk(error, target) from error
    return True


def sweep(folder: str | os.PathLike[str]) -> int:
    """Remove the stale parts under `folder`: untouched for a week, and held by no run.

    A part kept by a failed download stays a week, so a later run can resume it. A sidecar
    without its part goes after an hour. The hidden `.<name>.stage` files older versions left go
    like parts, and their orphan `.lock` files like sidecars.

    Args:
        folder: the target's folder; a missing one has nothing to sweep.

    Returns:
        How many files were removed.

    Raises:
        DiskError: the folder can't be read.
    """
    if not os.path.isdir(folder):
        return 0
    now = clock.wall()
    parts: list[os.DirEntry[str]] = []
    sides: dict[str, os.DirEntry[str]] = {}
    for entry in _scan(folder):
        if _is_part(entry.name):
            parts.append(entry)
        elif _temporary(entry.name):
            sides[entry.path] = entry
    removed = 0
    for entry in parts:
        own = [entry.path + suffix for suffix in SIDES if entry.path + suffix in sides]
        for path in own:
            sides.pop(path)
        if _older(entry, now, KEEP_AGE):
            removed += _remove_unheld(entry.path, own)
    for path, entry in sides.items():
        if _older(entry, now, SETTLE_AGE):
            removed += _unlink(path)
    return removed


# ---------------------------------------------------------------------------------------------
# Opening and locking


def _part_path(dest: Path) -> Path:
    """`<dest>.part`, or `<head>.<digest>.part` when the name with its sidecars passes 255 bytes.

    Args:
        dest: the file the part is for.

    Returns:
        The part's path, the same for the same `dest` every time, so a later run resumes it.
    """
    name = dest.name
    if len(name.encode()) + RESERVE <= NAME_MAX:
        return dest.with_name(name + PART)
    head = name.encode()[:HEAD_BYTES].decode("utf-8", "ignore")
    digest = hashlib.sha256(name.encode()).hexdigest()[:8]
    return dest.with_name(f"{head}.{digest}{PART}")


def _open(path: Path, *, wait: bool, resume: bool = False) -> tuple[int, int]:
    """Open and lock the part at `path`, returning its descriptor and the bytes it holds.

    1. Create it exclusively: a new part needs no check, since no one else had it.
    2. Else open it as it is, lock it, and check the path still names it, as the module says.
       When it doesn't, another run has just landed it: a download is busy, a write tries again.

    Args:
        path: the part.
        wait: wait for another holder (a small write) instead of raising `BusyError`.
        resume: True for a download; blocks a fresh part when the final file already landed.

    Returns:
        The locked descriptor, and the size of what the part already held (0 for a new one).

    Raises:
        BusyError: not `wait`, and another run holds or has just landed the part.
        OSError: the file system refused.
    """
    while True:
        try:
            fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_EXCL | BINARY, 0o666)
            fresh = True
        except FileExistsError:
            fd = os.open(path, os.O_RDWR | os.O_CREAT | BINARY, 0o666)
            fresh = False
        except FileNotFoundError:
            path.parent.mkdir(parents=True, exist_ok=True)
            continue
        try:
            if not _lock(fd, wait=wait):
                raise BusyError(f"{path.name}: being saved by another run",
                                hint="let the other run finish; the next run skips it")
            if fresh:
                return fd, 0
            found = os.fstat(fd)
            if _names(path, found):
                return fd, found.st_size
            if not wait:
                raise BusyError(f"{path.name}: just saved by another run")
            # A download must not start a fresh part over a file another run just landed.
            if resume and path.name.endswith(PART):
                landed = path.with_name(path.name[: -len(PART)])
                if landed.is_file() and not path.is_file():
                    raise BusyError(f"{path.name}: just saved by another run",
                                    hint="let the other run finish; the next run skips it")
        except BaseException:
            os.close(fd)
            raise
        os.close(fd)


def _names(path: Path, found: os.stat_result) -> bool:
    """Whether `path` still names the file `found` describes.

    Args:
        path: the part's path.
        found: the `fstat` of the descriptor locked.

    Returns:
        False when the path is gone or names another file: another run landed this one.
    """
    try:
        here = os.stat(path)
    except FileNotFoundError:
        return False
    return (here.st_dev, here.st_ino) == (found.st_dev, found.st_ino)


def _lock(fd: int, *, wait: bool) -> bool:
    """Lock `fd` for this process; False when another holds it and `wait` is off.

    On a file system without locks the write goes on unlocked, as it did before parts.

    Args:
        fd: the part's descriptor.
        wait: block until the other holder lets go, instead of returning False.

    Returns:
        True once this process holds the lock (or the file system has none).

    Raises:
        OSError: locking failed for another reason.
    """
    try:
        if WINDOWS:
            os.lseek(fd, LOCK_AT, os.SEEK_SET)
            # LK_LOCK tries once a second for 10 s, then raises: a writer that slow is stuck.
            msvcrt.locking(fd, msvcrt.LK_LOCK if wait else msvcrt.LK_NBLCK, 1)
            os.lseek(fd, 0, os.SEEK_SET)
        else:
            fcntl.flock(fd, fcntl.LOCK_EX if wait else fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        return False
    except OSError as error:
        if error.errno in NO_LOCKS:
            return True
        if WINDOWS and error.errno in (errno.EACCES, errno.EDEADLK):
            return False
        raise
    return True


def _close(fd: int) -> None:
    """Unlock and close `fd`. Closing alone frees a POSIX lock; Windows asks for the unlock.

    Args:
        fd: the part's descriptor.
    """
    if WINDOWS:
        with suppress(OSError):
            os.lseek(fd, LOCK_AT, os.SEEK_SET)
            msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
    os.close(fd)


def _end(held: Part) -> None:
    """Close a part, once: its buffer first (which never owns the descriptor), then the descriptor.

    A second call does nothing, since a descriptor number closed twice may by then be another
    thread's file.

    Args:
        held: the part.
    """
    if held._buffer is not None:
        with suppress(OSError):
            held._buffer.close()
        held._buffer = None
    if held.fd >= 0:
        fd, held.fd = held.fd, -1
        _close(fd)


# ---------------------------------------------------------------------------------------------
# Landing and leaving


def _land(held: Part) -> None:
    """Rename a finished part onto its `dest` with its time set, and record what landed.

    The time is set on the descriptor before close, and the rename keeps it. Windows can't set
    a time by descriptor or rename an open file, so there the time goes by path, then the part
    closes, then it is renamed: another run could take the part in that instant, and then the
    rename retries until it lets go.

    Args:
        held: the finished part; its `saved` is set.

    Raises:
        OSError: the time, the rename or the close failed.
    """
    held.flush()
    if held.mtime is not None:
        stamp = int(held.mtime * 1e9)
        os.utime(held.path if WINDOWS else held.fd, ns=(stamp, stamp))
    found = os.fstat(held.fd)
    hashed = None
    if held._digest:
        # The running digest when it covers every byte; else the part is read back once.
        whole = held._sha is not None and held.size == found.st_size
        hashed = held._sha.digest() if whole else _digest_fd(held.fd)
    if WINDOWS:
        _end(held)
        _replace(held.path, held.dest)
    else:
        try:
            os.replace(held.path, held.dest)
        finally:
            _end(held)
    held.saved = Saved(held.dest, found.st_size, found.st_mtime_ns, hashed)
    if held.side:
        _unlink(os.fspath(held.side_path))


def _digest_fd(fd: int) -> bytes:
    """The SHA-256 of an open file's bytes from its start.

    Args:
        fd: the open descriptor; on Windows its offset moves, and nothing else uses it then.

    Returns:
        The 32-byte digest.

    Raises:
        OSError: the file can't be read.
    """
    sha = hashlib.sha256()
    offset = 0
    while True:
        if WINDOWS:
            # Windows has no `pread`.
            os.lseek(fd, offset, os.SEEK_SET)
            chunk = os.read(fd, BUFFER)
        else:
            chunk = os.pread(fd, BUFFER, offset)
        if not chunk:
            return sha.digest()
        sha.update(chunk)
        offset += len(chunk)


def _leave(held: Part) -> None:
    """End a block that failed: keep a download's bytes for the next run, else remove them.

    Nothing here raises, so the block's own error is the one the caller sees.

    Args:
        held: the part whose block failed, or was dropped.
    """
    try:
        with suppress(OSError):
            held.flush()
        keep = held._resume and not held._dropped
        if keep:
            with suppress(OSError):
                keep = os.fstat(held.fd).st_size > 0
        if keep:
            if held._noted:
                with suppress(OSError):
                    _write_side(held.side_path, held.meta)
            return
        # POSIX removes the part while it is still locked, so no run takes it in between;
        # Windows can't remove an open file, so there it closes first.
        if WINDOWS:
            with suppress(OSError):
                _end(held)
        with suppress(OSError):
            os.unlink(held.path)
        if held.side:
            with suppress(OSError):
                os.unlink(held.side_path)
    finally:
        with suppress(OSError):
            _end(held)


def _replace(source: Path, dest: Path) -> None:
    """`os.replace`, retried for about a second while Windows holds `dest` (an indexer).

    Args:
        source: the closed part.
        dest: where it lands.

    Raises:
        PermissionError: Windows still held `dest` after the last try.
    """
    for attempt in range(1, REPLACE_TRIES + 1):
        try:
            os.replace(source, dest)
            return
        except PermissionError:
            if not WINDOWS or attempt == REPLACE_TRIES:
                raise
            clock.sleep(REPLACE_PAUSE)
        except FileNotFoundError:
            if WINDOWS and dest.exists():
                return
            raise


def _read_side(path: Path) -> dict[str, Any]:
    """A sidecar's facts.

    Args:
        path: the sidecar.

    Returns:
        What it holds; {} when there is none or it is damaged, so the download starts over.
    """
    try:
        found = json.loads(path.read_bytes())
    except (OSError, ValueError):
        return {}
    return found if isinstance(found, dict) else {}


def _write_side(path: Path, meta: dict[str, Any]) -> None:
    """Write a sidecar. It is written in place: a torn one reads as {} and costs a restart."""
    with open(path, "wb") as handle:
        handle.write(json.dumps(meta).encode("utf-8"))


# ---------------------------------------------------------------------------------------------
# Walking and sweeping


def _scan(folder: str | os.PathLike[str]) -> Iterator[os.DirEntry[str]]:
    """Every file under `folder`, depth first, from one `os.scandir` per folder.

    Symlinks are neither followed nor yielded, so a link can't lead a walk or a sweep out of the
    folder, or round in a loop.

    Args:
        folder: the folder to walk.

    Yields:
        Each regular file's `DirEntry`.

    Raises:
        DiskError: a folder can't be read, including `folder` itself when it is missing.
    """
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


def _is_part(name: str) -> bool:
    """Whether `name` is a part, or a stage an older flnd left (`.<name>.stage`)."""
    return name.endswith(PART) or (name.startswith(".") and name.endswith(OLD_STAGE))


def _temporary(name: str) -> bool:
    """Whether a file is a part, a part's sidecar, or an old stage's sidecar or lock.

    Args:
        name: the file's name.

    Returns:
        True for those, which a walk passes over and a sweep may remove.
    """
    if _is_part(name):
        return True
    for suffix in SIDES:
        if name.endswith(suffix) and _is_part(name[: -len(suffix)]):
            return True
    return False


def _older(entry: os.DirEntry[str], now: float, age: float) -> bool:
    """Whether a file was last modified at least `age` seconds ago.

    Args:
        entry: the file, from a scan.
        now: the wall clock, in epoch seconds.
        age: how old, in seconds.

    Returns:
        True when it is that old; False when it vanished meanwhile.

    Raises:
        DiskError: it can't be stat'ed.
    """
    try:
        return now - entry.stat(follow_symlinks=False).st_mtime >= age
    except FileNotFoundError:
        return False
    except OSError as error:
        raise disk(error) from error


def _remove_unheld(path: str, sides: list[str]) -> int:
    """Remove a stale part and its sidecars, unless a run holds it.

    Args:
        path: the part.
        sides: its sidecars and old lock files.

    Returns:
        How many files went: 0 when a run holds the part.

    Raises:
        DiskError: the part can't be opened or removed.
    """
    try:
        fd = os.open(path, os.O_RDWR | BINARY)
    except FileNotFoundError:
        return 0
    except OSError as error:
        raise disk(error) from error
    try:
        if not _lock(fd, wait=False):
            return 0
        if WINDOWS:
            # Windows can't remove an open file; a run that takes the part now just restarts it.
            _close(fd)
            fd = -1
        # On POSIX, removed while still locked, so no run can take it in between.
        return sum(_unlink(one) for one in (path, *sides))
    finally:
        if fd >= 0:
            _close(fd)


def _unlink(path: str | os.PathLike[str]) -> int:
    """Remove a file, counting it; a file already gone is no error, since that is the goal.

    Args:
        path: the file.

    Returns:
        1 when it was removed; 0 when it was already gone.

    Raises:
        DiskError: it exists, but the disk refused to remove it.
    """
    try:
        os.unlink(path)
        return 1
    except FileNotFoundError:
        return 0
    except OSError as error:
        raise disk(error) from error


# ---------------------------------------------------------------------------------------------
# What a file is: kinds by name and by content
# ---------------------------------------------------------------------------------------------

# What `sniff` names a server's web page or JSON answer: a login wall or an error page.
PAGES = (".html", ".json")
# The kinds of saved file; each gets its extension from its content.
IMAGE, VIDEO, AUDIO = "image", "video", "audio"
# The kind a download takes for a file whose content decides what it is: a post's file named
# without an extension, because neither its name nor its URL said.
SNIFF = "sniff"
# How an error names each kind.
NOUNS = {IMAGE: "a photo", VIDEO: "a video", AUDIO: "an audio file"}
# Common media extensions mapped to `IMAGE`, `VIDEO`, or `AUDIO` without probing disk.
MEDIA_EXTENSIONS = {
    ".jpg": IMAGE, ".jpeg": IMAGE, ".png": IMAGE, ".gif": IMAGE, ".webp": IMAGE,
    ".heic": IMAGE, ".heif": IMAGE, ".avif": IMAGE, ".bmp": IMAGE, ".tif": IMAGE, ".tiff": IMAGE,
    ".mp4": VIDEO, ".m4v": VIDEO, ".mov": VIDEO, ".webm": VIDEO, ".mkv": VIDEO, ".avi": VIDEO,
    ".mp3": AUDIO, ".m4a": AUDIO, ".aac": AUDIO, ".ogg": AUDIO, ".opus": AUDIO,
    ".wav": AUDIO, ".flac": AUDIO,
}


def kind_of(name: str) -> str | None:
    """What a saved file is by its name: `IMAGE`, `VIDEO` or `AUDIO`, or None.

    Args:
        name: the file's name or path; only its extension is read.

    Returns:
        `IMAGE`, `VIDEO` or `AUDIO`, or None when the extension is not media.
    """
    suffix = Path(name).suffix.lower()
    if suffix in MEDIA_EXTENSIONS:
        return MEDIA_EXTENSIONS[suffix]
    mime, _ = mimetypes.guess_type(f"x{suffix}")
    if mime:
        head = mime.split("/", 1)[0]
        if head in (IMAGE, VIDEO, AUDIO):
            return head
    return None


def sniff(path: Path | bytes) -> str | None:
    """Sniff a common media, archive, or web-page extension from a file's leading bytes.

    Args:
        path: the file, or its first 64 bytes already read (a download reads them through its
            part's descriptor).

    Returns:
        The extension, with its dot, or None when the bytes say nothing known.
    """
    if isinstance(path, bytes):
        head = path
    else:
        try:
            with path.open("rb") as handle:
                head = handle.read(64)
        except OSError:
            return None
    if head.startswith(b"\xff\xd8\xff"):
        return ".jpg"
    if head.startswith(b"\x89PNG\r\n\x1a\n"):
        return ".png"
    if head.startswith((b"GIF87a", b"GIF89a")):
        return ".gif"
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return ".webp"
    if head[4:8] == b"ftyp":
        brand = head[8:12]
        if brand in (b"avif", b"avis"):
            return ".avif"
        if brand in (b"heic", b"heix", b"hevc", b"mif1"):
            return ".heic"
        if brand == b"M4A ":
            return ".m4a"
        return ".mp4"
    if head.startswith(b"\x1aE\xdf\xa3"):
        return ".webm"
    if head.startswith((b"ID3", b"\xff\xfb", b"\xff\xf3", b"\xff\xf2")):
        return ".mp3"
    if head.startswith(b"OggS"):
        return ".ogg"
    if head.startswith(b"fLaC"):
        return ".flac"
    if head.startswith((b"PK\x03\x04", b"PK\x05\x06")):
        return ".zip"
    if head.startswith(b"7z\xbc\xaf\x27\x1c"):
        return ".7z"
    if head.startswith(b"Rar!\x1a\x07"):
        return ".rar"
    stripped = head.lstrip()
    if stripped.startswith((b"<", b"<!DOCTYPE", b"<!doctype")):
        return ".html"
    if stripped.startswith((b"{", b"[")):
        return ".json"
    return None


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

Kind = Literal["file", "dir"]

# The names that mark a file as an archive. A file with a password from its source is tried
# whatever its name.
SUFFIXES = (".zip", ".7z", ".rar", ".tar", ".tar.gz", ".tgz", ".tar.bz2", ".tbz2", ".tar.xz",
            ".txz")
DRIVE = re.compile(r"^[A-Za-z]:")
WIN_RESERVED = frozenset({
    "CON", "PRN", "AUX", "NUL",
    *(f"COM{i}" for i in range(1, 10)),
    *(f"LPT{i}" for i in range(1, 10)),
})
# Entries are copied in pieces of this size, so a 20 GB member never sits in memory.
CHUNK = 1024 * 1024
# The staging folder's suffix: `.<folder>.unpack`, hidden, beside the archive.
STAGE = ".unpack"
# A zip entry's general-purpose flag for "encrypted".
ENCRYPTED = 0x1
# What a damaged zip or tar raises inside Python's readers, besides their own error classes.
DAMAGED = (EOFError, zlib.error, gzip.BadGzipFile, lzma.LZMAError)


@dataclass(frozen=True)
class Entry:
    """One member of an archive, as its listing names it.

    Args:
        name: its path inside the archive, with `/` or `\\` between folders.
        size: its unpacked size in bytes; None when the listing doesn't say.
        kind: "file" or "dir".
    """

    name: str
    size: int | None
    kind: Kind


@dataclass(frozen=True)
class Limits:
    """How big an archive may unpack, so a zip bomb fails before it fills the disk.

    Args:
        size: the most bytes all entries may hold together (20 GB).
        ratio: the most unpacked bytes per archive byte (200: real media compresses far less).
        files: the most entries (100,000).
    """

    size: int = 20_000_000_000
    ratio: float = 200
    files: int = 100_000


def is_archive(path: Path) -> bool:
    """Whether `path`'s name marks it as an archive the unpack step takes.

    Args:
        path: a saved file.

    Returns:
        True for `.zip`, `.7z`, `.rar` and the `.tar` family, whatever the case.
    """
    return path.name.lower().endswith(SUFFIXES)


def folder(path: Path) -> Path:
    """The folder an archive extracts into: beside it, named after it without its suffix.

    Args:
        path: the archive.

    Returns:
        `a/b.tar.gz` gives `a/b`; a name with no archive suffix gets `_files` added.
    """
    lower = path.name.lower()
    for suffix in sorted(SUFFIXES, key=len, reverse=True):
        if lower.endswith(suffix) and len(lower) > len(suffix):
            return path.with_name(path.name[: -len(suffix)])
    return path.with_name(f"{path.name}_files")


def extract(path: Path, *, password: str | None = None, limits: Limits = Limits()) -> Path:
    """Extract `path` beside itself, refusing unsafe entries, and verify what was written.

    The steps are the module's: detect, list and check, stage, verify, place.

    Args:
        path: the archive.
        password: its password, from the source; None tries without one.
        limits: the bounds a zip bomb would pass.

    Returns:
        The folder now holding the archive's files.

    Raises:
        SiteError: the archive is damaged, unsupported, encrypted with a wrong or missing
            password, or unsafe (an escaping path, a link, past `limits`).
        MissingError: it needs 7-Zip, and `7zz` is not on PATH.
        DiskError: the disk refused a write.
    """
    kind = _detect(path)
    entries = _entries(path, kind, password=password)
    _check_limits(entries, path.stat().st_size, limits)
    dest = folder(path)
    stage = dest.with_name(f".{dest.name}{STAGE}")
    for entry in entries:
        _safe_dest(stage, entry.name)
    # A stage a crash left behind is half an extract: start again.
    shutil.rmtree(stage, ignore_errors=True)
    stage.mkdir(parents=True)
    try:
        if kind == "zip" and not _sevenzip_zip(path, password):
            _unpack_zip(path, stage, password=password)
        elif kind == "tar":
            _unpack_tar(path, stage)
        else:
            _unpack_7z(path, stage, password=password)
        _verify(stage, entries, path.name)
        _place(stage, dest)
    finally:
        shutil.rmtree(stage, ignore_errors=True)
    return dest


# ---------------------------------------------------------------------------------------------
# Detecting and listing


def _detect(path: Path) -> str:
    """The archive's format from its first bytes: "zip", "7z", "rar" or "tar".

    Args:
        path: the archive.

    Returns:
        Its format.

    Raises:
        SiteError: the file is missing, or no format this step knows.
    """
    if not path.is_file():
        raise SiteError(f"{path}: archive not found")
    with path.open("rb") as handle:
        head = handle.read(8)
    if head.startswith((b"PK\x03\x04", b"PK\x05\x06", b"PK\x07\x08")):
        return "zip"
    if head.startswith(b"7z\xbc\xaf\x27\x1c"):
        return "7z"
    if head.startswith(b"Rar!\x1a\x07"):
        return "rar"
    if tarfile.is_tarfile(path):
        return "tar"
    raise SiteError(f"{path.name}: unsupported or corrupt archive")


def _entries(path: Path, kind: str, *, password: str | None) -> list[Entry]:
    """Every entry of the archive, refusing links (the first two parts of `extract`).

    Args:
        path: the archive.
        kind: its format, from `_detect`.
        password: its password, for a 7-Zip listing; None for none.

    Returns:
        Its entries, in the archive's order.

    Raises:
        SiteError: the archive can't be read, has a link, or needs a password it wasn't given.
    """
    if kind == "zip":
        if _sevenzip_zip(path, password):
            return _ls_7z(path, password=password)
        try:
            with zipfile.ZipFile(path) as zf:
                out: list[Entry] = []
                for info in zf.infolist():
                    if _zip_link(info):
                        raise SiteError(f"{path.name}: unsafe zip entry {info.filename!r}")
                    out.append(Entry(info.filename, info.file_size,
                                       "dir" if info.is_dir() else "file"))
                return out
        except zipfile.BadZipFile as error:
            raise SiteError(f"{path.name}: {error}") from error
    if kind == "tar":
        try:
            with tarfile.open(path) as tf:
                out: list[Entry] = []
                for m in tf.getmembers():
                    if m.issym() or m.islnk() or not (m.isfile() or m.isdir()):
                        raise SiteError(f"{path.name}: unsafe tar entry {m.name!r}")
                    out.append(Entry(m.name, m.size, "dir" if m.isdir() else "file"))
                return out
        except (tarfile.TarError, *DAMAGED) as error:
            raise SiteError(f"{path.name}: {error}") from error
    return _ls_7z(path, password=password)


def _safe_dest(dest: Path, member: str) -> Path:
    """Where `member` lands under `dest`, refusing any name that would land outside it.

    Args:
        dest: the folder entries land in.
        member: an entry's name as the archive spells it.

    Returns:
        The path it lands at.

    Raises:
        SiteError: an absolute path, a drive letter, an empty or `..` part, or a name that
            resolves outside `dest`.
    """
    norm = member.replace("\\", "/").rstrip("/")
    if not norm or norm.startswith("/") or DRIVE.match(norm):
        raise SiteError(f"unsafe archive path {member!r}")
    parts = PurePosixPath(norm).parts
    if any(p in ("..", "") for p in parts):
        raise SiteError(f"unsafe archive path {member!r}")
    for part in parts:
        stem = part.split(".", 1)[0].upper()
        if stem in WIN_RESERVED:
            raise SiteError(f"unsafe archive path {member!r}")
        if os.name == "nt" and ":" in part:
            raise SiteError(f"unsafe archive path {member!r}")
    target = dest / Path(*parts)
    if not os.path.abspath(target).startswith(os.path.abspath(dest) + os.sep):
        raise SiteError(f"archive path escapes its folder: {member!r}")
    return target


def _check_limits(entries: list[Entry], archive_size: int, limits: Limits) -> None:
    """Refuse an archive whose entries pass `limits`.

    Args:
        entries: its entries.
        archive_size: its own size in bytes, for the ratio.
        limits: the bounds.

    Raises:
        SiteError: too many entries, too many bytes, or too high a compression ratio.
    """
    if len(entries) > limits.files:
        raise SiteError(f"too many entries ({len(entries)} > {limits.files})")
    total = sum(e.size or 0 for e in entries)
    if total > limits.size:
        raise SiteError(f"unpacked size exceeds limit ({total} > {limits.size})")
    if archive_size > 0 and total / archive_size > limits.ratio:
        raise SiteError(f"compression ratio exceeds limit ({limits.ratio})")


# ---------------------------------------------------------------------------------------------
# Extracting (the third part)


def _sevenzip_zip(path: Path, password: str | None) -> bool:
    """Whether a zip goes to 7-Zip: when it is encrypted and 7-Zip is installed.

    Args:
        path: the zip.
        password: its password; a zip with none never needs 7-Zip.

    Returns:
        True to extract it with 7-Zip.

    Python's zipfile reads only the old ZipCrypto, not AES, so an encrypted zip goes to 7-Zip
    when there is one, and to zipfile (which may then refuse an AES entry) when there isn't.
    """
    if password is None:
        return False
    with zipfile.ZipFile(path) as zf:
        if not any(info.flag_bits & ENCRYPTED for info in zf.infolist()):
            return False
    try:
        sevenzip()
    except MissingError:
        return False
    return True


def _unpack_zip(path: Path, dest: Path, *, password: str | None) -> None:
    """Extract a zip into `dest` with Python's zipfile, entry by entry.

    Args:
        path: the zip.
        dest: the staging folder.
        password: its password, for ZipCrypto entries; None for none.

    Raises:
        SiteError: a wrong or missing password, an AES entry, or a damaged entry.
    """
    pwd = password.encode("utf-8") if password is not None else None
    try:
        with zipfile.ZipFile(path) as zf:
            for info in zf.infolist():
                target = _safe_dest(dest, info.filename)
                if info.is_dir():
                    target.mkdir(parents=True, exist_ok=True)
                    continue
                target.parent.mkdir(parents=True, exist_ok=True)
                with zf.open(info, pwd=pwd) as src:
                    _save(src, target)
    except RuntimeError as error:
        if "password" in str(error).lower() or "encrypted" in str(error).lower():
            raise SiteError(f"{path.name}: wrong or missing password") from error
        raise SiteError(f"{path.name}: {error}") from error
    except NotImplementedError as error:
        raise SiteError(f"{path.name}: {error}",
                        hint="install 7-Zip (7zz) to extract AES-encrypted zips") from error
    except (zipfile.BadZipFile, *DAMAGED) as error:
        raise SiteError(f"{path.name}: {error}") from error


def _unpack_tar(path: Path, dest: Path) -> None:
    """Extract a tar (plain or compressed) into `dest` with the data filter.

    Args:
        path: the tar.
        dest: the staging folder.

    Raises:
        SiteError: the tar is damaged.
        DiskError: the disk refused a write.
    """
    try:
        shutil.unpack_archive(path, dest, filter="data")
    except (tarfile.TarError, *DAMAGED, shutil.ReadError) as error:
        raise SiteError(f"{path.name}: {error}") from error
    except OSError as error:
        raise disk(error, dest) from error


def _zip_link(info: zipfile.ZipInfo) -> bool:
    """True when a zip entry is a symbolic link (Unix mode in external attributes)."""
    mode = (info.external_attr >> 16) & 0o170000
    return mode == 0o120000


def _7z_password_args(password: str | None) -> tuple[str, ...]:
    """7-Zip password argv tail: `-p` alone when a password is set, else `-p` with no value."""
    return ("-p",) if password else (f"-p{''}",)


def _7z_password_input(password: str | None) -> str | None:
    """What goes to 7-Zip's standard input for the password, or None when there is none."""
    return password if password else None


def _save(stream: IO[bytes], target: Path) -> None:
    """Copy `stream` to the new file `target`, piece by piece.

    The stage is this run's own folder, so the file is written in place: a failure removes
    the whole stage.

    Args:
        stream: the entry's bytes.
        target: where they go; it must not exist yet.
    """
    with target.open("xb") as out:
        shutil.copyfileobj(stream, out, CHUNK)


def _ls_7z(path: Path, *, password: str | None) -> list[Entry]:
    """List a 7z, RAR or encrypted zip with `7zz l -slt`, refusing links.

    Args:
        path: the archive.
        password: its password; None lists without one.

    Returns:
        Its entries.

    Raises:
        SiteError: a wrong or missing password, a damaged archive, or a link.
        MissingError: 7-Zip is not installed.
    """
    tool = sevenzip()
    try:
        res = tool.run("l", "-slt", *_7z_password_args(password), "--", str(path),
                       input=_7z_password_input(password))
    except RunError as error:
        raise _failed(path, error) from error
    entries: list[Entry] = []
    cur: dict[str, str] = {}
    # `-slt` prints a block of `Key = value` lines per entry, blocks split by a blank line.
    for line in [*res.out.splitlines(), ""]:
        if not line.strip():
            if "Path" in cur:
                entries.append(_7z_entry(cur))
            cur = {}
        elif " = " in line:
            k, _, v = line.partition(" = ")
            cur[k.strip()] = v.strip()
    # The first block describes the archive itself, under its own path.
    return [entry for entry in entries if Path(entry.name) != path]


def _7z_entry(cur: dict[str, str]) -> Entry:
    """One `7zz l -slt` block as an `Entry`, refusing a link.

    Args:
        cur: the block's `Key = value` pairs.

    Returns:
        The entry.

    Raises:
        SiteError: the entry is a symbolic link, a hard link or a reparse point.
    """
    # `Attributes` holds Windows flags, then a Unix mode when there is one: `A_ lrwxrwxrwx`.
    flags, _, mode = cur.get("Attributes", "").partition(" ")
    # `L` is a Windows reparse point (a link or junction), `l` a Unix symbolic link.
    if "L" in flags or mode.startswith("l") or cur.get("Symbolic Link") or cur.get("Hard Link"):
        raise SiteError(f"unsafe archive entry {cur['Path']!r}: a link")
    is_dir = cur.get("Folder") == "+" or flags.startswith("D")
    size_raw = cur.get("Size", "")
    size = int(size_raw) if size_raw.isdigit() else None
    return Entry(cur["Path"], size, "dir" if is_dir else "file")


def _unpack_7z(path: Path, dest: Path, *, password: str | None) -> None:
    """Extract with `7zz x` into `dest` (a 7z, a RAR, or an encrypted zip).

    Args:
        path: the archive.
        dest: the staging folder.
        password: its password; None extracts without one.

    Raises:
        SiteError: a wrong or missing password, or a damaged archive.
        MissingError: 7-Zip is not installed.
    """
    tool = sevenzip()
    try:
        tool.run("x", "-y", *_7z_password_args(password), f"-o{dest}", "--", str(path),
                 input=_7z_password_input(password))
    except RunError as error:
        raise _failed(path, error) from error


def _failed(path: Path, error: RunError) -> SiteError:
    """The `SiteError` for a failed 7-Zip run: a password problem, or a damaged archive."""
    said = f"{error.err}\n{error.out}".lower()
    if "password" in said:
        return SiteError(f"{path.name}: wrong or missing password")
    return SiteError(f"{path.name}: {error}")


# ---------------------------------------------------------------------------------------------
# Verifying and placing (the last two parts)


def _verify(stage: Path, entries: list[Entry], name: str) -> None:
    """Check every file entry is in the stage at its listed size.

    Args:
        stage: the staging folder.
        entries: what the listing said the archive holds.
        name: the archive's name, for the error.

    Raises:
        SiteError: a file is missing or the wrong size: the extract can't be trusted.
    """
    for entry in entries:
        if entry.kind != "file":
            continue
        written = _safe_dest(stage, entry.name)
        try:
            size = written.stat().st_size
        except OSError as error:
            raise disk(error, written) from error
        if entry.size is not None and size != entry.size:
            raise SiteError(f"{name}: {entry.name!r} came out {size} bytes, not {entry.size}")


def _place(stage: Path, dest: Path) -> None:
    """Move the verified stage to `dest`: one rename when `dest` is new, else file by file.

    A file already at `dest` from an earlier extract of the same archive is replaced.

    Args:
        stage: the verified staging folder.
        dest: the folder named after the archive.
    """
    if not dest.exists():
        os.replace(stage, dest)
        return
    for root, _, names in os.walk(stage):
        here = dest / Path(root).relative_to(stage)
        here.mkdir(parents=True, exist_ok=True)
        for name in names:
            os.replace(Path(root, name), here / name)