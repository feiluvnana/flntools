"""A key-value store in one sqlite file, used like a dict: `kv.open(path)`.

`open` connects to the file with `sqlite3` and returns a `Store`
(`MutableMapping[str, Any]`) over one table. Tables share one sqlite table `kv` as
(table, key, JSON value, expiry time). `Store.table(name)` accesses another table
on the same connection. Keys support TTL.

    from shared import kv

    with kv.open(".cache.db", table="downloads") as store:
        store["ins:1"] = {"path": "ins/a.jpg"}
        store.set("loi:quota", 3, ttl=86_400)
        quotas = store.table("quotas")
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

import shared
from shared import _clock as clock

__all__ = ["ClosedError", "Error", "Store", "UnstorableError", "open"]

PathLike = Union[str, "os.PathLike[str]"]

# The row format every `.flnd/*.db` uses; must keep reading them.
SCHEMA = """
CREATE TABLE IF NOT EXISTS kv (
  tbl TEXT NOT NULL, key TEXT NOT NULL, value TEXT NOT NULL, expires REAL,
  PRIMARY KEY (tbl, key)
) WITHOUT ROWID
"""
# A row is live until its `expires` time, a Unix time in seconds; NULL never expires.
LIVE = "(expires IS NULL OR expires > ?)"
# How long a call waits for another connection's write to finish before it fails, in seconds:
# long enough for any one flnd write, short enough that a stuck process shows up as an error.
BUSY_TIMEOUT = 5.0
# Wait before trying switch to WAL mode again, in seconds; switch takes microseconds.
WAL_RETRY = 0.01


class Error(shared.Error):
    """The base of every error `shared.kv` raises."""


# A RuntimeError: using a closed store is a caller mistake, never bad data.
class ClosedError(Error, RuntimeError):
    """The Store was used after `close()`, or after connection closed."""


# A TypeError, so `store[1] = ...` fails the way a typed mapping would.
class UnstorableError(Error, TypeError):
    """A key that isn't a `str`, or a value `json.dumps` can't write."""


def open(path: PathLike, *, table: str = "default", timeout: float = BUSY_TIMEOUT) -> Store:
    """Open or create store at `path`, and sweep out `table`'s expired keys.

        with kv.open(".flnd/metadata/history.db", table="downloads") as history:
            history["ins:1"] = {"path": "ins/a.jpg"}

    Args:
        path: the sqlite file; it and its folder are created when missing.
        table: which table of the file the `Store` reads and writes; tables are kept apart.
        timeout: how long, in seconds, a call waits while another connection to the file writes,
            before it fails with `sqlite3.OperationalError` ("database is locked").

    Returns:
        A `Store`, which owns the connection: closing it closes every `Store.table` made from it.

    Raises:
        OSError: the folder can't be created.
        sqlite3.DatabaseError: the file isn't a sqlite database, or can't be opened.

    The file is switched to WAL mode, which sqlite keeps in the file: it lets readers go on
    while one connection writes, and puts two files beside it while it is open, `<name>-wal` and
    `<name>-shm`. They are part of the database: copy or delete all three together. WAL needs
    a local disk, not a network share. It may be called from several threads.
    """
    file = Path(path)
    file.parent.mkdir(parents=True, exist_ok=True)
    # Autocommit (isolation_level=None), so every statement is its own short transaction: no
    # write holds the file's lock while waiting on another thread, and nothing needs a commit.
    # `timeout` is sqlite's busy timeout: a write that finds the file locked by another process
    # retries for that long instead of failing at once, so two flnd commands can run together.
    connection = sqlite3.connect(
        file, timeout=timeout, check_same_thread=False, isolation_level=None
    )
    try:
        # WAL: readers never block the writer or each other, so a long `flnd sync` reading the
        # history doesn't stop a `flnd download` writing to it. Files written before were in WAL
        # already; on a new file it is set once and sqlite remembers it.
        _wal(connection, timeout)
        connection.execute("PRAGMA synchronous=NORMAL")
        connection.execute(SCHEMA)
        link = _Link(connection)
        store = link.owner = Store(link, table)
    except BaseException:
        connection.close()
        raise
    return store


def _wal(connection: sqlite3.Connection, timeout: float) -> None:
    """Switch file to WAL mode, waiting up to `timeout` seconds while switching.

    Changing the journal mode takes the file's exclusive lock, and sqlite doesn't always wait out
    its busy timeout for it: two threads or processes opening a new file at once (two sources'
    first run, each opening `sources.db`) saw "database is locked" at once. So it is tried again,
    briefly, until `timeout`; any other error, or one still locked then, is raised.
    """
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
    """A `MutableMapping[str, Any]` over one table of a sqlite file, made by `kv.open` or `table`.

        with kv.open("state.db") as store:
            store.set("loi:quota", 3, ttl=86_400)
            dict(store)                     # {"loi:quota": 3}

    Every value is stored as JSON, so it reads back as the JSON types: a tuple comes back a list.
    A missing or expired key raises `KeyError`, as a dict's does. Many threads may use one
    `Store`: every call takes the connection's lock.
    """

    def __init__(self, link: _Link, table: str):
        self._link = link
        self._table = table
        self._closed = False
        # Each `Store` sweeps its own table, so a table nobody opens keeps its expired rows,
        # which read as missing anyway.
        self._execute("DELETE FROM kv WHERE tbl = ? AND expires <= ?", (table, clock.wall()))

    def __getitem__(self, key: str) -> Any:
        text = self._read(key)
        if text is None:
            raise KeyError(key)
        return json.loads(text)

    def __setitem__(self, key: str, value: Any) -> None:
        self.set(key, value)

    def __delitem__(self, key: str) -> None:
        # The lock makes the check and the delete one step for this connection's threads.
        with self._link.lock:
            if self._read(key) is None:
                raise KeyError(key)
            self._execute("DELETE FROM kv WHERE tbl = ? AND key = ?", (self._table, key))

    def __contains__(self, key: object) -> bool:
        return isinstance(key, str) and self._read(key) is not None

    def __iter__(self) -> Iterator[str]:
        # A snapshot, so the caller may change the store while iterating, as `clear()` does.
        return iter(self._keys(""))

    def __len__(self) -> int:
        return self._count("")

    def __enter__(self) -> Store:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    def get(self, key: str, default: Any = None) -> Any:
        """The value of `key`, or `default` when it is missing or expired.

            store.get("ins:1", default={})

        Args:
            key: the key to read.
            default: what to return when the key is missing or expired.
        """
        # Defined here because the standard `Mapping.get` takes `default` only by position.
        text = self._read(key)
        return default if text is None else json.loads(text)

    def set(self, key: str, value: Any, *, ttl: float | None = None) -> None:
        """Store `value` under `key`, like `store[key] = value`, expiring after `ttl` seconds.

            store.set("loi:quota", 3, ttl=86_400)

        Args:
            key: the key, any `str`.
            value: anything `json.dumps` writes: dicts, lists, str, numbers, bools and None.
            ttl: seconds until the key expires; None keeps it until it is deleted. Setting a key
                again replaces its ttl, so `store[key] = value` makes it permanent.

        Raises:
            UnstorableError: `key` isn't a `str`, or `json.dumps` can't write `value`.
        """
        text = _encode(key, value)
        expires = None if ttl is None else clock.wall() + ttl
        self._execute("INSERT OR REPLACE INTO kv (tbl, key, value, expires) VALUES (?, ?, ?, ?)",
                      (self._table, key, text, expires))

    def keys(self, prefix: str = "") -> KeysView[str]:
        """The keys that start with `prefix`, sorted: the mapping's own `keys()`, narrowed.

            store.keys(prefix="tlg:123:")       # list() it for a snapshot

        Args:
            prefix: only keys that start with it; "" (the default) is every key, as a dict's.

        Returns:
            A live, set-like view, as a dict's `keys()`: each `len`, `in` or loop reads the file
            again, and a loop goes over the keys as they were when it began. It finds the
            prefix through the primary key's index, so it is quick on a big table.
        """
        return _Keys(self, prefix)

    def items(self) -> ItemsView[str, Any]:
        """Live view of `(key, value)` pairs, sorted by key, read in one query."""
        return _Items(self)

    def values(self) -> ValuesView[Any]:
        """Live view of values, sorted by key, read in one query."""
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
            conn.execute("BEGIN")
            try:
                conn.executemany(
                    "INSERT OR REPLACE INTO kv (tbl, key, value, expires) VALUES (?, ?, ?, ?)",
                    rows,
                )
                conn.execute("COMMIT")
            except BaseException:
                conn.execute("ROLLBACK")
                raise

    def table(self, name: str) -> Store:
        """Another table of the same file, on this store's connection.

            with kv.open(".flnd/state.db") as store:
                downloads = store.table("downloads")

        Args:
            name: table name; created on first key set.

        Raises:
            ClosedError: this store is closed.
        """
        self._check_open()
        return Store(self._link, name)

    @property
    def raw(self) -> sqlite3.Connection:
        """The `sqlite3.Connection`, shared with every table of this store.

            store.raw.execute("VACUUM")

        It is in autocommit mode. Hold no transaction open on it while other threads use the store.
        """
        return self._link.connection

    def close(self) -> None:
        """Close this store; using it afterwards raises `ClosedError`. Closing twice does nothing.

            store.close()

        On the `Store` that `kv.open` returned, it closes the connection, so every `table` made
        from it closes too; on a `table`, it closes only that one.
        """
        with self._link.lock:
            self._closed = True
            if self._link.owner is self:
                self._link.closed = True
                self._link.connection.close()

    def _items(self) -> list[tuple[str, Any]]:
        """A sorted snapshot of live `(key, value)` pairs in one query."""
        rows = self._execute(
            f"SELECT key, value FROM kv WHERE tbl = ? AND {LIVE} ORDER BY key",
            (self._table, clock.wall()),
        )
        return [(key, json.loads(text)) for key, text in rows]

    def _values(self) -> list[Any]:
        """A sorted snapshot of live values in one query."""
        rows = self._execute(
            f"SELECT value FROM kv WHERE tbl = ? AND {LIVE} ORDER BY key",
            (self._table, clock.wall()),
        )
        return [json.loads(text) for (text,) in rows]

    def _keys(self, prefix: str) -> list[str]:
        """A sorted snapshot of the live keys that start with `prefix`."""
        where, parameters = self._prefix_where(prefix)
        query = f"SELECT key FROM kv WHERE {where} ORDER BY key"
        return [key for (key,) in self._execute(query, parameters)]

    def _count(self, prefix: str) -> int:
        """How many live keys start with `prefix`."""
        where, parameters = self._prefix_where(prefix)
        return self._execute(f"SELECT COUNT(*) FROM kv WHERE {where}", parameters)[0][0]

    def _prefix_where(self, prefix: str) -> tuple[str, tuple[Any, ...]]:
        """WHERE clause and parameters for live keys that start with `prefix`."""
        # A range on the primary key uses the index, where LIKE would scan every row.
        where = f"tbl = ? AND key >= ? AND {LIVE}"
        parameters: tuple[Any, ...] = (self._table, prefix, clock.wall())
        upper = _after_prefix(prefix)
        if upper is not None:
            where += " AND key < ?"
            parameters += (upper,)
        return where, parameters

    def _read(self, key: str) -> str | None:
        """The stored JSON for `key`, or None when missing or expired."""
        with self._link.lock:
            stmt = "SELECT value, expires FROM kv WHERE tbl = ? AND key = ?"
            rows = self._execute(stmt, (self._table, key))
            if not rows:
                return None
            text, expires = rows[0]
            if expires is not None and expires <= clock.wall():
                self._execute("DELETE FROM kv WHERE tbl = ? AND key = ?", (self._table, key))
                return None
            return text

    def _execute(self, statement: str, parameters: tuple[Any, ...] = ()) -> list[Any]:
        """Run one statement under the connection's lock and return all its rows."""
        # One `sqlite3.Connection` isn't safe to use from two threads at once, and a cursor's rows
        # must be fetched before another thread runs a statement; the lock covers both.
        with self._link.lock:
            self._check_open()
            return self._link.connection.execute(statement, parameters).fetchall()

    def _check_open(self) -> None:
        if self._closed or self._link.closed:
            raise ClosedError("this kv store is closed")


