"""Level 3 vault: streaming AES-256-GCM + scrypt pack/unpack (`SPEC.md#level-3-vault`)."""

from __future__ import annotations

import hashlib
import hmac
import io
import os
import secrets
import shutil
import stat
import struct
import sys
import tarfile
from pathlib import Path, PurePosixPath
from typing import IO, Any

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.scrypt import Scrypt

from flnh import errors
from flnkit import files

MAGIC = b"FLNHV1\x00\x01"
HEADER_FMT = ">8sBBBB32s8sI"
HEADER_SIZE = struct.calcsize(HEADER_FMT)
CHUNK_HDR_FMT = ">IB"
CHUNK_HDR_SIZE = struct.calcsize(CHUNK_HDR_FMT)
GCM_TAG_SIZE = 16

FLAG_SINGLE_FILE = 1
FLAG_KEYFILE = 2

SCRYPT_N_LOG2 = 15
SCRYPT_R = 8
SCRYPT_P = 1
CHUNK_SIZE = 1024 * 1024

WIN_CLSID = "{21EC2020-3AEA-1069-A2DD-08002B30309D}"


def item_id(name: str) -> str:
    """Deterministic short identifier derived from a filename without leaking it."""
    return hashlib.sha256(name.casefold().encode("utf-8")).hexdigest()[:12]


def derive_key(
    secret: bytes,
    salt: bytes,
    *,
    n_log2: int = SCRYPT_N_LOG2,
    r: int = SCRYPT_R,
    p: int = SCRYPT_P,
) -> bytes:
    """Derive a 256-bit key using scrypt."""
    if not (1 <= n_log2 <= 22 and 1 <= r <= 32 and 1 <= p <= 16):
        raise errors.CorruptHeaderError("invalid scrypt parameters in vault header")
    kdf = Scrypt(salt=salt, length=32, n=1 << n_log2, r=r, p=p)
    return kdf.derive(secret)


def hash_password(password: str) -> tuple[str, str]:
    """Return `(salt_hex, hash_hex)` for verifying Level 2 `--pass` locks."""
    salt = os.urandom(16)
    digest = derive_key(password.encode("utf-8"), salt)
    return salt.hex(), digest.hex()


def verify_password(password: str, salt_hex: str, hash_hex: str) -> bool:
    """Verify a password against a stored `(salt_hex, hash_hex)` pair."""
    try:
        salt = bytes.fromhex(salt_hex)
    except ValueError:
        return False
    digest = derive_key(password.encode("utf-8"), salt)
    return hmac.compare_digest(digest.hex(), hash_hex)


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


class _EncryptStream(io.RawIOBase):
    """Writable stream that chunks plaintext and writes authenticated AES-256-GCM frames."""

    def __init__(
        self,
        out: IO[bytes],
        secret: bytes,
        *,
        flags: int,
        chunk_size: int = CHUNK_SIZE,
    ) -> None:
        self._out = out
        self._chunk_size = chunk_size
        salt = os.urandom(32)
        nonce_prefix = os.urandom(8)
        self._nonce_prefix = nonce_prefix
        self._header = struct.pack(
            HEADER_FMT,
            MAGIC,
            flags,
            SCRYPT_N_LOG2,
            SCRYPT_R,
            SCRYPT_P,
            salt,
            nonce_prefix,
            chunk_size,
        )
        key = derive_key(secret, salt)
        self._aesgcm = AESGCM(key)
        self._buf = bytearray()
        self._index = 0
        self._closed = False
        self._out.write(self._header)

    def writable(self) -> bool:
        return True

    def write(self, data: Any) -> int:
        if self._closed:
            raise ValueError("write to closed encrypt stream")
        view = memoryview(data)
        self._buf.extend(view)
        while len(self._buf) > self._chunk_size:
            chunk = bytes(self._buf[: self._chunk_size])
            del self._buf[: self._chunk_size]
            self._emit(chunk, last=0)
        return len(view)

    def _emit(self, plain: bytes, *, last: int) -> None:
        nonce = self._nonce_prefix + struct.pack(">I", self._index)
        aad = self._header + struct.pack(">IB", self._index, last)
        ct = self._aesgcm.encrypt(nonce, plain, aad)
        self._out.write(struct.pack(CHUNK_HDR_FMT, len(ct), last) + ct)
        self._index += 1

    def finish(self) -> None:
        if not self._closed:
            self._emit(bytes(self._buf), last=1)
            self._buf.clear()
            self._closed = True


