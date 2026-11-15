"""Hashes, and AES-256-GCM to seal your own data.

`digest` hashes text, bytes-like objects, a `Path`, or a binary file with any `hashlib` algorithm.
`encrypt` and `decrypt` wrap cryptography's AESGCM cipher with authenticated encryption, loaded
on first use.

    import secrets
    from shared import crypto

    key = secrets.token_bytes(32)
    blob = crypto.encrypt("sessionid=abc", key, context=b"cookies")
    assert crypto.decrypt(blob, key, context=b"cookies") == b"sessionid=abc"
"""

from __future__ import annotations

import hashlib
import io
import secrets
from pathlib import Path
from typing import TYPE_CHECKING, Any, BinaryIO, Union, cast

import shared

if TYPE_CHECKING:
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

__all__ = ["Error", "MissingError", "decrypt", "digest", "encrypt"]

Data = Union[str, bytes, bytearray, memoryview]
HashInput = Union[str, bytes, bytearray, memoryview, Path, BinaryIO]

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
    """Data could not be decrypted, key length is wrong, or the hash algorithm is unknown."""


class MissingError(Error, shared.MissingError):
    """`cryptography` isn't installed (`pip install 'flntools[browser]'`)."""


def digest(data: HashInput, *, algorithm: str = "sha256") -> str:
    """The hex digest of text, bytes-like data, a `Path`, or a binary stream.

        crypto.digest(Path("photo.jpg"))
        crypto.digest(b"bytes", algorithm="blake2b")
    """
    if isinstance(data, Path):
        with data.open("rb") as handle:
            return digest(handle, algorithm=algorithm)
    try:
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
    except ValueError as error:
        raise Error(str(error)) from None
    raise TypeError(
        f"cannot hash {type(data).__name__}: pass str, bytes, a Path, or a binary file"
    )


def encrypt(data: Data, key: bytes, *, context: bytes = b"") -> bytes:
    """Encrypt and authenticate `data` with AES-256-GCM into a blob only `decrypt` opens.

        blob = crypto.encrypt("sessionid=abc", key, context=b"cookies")
    """
    cipher = _aes_gcm(key)
    nonce = secrets.token_bytes(GCM_NONCE_SIZE)
    return bytes([GCM_VERSION]) + nonce + cipher.encrypt(nonce, _as_bytes(data), context)


def decrypt(blob: bytes, key: bytes, *, context: bytes = b"") -> bytes:
    """Decrypt a blob from `encrypt`, with the same key and context, and check nobody changed it.

        data = crypto.decrypt(blob, key, context=b"cookies")
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
        raise Error(
            f"an AES-256 key must be {KEY_SIZE} bytes, not {len(key)}: use secrets.token_bytes(32)"
        )
    try:
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    except ImportError:
        raise MissingError(MISSING_CRYPTOGRAPHY) from None
    return AESGCM(key)
