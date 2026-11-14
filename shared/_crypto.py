"""Hashes, and AES-256-GCM to seal your own data.

The five hashes are hashlib's (blake2b, md5, sha1, sha256, sha512) and accept
text, bytes, a file Path, or a binary file. `encrypt` and `decrypt` wrap
cryptography's AESGCM cipher with authenticated encryption; cryptography is loaded
on first use.

    import secrets
    from pathlib import Path
    from shared import crypto

    key = secrets.token_bytes(32)
    blob = crypto.encrypt("sessionid=abc", key, context=b"cookies")
    assert crypto.decrypt(blob, key, context=b"cookies") == b"sessionid=abc"
"""

from __future__ import annotations

import hashlib
import secrets
from pathlib import Path
from typing import TYPE_CHECKING, BinaryIO, Union

import shared

if TYPE_CHECKING:
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

__all__ = ["Error", "blake2b", "decrypt", "encrypt", "md5", "sha1", "sha256", "sha512"]

Data = Union[str, bytes]
HashInput = Union[str, bytes, Path, BinaryIO]

# How much of a file a hash reads at a time: big enough to be fast, small enough for any file.
CHUNK_SIZE = 1024 * 1024
# The blob is one version byte, a 12-byte random nonce (GCM's standard size), then the
# ciphertext with its 16-byte tag. The format is fixed: vaults hold blobs of version 1.
GCM_VERSION = 1
GCM_NONCE_SIZE = 12
GCM_TAG_SIZE = 16
# AES-256: the only key size offered, the size `secrets.token_bytes(32)` makes.
KEY_SIZE = 32
# One message for every way decryption fails, so it says nothing about the key or the data.
DECRYPT_FAILED = "cannot decrypt: wrong key or damaged data"
MISSING_CRYPTOGRAPHY = "shared.crypto needs cryptography: pip install 'flntools[browser]'"


# Subclasses ValueError so callers expecting bad input exceptions catch it.
class Error(shared.Error, ValueError):
    """Data could not be decrypted: wrong key, changed bytes, or invalid format."""


def md5(data: HashInput) -> str:
    """The MD5 of text, bytes, a file at a `Path`, or a binary file object, as hex.

        crypto.md5(Path("photo.jpg"))

    Args:
        data: text, bytes, a `Path` read in chunks, or an open binary file.

    Returns:
        The digest as lowercase hex.
    """
    return _hash("md5", data)


def sha1(data: HashInput) -> str:
    """The SHA-1 of `data` as hex, read the same way as `md5`.

        crypto.sha1(b"bytes")

    Args:
        data: text, bytes, a `Path` or a binary file object, as `md5` reads them.

    Raises:
        TypeError: `data` is none of these.
        OSError: the file at the `Path` can't be read.
    """
    return _hash("sha1", data)


def sha256(data: HashInput) -> str:
    """The SHA-256 of `data` as hex, read the same way as `md5`.

        crypto.sha256("text")

    Args:
        data: text, bytes, a `Path` or a binary file object, as `md5` reads them.

    Raises:
        TypeError: `data` is none of these.
        OSError: the file at the `Path` can't be read.
    """
    return _hash("sha256", data)


def sha512(data: HashInput) -> str:
    """The SHA-512 of `data` as hex, read the same way as `md5`.

        crypto.sha512(Path("f.bin"))

    Args:
        data: text, bytes, a `Path` or a binary file object, as `md5` reads them.

    Raises:
        TypeError: `data` is none of these.
        OSError: the file at the `Path` can't be read.
    """
    return _hash("sha512", data)


def blake2b(data: HashInput) -> str:
    """The BLAKE2b (64-byte digest) of `data` as hex, read the same way as `md5`.

        crypto.blake2b(open("f.bin", "rb"))

    Args:
        data: text, bytes, a `Path` or a binary file object, as `md5` reads them.

    Raises:
        TypeError: `data` is none of these.
        OSError: the file at the `Path` can't be read.
    """
    return _hash("blake2b", data)


def encrypt(data: Data, key: bytes, *, context: bytes = b"") -> bytes:
    """Encrypt and authenticate `data` with AES-256-GCM into a blob only `decrypt` opens.

        blob = crypto.encrypt("sessionid=abc", key, context=b"cookies")

    Args:
        data: text (sealed as UTF-8) or bytes.
        key: 32 bytes, kept secret.
        context: authenticated additional data that `decrypt` must match.

    Returns:
        The encrypted blob (version, nonce, ciphertext, and authentication tag).
    """
    cipher = _aes_gcm(key)
    nonce = secrets.token_bytes(GCM_NONCE_SIZE)
    return bytes([GCM_VERSION]) + nonce + cipher.encrypt(nonce, _as_bytes(data), context)


def decrypt(blob: bytes, key: bytes, *, context: bytes = b"") -> bytes:
    """Decrypt a blob from `encrypt`, with the same key and context, and check nobody changed it.

        data = crypto.decrypt(blob, key, context=b"cookies")

    Args:
        blob: what `encrypt` returned.
        key: the 32-byte key it was sealed with.
        context: the context it was sealed with.

    Returns:
        The data, as bytes; `.decode()` it if it was text.

    Raises:
        Error: a wrong key, a wrong context, a changed or cut blob, or one that isn't from
            `encrypt`. The message is the same for each, and never holds the key or the data.
        ValueError: `key` isn't 32 bytes.
        ImportError: `cryptography` isn't installed; the message names the extra.
    """
    cipher = _aes_gcm(key)
    from cryptography.exceptions import InvalidTag

    if len(blob) < 1 + GCM_NONCE_SIZE + GCM_TAG_SIZE or blob[0] != GCM_VERSION:
        raise Error(DECRYPT_FAILED)
    nonce, ciphertext = blob[1:1 + GCM_NONCE_SIZE], blob[1 + GCM_NONCE_SIZE:]
    try:
        return cipher.decrypt(nonce, ciphertext, context)
    # InvalidTag is the only way GCM rejects; drop its traceback which says nothing more.
    except InvalidTag:
        raise Error(DECRYPT_FAILED) from None


def _hash(algorithm: str, data: HashInput) -> str:
    """The hex digest of `data` under hashlib's `algorithm`, reading a `Path` or file in chunks."""
    if isinstance(data, Path):
        with data.open("rb") as handle:
            return _hash(algorithm, handle)
    hasher = hashlib.new(algorithm)
    if isinstance(data, (str, bytes)):
        hasher.update(_as_bytes(data))
    elif hasattr(data, "read"):
        while chunk := data.read(CHUNK_SIZE):
            hasher.update(chunk)
    else:
        raise TypeError(
            f"cannot hash {type(data).__name__}: pass str, bytes, a Path, or a binary file"
        )
    return hasher.hexdigest()


def _as_bytes(data: Data) -> bytes:
    if isinstance(data, str):
        return data.encode("utf-8")
    if isinstance(data, (bytes, bytearray, memoryview)):
        return bytes(data)
    raise TypeError(f"expected str or bytes, not {type(data).__name__}")


def _aes_gcm(key: bytes) -> AESGCM:
    """`cryptography`'s AESGCM for a 32-byte key, imported here so the hashes never need it."""
    # The message gives the key's length only, never its bytes.
    if len(key) != KEY_SIZE:
        raise ValueError(
            f"an AES-256 key must be {KEY_SIZE} bytes, not {len(key)}: use secrets.token_bytes(32)"
        )
    try:
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    except ImportError:
        raise ImportError(MISSING_CRYPTOGRAPHY) from None
    return AESGCM(key)
