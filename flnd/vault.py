"""Login data sealed at rest in `.flnd/secrets.db`, with the key in `.flnd/secret.key`.

Each value is encrypted with AES-GCM, bound to its source and name, so a value copied into
another slot no longer decrypts. This protects against someone who copies only `secrets.db`.
It doesn't protect against someone who can read the whole `.flnd/` folder, which is the same
exposure as the cookie files beside it.
"""

from __future__ import annotations

import base64
import os
import time
from collections.abc import Iterator, MutableMapping
from pathlib import Path

from shared import crypto, kv

DB_FILE = "secrets.db"
KEY_FILE = "secret.key"
TABLE = "secrets"
KEY_SIZE = 32
# How long a key file shorter than KEY_SIZE may be another run's key still being written, in
# seconds, before it counts as damaged; 32 bytes are written in microseconds, so 2 s is ample.
KEY_WAIT = 2.0
# How often a short key file is read again while waiting, in seconds.
KEY_POLL = 0.01


def open(home: Path, code: str) -> Vault:
    """The secrets of one source, as a mapping of `str` to `str`.

        with vault.open(Path(".flnd"), "tlg") as secrets:
            secrets["session"] = "1BVts…"
    """
    return Vault(home, code)


class Vault(MutableMapping[str, str]):
    """One source's secrets; see `open`. The key file is created on the first read or write."""

    def __init__(self, home: Path, code: str):
        self._home = home
        self._code = code
        self._db = kv.open(home / DB_FILE, table=TABLE)
        self._key: bytes | None = None

    def __getitem__(self, name: str) -> str:
        sealed = base64.b64decode(self._db[self._slot(name)])
        return crypto.decrypt_aes_gcm(sealed, self._secret_key(), context=self._context(name)).decode("utf-8")

    def __setitem__(self, name: str, value: str) -> None:
        sealed = crypto.encrypt_aes_gcm(value, self._secret_key(), context=self._context(name))
        self._db[self._slot(name)] = base64.b64encode(sealed).decode("ascii")

    def __delitem__(self, name: str) -> None:
        del self._db[self._slot(name)]

    def __iter__(self) -> Iterator[str]:
        prefix = self._slot("")
        return iter([key[len(prefix):] for key in self._db.get_keys(prefix=prefix)])

    def __len__(self) -> int:
        return len(self._db.get_keys(prefix=self._slot("")))

    def __enter__(self) -> Vault:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    def close(self) -> None:
        self._db.close()

    def _slot(self, name: str) -> str:
        return f"{self._code}:{name}"

    def _context(self, name: str) -> bytes:
        return f"{self._code}\0{name}".encode("utf-8")

    def _secret_key(self) -> bytes:
        if self._key is None:
            self._key = key(self._home)
        return self._key


def key(home: Path) -> bytes:
    """The key in `home/secret.key`, created with mode 0600 when missing.

        secret = vault.key(Path(".flnd"))

    Args:
        home: flnd's folder, `.flnd`; it is created when missing.

    Returns:
        The 32-byte key.

    Raises:
        ValueError: the key file holds anything but 32 bytes, or exists but can't be read (a
            dangling symlink), and still does after `KEY_WAIT`.
        OSError: the folder or the key file can't be created or read.

    When two runs create the key at once, the first one to create the file wins and the other
    reads its key: both end with the same one. No temporary file is written.
    """
    path = home / KEY_FILE
    # A short key may be another run's that is still being written, so it is read again until
    # `KEY_WAIT` has passed; only then is it damaged (a run that died while writing it).
    deadline = time.monotonic() + KEY_WAIT
    while True:
        try:
            secret = path.read_bytes()
        except FileNotFoundError:
            created = _create_key(path)
            if created is not None:
                return created
            # Lost the race: the winner's file exists now, maybe still short, so read it again. A
            # path that exists but can't be read, such as a dangling symlink (O_EXCL refuses it,
            # read_bytes finds nothing), loses every time, so it too gives up at the deadline
            # instead of spinning forever.
            if time.monotonic() >= deadline:
                raise ValueError(f"{path} is damaged: it exists but can't be read, such as a broken link; "
                                 "delete it, then log in again")
            time.sleep(KEY_POLL)
            continue
        if len(secret) == KEY_SIZE:
            return secret
        # A run killed between creating the file and writing it leaves it empty for good, so the
        # message says how to get out: without the key, the secrets it sealed can't be read anyway.
        if len(secret) > KEY_SIZE or time.monotonic() >= deadline:
            raise ValueError(f"{path} is damaged: it should hold {KEY_SIZE} bytes, not {len(secret)}; "
                             "delete it, then log in again")
        time.sleep(KEY_POLL)


def _create_key(path: Path) -> bytes | None:
    """Create the key file with a new key, and return the key, or None when another run created it first."""
    path.parent.mkdir(parents=True, exist_ok=True)
    # O_EXCL makes "first creator wins" without a hard link: `os.link`, used before 2026-11, is
    # refused with a plain OSError on FAT, exFAT and some network drives, so `flnd login tlg`
    # failed there. O_BINARY (Windows only, 0 elsewhere) keeps the C runtime from turning a
    # 0x0A byte of the key into 0x0D 0x0A. Mode 0600: the key is as private as the cookie files.
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0)
    try:
        handle = os.open(path, flags, 0o600)
    except FileExistsError:
        return None
    # The winner writes into the published file, so a reader can see it empty or short for a
    # moment. That is safe because the key is only 32 bytes, written in one write of a fresh
    # file, and every reader (key() above) treats a short file as "not written yet" and reads
    # again until KEY_WAIT has passed, never using a short key. A key that fails to be written
    # is removed, so no run is left with a damaged file.
    secret = crypto.random_key(KEY_SIZE)
    try:
        with os.fdopen(handle, "wb") as file:
            file.write(secret)
    except BaseException:
        path.unlink(missing_ok=True)
        raise
    return secret
