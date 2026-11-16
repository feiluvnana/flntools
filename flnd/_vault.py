"""Secrets sealed with AES-256-GCM, and a vault of them in one SQLite file under a key file.

What it offers, for any caller:
- `encrypt` and `decrypt`: authenticated encryption of a value under a 32-byte key, bound to a
  context (associated data), so a sealed value moved elsewhere doesn't open.
- `Vault(path, key_path, slot)`: one slot's secrets as a mapping of text to text, each value
  sealed in its own row of the database at `path`, under the key in `key_path`.
- `key(path)`: the key in a key file, made with mode 0600 on first use.
- `digest`: the hex hash of text, bytes, a file or a stream.

    with Vault(Path("secrets.db"), Path("secret.key"), "tlg") as saved:
        saved["session"] = "1BVts…"

Rules a change must keep:
- A secret's value never reaches a message: every way decryption fails says the same thing.
- Opening a vault creates nothing: the database is made on the first write, the key on the
  first value read or written.
- The key file is made with `O_EXCL` and mode 0600, so two processes never make two keys; one
  that finds a short key waits `KEY_WAIT` for the other to finish writing it.
- It imports only the stdlib, `cryptography` (on first use), `flnd._db` and `flnd.errors`.
"""

from __future__ import annotations

import hashlib
import io
import os
import secrets as _secrets
import time
from collections.abc import Iterator, MutableMapping
from pathlib import Path
from typing import TYPE_CHECKING, Any, BinaryIO, Union, cast

from flnd import _db
from flnd.errors import DiskError, LoginError, ToolError

if TYPE_CHECKING:
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

__all__ = ["Vault", "decrypt", "digest", "encrypt", "key"]

# ---------------------------------------------------------------------------------------------
# Sealing
# ---------------------------------------------------------------------------------------------

Data = Union[str, bytes, bytearray, memoryview]
HashInput = Union[str, bytes, bytearray, memoryview, Path, BinaryIO]

# The blob is one version byte, a 12-byte random nonce (GCM's standard size), then the
# ciphertext with its 16-byte tag. The format is fixed: vaults hold blobs of version 1.
GCM_VERSION = 1
GCM_NONCE_SIZE = 12
GCM_TAG_SIZE = 16
# AES-256: the only key size, the size `secrets.token_bytes(32)` makes.
KEY_SIZE = 32
# One message for every way decryption fails, so it says nothing about the key or the data.
DECRYPT_FAILED = "cannot decrypt: wrong key or damaged data"
# What a blob that does not open says to do; a `Vault` gives its own advice instead.
SEAL_AGAIN = "seal it again, with the key it is opened with"
MISSING_CRYPTOGRAPHY = "sealing secrets needs cryptography"


def digest(data: HashInput, *, algorithm: str = "sha256") -> str:
    """The hex digest of text, bytes-like data, a `Path`, or a binary stream.

    Args:
        data: what to hash; a `Path` is read as a file, text as UTF-8.
        algorithm: any `hashlib` name.

    Returns:
        The digest, in lowercase hex.

    Raises:
        TypeError: `data` is none of those.

    Example:
        >>> digest("abc")[:16]
        'ba7816bf8f01cfea'
    """
    if isinstance(data, Path):
        with data.open("rb") as handle:
            return digest(handle, algorithm=algorithm)
    if isinstance(data, str):
        return hashlib.new(algorithm, data.encode("utf-8")).hexdigest()
    if isinstance(data, (bytes, bytearray, memoryview)):
        return hashlib.new(algorithm, data).hexdigest()
    if isinstance(data, io.BytesIO):
        view = data.getbuffer()[data.tell():]
        data.seek(0, io.SEEK_END)
        return hashlib.new(algorithm, view).hexdigest()
    if hasattr(data, "read"):
        return hashlib.file_digest(cast(Any, data), algorithm).hexdigest()
    raise TypeError(
        f"cannot hash {type(data).__name__}: pass str, bytes, a Path, or a binary file"
    )


