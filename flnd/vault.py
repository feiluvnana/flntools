"""Login data sealed at rest in `.flnd/secrets.db`, with the key in `.flnd/secret.key`.

Each value is encrypted with AES-GCM, bound to its source and name, so a value copied into
another slot no longer decrypts. This protects against someone who copies only `secrets.db`.
It doesn't protect against someone who can read the whole `.flnd/` folder, which is the same
exposure as the cookie files beside it.
"""

from __future__ import annotations

import base64
import os
import tempfile
from collections.abc import Iterator, MutableMapping
from pathlib import Path

from shared import crypto, kv

DB_FILE = "secrets.db"
KEY_FILE = "secret.key"
TABLE = "secrets"
KEY_SIZE = 32


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

    The new key is written to a temp file and linked into place, so the key file is never
    seen empty, and when two runs race, both end up with the one that was linked first.
    """
    path = home / KEY_FILE
    if not path.exists():
        home.mkdir(parents=True, exist_ok=True)
        handle, temp_path = tempfile.mkstemp(dir=home, prefix=f".{KEY_FILE}.")
        try:
            with os.fdopen(handle, "wb") as file:
                file.write(crypto.random_key(KEY_SIZE))
            os.link(temp_path, path)
        except FileExistsError:
            pass
        finally:
            os.unlink(temp_path)
    secret = path.read_bytes()
    if len(secret) != KEY_SIZE:
        raise ValueError(f"{path} is damaged: it should hold {KEY_SIZE} bytes, not {len(secret)}")
    return secret
