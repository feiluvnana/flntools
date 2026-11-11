"""Hashes, HMAC, random values, and one way to encrypt a secret at rest.

Everything except `seal` and `unseal` uses only the stdlib. Those two need the `crypto`
extra (`cryptography`) and raise ImportError without it.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
from pathlib import Path
from typing import Union

Data = Union[str, bytes]

CHUNK_SIZE = 1024 * 1024
KEY_SIZE = 32
NONCE_SIZE = 12
SEAL_VERSION = 1
MIN_SALT_SIZE = 16


class CryptoError(Exception):
    """A sealed blob could not be opened: wrong key, changed bytes, or unknown format."""


def digest(data: Data | Path, *, algo: str = "sha256") -> str:
    """The hex digest of a `str` (as UTF-8), `bytes`, or a file read in 1 MB chunks."""
    hasher = hashlib.new(algo)
    if isinstance(data, Path):
        with open(data, "rb") as handle:
            while chunk := handle.read(CHUNK_SIZE):
                hasher.update(chunk)
    else:
        hasher.update(_as_bytes(data))
    return hasher.hexdigest()


def sign(key: Data, data: Data, *, algo: str = "sha256") -> str:
    """The HMAC of `data` as hex."""
    return hmac.new(_as_bytes(key), _as_bytes(data), algo).hexdigest()


def verify(key: Data, data: Data, signature: str, *, algo: str = "sha256") -> bool:
    """Whether `signature` is the HMAC of `data`, compared in constant time."""
    return equal(sign(key, data, algo=algo), signature)


def equal(a: Data, b: Data) -> bool:
    """Compare two values in constant time."""
    return hmac.compare_digest(_as_bytes(a), _as_bytes(b))


def token(size: int = 32) -> str:
    """`size` random bytes as URL-safe base64 text."""
    return secrets.token_urlsafe(size)


def randbytes(n: int) -> bytes:
    return secrets.token_bytes(n)


def keygen() -> bytes:
    """A random 32-byte key for `seal`."""
    return secrets.token_bytes(KEY_SIZE)


def derive(password: Data, salt: bytes, *, n: int = 2**15, r: int = 8, p: int = 1) -> bytes:
    """A 32-byte key from a password with scrypt. Keep `salt` (16+ random bytes) with the data."""
    if len(salt) < MIN_SALT_SIZE:
        raise ValueError(f"salt must be at least {MIN_SALT_SIZE} bytes, not {len(salt)}")
    # scrypt needs 128 * n * r bytes of memory; the default limit (32 MB) is just too small for n=2**15.
    return hashlib.scrypt(_as_bytes(password), salt=salt, n=n, r=r, p=p, maxmem=256 * n * r + 1024 * 1024,
                          dklen=KEY_SIZE)


def seal(data: Data, key: bytes) -> bytes:
    """Encrypt with AES-256-GCM. The blob is a version byte, a 12-byte nonce, then the ciphertext and tag."""
    cipher = _cipher(key)
    nonce = secrets.token_bytes(NONCE_SIZE)
    return bytes([SEAL_VERSION]) + nonce + cipher.encrypt(nonce, _as_bytes(data), None)


def unseal(blob: bytes, key: bytes) -> bytes:
    """Reverse `seal`. Any failure raises `CryptoError` without saying which, so it leaks nothing."""
    cipher = _cipher(key)
    from cryptography.exceptions import InvalidTag

    if len(blob) < 1 + NONCE_SIZE or blob[0] != SEAL_VERSION:
        raise CryptoError("cannot unseal: wrong key or damaged data")
    nonce, ciphertext = blob[1:1 + NONCE_SIZE], blob[1 + NONCE_SIZE:]
    try:
        return cipher.decrypt(nonce, ciphertext, None)
    except InvalidTag:
        raise CryptoError("cannot unseal: wrong key or damaged data") from None


def _cipher(key: bytes):
    if len(key) != KEY_SIZE:
        raise ValueError(f"key must be {KEY_SIZE} bytes, not {len(key)}: use crypto.keygen() or crypto.derive()")
    try:
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    except ImportError:
        raise ImportError("shared.crypto.seal needs cryptography: pip install 'flntools[crypto]'") from None
    return AESGCM(key)


def _as_bytes(data: Data) -> bytes:
    if isinstance(data, str):
        return data.encode("utf-8")
    if isinstance(data, (bytes, bytearray, memoryview)):
        return bytes(data)
    raise TypeError(f"expected str or bytes, not {type(data).__name__}")