def encrypt(data: Data, key: bytes, *, context: bytes = b"") -> bytes:
    """Seal `data` with AES-256-GCM into a blob only `decrypt`, with the same key, opens.

    Args:
        data: the secret; text is sealed as UTF-8.
        key: 32 bytes.
        context: associated data the blob is bound to: `decrypt` needs the same.

    Returns:
        The version byte, the nonce, then the ciphertext and its tag.

    Raises:
        ValueError: the key isn't 32 bytes.
        ToolError: `cryptography` isn't installed.
    """
    cipher = _aes_gcm(key)
    nonce = _secrets.token_bytes(GCM_NONCE_SIZE)
    return bytes([GCM_VERSION]) + nonce + cipher.encrypt(nonce, _as_bytes(data), context)


def decrypt(blob: bytes, key: bytes, *, context: bytes = b"") -> bytes:
    """Open a blob from `encrypt`, checking that nobody changed it.

    Args:
        blob: what `encrypt` returned.
        key: the key it was sealed with.
        context: the associated data it was sealed with.

    Returns:
        The secret's bytes.

    Raises:
        LoginError: the key, the context or the blob is wrong; the message says no more.
        ToolError: `cryptography` isn't installed.
    """
    cipher = _aes_gcm(key)
    from cryptography.exceptions import InvalidTag

    if len(blob) < 1 + GCM_NONCE_SIZE + GCM_TAG_SIZE or blob[0] != GCM_VERSION:
        raise LoginError(DECRYPT_FAILED, hint=SEAL_AGAIN)
    nonce, ciphertext = blob[1:1 + GCM_NONCE_SIZE], blob[1 + GCM_NONCE_SIZE:]
    try:
        return cipher.decrypt(nonce, ciphertext, context)
    # InvalidTag is the only way GCM rejects; its traceback says nothing more.
    except InvalidTag:
        raise LoginError(DECRYPT_FAILED, hint=SEAL_AGAIN) from None


def _as_bytes(data: Data) -> bytes:
    if isinstance(data, str):
        return data.encode("utf-8")
    if isinstance(data, (bytes, bytearray, memoryview)):
        return bytes(data)
    raise TypeError(f"expected str or bytes, not {type(data).__name__}")


def _aes_gcm(key: bytes) -> AESGCM:
    """`cryptography`'s AESGCM for a 32-byte key, imported here so `digest` never needs it."""
    # The message gives the key's length only, never its bytes.
    if len(key) != KEY_SIZE:
        raise ValueError(
            f"an AES-256 key must be {KEY_SIZE} bytes, not {len(key)}: use secrets.token_bytes(32)"
        )
    try:
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    except ImportError:
        raise ToolError(MISSING_CRYPTOGRAPHY, hint="pip install cryptography") from None
    return AESGCM(key)



# ---------------------------------------------------------------------------------------------
# The vault: sealed values in a database, under a key file
# ---------------------------------------------------------------------------------------------

# The vault's one table: each value sealed under the key, bound to its slot and name. `{}` takes
# a schema name ("" or "<name>." for an attached database).
SCHEMA = ("CREATE TABLE IF NOT EXISTS {}secrets (slot TEXT NOT NULL, name TEXT NOT NULL,"
          " value BLOB NOT NULL, PRIMARY KEY (slot, name)) WITHOUT ROWID")
# How long a key file shorter than KEY_SIZE may be another process's key still being written, in
# seconds, before it counts as damaged; 32 bytes are written in microseconds, so 2 s is ample.
KEY_WAIT = 2.0
# How often a short key file is read again while waiting, in seconds.
KEY_POLL = 0.01
# What a damaged key file says to do, unless the caller gives its own advice.
DAMAGED_KEY = "delete it, then seal the secrets again"


