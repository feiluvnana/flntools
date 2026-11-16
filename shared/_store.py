"""A key-value store in one SQLite file: tables of JSON values, or of typed `Row` records.

    db = store.open("state.db")
    db["state"]["cursor"] = "abc"

    class File(store.Row):
        table = "files"
        state: str
        size: int

    files = db[File]
    files["a.jpg"] = File("kept", 1000)
    files.rows.prefix("a").filter(state="kept").get()   # [("a.jpg", File(...))], run in SQL
    with db.batch():                                    # one transaction for many writes
        ...
"""

from __future__ import annotations

import json
import os
import sqlite3
import threading
from collections.abc import Iterator, MutableMapping
from contextlib import contextmanager
from pathlib import Path
from typing import Any, ClassVar, Generic, TypeVar, Union, overload

import msgspec

import shared
from shared._query import Plan, Query, run as run_plan

__all__ = ["ClosedError", "Db", "Error", "Row", "Rows", "Table", "open"]

PathLike = Union[str, "os.PathLike[str]"]
V = TypeVar("V")
R = TypeVar("R", bound="Row")

# A table's rows live in an SQL table of this name, so a user's name never meets SQL's own.
PREFIX = "t_"
# How long a writer waits for another process's transaction before giving up, in ms.
BUSY_MS = 30_000
# SQLite's header; a store file without it is a JSON store from before, converted on open.
SQLITE_MAGIC = b"SQLite format 3\x00"


class Error(shared.Error):
    """The base of every error `shared.store` raises."""


class ClosedError(Error, RuntimeError):
    """The `Db` was closed before this call."""


class Row(msgspec.Struct):
    """A typed record for `db[RowType]`; a subclass names its table with `table = "..."`."""

    table: ClassVar[str]