class _DecryptStream(io.RawIOBase):
    """Readable stream that verifies and decrypts AES-256-GCM vault frames."""

    def __init__(self, inp: IO[bytes], secret: bytes) -> None:
        self._inp = inp
        header = _read_exact(inp, HEADER_SIZE)
        if len(header) != HEADER_SIZE:
            raise errors.CorruptHeaderError("vault header is truncated")
        magic, flags, n_log2, r, p, salt, nonce_prefix, chunk_size = struct.unpack(
            HEADER_FMT, header
        )
        if magic != MAGIC:
            raise errors.CorruptHeaderError("not a valid flnh vault")
        if chunk_size < 1024 or chunk_size > 64 * 1024 * 1024:
            raise errors.CorruptHeaderError("invalid vault chunk size")
        self.flags = flags
        self._header = header
        self._nonce_prefix = nonce_prefix
        self._chunk_size = chunk_size
        key = derive_key(secret, salt, n_log2=n_log2, r=r, p=p)
        self._aesgcm = AESGCM(key)
        self._index = 0
        self._eof = False
        self._buf = bytearray()

    def readable(self) -> bool:
        return True

    def _next_chunk(self) -> bool:
        if self._eof:
            return False
        hdr = _read_exact(self._inp, CHUNK_HDR_SIZE)
        if not hdr:
            raise errors.CorruptHeaderError("vault truncated before end-of-stream marker")
        if len(hdr) != CHUNK_HDR_SIZE:
            raise errors.CorruptHeaderError("vault chunk header is truncated")
        ct_len, last = struct.unpack(CHUNK_HDR_FMT, hdr)
        if last not in (0, 1):
            raise errors.CorruptHeaderError("invalid vault chunk flag")
        if ct_len < GCM_TAG_SIZE or ct_len > self._chunk_size + GCM_TAG_SIZE:
            raise errors.CorruptHeaderError("invalid vault chunk length")
        ct = _read_exact(self._inp, ct_len)
        if len(ct) != ct_len:
            raise errors.CorruptHeaderError("vault chunk payload is truncated")
        nonce = self._nonce_prefix + struct.pack(">I", self._index)
        aad = self._header + struct.pack(">IB", self._index, last)
        try:
            plain = self._aesgcm.decrypt(nonce, ct, aad)
        except InvalidTag as exc:
            if self._index == 0:
                raise errors.AuthError(
                    "wrong password or keyfile",
                    hint="check the password or --keyfile and try again",
                ) from exc
            raise errors.CorruptHeaderError("vault chunk failed authentication") from exc
        self._index += 1
        self._buf.extend(plain)
        if last == 1:
            if self._inp.read(1):
                raise errors.CorruptHeaderError("unexpected trailing bytes after vault end")
            self._eof = True
        return True

    def read(self, size: int = -1) -> bytes:
        if size == 0:
            return b""
        if size is None or size < 0:
            while self._next_chunk():
                pass
            out = bytes(self._buf)
            self._buf.clear()
            return out
        while len(self._buf) < size and self._next_chunk():
            pass
        out = bytes(self._buf[:size])
        del self._buf[:size]
        return out

    def drain_verify(self) -> None:
        """Consume and authenticate all remaining frames through EOF."""
        while self._next_chunk():
            self._buf.clear()


def _read_exact(stream: IO[bytes], n: int) -> bytes:
    chunks: list[bytes] = []
    remaining = n
    while remaining > 0:
        piece = stream.read(remaining)
        if not piece:
            break
        chunks.append(piece)
        remaining -= len(piece)
    return b"".join(chunks)


def pack_to_file(
    source: Path,
    dest_vault: Path,
    secret: bytes,
    *,
    is_keyfile: bool = False,
    chunk_size: int = CHUNK_SIZE,
) -> tuple[int, str]:
    """Pack and encrypt `source` atomically into `dest_vault`, then verify.

    Returns `(vault_size_bytes, sha256_hex)`.
    """
    is_file = source.is_file() and not source.is_symlink()
    flags = (FLAG_SINGLE_FILE if is_file else 0) | (FLAG_KEYFILE if is_keyfile else 0)
    with files.part(dest_vault) as held:
        with os.fdopen(held.fd, "wb", closefd=False) as raw:
            enc = _EncryptStream(raw, secret, flags=flags, chunk_size=chunk_size)
            with tarfile.open(fileobj=enc, mode="w|", format=tarfile.PAX_FORMAT) as tar:
                if is_file:
                    tar.add(source, arcname="payload", recursive=False)
                else:
                    tar.add(source, arcname=".", recursive=True)
            enc.finish()
            raw.flush()
            os.fsync(held.fd)
        held.size = dest_vault.parent.joinpath(held.path.name).stat().st_size
    sha256 = verify_vault(dest_vault, secret)
    return dest_vault.stat().st_size, sha256


