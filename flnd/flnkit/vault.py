"""Secrets sealed per account and name under a key file kept outside any home, and
`digest`. src/docs/storage explains it.
"""

from __future__ import annotations

import hashlib
import io
import operator
import os
import secrets as _secrets
import sys
import time
import unicodedata
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING, Any, BinaryIO, cast

from flnkit.errors import DiskError, LoginError, ToolError, UsageError, disk

if TYPE_CHECKING:
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

__all__ = ["decrypt", "digest", "encrypt", "key", "keyfile", "seal", "unseal"]

# Rules a change must keep:
# - no secret or key reaches a message: every way a value fails to open says the same thing;
# - a key file is written whole, with mode 0600, and never replaced: the first creator wins.

_PLATFORM = sys.platform

# ---------------------------------------------------------------------------------------------
# Sealing
# ---------------------------------------------------------------------------------------------

Data = str | bytes | bytearray | memoryview
HashInput = str | bytes | bytearray | memoryview | Path | BinaryIO

# The blob is one version byte, a 12-byte random nonce (GCM's standard size), then the
# ciphertext with its 16-byte tag. The format is fixed: vaults hold blobs of version 1.
GCM_VERSION = 1
GCM_NONCE_SIZE = 12
GCM_TAG_SIZE = 16
# AES-256: the only key size, the size `secrets.token_bytes(32)` makes.
KEY_SIZE = 32
# One message for every way decryption fails, so it says nothing about the key or the data.
DECRYPT_FAILED = "cannot decrypt: wrong key or damaged data"
# What a blob that does not open says to do, unless the caller gives its own advice.
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
# Sealed values, under one key file kept outside any home
# ---------------------------------------------------------------------------------------------

# What a damaged key file says to do, unless the caller gives its own advice.
DAMAGED_KEY = "delete it, then seal the secrets again"

# Every way a sealed value fails to open says this, with no advice: the caller adds its own.
KEY_GONE = "the vault's key is gone"
# How long a key file shorter than KEY_SIZE may be another process's key still being written, in
# seconds, before it counts as damaged. Only a drive without hard links writes a key in place
# (`_create`); 32 bytes are written in microseconds, so 2 s is ample.
KEY_WAIT = 2.0
# How often a short key file is read again while waiting, in seconds.
KEY_POLL = 0.01
# The variable flnd's test suite sets (as `flnkit.cookies.NO_PROFILES` reads it): while it is
# set, the real key folder is refused, so no test can touch it.
NO_PROFILES = "FLND_TEST_NO_PROFILES"


def seal(key: bytes, account: int, name: str, value: str) -> bytes:
    """Seal one secret into its slot, an account's `name`; only `unseal` of that slot opens it.

    Args:
        key: the 32-byte key, from `key`.
        account: the account's id; with `name`, the slot the value is bound to.
        name: the secret's name, such as `cookies`.
        value: the secret, sealed as UTF-8.

    Returns:
        The version byte (1), a 12-byte nonce, then the AES-256-GCM ciphertext and its tag.

    Raises:
        TypeError: `account` isn't an int, or `name` or `value` isn't text.
        ValueError: the key isn't 32 bytes.
        ToolError: `cryptography` isn't installed.
    """
    if not isinstance(value, str):
        raise TypeError(f"a sealed value is text, not {type(value).__name__}")
    return encrypt(value, key, context=_slot(account, name))


def unseal(key: bytes, account: int, name: str, sealed: bytes) -> str:
    """Open a value that `seal` made for the same slot.

    Args:
        key: the key it was sealed with.
        account: the account's id it was sealed for.
        name: the secret's name it was sealed for.
        sealed: what `seal` returned; anything that isn't bytes is a damaged value.

    Returns:
        The secret.

    Raises:
        LoginError: `KEY_GONE`, for a wrong key, a value sealed for another slot, a damaged
            value, or one of another version alike.
        TypeError: `account` isn't an int, or `name` isn't text.
        ValueError: the key isn't 32 bytes.
        ToolError: `cryptography` isn't installed.
    """
    context = _slot(account, name)
    opened = None
    # A row that isn't bytes, or a value that doesn't decode, is damaged like a wrong tag.
    if isinstance(sealed, (bytes, bytearray, memoryview)):
        try:
            opened = decrypt(bytes(sealed), key, context=context).decode("utf-8")
        except (LoginError, UnicodeDecodeError):
            pass
    if opened is None:
        # Raised outside the `except`, so no context keeps the failure, whose
        # UnicodeDecodeError holds the plaintext.
        raise LoginError(KEY_GONE)
    return opened


def _slot(account: int, name: str) -> bytes:
    """The associated data that binds a sealed value to its slot."""
    try:
        # `True` is an int to Python, but never account 1.
        if isinstance(account, bool):
            raise TypeError
        number = operator.index(account)
    except TypeError:
        raise TypeError(f"an account is an int, not {type(account).__name__}") from None
    if not isinstance(name, str):
        raise TypeError(f"a secret's name is text, not {type(name).__name__}")
    return f"{number}\0{name}".encode()


def keyfile() -> Path:
    """Where the user's key file is kept: `~/.config/flnd/key`, `%APPDATA%\\flnd\\key` on Windows.

    Returns:
        The path; nothing is read or made.
    """
    if _PLATFORM == "win32":
        appdata = os.environ.get("APPDATA")
        base = Path(appdata) if appdata else Path.home() / "AppData" / "Roaming"
        return base / "flnd" / "key"
    return Path.home() / ".config" / "flnd" / "key"