class Db:
    """An open store file; `db[name]` and `db[RowType]` are its tables."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._lock = threading.RLock()
        self._tables: dict[Any, Table[Any]] = {}
        self._depth = 0
        self._closed = False
        # Connected on first use, so opening a store that is only read never creates its file.
        self._sql: sqlite3.Connection | None = None

    def __enter__(self) -> Db:
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    @overload
    def __getitem__(self, key: str) -> Table[Any]: ...
    @overload
    def __getitem__(self, key: type[R]) -> Table[R]: ...
    def __getitem__(self, key: str | type[Row]) -> Table[Any]:
        with self._lock:
            found = self._tables.get(key)
            if found is None:
                name, kind = (key, None) if isinstance(key, str) else (key.table, key)
                found = self._tables[key] = Table(self, name, kind)
            return found

    @property
    def tables(self) -> list[str]:
        """The names of the tables that hold rows."""
        cursor = self._run("SELECT name FROM sqlite_master WHERE type = 'table'", read=True)
        return sorted(name[len(PREFIX):] for (name,) in cursor if name.startswith(PREFIX))

    @contextmanager
    def batch(self) -> Iterator[None]:
        """Run the writes inside the block as one transaction, rolled back on an error."""
        with self._lock:
            # Counted only once BEGIN worked, so a busy file never leaves later batches unwrapped.
            if self._depth == 0:
                self._run("BEGIN IMMEDIATE")
            self._depth += 1
            try:
                yield
                if self._depth == 1:
                    self._run("COMMIT")
            except BaseException:
                if self._depth == 1:
                    self._rollback()
                raise
            finally:
                self._depth -= 1

    def _rollback(self) -> None:
        # A failed COMMIT may have ended the transaction already.
        if self._sql is not None and self._sql.in_transaction:
            self._run("ROLLBACK")
        # The rollback undid any CREATE TABLE in the batch too.
        for table in self._tables.values():
            table._made = False

    def close(self) -> None:
        """Close the file; later calls raise `ClosedError`."""
        with self._lock:
            self._closed = True
            if self._sql is not None:
                self._sql.close()
                self._sql = None

    def _run(self, sql: str, args: tuple[Any, ...] = (), *, read: bool = False) -> list[Any]:
        with self._lock:
            if self._closed:
                raise ClosedError(f"{self.path}: store is closed")
            if self._sql is None:
                if read and not self.path.exists():
                    return []
                self._sql = _connect(self.path)
            try:
                return self._sql.execute(sql, args).fetchall()
            except sqlite3.Error as error:
                raise Error(f"{self.path}: {error}") from error


class Table(MutableMapping[str, V], Generic[V]):
    """One table: a mapping of string keys to JSON values, or to one `Row` type."""

    def __init__(self, db: Db, name: str, kind: type[V] | None) -> None:
        if not name or '"' in name:
            raise Error(f"{db.path}: bad table name {name!r}")
        self.db, self.name = db, name
        self._sql_name = f'"{PREFIX}{name}"'
        self._decode = msgspec.json.Decoder(kind) if kind is not None else msgspec.json.Decoder()
        self._made = False

    def __getitem__(self, key: str) -> V:
        found = self._select("SELECT value FROM {} WHERE key = ?", (key,))
        if not found:
            raise KeyError(key)
        return self._load(found[0][0])

    def __setitem__(self, key: str, value: V) -> None:
        if not isinstance(key, str):
            raise Error(f"{self.db.path}: keys must be strings, not {type(key).__name__}")
        self._write("INSERT OR REPLACE INTO {} (key, value) VALUES (?, ?)", (key, _encode(value)))

    def __delitem__(self, key: str) -> None:
        if key not in self:
            raise KeyError(key)
        self._write("DELETE FROM {} WHERE key = ?", (key,))

    def __contains__(self, key: object) -> bool:
        return bool(self._select("SELECT 1 FROM {} WHERE key = ?", (key,)))

    def __iter__(self) -> Iterator[str]:
        return iter([key for (key,) in self._select("SELECT key FROM {} ORDER BY key")])

    def __len__(self) -> int:
        found = self._select("SELECT count(*) FROM {}")
        return found[0][0] if found else 0

    def items(self) -> Any:
        """Every (key, value) pair, read in one query."""
        return dict(self._pairs("SELECT key, value FROM {} ORDER BY key")).items()

    def values(self) -> Any:
        """Every value, read in one query."""
        return [value for _, value in self._pairs("SELECT key, value FROM {} ORDER BY key")]

    def clear(self) -> None:
        """Remove every row."""
        self._write("DELETE FROM {}", ())

    @property
    def rows(self) -> Rows[V]:
        """Every (key, value) pair as a query; `prefix`, `filter`, `sort` and `limit` run in SQL."""
        return Rows(_Pushed(self))

    def _pairs(self, sql: str, args: tuple[Any, ...] = ()) -> list[tuple[str, V]]:
        return [(key, self._load(raw)) for key, raw in self._select(sql, args)]

    def _load(self, raw: str) -> V:
        try:
            return self._decode.decode(raw)
        # A row written before its type gained a required field no longer fits it.
        except msgspec.DecodeError as error:
            raise Error(f"{self.db.path}: {self.name}: {error}") from error

    def _select(self, sql: str, args: tuple[Any, ...] = ()) -> list[tuple[Any, ...]]:
        try:
            return self.db._run(sql.format(self._sql_name), args, read=True)
        except Error as error:
            # A table nothing was written to yet reads as empty.
            if not self._made and "no such table" in str(error):
                return []
            raise

    def _write(self, sql: str, args: tuple[Any, ...]) -> None:
        if not self._made:
            self.db._run(
                f"CREATE TABLE IF NOT EXISTS {self._sql_name} "
                "(key TEXT PRIMARY KEY, value TEXT NOT NULL) WITHOUT ROWID"
            )
            self._made = True
        self.db._run(sql.format(self._sql_name), args)


class Rows(Query[tuple[str, V]]):
    """A table's rows as a query, with `prefix` to keep keys that start with a text."""

    def prefix(self, text: str) -> Rows[V]:
        """Keep rows whose key starts with `text`."""
        source = self._source
        assert isinstance(source, _Pushed)
        return Rows(_Pushed(source.table, source.prefix + text), self._plan)

    def filter(self, predicate: Any = None, /, **equal: Any) -> Rows[V]:
        """Keep rows where `predicate((key, value))` holds and each value field equals its value."""
        found = super().filter(predicate, **equal)
        return Rows(found._source, found._plan)

    def sort(self, by: Any, *, desc: bool = False) -> Rows[V]:
        """Order rows by a value field, `"key"`, or a key function of `(key, value)`."""
        found = super().sort(by, desc=desc)
        return Rows(found._source, found._plan)

    def limit(self, n: int) -> Rows[V]:
        """Keep at most `n` rows."""
        found = super().limit(n)
        return Rows(found._source, found._plan)