def verify_vault(vault_path: Path, secret: bytes) -> str:
    """Verify header, every GCM chunk tag, and EOF of `vault_path`, returning SHA-256 hex."""
    hasher = hashlib.sha256()
    with open(vault_path, "rb") as f:
        dec = _DecryptStream(_HashingReader(f, hasher), secret)
        dec.drain_verify()
    return hasher.hexdigest()


class _HashingReader:
    def __init__(self, inner: IO[bytes], hasher: Any) -> None:
        self._inner = inner
        self._hasher = hasher

    def read(self, n: int = -1) -> bytes:
        data = self._inner.read(n)
        if data:
            self._hasher.update(data)
        return data


def _safe_member_path(root: Path, name: str) -> Path:
    if not name or name == ".":
        return root
    clean = name.replace("\\", "/")
    if clean.startswith("/") or (len(clean) >= 2 and clean[1] == ":"):
        raise errors.CorruptHeaderError(f"unsafe path in vault: {name!r}")
    parts = PurePosixPath(clean).parts
    if any(part == ".." for part in parts):
        raise errors.CorruptHeaderError(f"path traversal in vault: {name!r}")
    target = (root / Path(*parts)).resolve()
    root_resolved = root.resolve()
    if target != root_resolved and not target.is_relative_to(root_resolved):
        raise errors.CorruptHeaderError(f"path escapes destination: {name!r}")
    return target


def unpack_from_file(vault_path: Path, dest: Path, secret: bytes) -> str:
    """Decrypt and atomically restore `vault_path` to `dest`, returning `'file'` or `'dir'`."""
    if dest.exists() or dest.is_symlink():
        raise errors.UsageError(
            f"destination already exists: {dest}",
            hint="move or delete the existing path before running flnh show",
        )
    dest.parent.mkdir(parents=True, exist_ok=True)
    with open(vault_path, "rb") as f:
        dec = _DecryptStream(f, secret)
        is_file = bool(dec.flags & FLAG_SINGLE_FILE)
        if is_file:
            _extract_single_file(dec, dest)
            dec.drain_verify()
            return "file"
        stage = dest.parent / f".flnh-tmp-{secrets.token_hex(6)}"
        stage.mkdir(parents=True, exist_ok=False)
        try:
            _extract_dir(dec, stage)
            dec.drain_verify()
            os.replace(stage, dest)
        except BaseException:
            shutil.rmtree(stage, ignore_errors=True)
            raise
        return "dir"


def _extract_single_file(dec: _DecryptStream, dest: Path) -> None:
    with tarfile.open(fileobj=dec, mode="r|*") as tar:
        member = tar.next()
        if member is None or not member.isfile():
            raise errors.CorruptHeaderError("vault payload is missing file entry")
        extracted = tar.extractfile(member)
        if extracted is None:
            raise errors.CorruptHeaderError("vault payload stream is unreadable")
        with files.part(dest) as held:
            with os.fdopen(held.fd, "wb", closefd=False) as out:
                shutil.copyfileobj(extracted, out, length=128 * 1024)
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
            raise errors.CorruptHeaderError("unexpected extra entries in single-file vault")


def _extract_dir(dec: _DecryptStream, stage: Path) -> None:
    dir_times: list[tuple[Path, float, int]] = []
    stage_resolved = stage.resolve()
    with tarfile.open(fileobj=dec, mode="r|*") as tar:
        for member in tar:
            if member.isdev() or member.ischr() or member.isblk() or member.isfifo():
                raise errors.CorruptHeaderError(f"unsupported special file in vault: {member.name}")
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
                        f"unsafe link target in vault: {member.name}"
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
                    raise errors.CorruptHeaderError(f"unreadable entry in vault: {member.name}")
                with open(target, "wb") as out:
                    shutil.copyfileobj(src, out, length=128 * 1024)
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