class Vault(MutableMapping[str, str]):
    """One slot's secrets: a mapping of `str` to `str`, each value sealed in its own row.

    Each value is sealed with AES-256-GCM under the key in `key_path`, with `<slot>\\0<name>` as
    associated data, so a value copied to another row doesn't open. Opening a vault creates
    nothing: the database is made on the first write, and the key on the first read or write
    of a value.

    Args:
        path: the database file (`secrets.db`).
        key_path: the key file, made with mode 0600 when missing.
        slot: whose secrets these are (a source's code); another slot's aren't visible.
        hint: what a value that doesn't open, or a damaged key, says to do.

    Example:
        >>> import tempfile
        >>> folder = Path(tempfile.mkdtemp())
        >>> with Vault(folder / "secrets.db", folder / "secret.key", "tlg") as saved:
        ...     saved["session"] = "s"
        ...     dict(saved)
        {'session': 's'}
    """

    def __init__(self, path: Path, key_path: Path, slot: str, *, hint: str = DAMAGED_KEY):
        self._key_path, self._slot, self._hint = key_path, slot, hint
        self._key: bytes | None = None
        self._db = _db.Db(path, schema=SCHEMA.format(""))

    def __getitem__(self, name: str) -> str:
        found = self._db.one("SELECT value FROM secrets WHERE slot = ? AND name = ?",
                             (self._slot, name))
        if found is None:
            raise KeyError(name)
        try:
            return decrypt(found[0], self._secret_key(), context=self._context(name)).decode()
        except LoginError as error:
            raise LoginError(str(error), hint=self._hint) from None

    def __setitem__(self, name: str, value: str) -> None:
        sealed = encrypt(value, self._secret_key(), context=self._context(name))
        self._db.write("INSERT OR REPLACE INTO secrets VALUES (?, ?, ?)",
                       (self._slot, name, sealed))

    def __delitem__(self, name: str) -> None:
        if name not in self:
            raise KeyError(name)
        self._db.write("DELETE FROM secrets WHERE slot = ? AND name = ?", (self._slot, name))

    def __contains__(self, name: object) -> bool:
        return self._db.one("SELECT 1 FROM secrets WHERE slot = ? AND name = ?",
                            (self._slot, name)) is not None

    def __iter__(self) -> Iterator[str]:
        found = self._db.all("SELECT name FROM secrets WHERE slot = ? ORDER BY name", (self._slot,))
        return iter([name for (name,) in found])

    def __len__(self) -> int:
        found = self._db.one("SELECT count(*) FROM secrets WHERE slot = ?", (self._slot,))
        return found[0] if found else 0

    def clear(self) -> None:
        """Remove every secret of the slot; a vault with no database is left without one."""
        if self._db.path.is_file():
            self._db.write("DELETE FROM secrets WHERE slot = ?", (self._slot,))

    def __enter__(self) -> Vault:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    def close(self) -> None:
        """Close the database; the same as leaving `with`."""
        self._db.close()

    def _context(self, name: str) -> bytes:
        return f"{self._slot}\0{name}".encode("utf-8")

    def _secret_key(self) -> bytes:
        if self._key is None:
            self._key = key(self._key_path, hint=self._hint)
        return self._key


def key(path: Path, *, hint: str = DAMAGED_KEY) -> bytes:
    """The 32-byte key in a key file, made with mode 0600 when missing.

    Args:
        path: the key file.
        hint: what a damaged key says to do.

    Returns:
        The key's 32 bytes.

    Raises:
        DiskError: the file is damaged: too long, or still short after `KEY_WAIT`.
    """
    # A short key may be another process's that is still being written, so it is read again
    # until `KEY_WAIT` has passed; only then is it damaged (a process that died writing it).
    deadline = time.monotonic() + KEY_WAIT
    while True:
        try:
            secret = path.read_bytes()
        except FileNotFoundError:
            created = _create_key(path)
            if created is not None:
                return created
            # Lost the race: the winner's file exists now, maybe still short, so read it again.
            if time.monotonic() >= deadline:
                raise DiskError(
                    f"{path} is damaged: exists but cannot be read (e.g. broken link)", hint=hint,
                ) from None
            time.sleep(KEY_POLL)
            continue
        if len(secret) == KEY_SIZE:
            return secret
        # A process killed between creating the file and writing it leaves it empty for good.
        if len(secret) > KEY_SIZE or time.monotonic() >= deadline:
            raise DiskError(
                f"{path} is damaged: it should hold {KEY_SIZE} bytes, not {len(secret)}", hint=hint,
            )
        time.sleep(KEY_POLL)


def _create_key(path: Path) -> bytes | None:
    """Make the key file with a new key; None when another process made it first.

    Args:
        path: the key file, which must not exist yet.

    Returns:
        The new key; None when the file was there already.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    # O_EXCL makes "the first creator wins" without a hard link.
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0)
    try:
        handle = os.open(path, flags, 0o600)
    except FileExistsError:
        return None
    # Safe because the key is 32 bytes, written in one write, and readers wait up to KEY_WAIT.
    secret = _secrets.token_bytes(KEY_SIZE)
    try:
        try:
            file = os.fdopen(handle, "wb")
        except BaseException:
            os.close(handle)
            raise
        with file:
            file.write(secret)
    except BaseException:
        path.unlink(missing_ok=True)
        raise
    return secret
