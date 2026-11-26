"""Zero-encryption streaming tar packing and helpers (`SPEC.md#level-3-volume`)."""

from __future__ import annotations

import hashlib
import os
import secrets
import shutil
import stat
import tarfile
from pathlib import Path, PurePosixPath
from typing import IO, Any

from flnh import errors
from flnkit import files

TAR_BUFSIZE = 1024 * 1024


def item_id(name: str) -> str:
    """Deterministic short identifier derived from a path without leaking it.

    Case is kept: on a case-sensitive filesystem `A/docs` and `a/docs` are two items.
    """
    return hashlib.sha256(name.encode("utf-8")).hexdigest()[:12]


def tree_size(path: Path) -> int:
    """Total byte size of `path` (file size, or sum of files in directory)."""
    if path.is_file() or path.is_symlink():
        return path.lstat().st_size
    total = 0
    for root, _, filenames in os.walk(path):
        for name in filenames:
            try:
                total += os.lstat(os.path.join(root, name)).st_size
            except OSError:
                pass
    return total


def estimate_tar_size(source: Path) -> int:
    """Upper-bound PAX tar byte size for `source` without reading file payloads."""
    if source.is_file() or source.is_symlink():
        st = source.lstat()
        blocks = ((st.st_size + 511) // 512) * 512 if stat.S_ISREG(st.st_mode) else 0
        return blocks + 2048 + tarfile.RECORDSIZE + 64 * 1024
    total = 2048
    for root, dirs, filenames in os.walk(source):
        for name in dirs:
            full = os.path.join(root, name)
            rel = os.path.relpath(full, source).encode("utf-8", errors="replace")
            total += 2048 + len(rel)
        for name in filenames:
            full = os.path.join(root, name)
            rel = os.path.relpath(full, source).encode("utf-8", errors="replace")
            try:
                st = os.lstat(full)
                blocks = ((st.st_size + 511) // 512) * 512 if stat.S_ISREG(st.st_mode) else 0
            except OSError:
                blocks = 0
            total += blocks + 2048 + len(rel)
    return total + tarfile.RECORDSIZE + 64 * 1024


def delete_plaintext(path: Path) -> None:
    """Delete the original plaintext file or directory after verified write."""
    if path.is_symlink() or path.is_file():
        path.unlink()
    else:
        shutil.rmtree(path)


class _HashingWriter:
    """Writable stream wrapper computing byte count and SHA-256 in one pass."""

    def __init__(self, inner: IO[bytes], hasher: Any) -> None:
        self._inner = inner
        self._hasher = hasher
        self.bytes_written = 0

    def write(self, data: Any) -> int:
        view = memoryview(data)
        self._hasher.update(view)
        n = self._inner.write(view)
        self.bytes_written += n
        return n

    def flush(self) -> None:
        self._inner.flush()


def pack_to_stream(source: Path, dest_stream: IO[bytes]) -> tuple[int, str, str]:
    """Stream `source` as an uncompressed PAX tar into `dest_stream`.

    Returns `(archive_size_bytes, sha256_hex, kind)` where `kind` is `'file'` or `'dir'`.
    """
    is_file = source.is_file() and not source.is_symlink()
    kind = "file" if is_file else "dir"
    hasher = hashlib.sha256()
    hw = _HashingWriter(dest_stream, hasher)
    with tarfile.open(
        fileobj=hw, mode="w|", format=tarfile.PAX_FORMAT, bufsize=TAR_BUFSIZE
    ) as tar:
        tar.add(source, arcname="payload" if is_file else ".", recursive=not is_file)
    hw.flush()
    return hw.bytes_written, hasher.hexdigest(), kind


def pack_to_file(source: Path, dest: Path) -> tuple[int, str, str]:
    """Stream `source` as an uncompressed PAX tar into `dest`."""
    with files.part(dest) as held:
        with os.fdopen(held.fd, "wb", closefd=False) as raw:
            size, sha256, kind = pack_to_stream(source, raw)
            raw.flush()
            os.fsync(held.fd)
        held.size = dest.parent.joinpath(held.path.name).stat().st_size
    return size, sha256, kind


def _chown(path: Path, member: tarfile.TarInfo) -> None:
    """Give `path` back to the archive's owner when running as root (a `sudo` restore)."""
    if os.name == "nt" or os.geteuid() != 0:
        return
    try:
        os.chown(path, member.uid, member.gid, follow_symlinks=False)
    except OSError:
        pass


def _check_member(member: tarfile.TarInfo) -> None:
    if member.isdev() or member.ischr() or member.isblk() or member.isfifo():
        raise errors.CorruptHeaderError(f"unsupported special file in archive: {member.name}")


def check_archive(archive_path: Path, *, kind: str) -> None:
    """Read `archive_path` through once and refuse it if extracting it would fail.

    Checks entry names and types without writing anything, so a restore can stop while the
    archive is still safe in the disk tail.
    """
    try:
        with open(archive_path, "rb") as f, tarfile.open(
            fileobj=f, mode="r|*", bufsize=TAR_BUFSIZE
        ) as tar:
            members = 0
            for member in tar:
                members += 1
                if kind == "file":
                    if members > 1 or not member.isfile():
                        raise errors.CorruptHeaderError("archive payload is not a single file")
                    continue
                _check_member(member)
                _check_name(member.name)
                if member.islnk():
                    _check_name(member.linkname)
    except tarfile.TarError as exc:
        raise errors.CorruptHeaderError(f"hidden archive is not a readable tar: {exc}") from exc
    if kind == "file" and members != 1:
        raise errors.CorruptHeaderError("archive payload is missing file entry")


def _check_name(name: str) -> PurePosixPath:
    clean = name.replace("\\", "/")
    if clean.startswith("/") or (len(clean) >= 2 and clean[1] == ":"):
        raise errors.CorruptHeaderError(f"unsafe path in archive: {name!r}")
    parts = PurePosixPath(clean)
    if any(part == ".." for part in parts.parts):
        raise errors.CorruptHeaderError(f"path traversal in archive: {name!r}")
    return parts


def _safe_member_path(root: Path, name: str) -> Path:
    if not name or name == ".":
        return root
    parts = _check_name(name).parts
    if not parts:
        return root
    target = (root / Path(*parts)).resolve()
    root_resolved = root.resolve()
    if target != root_resolved and not target.is_relative_to(root_resolved):
        raise errors.CorruptHeaderError(f"path escapes destination: {name!r}")
    return target


def unpack_from_stream(stream: IO[bytes], dest: Path, *, kind: str) -> None:
    """Atomically extract tar `stream` to `dest` (`kind` is `'file'` or `'dir'`)."""
    if dest.exists() or dest.is_symlink():
        raise errors.UsageError(
            f"destination already exists: {dest}",
            hint="move or delete the existing path before running flnh show",
        )
    dest.parent.mkdir(parents=True, exist_ok=True)
    if kind == "file":
        _extract_single_file(stream, dest)
        return
    stage = dest.parent / f".flnh-tmp-{secrets.token_hex(6)}"
    stage.mkdir(parents=True, exist_ok=False)
    try:
        _extract_dir(stream, stage)
        os.replace(stage, dest)
    except BaseException:
        shutil.rmtree(stage, ignore_errors=True)
        raise


def unpack_from_file(archive_path: Path, dest: Path, *, kind: str) -> None:
    """Atomically extract `archive_path` to `dest` (`kind` is `'file'` or `'dir'`)."""
    with open(archive_path, "rb") as f:
        unpack_from_stream(f, dest, kind=kind)


def _extract_single_file(stream: IO[bytes], dest: Path) -> None:
    with tarfile.open(fileobj=stream, mode="r|*", bufsize=TAR_BUFSIZE) as tar:
        member = tar.next()
        if member is None or not member.isfile():
            raise errors.CorruptHeaderError("archive payload is missing file entry")
        extracted = tar.extractfile(member)
        if extracted is None:
            raise errors.CorruptHeaderError("archive payload stream is unreadable")
        with files.part(dest) as held:
            with os.fdopen(held.fd, "wb", closefd=False) as out:
                shutil.copyfileobj(extracted, out, length=TAR_BUFSIZE)
                out.flush()
                os.fsync(held.fd)
            held.size = dest.parent.joinpath(held.path.name).stat().st_size
            held.mtime = member.mtime
        if os.name != "nt":
            try:
                os.chmod(dest, stat.S_IMODE(member.mode))
            except OSError:
                pass
        _chown(dest, member)
        if tar.next() is not None:
            raise errors.CorruptHeaderError("unexpected extra entries in single-file archive")


def _extract_dir(stream: IO[bytes], stage: Path) -> None:
    dir_times: list[tuple[Path, float, int]] = []
    stage_resolved = stage.resolve()
    with tarfile.open(fileobj=stream, mode="r|*", bufsize=TAR_BUFSIZE) as tar:
        for member in tar:
            _check_member(member)
            target = _safe_member_path(stage, member.name)
            if member.isdir():
                target.mkdir(parents=True, exist_ok=True)
                _chown(target, member)
                dir_times.append((target, member.mtime, member.mode))
            elif member.issym():
                # A symlink may point anywhere, as it did before hiding: making it writes
                # nothing there, and every later entry is resolved through it and checked.
                target.parent.mkdir(parents=True, exist_ok=True)
                os.symlink(member.linkname, target)
                _chown(target, member)
            elif member.islnk():
                resolved = _safe_member_path(stage, member.linkname)
                if resolved != stage_resolved and not resolved.is_relative_to(stage_resolved):
                    raise errors.CorruptHeaderError(
                        f"unsafe link target in archive: {member.name}"
                    )
                target.parent.mkdir(parents=True, exist_ok=True)
                os.link(resolved, target)
            elif member.isfile():
                target.parent.mkdir(parents=True, exist_ok=True)
                src = tar.extractfile(member)
                if src is None:
                    raise errors.CorruptHeaderError(f"unreadable entry in archive: {member.name}")
                with open(target, "wb") as out:
                    shutil.copyfileobj(src, out, length=TAR_BUFSIZE)
                _chown(target, member)
                if os.name != "nt":
                    try:
                        os.chmod(target, stat.S_IMODE(member.mode))
                    except OSError:
                        pass
                try:
                    os.utime(target, (member.mtime, member.mtime))
                except OSError:
                    pass
    for dpath, mtime, mode in reversed(dir_times):
        if os.name != "nt":
            try:
                os.chmod(dpath, stat.S_IMODE(mode))
            except OSError:
                pass
        try:
            os.utime(dpath, (mtime, mtime))
        except OSError:
            pass