def key(path: Path, *, home: Path, create: bool = False) -> bytes:
    """The 32-byte key in the key file at `path`.

    Args:
        path: the key file, usually `keyfile()`.
        home: the folder whose secrets the key seals; a key file inside it is refused, since a
            copy of the folder would carry its key.
        create: True before sealing: a missing file is made with a new key, mode 0600, written
            whole or not at all. False before opening: a missing file is `KEY_GONE`.

    Returns:
        The key's 32 bytes.

    Raises:
        UsageError: `path` is inside `home`.
        LoginError: there is no key file, and `create` is False.
        DiskError: the file is damaged (too long, still short after `KEY_WAIT`, or a folder),
            or can't be read.
        RuntimeError: the test suite's guard (`NO_PROFILES`) is set, and `path` is in the real
            `~/.config/flnd` (or `%APPDATA%\\flnd`).
    """
    path, home = Path(path), Path(home)
    _refuse_real(path)
    if _inside(path, home):
        raise UsageError(f"the key file {path} is inside {home}: a copy of it would carry the key")
    # A short key may be another process's that is still being written, so it is read again
    # until `KEY_WAIT` has passed; only then is it damaged (a process that died writing it).
    deadline = time.monotonic() + KEY_WAIT
    while True:
        try:
            secret = path.read_bytes()
        except FileNotFoundError:
            if not create:
                raise LoginError(KEY_GONE) from None
            made = _create(path)
            if made is not None:
                return made
            # Lost the race: the winner's file exists now, so read it again.
            if time.monotonic() >= deadline:
                raise DiskError(
                    f"{path} is damaged: exists but cannot be read (e.g. broken link)",
                    hint=DAMAGED_KEY,
                ) from None
            time.sleep(KEY_POLL)
            continue
        except OSError as error:
            # Windows says PermissionError where POSIX says IsADirectoryError.
            if path.is_dir():
                raise DiskError(f"{path} is damaged: it is a folder, not a key file",
                                hint=DAMAGED_KEY) from None
            raise disk(error, path) from error
        if len(secret) == KEY_SIZE:
            return secret
        # A process killed between creating the file and writing it leaves it short for good.
        if len(secret) > KEY_SIZE or time.monotonic() >= deadline:
            raise DiskError(f"{path} is damaged: it should hold {KEY_SIZE} bytes, "
                            f"not {len(secret)}", hint=DAMAGED_KEY)
        time.sleep(KEY_POLL)


def _create(path: Path) -> bytes | None:
    """Make the key file with a new key; None when another process made it first."""
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    secret = _secrets.token_bytes(KEY_SIZE)
    draft = path.with_name(f".{path.name}.{_secrets.token_hex(8)}")
    if not _write_new(draft, secret):
        return None
    try:
        os.link(draft, path)
    except FileExistsError:
        return None
    except OSError:
        # FAT, exFAT and some network drives have no hard links: write in place, with O_EXCL so
        # the first creator still wins; readers wait out a short file (`KEY_WAIT`).
        return secret if _write_new(path, secret) else None
    finally:
        draft.unlink(missing_ok=True)
    return secret


def _write_new(path: Path, secret: bytes) -> bool:
    """Write `secret` to a new file with mode 0600, synced to disk; False when it exists."""
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0)
    try:
        handle = os.open(path, flags, 0o600)
    except FileExistsError:
        return False
    try:
        try:
            file = os.fdopen(handle, "wb")
        except BaseException:
            os.close(handle)
            raise
        with file:
            file.write(secret)
            file.flush()
            os.fsync(file.fileno())
    except BaseException:
        path.unlink(missing_ok=True)
        raise
    return True


def _inside(path: Path, folder: Path) -> bool:
    """Whether `path` is in `folder`, through links and `..`, in any case or Unicode form."""
    resolved, folder = path.resolve(), folder.resolve()
    if _fold(resolved).is_relative_to(_fold(folder)):
        return True
    # A second spelling of the same folder (macOS's /System/Volumes/Data, a mount, a Windows
    # short name) only `samefile` sees, and only for what exists: so match the folder's
    # nearest existing ancestor, and compare the missing rest by name.
    anchor, rest = folder, Path()
    while not anchor.exists() and anchor != anchor.parent:
        anchor, rest = anchor.parent, Path(anchor.name) / rest
    for parent in (resolved, *resolved.parents):
        try:
            if os.path.samefile(parent, anchor):
                return _fold(resolved.relative_to(parent)).is_relative_to(_fold(rest))
        except OSError:
            continue
    return False


def _fold(path: Path) -> PurePosixPath:
    """A path in one case and one Unicode form, for comparing (Unicode's caseless match)."""
    text = unicodedata.normalize("NFD", str(path).replace("\\", "/"))
    return PurePosixPath(unicodedata.normalize("NFD", text.casefold()))


def _refuse_real(path: Path) -> None:
    """Refuse the real `~/.config/flnd` (and, on Windows, `%APPDATA%\\flnd`) under the guard.

    The real home is the account's own, not `Path.home()`, which a test may point elsewhere.

    Raises:
        RuntimeError: the guard is set and `path` is in the real folder.
    """
    if not os.environ.get(NO_PROFILES):
        return
    from flnkit import cookies

    real = cookies.home()
    folders = [real / ".config" / "flnd", real / "AppData" / "Roaming" / "flnd"]
    if _PLATFORM == "win32" and os.environ.get("APPDATA"):
        folders.append(Path(os.environ["APPDATA"]) / "flnd")
    for folder in folders:
        if _inside(path, folder):
            raise RuntimeError(f"{NO_PROFILES} is set: a test may not use the real key file "
                               f"{path}; give one in a temporary folder")