def _vault_location(orig_path: Path) -> tuple[Path, Path | None]:
    """Return `(vault_file_path, wrapper_dir_or_none)` for `orig_path`."""
    vid = item_id(orig_path.name)
    parent = orig_path.parent
    if sys.platform == "win32":
        wrapper = parent / f"Control Panel.{vid}.{WIN_CLSID}"
        return wrapper / "data.flnh", wrapper
    return parent / f".flnh-{vid}.vault", None


def _vanish_vault(vault_file: Path, wrapper: Path | None) -> None:
    if sys.platform == "win32":
        from flnh.os_hide import win32

        win32.add_hidden_system(vault_file)
        if wrapper is not None:
            win32.add_hidden_system(wrapper)
    elif sys.platform == "darwin":
        from flnh.os_hide import darwin

        darwin.set_hidden_flag(vault_file, True)
    else:
        from flnh.os_hide import linux

        linux.add_dot_hidden(vault_file.parent, vault_file.name)


def _remove_vanished_vault(vault_file: Path, wrapper: Path | None) -> None:
    if sys.platform == "win32":
        from flnh.os_hide import win32

        if vault_file.exists():
            win32.clear_hidden_system(vault_file)
            vault_file.unlink()
        if wrapper is not None and wrapper.exists():
            win32.clear_hidden_system(wrapper)
            try:
                wrapper.rmdir()
            except OSError:
                shutil.rmtree(wrapper, ignore_errors=True)
    elif sys.platform == "darwin":
        from flnh.os_hide import darwin

        if vault_file.exists():
            darwin.set_hidden_flag(vault_file, False)
            vault_file.unlink()
    else:
        from flnh.os_hide import linux

        if vault_file.exists():
            vault_file.unlink()
        linux.remove_dot_hidden(vault_file.parent, vault_file.name)


def delete_plaintext(path: Path) -> None:
    """Delete the original plaintext file or directory after verified vault write."""
    if path.is_symlink() or path.is_file():
        path.unlink()
    else:
        shutil.rmtree(path)


def hide(
    path: Path,
    secret: bytes,
    *,
    is_keyfile: bool = False,
) -> dict[str, Any]:
    """Encrypt `path` into a vanished vault and remove plaintext after verification."""
    if not path.exists():
        raise errors.UsageError(f"path not found: {path}")
    if path.parent == path:
        raise errors.SafetyError(f"refusing to hide root path: {path}")
    vault_file, wrapper = _vault_location(path)
    if vault_file.exists() or (wrapper is not None and wrapper.exists()):
        raise errors.UsageError(
            f"already hidden: {path}",
            hint=f"run flnh show {path} to restore it",
        )
    orig_size = tree_size(path)
    kind = "file" if path.is_file() and not path.is_symlink() else "dir"
    if wrapper is not None:
        wrapper.mkdir(parents=True, exist_ok=True)
    try:
        vault_size, sha256 = pack_to_file(path, vault_file, secret, is_keyfile=is_keyfile)
        _vanish_vault(vault_file, wrapper)
    except BaseException:
        _remove_vanished_vault(vault_file, wrapper)
        raise
    delete_plaintext(path)
    return {
        "kind": kind,
        "hidden_path": str(vault_file),
        "size_bytes": orig_size,
        "meta": {
            "wrapper": str(wrapper) if wrapper is not None else "",
            "vault_size": vault_size,
            "sha256": sha256,
            "keyfile": is_keyfile,
        },
    }


def locate(orig_path: Path, entry: Any = None) -> tuple[Path, Path | None] | None:
    """Locate a vanished vault for `orig_path` (from `entry` or deterministic path)."""
    if entry is not None:
        vf = Path(entry.hidden_path)
        wrap_str = entry.meta.get("wrapper", "")
        wrap = Path(wrap_str) if wrap_str else None
        if vf.exists():
            return vf, wrap
    vf, wrap = _vault_location(orig_path)
    if vf.exists():
        return vf, wrap
    return None


def show(
    orig_path: Path,
    secret: bytes,
    *,
    entry: Any = None,
) -> Path:
    """Decrypt and restore a vanished Level 3 vault to `orig_path`."""
    found = locate(orig_path, entry)
    if found is None:
        raise errors.UsageError(
            f"no hidden vault found for: {orig_path}",
            hint="run flnh show --list to see tracked items",
        )
    vault_file, wrapper = found
    unpack_from_file(vault_file, orig_path, secret)
    _remove_vanished_vault(vault_file, wrapper)
    return orig_path
