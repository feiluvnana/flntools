"""A key-value store in one SQLite WAL file, backed by `msgspec.json` with optional TTL.

    from shared import store

    with store.open(".cache.db", table="downloads") as kv:
        kv["ins:1"] = {"path": "ins/a.jpg"}
        kv.set("loi:quota", 3, ttl=86_400)
        quotas = kv.table("quotas")
"""

from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
from collections.abc import ItemsView, Iterator, KeysView, Mapping, MutableMapping, ValuesView
from pathlib import Path
from typing import Any, Union

import msgspec

import shared
from shared import _clock as clock

__all__ = ["ClosedError", "Error", "Store", "UnstorableError", "open"]

PathLike = Union[str, "os.PathLike[str]"]

SCHEMA = """
CREATE TABLE IF NOT EXISTS kv (
  tbl TEXT NOT NULL, key TEXT NOT NULL, value TEXT NOT NULL, expires REAL,
  PRIMARY KEY (tbl, key)
) WITHOUT ROWID
"""
LIVE = "(expires IS NULL OR expires > ?)"
BUSY_TIMEOUT = 5.0
WAL_RETRY = 0.01


class Error(shared.Error):
    """The base of every error `shared.store` raises."""


class ClosedError(Error, RuntimeError):
    """Raised when a `Store` is used after `close()`."""


class UnstorableError(Error, TypeError):
    """Raised when a key is not valid UTF-8 `str` or a value cannot be JSON-encoded."""


def open(
    path: PathLike,
    *,
    table: str = "kv",
    schema: type[Any] | None = None,
    timeout: float = BUSY_TIMEOUT,
) -> Store:
    """Open or create a WAL SQLite store at `path` for `table`."""
    file = Path(path)
    try:
        file.parent.mkdir(parents=True, exist_ok=True)
        # Autocommit keeps every statement short so readers and writers never hold locks.
        connection = sqlite3.connect(
            file, timeout=timeout, check_same_thread=False, isolation_level=None
        )
    except (OSError, sqlite3.Error) as error:
        raise Error(str(error)) from error
    try:
        _wal(connection, timeout)
        connection.execute("PRAGMA synchronous=NORMAL")
        connection.execute(SCHEMA)
        link = _Link(connection)
        store = link.owner = Store(link, table, schema)
    except sqlite3.Error as error:
        connection.close()
        raise Error(str(error)) from error
    except BaseException:
        connection.close()
        raise
    return store


def _wal(connection: sqlite3.Connection, timeout: float) -> None:
    """Switch `connection` to WAL mode, retrying briefly while another opener holds the lock."""
    # Real time, not `clock`: the wait is for another connection's lock, which no fake can hurry.
    deadline = time.monotonic() + timeout
    while True:
        try:
            connection.execute("PRAGMA journal_mode=WAL")
            return
        except sqlite3.OperationalError as error:
            if "locked" not in str(error) or time.monotonic() >= deadline:
                raise
            time.sleep(WAL_RETRY)