class _Link:
    """The connection every `Store` of one `kv.open` shares, its lock, and which `Store` owns it."""

    def __init__(self, connection: sqlite3.Connection):
        self.connection = connection
        # Reentrant, because `_read` holds it around `_execute`, which takes it too.
        self.lock = threading.RLock()
        self.closed = False
        # The `Store` that `kv.open` returned; only its `close()` closes the connection.
        self.owner: Store | None = None


class _Keys(KeysView[str]):
    """`Store.keys(prefix)`: a dict-like keys view over the keys that start with `prefix`."""

    def __init__(self, store: Store, prefix: str):
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
    """`Store.items()`: live view reading all pairs in one query."""

    def __init__(self, store: Store):
        super().__init__(store)
        self._store = store

    def __iter__(self) -> Iterator[tuple[str, Any]]:
        return iter(self._store._items())


class _Values(ValuesView[Any]):
    """`Store.values()`: live view reading all values in one query."""

    def __init__(self, store: Store):
        super().__init__(store)
        self._store = store

    def __iter__(self) -> Iterator[Any]:
        return iter(self._store._values())


def _encode(key: Any, value: Any) -> str:
    """Validate `key` and JSON-encode `value` for storage."""
    if not isinstance(key, str):
        raise UnstorableError(f"kv keys are str, not {type(key).__name__}")
    try:
        key.encode("utf-8")
    except UnicodeEncodeError as error:
        raise UnstorableError(f"kv keys must be valid UTF-8: {error}") from None
    try:
        text = json.dumps(value, ensure_ascii=False)
    except (TypeError, ValueError) as error:
        # TypeError for a type json can't write, ValueError for a circular value or NaN rules.
        raise UnstorableError(f"kv values must be JSON: {error}") from None
    try:
        text.encode("utf-8")
    except UnicodeEncodeError:
        # Lone surrogates (e.g. os.fsdecode on non-UTF-8 paths) cannot bind as UTF-8 text in SQLite.
        return json.dumps(value, ensure_ascii=True)
    return text


def _after_prefix(prefix: str) -> str | None:
    """The smallest string greater than every string that starts with `prefix`, or None."""
    stripped = prefix.rstrip(chr(0x10FFFF))
    if not stripped:
        return None
    nxt = ord(stripped[-1]) + 1
    # Skip UTF-16 surrogates (0xD800..0xDFFF), which sqlite3 rejects on bind.
    if 0xD800 <= nxt <= 0xDFFF:
        nxt = 0xE000
    return stripped[:-1] + chr(nxt)
