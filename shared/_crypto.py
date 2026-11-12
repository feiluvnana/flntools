"""Hashes, keys, and AES encryption.

Hashing and keys use only the stdlib. The ciphers need `cryptography`, which comes with
the `browser` extra, and raise ImportError on first use without it.
"""

from __future__ import annotations

import hashlib
import secrets
from pathlib import Path
from typing import TYPE_CHECKING, BinaryIO, Union

import shared

if TYPE_CHECKING:
    from cryptography.hazmat.primitives.ciphers import Cipher, modes
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

__all__ = ["CryptoError", "Error", "decrypt_aes_cbc", "decrypt_aes_gcm", "derive_scrypt", "encrypt_aes_cbc",
           "encrypt_aes_gcm", "hash_blake2b", "hash_md5", "hash_sha1", "hash_sha256", "hash_sha512", "random_key"]

Data = Union[str, bytes]
HashInput = Union[str, bytes, Path, BinaryIO]

CHUNK_SIZE = 1024 * 1024
MIN_SALT_SIZE = 16
GCM_VERSION = 1
GCM_NONCE_SIZE = 12
GCM_TAG_SIZE = 16
DECRYPT_FAILED = "cannot decrypt: wrong key or damaged data"
MISSING_CRYPTOGRAPHY = "shared.crypto needs cryptography: pip install 'flntools[browser]'"


class Error(shared.Error):
    """The base of every error `shared.crypto` raises."""


# Still a ValueError, as before the error tree, so `except ValueError` keeps catching it.
class CryptoError(Error, ValueError):
    """Data could not be decrypted: a wrong key, changed bytes, bad padding, or an unknown format."""


def hash_md5(data: HashInput) -> str:
    """The MD5 of text, bytes, a file at a `Path`, or a binary file object, as hex.

        crypto.hash_md5(Path("photo.jpg"))

    Only a `Path` means a file: `hash_md5("photo.jpg")` hashes the text "photo.jpg".
    """
    return _hash("md5", data)


def hash_sha1(data: HashInput) -> str:
    """The SHA-1 of `data` as hex, read the same way as `hash_md5`.

        crypto.hash_sha1(b"bytes")
    """
    return _hash("sha1", data)


def hash_sha256(data: HashInput) -> str:
    """The SHA-256 of `data` as hex, read the same way as `hash_md5`.

        crypto.hash_sha256("text")
    """
    return _hash("sha256", data)


def hash_sha512(data: HashInput) -> str:
    """The SHA-512 of `data` as hex, read the same way as `hash_md5`.

        crypto.hash_sha512(Path("f.bin"))
    """
    return _hash("sha512", data)


def hash_blake2b(data: HashInput) -> str:
    """The BLAKE2b of `data` as hex, read the same way as `hash_md5`.

        crypto.hash_blake2b(open("f.bin", "rb"))
    """
    return _hash("blake2b", data)


def random_key(size: int = 32) -> bytes:
    """`size` random bytes from the OS, for a key or a salt.

        key = crypto.random_key()
    """
    return secrets.token_bytes(size)


def derive_scrypt(password: Data, salt: bytes, *, size: int = 32) -> bytes:
    """A `size`-byte key from a password, with scrypt (n=2**15, r=8, p=1).

        key = crypto.derive_scrypt("password", salt)

    `salt` must be at least 16 random bytes, kept next to the data. It is slow on purpose.
    """
    if len(salt) < MIN_SALT_SIZE:
        raise ValueError(f"salt must be at least {MIN_SALT_SIZE} bytes, not {len(salt)}")
    cost, block_size = 2**15, 8
    # scrypt needs a little over 128 * n * r bytes (32 MB here), just past OpenSSL's default limit.
    return hashlib.scrypt(_as_bytes(password), salt=salt, n=cost, r=block_size, p=1,
                          maxmem=256 * cost * block_size, dklen=size)


