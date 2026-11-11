"""A key-value store in one sqlite file, used like a dict.

Values are stored as JSON. Several tables can share one file, and `db.raw` is the real
`sqlite3.Connection` for anything else.
"""

from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
from collections.abc import Iterator, MutableMapping
from pathlib import Path
from typing import Any, Union

PathLike = Union[str, "os.PathLike[str]"]

SCHEMA = """
CREATE TABLE IF NOT EXISTS kv (
  tbl TEXT NOT NULL, key TEXT NOT NULL, value TEXT NOT NULL, expires REAL,
  PRIMARY KEY (tbl, key)
) WITHOUT ROWID
"""
LIVE = "(expires IS NULL OR expires > ?)"


def open(path: PathLike, *, table: str = "default") -> KV:
    """Open or create the store at `path`, creating its folder, and sweep out expired keys.

        db = kv.open(".flnd/state.db", table="downloads")
        db["ins:1"] = {"path": "ins/a.jpg"}

    Use it with `with`, or call `close()` when done.
    """
    return KV(path, table)


class KV(MutableMapping[str, Any]):
    """A `MutableMapping[str, Any]` over one table of a sqlite file.

        with kv.open("state.db") as db:
            db.set("loi:quota", 3, ttl=86400)

    Many threads may use one `KV`: they share one connection behind a lock.
    """

    def __init__(self, path: PathLike, table: str):
        file = Path(path)
        file.parent.mkdir(parents=True, exist_ok=True)
        self._table = table
        self._lock = threading.RLock()
        self._closed = False
        # Autocommit, so every write is its own transaction and nothing waits on another thread's commit.
        self._connection = sqlite3.connect(file, check_same_thread=False, isolation_level=None)
        self._execute("PRAGMA journal_mode=WAL")
        self._execute(SCHEMA)
        self._execute("DELETE FROM kv WHERE tbl = ? AND expires <= ?", (table, _now()))

    def __getitem__(self, key: str) -> Any:
        text = self._read(key)
        if text is None:
            raise KeyError(key)
        return json.loads(text)

    def __setitem__(self, key: str, value: Any) -> None:
        self.set(key, value)

    def __delitem__(self, key: str) -> None:
        with self._lock:
            if self._read(key) is None:
                raise KeyError(key)
            self._execute("DELETE FROM kv WHERE tbl = ? AND key = ?", (self._table, key))

    def __contains__(self, key: object) -> bool:
        return isinstance(key, str) and self._read(key) is not None

    def __iter__(self) -> Iterator[str]:
        return iter(self.get_keys())

    def __len__(self) -> int:
        return self._execute(f"SELECT COUNT(*) FROM kv WHERE tbl = ? AND {LIVE}", (self._table, _now()))[0][0]

    def __enter__(self) -> KV:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    def get(self, key: str, default: Any = None) -> Any:
        """The value of `key`, or `default` when it is missing or expired.

            db.get("ins:1", default={})
        """
        # Defined here because the standard `Mapping.get` takes `default` only by position.
        text = self._read(key)
        return default if text is None else json.loads(text)

    def set(self, key: str, value: Any, *, ttl: float | None = None) -> None:
        """Store `value` under `key`, like `db[key] = value`, optionally expiring after `ttl` seconds.

            db.set("loi:quota", 3, ttl=86400)

        A value `json.dumps` can't write raises `TypeError`.
        """
        if not isinstance(key, str):
            raise TypeError(f"kv keys are str, not {type(key).__name__}")
        try:
            text = json.dumps(value, ensure_ascii=False)
        except (TypeError, ValueError) as error:
            raise TypeError(f"kv values must be JSON: {error}") from None
        expires = None if ttl is None else _now() + ttl
        self._execute("INSERT OR REPLACE INTO kv (tbl, key, value, expires) VALUES (?, ?, ?, ?)",
                      (self._table, key, text, expires))

    def get_keys(self, prefix: str = "") -> list[str]:
        """A sorted snapshot of the keys that start with `prefix`.

            db.get_keys(prefix="tlg:123:")
        """
        # A range on the primary key uses the index, where LIKE would scan every row.
        statement = f"SELECT key FROM kv WHERE tbl = ? AND key >= ? AND {LIVE}"
        parameters: tuple[Any, ...] = (self._table, prefix, _now())
        upper = _after_prefix(prefix)
        if upper is not None:
            statement += " AND key < ?"
            parameters += (upper,)
        return [key for (key,) in self._execute(statement + " ORDER BY key", parameters)]

    @property
    def raw(self) -> sqlite3.Connection:
        """The `sqlite3.Connection`, for anything `KV` doesn't do.

            db.raw.execute("VACUUM")
        """
        return self._connection

    def close(self) -> None:
        """Close the connection; using the `KV` afterwards raises `RuntimeError`.

            db.close()
        """
        with self._lock:
            self._closed = True
            self._connection.close()

    def _read(self, key: str) -> str | None:
        """The stored JSON for `key`, or None when it is missing or expired."""
        with self._lock:
            rows = self._execute("SELECT value, expires FROM kv WHERE tbl = ? AND key = ?", (self._table, key))
            if not rows:
                return None
            text, expires = rows[0]
            if expires is not None and expires <= _now():
                self._execute("DELETE FROM kv WHERE tbl = ? AND key = ?", (self._table, key))
                return None
            return text

    def _execute(self, statement: str, parameters: tuple[Any, ...] = ()) -> list[Any]:
        with self._lock:
            if self._closed:
                raise RuntimeError("this kv store is closed")
            return self._connection.execute(statement, parameters).fetchall()


def _now() -> float:
    """The current time; tests replace it to expire keys without waiting."""
    return time.time()


def _after_prefix(prefix: str) -> str | None:
    """The smallest string greater than every string that starts with `prefix`, or None."""
    stripped = prefix.rstrip(chr(0x10FFFF))
    if not stripped:
        return None
    return stripped[:-1] + chr(ord(stripped[-1]) + 1)
