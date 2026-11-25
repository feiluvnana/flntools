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
    """Deterministic short identifier derived from a filename without leaking it."""
    return hashlib.sha256(name.casefold().encode("utf-8")).hexdigest()[:12]


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


def delete_plaintext(path: Path) -> None:
    """Delete the original plaintext file or directory after verified write."""
    if path.is_symlink() or path.is_file():
        path.unlink()
    else:
        shutil.rmtree(path)


class _HashingWriter:
    """Writable file wrapper computing SHA-256 in one streaming pass."""

    def __init__(self, inner: IO[bytes], hasher: Any) -> None:
        self._inner = inner
        self._hasher = hasher

    def write(self, data: Any) -> int:
        view = memoryview(data)
        self._hasher.update(view)
        return self._inner.write(view)

    def flush(self) -> None:
        self._inner.flush()


def pack_to_file(source: Path, dest: Path) -> tuple[int, str, str]:
    """Stream `source` as an uncompressed PAX tar into `dest`.

    Returns `(archive_size_bytes, sha256_hex, kind)` where `kind` is `'file'` or `'dir'`.
    """
    is_file = source.is_file() and not source.is_symlink()
    kind = "file" if is_file else "dir"
    hasher = hashlib.sha256()
    with files.part(dest) as held:
        with os.fdopen(held.fd, "wb", closefd=False) as raw:
            hw = _HashingWriter(raw, hasher)
            with tarfile.open(
                fileobj=hw, mode="w|", format=tarfile.PAX_FORMAT, bufsize=TAR_BUFSIZE
            ) as tar:
                tar.add(source, arcname="payload" if is_file else ".", recursive=not is_file)
            raw.flush()
            os.fsync(held.fd)
        held.size = dest.parent.joinpath(held.path.name).stat().st_size
    return dest.stat().st_size, hasher.hexdigest(), kind


def _safe_member_path(root: Path, name: str) -> Path:
    if not name or name == ".":
        return root
    clean = name.replace("\\", "/")
    if clean.startswith("/") or (len(clean) >= 2 and clean[1] == ":"):
        raise errors.CorruptHeaderError(f"unsafe path in archive: {name!r}")
    parts = PurePosixPath(clean).parts
    if any(part == ".." for part in parts):
        raise errors.CorruptHeaderError(f"path traversal in archive: {name!r}")
    target = (root / Path(*parts)).resolve()
    root_resolved = root.resolve()
    if target != root_resolved and not target.is_relative_to(root_resolved):
        raise errors.CorruptHeaderError(f"path escapes destination: {name!r}")
    return target


def unpack_from_file(archive_path: Path, dest: Path, *, kind: str) -> None:
    """Atomically extract `archive_path` to `dest` (`kind` is `'file'` or `'dir'`)."""
    if dest.exists() or dest.is_symlink():
        raise errors.UsageError(
            f"destination already exists: {dest}",
            hint="move or delete the existing path before running flnh show",
        )
    dest.parent.mkdir(parents=True, exist_ok=True)
    with open(archive_path, "rb") as f:
        if kind == "file":
            _extract_single_file(f, dest)
            return
        stage = dest.parent / f".flnh-tmp-{secrets.token_hex(6)}"
        stage.mkdir(parents=True, exist_ok=False)
        try:
            _extract_dir(f, stage)
            os.replace(stage, dest)
        except BaseException:
            shutil.rmtree(stage, ignore_errors=True)
            raise


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
        if tar.next() is not None:
            raise errors.CorruptHeaderError("unexpected extra entries in single-file archive")


def _extract_dir(stream: IO[bytes], stage: Path) -> None:
    dir_times: list[tuple[Path, float, int]] = []
    stage_resolved = stage.resolve()
    with tarfile.open(fileobj=stream, mode="r|*", bufsize=TAR_BUFSIZE) as tar:
        for member in tar:
            if member.isdev() or member.ischr() or member.isblk() or member.isfifo():
                raise errors.CorruptHeaderError(
                    f"unsupported special file in archive: {member.name}"
                )
            target = _safe_member_path(stage, member.name)
            if member.isdir():
                target.mkdir(parents=True, exist_ok=True)
                dir_times.append((target, member.mtime, member.mode))
            elif member.issym() or member.islnk():
                link_target = member.linkname
                if member.issym():
                    resolved = (target.parent / link_target).resolve()
                else:
                    resolved = _safe_member_path(stage, link_target)
                if resolved != stage_resolved and not resolved.is_relative_to(stage_resolved):
                    raise errors.CorruptHeaderError(
                        f"unsafe link target in archive: {member.name}"
                    )
                target.parent.mkdir(parents=True, exist_ok=True)
                if member.issym():
                    os.symlink(link_target, target)
                else:
                    os.link(resolved, target)
            elif member.isfile():
                target.parent.mkdir(parents=True, exist_ok=True)
                src = tar.extractfile(member)
                if src is None:
                    raise errors.CorruptHeaderError(f"unreadable entry in archive: {member.name}")
                with open(target, "wb") as out:
                    shutil.copyfileobj(src, out, length=TAR_BUFSIZE)
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