class Store(MutableMapping[str, Any]):
    """A thread-safe `MutableMapping[str, Any]` over one table of a SQLite WAL file."""

    def __init__(self, link: _Link, table: str, schema: type[Any] | None = None) -> None:
        self._link = link
        self._table = table
        self._schema = schema
        self._closed = False

    def __getitem__(self, key: str) -> Any:
        text = self._read(key)
        if text is None:
            raise KeyError(key)
        return _decode(text, self._schema)

    def __setitem__(self, key: str, value: Any) -> None:
        self.set(key, value)

    def __delitem__(self, key: str) -> None:
        with self._link.lock:
            if self._read(key) is None:
                raise KeyError(key)
            self._execute("DELETE FROM kv WHERE tbl = ? AND key = ?", (self._table, key))

    def __contains__(self, key: object) -> bool:
        return isinstance(key, str) and self._read(key) is not None

    def __iter__(self) -> Iterator[str]:
        return iter(self._keys(""))

    def __len__(self) -> int:
        return self._count("")

    def __enter__(self) -> Store:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    def get(self, key: str, default: Any = None) -> Any:
        """Return the value for `key`, or `default` when missing or expired."""
        text = self._read(key)
        return default if text is None else _decode(text, self._schema)

    def set(self, key: str, value: Any, *, ttl: float | None = None) -> None:
        """Store `value` under `key`, optionally expiring after `ttl` seconds."""
        text = _encode(key, value)
        expires = None if ttl is None else clock.wall() + ttl
        self._execute(
            "INSERT OR REPLACE INTO kv (tbl, key, value, expires) VALUES (?, ?, ?, ?)",
            (self._table, key, text, expires),
        )

    def keys(self, prefix: str = "") -> KeysView[str]:
        """Return a live view of keys starting with `prefix`, sorted by key."""
        return _Keys(self, prefix)

    def items(self) -> ItemsView[str, Any]:
        """Return a live view of `(key, value)` pairs, sorted by key, read in one query."""
        return _Items(self)

    def values(self) -> ValuesView[Any]:
        """Return a live view of values, sorted by key, read in one query."""
        return _Values(self)

    def clear(self) -> None:
        """Delete every key in this table in one statement."""
        self._execute("DELETE FROM kv WHERE tbl = ?", (self._table,))

    def update(self, other: Any = (), /, **kwargs: Any) -> None:
        """Write every `(key, value)` pair from `other` and `kwargs` in one transaction."""
        pairs = list(other.items()) if isinstance(other, Mapping) else list(other)
        pairs.extend(kwargs.items())
        if not pairs:
            return
        rows = [(self._table, key, _encode(key, value), None) for key, value in pairs]
        with self._link.lock:
            self._check_open()
            conn = self._link.connection
            try:
                conn.execute("BEGIN")
                conn.executemany(
                    "INSERT OR REPLACE INTO kv (tbl, key, value, expires) VALUES (?, ?, ?, ?)",
                    rows,
                )
                conn.execute("COMMIT")
            except sqlite3.Error as error:
                _rollback(conn)
                raise Error(str(error)) from error
            except BaseException:
                _rollback(conn)
                raise

    def prune(self) -> int:
        """Delete expired keys in this table and return how many were removed."""
        with self._link.lock:
            self._check_open()
            try:
                cursor = self._link.connection.execute(
                    "DELETE FROM kv WHERE tbl = ? AND expires <= ?",
                    (self._table, clock.wall()),
                )
                return cursor.rowcount
            except sqlite3.Error as error:
                raise Error(str(error)) from error

    def table(self, name: str, *, schema: type[Any] | None = None) -> Store:
        """Return another table on this store's connection."""
        self._check_open()
        return Store(self._link, name, schema)

    @property
    def raw(self) -> sqlite3.Connection:
        """Return the underlying `sqlite3.Connection` shared across tables."""
        return self._link.connection

    def close(self) -> None:
        """Close this store; closing the root store closes the shared connection."""
        with self._link.lock:
            self._closed = True
            if self._link.owner is self and not self._link.closed:
                self._link.closed = True
                try:
                    self._link.connection.close()
                except sqlite3.Error as error:
                    raise Error(str(error)) from error

    def _items(self) -> list[tuple[str, Any]]:
        """Read all live `(key, value)` pairs in one query."""
        rows = self._execute(
            f"SELECT key, value FROM kv WHERE tbl = ? AND {LIVE} ORDER BY key",
            (self._table, clock.wall()),
        )
        return [(key, _decode(text, self._schema)) for key, text in rows]

    def _values(self) -> list[Any]:
        """Read all live values in one query."""
        rows = self._execute(
            f"SELECT value FROM kv WHERE tbl = ? AND {LIVE} ORDER BY key",
            (self._table, clock.wall()),
        )
        return [_decode(text, self._schema) for (text,) in rows]

    def _keys(self, prefix: str) -> list[str]:
        """Read all live keys starting with `prefix`."""
        where, parameters = self._prefix_where(prefix)
        query = f"SELECT key FROM kv WHERE {where} ORDER BY key"
        return [key for (key,) in self._execute(query, parameters)]

    def _count(self, prefix: str) -> int:
        """Count live keys starting with `prefix`."""
        where, parameters = self._prefix_where(prefix)
        return self._execute(f"SELECT COUNT(*) FROM kv WHERE {where}", parameters)[0][0]

    def _prefix_where(self, prefix: str) -> tuple[str, tuple[Any, ...]]:
        """Build an index-friendly WHERE clause for live keys starting with `prefix`."""
        where = f"tbl = ? AND key >= ? AND {LIVE}"
        parameters: tuple[Any, ...] = (self._table, prefix, clock.wall())
        upper = _after_prefix(prefix)
        if upper is not None:
            where += " AND key < ?"
            parameters += (upper,)
        return where, parameters

    def _read(self, key: str) -> str | None:
        """Read the stored JSON for `key` without taking a write lock."""
        rows = self._execute(
            "SELECT value, expires FROM kv WHERE tbl = ? AND key = ?",
            (self._table, key),
        )
        if not rows:
            return None
        text, expires = rows[0]
        if expires is not None and expires <= clock.wall():
            return None
        return text

    def _execute(self, statement: str, parameters: tuple[Any, ...] = ()) -> list[Any]:
        """Run one statement under the connection lock and return all rows."""
        with self._link.lock:
            self._check_open()
            try:
                return self._link.connection.execute(statement, parameters).fetchall()
            except sqlite3.Error as error:
                raise Error(str(error)) from error

    def _check_open(self) -> None:
        """Raise `ClosedError` if this store or its connection is closed."""
        if self._closed or self._link.closed:
            raise ClosedError("this store is closed")