class _Pushed(Generic[V]):
    """Runs a `Plan` over a table: in SQL when it can, else filtering decoded rows in Python."""

    def __init__(self, table: Table[V], prefix: str = "") -> None:
        self.table, self.prefix = table, prefix

    def run(self, plan: Plan) -> list[tuple[str, V]]:
        where, args = self._where(plan)
        if plan.predicates or callable(plan.by):
            rows = self.table._pairs(f"SELECT key, value FROM {{}}{where} ORDER BY key", args)
            return run_plan(rows, _on_values(plan))
        order = " ORDER BY key"
        if isinstance(plan.by, str) and plan.by != "key":
            order = " ORDER BY json_extract(value, ?)"
            args = (*args, f"$.{plan.by}")
        if plan.by is not None and plan.desc:
            order += " DESC"
        limit = "" if plan.limit is None else f" LIMIT {int(plan.limit)}"
        return self.table._pairs(f"SELECT key, value FROM {{}}{where}{order}{limit}", args)

    def count(self, plan: Plan) -> int:
        if plan.predicates or plan.limit is not None:
            return len(self.run(plan))
        where, args = self._where(plan)
        found = self.table._select(f"SELECT count(*) FROM {{}}{where}", args)
        return found[0][0] if found else 0

    def _where(self, plan: Plan) -> tuple[str, tuple[Any, ...]]:
        clauses: list[str] = []
        args: list[Any] = []
        if self.prefix:
            clauses.append("key >= ? AND key < ?")
            args += [self.prefix, self.prefix + "\U0010ffff"]
        for name, value in plan.equal:
            if name == "key":
                clauses.append("key = ?")
                args.append(value)
            elif value is None:
                clauses.append("json_extract(value, ?) IS NULL")
                args.append(f"$.{name}")
            else:
                clauses.append("json_extract(value, ?) = ?")
                args += [f"$.{name}", value]
        return (" WHERE " + " AND ".join(clauses) if clauses else ""), tuple(args)


def _on_values(plan: Plan) -> Plan:
    """`plan` with its equality filters and sort field read from a (key, value) pair's value."""
    def read(pair: tuple[str, Any], name: str) -> Any:
        key, value = pair
        if name == "key":
            return key
        return value.get(name) if isinstance(value, dict) else getattr(value, name, None)

    by = plan.by
    if isinstance(by, str):
        field_name = by
        by = lambda pair: read(pair, field_name)  # noqa: E731
    checks = tuple(
        (lambda pair, n=name, v=value: read(pair, n) == v) for name, value in plan.equal
    )
    return Plan(predicates=(*checks, *plan.predicates), by=by, desc=plan.desc, limit=plan.limit)


def open(path: PathLike) -> Db:
    """Open the store at `path`, creating it, and converting a JSON store from before."""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.is_file() and target.stat().st_size and not _is_sqlite(target):
        _convert(target)
    return Db(target)


def _connect(path: Path) -> sqlite3.Connection:
    try:
        sql = sqlite3.connect(path, isolation_level=None, check_same_thread=False)
    except sqlite3.Error as error:
        raise Error(f"{path}: cannot open store: {error}") from error
    try:
        sql.execute(f"PRAGMA busy_timeout = {BUSY_MS}")
        sql.execute("PRAGMA journal_mode = WAL")
        # WAL keeps the file whole; a power cut may lose the last commits, never corrupt them.
        sql.execute("PRAGMA synchronous = NORMAL")
        return sql
    except sqlite3.Error as error:
        sql.close()
        raise Error(f"{path}: cannot open store: {error}") from error


def _encode(value: Any) -> str:
    try:
        return msgspec.json.encode(value).decode()
    except (TypeError, msgspec.EncodeError) as error:
        raise Error(f"cannot store a {type(value).__name__}: {error}") from error


def _is_sqlite(path: Path) -> bool:
    with path.open("rb") as handle:
        return handle.read(len(SQLITE_MAGIC)) == SQLITE_MAGIC


def _convert(path: Path) -> None:
    """Rewrite a JSON store (`{table: {key: value}}`) as SQLite, in place and atomically."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise Error(f"{path}: cannot read store: {error}") from error
    if not isinstance(data, dict):
        raise Error(f"{path}: expected a JSON object")
    staged = path.with_name(f".{path.name}.convert")
    staged.unlink(missing_ok=True)
    db = Db(staged)
    try:
        with db.batch():
            for name, rows in data.items():
                if isinstance(rows, dict):
                    table = db[str(name)]
                    for key, value in rows.items():
                        table[str(key)] = value
    finally:
        db.close()
    for suffix in ("-wal", "-shm"):
        Path(f"{staged}{suffix}").unlink(missing_ok=True)
    os.replace(staged, path)
