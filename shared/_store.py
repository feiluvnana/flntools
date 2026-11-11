"""A key-value store in one sqlite file, plus an encrypted secret store on top of it.

`db.sql` is the real `sqlite3.Connection`; your own tables can live next to `kv`.

Secrets are sealed with `crypto.seal`. By default the key lives in `<db>.key`, created with
mode 0600. That keeps secrets safe from someone who copies only the database file. It does
not protect against someone who can read the whole folder; pass `password=` for that.
"""

from __future__ import annotations

import base64
import datetime
import json
import os
import sqlite3
import threading
import time
from collections.abc import MutableMapping
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator, Union

from shared import _crypto as crypto
from shared import _path as path

PathLike = Union[str, "os.PathLike[str]"]

SCHEMA = """
CREATE TABLE IF NOT EXISTS kv (
  ns TEXT NOT NULL, key TEXT NOT NULL, value TEXT NOT NULL, updated REAL NOT NULL,
  PRIMARY KEY (ns, key)
) WITHOUT ROWID
"""
SECRET_NAMESPACE = "secret:"
SALT_NAMESPACE, SALT_KEY = "meta", "secrets.salt"


def open(file: PathLike) -> Store:
    """Open or create the store at `file`, creating its folder."""
    return Store(file)


class Store:
    def __init__(self, file: PathLike):
        self.file = Path(file)
        path.mkdir(self.file.parent)
        # Autocommit mode: each statement commits on its own unless `batch` opened a transaction.
        self.sql = sqlite3.connect(self.file, check_same_thread=False, isolation_level=None)
        self.lock = threading.RLock()
        self._batch_depth = 0
        with self.lock:
            self.sql.execute("PRAGMA journal_mode=WAL")
            self.sql.execute(SCHEMA)

    def table(self, name: str) -> Table:
        """A named key-value table. Tables in one file never see each other's keys."""
        return Table(self, name)

    def secrets(self, key: bytes | None = None, *, password: str | bytes | None = None) -> Table:
        """A table whose `str` values are encrypted with `crypto.seal`.

        With neither argument, the key comes from `<db>.key`. `password` derives the key with
        `crypto.derive` and a random salt that is stored in the database on first use.
        """
        if key is not None and password is not None:
            raise ValueError("pass key or password, not both")
        if password is not None:
            key = crypto.derive(password, self._salt())
        elif key is None:
            key = self._key_file()
        return SecretTable(self, SECRET_NAMESPACE, key)

    @contextmanager
    def batch(self) -> Iterator[Store]:
        """One transaction for many writes: commits at the end, or rolls back if the block raises.

        Other threads' writes wait until the batch ends.
        """
        with self.lock:
            outermost = self._batch_depth == 0
            if outermost:
                self.sql.execute("BEGIN")
            self._batch_depth += 1
            try:
                yield self
            except BaseException:
                self._batch_depth -= 1
                if outermost:
                    self.sql.execute("ROLLBACK")
                raise
            self._batch_depth -= 1
            if outermost:
                self.sql.execute("COMMIT")

    def close(self) -> None:
        with self.lock:
            self.sql.close()

    def __enter__(self) -> Store:
        return self

    def __exit__(self, *exc_info: Any) -> None:
        self.close()

    def _execute(self, statement: str, parameters: tuple = ()) -> list[tuple]:
        with self.lock:
            return self.sql.execute(statement, parameters).fetchall()

    def _key_file(self) -> bytes:
        key_path = self.file.with_name(self.file.name + ".key")
        try:
            descriptor = os.open(key_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            return key_path.read_bytes()
        key = crypto.keygen()
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(key)
        return key

    def _salt(self) -> bytes:
        meta = self.table(SALT_NAMESPACE)
        with self.lock:
            if SALT_KEY not in meta:
                meta[SALT_KEY] = base64.b64encode(crypto.randbytes(16)).decode()
            return base64.b64decode(meta[SALT_KEY])


class Table(MutableMapping):
    """A `MutableMapping[str, Any]` whose values are stored as JSON."""

    def __init__(self, store: Store, name: str):
        self.store = store
        self.name = name

    def __getitem__(self, key: str) -> Any:
        rows = self.store._execute("SELECT value FROM kv WHERE ns = ? AND key = ?", (self.name, key))
        if not rows:
            raise KeyError(key)
        return self._decode(rows[0][0])

    def __setitem__(self, key: str, value: Any) -> None:
        if not isinstance(key, str):
            raise TypeError(f"store keys are str, not {type(key).__name__}")
        self.store._execute(
            "INSERT OR REPLACE INTO kv (ns, key, value, updated) VALUES (?, ?, ?, ?)",
            (self.name, key, self._encode(value), time.time()),
        )

    def __delitem__(self, key: str) -> None:
        with self.store.lock:
            if key not in self:
                raise KeyError(key)
            self.store._execute("DELETE FROM kv WHERE ns = ? AND key = ?", (self.name, key))

    def __contains__(self, key: object) -> bool:
        rows = self.store._execute("SELECT 1 FROM kv WHERE ns = ? AND key = ?", (self.name, key))
        return bool(rows)

    def __iter__(self) -> Iterator[str]:
        return iter(self.keys())

    def __len__(self) -> int:
        return self.store._execute("SELECT COUNT(*) FROM kv WHERE ns = ?", (self.name,))[0][0]

    def keys(self, prefix: str = "") -> list[str]:  # type: ignore[override]
        return [key for key, _ in self._rows(prefix)]

    def values(self, prefix: str = "") -> list[Any]:  # type: ignore[override]
        return [self._decode(value) for _, value in self._rows(prefix)]

    def items(self, prefix: str = "") -> list[tuple[str, Any]]:  # type: ignore[override]
        return [(key, self._decode(value)) for key, value in self._rows(prefix)]

    def mtime(self, key: str) -> datetime.datetime | None:
        """When `key` was last written, or None if it is missing."""
        rows = self.store._execute("SELECT updated FROM kv WHERE ns = ? AND key = ?", (self.name, key))
        return datetime.datetime.fromtimestamp(rows[0][0]) if rows else None

    def __repr__(self) -> str:
        return f"<store.Table {self.name!r} in {self.store.file}>"

    def _rows(self, prefix: str) -> list[tuple[str, str]]:
        # A range on the primary key uses the index, where LIKE would scan every row.
        statement = "SELECT key, value FROM kv WHERE ns = ? AND key >= ?"
        parameters: tuple = (self.name, prefix)
        upper = _after_prefix(prefix)
        if upper is not None:
            statement += " AND key < ?"
            parameters += (upper,)
        return self.store._execute(statement + " ORDER BY key", parameters)

    def _encode(self, value: Any) -> str:
        try:
            return json.dumps(value, ensure_ascii=False)
        except (TypeError, ValueError) as error:
            raise TypeError(f"store values must be JSON: {error}") from None

    def _decode(self, text: str) -> Any:
        return json.loads(text)


class SecretTable(Table):
    """Values are `str`, sealed with `crypto.seal` and stored as base64."""

    def __init__(self, store: Store, name: str, key: bytes):
        super().__init__(store, name)
        self._key = key

    def _encode(self, value: Any) -> str:
        if not isinstance(value, str):
            raise TypeError(f"secret values are str, not {type(value).__name__}")
        return json.dumps(base64.b64encode(crypto.seal(value, self._key)).decode())

    def _decode(self, text: str) -> str:
        return crypto.unseal(base64.b64decode(json.loads(text)), self._key).decode("utf-8")


def _after_prefix(prefix: str) -> str | None:
    """The smallest string greater than every string that starts with `prefix`, or None."""
    stripped = prefix.rstrip(chr(0x10FFFF))
    if not stripped:
        return None
    return stripped[:-1] + chr(ord(stripped[-1]) + 1)