class _Link:
    """Shared connection, reentrant lock, and owning `Store`."""

    def __init__(self, connection: sqlite3.Connection) -> None:
        self.connection = connection
        # Reentrant so callers like `__delitem__` can guard a read+delete sequence.
        self.lock = threading.RLock()
        self.closed = False
        self.owner: Store | None = None


class _Keys(KeysView[str]):
    """Live `KeysView` over keys starting with `prefix`."""

    def __init__(self, store: Store, prefix: str) -> None:
        super().__init__(store)
        self._store = store
        self._prefix = prefix

    def __iter__(self) -> Iterator[str]:
        return iter(self._store._keys(self._prefix))

    def __len__(self) -> int:
        return self._store._count(self._prefix)

    def __contains__(self, key: object) -> bool:
        return isinstance(key, str) and key.startswith(self._prefix) and key in self._store

    def __repr__(self) -> str:
        return f"{type(self).__name__}({list(self)!r})"


class _Items(ItemsView[str, Any]):
    """Live `ItemsView` reading all pairs in one query."""

    def __init__(self, store: Store) -> None:
        super().__init__(store)
        self._store = store

    def __iter__(self) -> Iterator[tuple[str, Any]]:
        return iter(self._store._items())


class _Values(ValuesView[Any]):
    """Live `ValuesView` reading all values in one query."""

    def __init__(self, store: Store) -> None:
        super().__init__(store)
        self._store = store

    def __iter__(self) -> Iterator[Any]:
        return iter(self._store._values())


def _encode(key: Any, value: Any) -> str:
    """Validate `key` and JSON-encode `value` with surrogate-safe fallback."""
    if not isinstance(key, str):
        raise UnstorableError(f"store keys are str, not {type(key).__name__}")
    try:
        key.encode("utf-8")
    except UnicodeEncodeError as error:
        raise UnstorableError(f"store keys must be valid UTF-8: {error}") from None
    try:
        return msgspec.json.encode(value).decode("utf-8")
    except UnicodeEncodeError:
        # Lone surrogates (e.g. non-UTF-8 paths) cannot bind as UTF-8 text in SQLite.
        try:
            return json.dumps(msgspec.to_builtins(value), ensure_ascii=True)
        except (TypeError, ValueError) as error:
            raise UnstorableError(f"store values must be JSON: {error}") from None
    except (TypeError, ValueError, msgspec.EncodeError) as error:
        raise UnstorableError(f"store values must be JSON: {error}") from None


def _decode(text: str, schema: type[Any] | None) -> Any:
    """Decode JSON `text`, optionally validating against `schema`."""
    try:
        if schema is None:
            return msgspec.json.decode(text)
        return msgspec.json.decode(text, type=schema)
    except msgspec.ValidationError as error:
        raise Error(str(error)) from error
    except msgspec.DecodeError as error:
        # msgspec rejects lone UTF-16 surrogates that stdlib json round-trips.
        try:
            parsed = json.loads(text)
        except ValueError:
            raise Error(str(error)) from error
        if schema is None:
            return parsed
        try:
            return msgspec.convert(parsed, type=schema)
        except msgspec.ValidationError as conv_error:
            raise Error(str(conv_error)) from conv_error


def _rollback(connection: sqlite3.Connection) -> None:
    """Roll back an active transaction without masking the original exception."""
    try:
        connection.execute("ROLLBACK")
    except sqlite3.Error:
        pass


def _after_prefix(prefix: str) -> str | None:
    """Return the smallest string greater than every string starting with `prefix`."""
    stripped = prefix.rstrip(chr(0x10FFFF))
    if not stripped:
        return None
    nxt = ord(stripped[-1]) + 1
    # Skip UTF-16 surrogates (0xD800..0xDFFF), which sqlite3 rejects on bind.
    if 0xD800 <= nxt <= 0xDFFF:
        nxt = 0xE000
    return stripped[:-1] + chr(nxt)