def encrypt_aes_gcm(data: Data, key: bytes, *, context: bytes = b"") -> bytes:
    """Encrypt and authenticate `data` into a version byte, a random nonce, the ciphertext and its tag.

        blob = crypto.encrypt_aes_gcm("secret", key, context=b"cookies")

    `context` is checked on decryption but not stored, so a blob sealed for one slot can't be moved to another.
    """
    nonce = secrets.token_bytes(GCM_NONCE_SIZE)
    return bytes([GCM_VERSION]) + nonce + _aes_gcm(key).encrypt(nonce, _as_bytes(data), context)


def decrypt_aes_gcm(blob: bytes, key: bytes, *, context: bytes = b"") -> bytes:
    """Decrypt a blob from `encrypt_aes_gcm` with the same key and context.

        data = crypto.decrypt_aes_gcm(blob, key, context=b"cookies")

    Any failure raises `CryptoError` without saying which, so it leaks nothing.
    """
    cipher = _aes_gcm(key)
    from cryptography.exceptions import InvalidTag

    if len(blob) < 1 + GCM_NONCE_SIZE + GCM_TAG_SIZE or blob[0] != GCM_VERSION:
        raise CryptoError(DECRYPT_FAILED)
    nonce, ciphertext = blob[1:1 + GCM_NONCE_SIZE], blob[1 + GCM_NONCE_SIZE:]
    try:
        return cipher.decrypt(nonce, ciphertext, context)
    except InvalidTag:
        raise CryptoError(DECRYPT_FAILED) from None


def encrypt_aes_cbc(data: Data, key: bytes, *, iv: bytes) -> bytes:
    """Encrypt `data` with AES-CBC and PKCS7 padding, for a format you don't control.

        blob = crypto.encrypt_aes_cbc("field", key, iv=iv)

    CBC is not authenticated; your own data belongs in `encrypt_aes_gcm`.
    """
    encryptor = _aes_cbc(key, iv).encryptor()
    from cryptography.hazmat.primitives.padding import PKCS7

    padder = PKCS7(128).padder()
    padded = padder.update(_as_bytes(data)) + padder.finalize()
    return encryptor.update(padded) + encryptor.finalize()


def decrypt_aes_cbc(blob: bytes, key: bytes, *, iv: bytes) -> bytes:
    """Decrypt AES-CBC and remove the PKCS7 padding.

        data = crypto.decrypt_aes_cbc(blob, key, iv=iv)

    A blob that isn't whole blocks, or whose padding is wrong, raises `CryptoError`.
    """
    decryptor = _aes_cbc(key, iv).decryptor()
    from cryptography.hazmat.primitives.padding import PKCS7

    unpadder = PKCS7(128).unpadder()
    try:
        padded = decryptor.update(blob) + decryptor.finalize()
        return unpadder.update(padded) + unpadder.finalize()
    except ValueError:
        raise CryptoError(DECRYPT_FAILED) from None


def _hash(algorithm: str, data: HashInput) -> str:
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
        raise TypeError(f"cannot hash {type(data).__name__}: pass str, bytes, a Path, or a binary file")
    return hasher.hexdigest()


def _as_bytes(data: Data) -> bytes:
    if isinstance(data, str):
        return data.encode("utf-8")
    if isinstance(data, (bytes, bytearray, memoryview)):
        return bytes(data)
    raise TypeError(f"expected str or bytes, not {type(data).__name__}")


def _aes_gcm(key: bytes) -> AESGCM:
    _check_key(key)
    try:
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    except ImportError:
        raise ImportError(MISSING_CRYPTOGRAPHY) from None
    return AESGCM(key)


def _aes_cbc(key: bytes, iv: bytes) -> Cipher[modes.CBC]:
    _check_key(key)
    try:
        from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
    except ImportError:
        raise ImportError(MISSING_CRYPTOGRAPHY) from None
    return Cipher(algorithms.AES(key), modes.CBC(iv))


def _check_key(key: bytes) -> None:
    if len(key) not in (16, 24, 32):
        raise ValueError(f"an AES key must be 16, 24 or 32 bytes, not {len(key)}: use crypto.random_key()")
