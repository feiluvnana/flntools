"""Safe file writes: whole files through locked parts, plus walking, linking and file kinds.

A library module: it imports only the stdlib, the clock library (`flnd._time`) and flnd's error
classes, and any program that writes files several processes may race on can use it. It offers:
- `part(dest, resume=…, digest=…)`: a context manager yielding a locked `Part` to write into,
  renamed onto `dest` only when the block ends cleanly;
- `write(dest, data)`: a small file written whole through a part;
- `walk(folder)`: every file under a folder but parts and their sidecars;
- `digest(path)`: a file's SHA-256; `link(source, dest)`: an atomic hard link;
- `sweep(folder)`: removes the stale parts no process holds;
- `kind_of(name)` and `sniff(bytes)`: whether a file is an image, a video or audio, by its name
  or by its first bytes.

Every file written goes through a **part**: the file `<dest>.part` beside its destination. The
writer opens the part, locks it, writes into it and renames it onto `dest`. A file at its real
name is therefore always whole, and the part is its own lock: there is no lock file and no
folder-wide lock.

Who calls it in flnd: the downloader (`flnd._net`, `flnd._transfer`) with `resume=True`, and
flnd's own small writes (`write`: cookies, archives, the store converter, the tool version
cache) with the default `resume=False`. The job sweeps a target's folder of stale parts
(`sweep`) and walks it (`walk`) to find what is saved.

How one write goes (`part`):
1. Open `<dest>.part`. A part that doesn't exist yet is created exclusively (`O_EXCL`), so no
   other run can hold it; one that exists is opened as it is.
2. Lock it: a non-blocking `fcntl.flock` on the descriptor (`msvcrt.locking` on Windows). For a
   download, a part another run holds raises `BusyError`, and the job skips that file as "being
   saved by another run". A small write waits for the other writer instead.
3. For a part that was there already, check that the path still names the file locked: another
   run may have renamed it into place between our open and our lock. That costs one `stat`.
4. The caller writes. A download resumes from `Part.size`; a small write starts from empty.
5. On a clean exit: set the modified time on the descriptor (`os.utime(fd)`), read the size and
   time back with one `fstat`, rename onto `dest`, close. On POSIX the rename happens while the
   lock is held. Windows can't rename an open file, so there it closes first.
   A part opened with `digest=True` also gives the SHA-256 of what landed (`Saved.hash`): from
   the bytes as `write` appended them, so a straight download reads nothing back; or, when the
   bytes came another way (a resumed part, `write_at`, a tool writing by name), by reading the
   part once through its descriptor before the rename.
6. On a failure: a download's part is kept for the next run, with its HTTP validator in
   `<dest>.part.json`; an empty part, and any part of a small write, is removed.

Rules a change must keep:
- No fsync: media can be downloaded again, and SQLite keeps its own durability.
- No extra `stat` per file. `tests/test_fs.py` counts the system calls one download makes.
- A digest is of the bytes that land, whole: never of a part half written.
- A part's name, with `.json` and a small write's own `.part` after it, fits in 255 bytes: a
  longer name is shortened to a head and a digest (`_part_path`).
"""

from __future__ import annotations

import errno
import hashlib
import io
import json
import mimetypes
import os
import threading
from collections.abc import Iterator
from contextlib import contextmanager, suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from flnd import _time
from flnd.errors import DiskError, disk

__all__ = ["AUDIO", "BusyError", "IMAGE", "Part", "SNIFF", "Saved", "VIDEO", "digest",
           "kind_of", "link", "part", "sniff", "sweep", "walk", "write"]

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
# What older flnd versions left: hidden `.<name>.stage` (or `.part`) files.
OLD_PARTS = (".stage", ".part")
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

    def flush_note(self) -> None:
        """Write the sidecar now, before bytes it describes are written (segmented downloads)."""
        _write_side(self.side_path, self.meta)
        self.side = True

    def write(self, data: bytes) -> None:
        """Append `data` after the part's bytes, through a 1 MiB buffer.

        Args:
            data: the bytes, which reach the file by 1 MiB at a time or at `flush`.
        """
        if self._buffer is None:
            self._buffer = open(self.fd, "r+b", buffering=BUFFER, closefd=False)  # noqa: SIM115
            self._buffer.seek(self.size)
        self._buffer.write(data)
        self.size += len(data)
        if self._sha is not None:
            # hashlib lets go of the GIL for a chunk this size, so other downloads go on.
            self._sha.update(data)

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
        fd, size = _open(path, wait=not resume)
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
    now = _time.wall()
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


def _open(path: Path, *, wait: bool) -> tuple[int, int]:
    """Open and lock the part at `path`, returning its descriptor and the bytes it holds.

    1. Create it exclusively: a new part needs no check, since no one else had it.
    2. Else open it as it is, lock it, and check the path still names it, as the module says.
       When it doesn't, another run has just landed it: a download is busy, a write tries again.

    Args:
        path: the part.
        wait: wait for another holder (a small write) instead of raising `BusyError`.

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
                                hint="let the other flnd run finish; the next run skips it")
            if fresh:
                return fd, 0
            found = os.fstat(fd)
            if _names(path, found):
                return fd, found.st_size
            if not wait:
                raise BusyError(f"{path.name}: just saved by another run")
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
        if not WINDOWS:
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
            _time.sleep(REPLACE_PAUSE)


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
    return name.endswith(PART) or (name.startswith(".") and name.endswith(OLD_PARTS))


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
