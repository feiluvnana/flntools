"""SQLite WAL store: one shared connection, explicit transactions.

SPEC.md#for-developers explains it."""

from __future__ import annotations

import os
import sqlite3
import sys
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from flnkit.errors import DiskError

__all__ = ["Db", "connect"]

# How long a writer waits for another process's transaction before giving up, in ms.
BUSY_MS = 30_000
# How often `_wal` tries again while another process is making the same database, in seconds.
WAL_POLL = 0.01


def _harden(path: Path) -> None:
    """Give a private database file mode 0600 and its folder 0700 (Unix only)."""
    if os.name == "nt":
        return
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        os.chmod(path.parent, 0o700)
    except OSError:
        pass
    if path.exists():
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass


def connect(path: Path) -> sqlite3.Connection:
    """A connection to a database file, in WAL mode, made with its folder.

    WAL keeps the file whole through a crash: a power cut may lose the last commits, never
    corrupt it. `synchronous = NORMAL` syncs at checkpoints only, which a commit per small
    batch needs (2.9 µs a row against 39 µs with FULL). On macOS `fsync` stops at the drive's
    cache, so `fullfsync` makes SQLite's own syncs reach the disk.

    Args:
        path: the database's file.

    Returns:
        The connection, in autocommit mode (transactions are explicit), usable from any thread.

    Raises:
        DiskError: the folder can't be made, or the file isn't a database.
    """
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        sql = sqlite3.connect(path, isolation_level=None, check_same_thread=False)
    except (OSError, sqlite3.Error) as error:
        raise DiskError(f"{path}: cannot open: {error}") from error
    try:
        sql.execute(f"PRAGMA busy_timeout = {BUSY_MS}")
        _wal(sql)
        sql.execute("PRAGMA synchronous = NORMAL")
        if sys.platform == "darwin":
            sql.execute("PRAGMA fullfsync = ON")
            sql.execute("PRAGMA checkpoint_fullfsync = ON")
        return sql
    except sqlite3.Error as error:
        sql.close()
        raise DiskError(f"{path}: cannot open: {error}") from error


def _wal(sql: sqlite3.Connection) -> None:
    """Put a database in WAL mode, waiting while another process makes the same file."""
    deadline = time.monotonic() + BUSY_MS / 1000
    while True:
        try:
            sql.execute("PRAGMA journal_mode = WAL")
            return
        except sqlite3.OperationalError as error:
            if "locked" not in str(error) or time.monotonic() >= deadline:
                raise
            time.sleep(WAL_POLL)


class Db:
    """One SQLite file with its tables, connected on first use and shared by threads.

    Args:
        path: the database's file; made, with its folder, on the first write.
        schema: SQL run on every first connection, to make the tables (`CREATE … IF NOT
            EXISTS`); "" for none.
        setup: called with the connection after `schema`, before anything else uses it; a
            failure closes the connection and is raised. None for nothing.
        secure: when True, chmod the file to 0600 and its folder to 0700 on first open (Unix).

    Example:
        >>> import tempfile
        >>> db = Db(Path(tempfile.mkdtemp()) / "a.db", schema="CREATE TABLE t (k TEXT)")
        >>> db.all("SELECT k FROM t"), db.path.exists()
        ([], False)
        >>> db.write("INSERT INTO t VALUES (?)", ("x",))
        >>> db.one("SELECT k FROM t", ())
        ('x',)
        >>> db.close()
    """

    def __init__(self, path: Path, *, schema: str = "",
                 setup: Callable[[sqlite3.Connection], None] | None = None,
                 secure: bool = False) -> None:
        self.path = path
        self._schema, self._setup = schema, setup
        self._secure = secure
        # Re-entrant: a `transaction` holds it while the caller's block calls `write` or `one`.
        self._lock = threading.RLock()
        self._sql: sqlite3.Connection | None = None
        self._closed = False

    def __enter__(self) -> Db:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def close(self) -> None:
        """Close the connection; closing twice is harmless, and later calls raise."""
        with self._lock:
            self._closed = True
            if self._sql is not None:
                self._sql.close()
                self._sql = None

    def connection(self) -> sqlite3.Connection:
        """The connection, made (with the file, its tables and its `setup`) on first use.

        Returns:
            The one connection of this `Db`.

        Raises:
            RuntimeError: the `Db` is closed: a bug in the caller.
            DiskError: the file can't be opened or made, or `setup` failed in SQLite.
        """
        with self._lock:
            if self._closed:
                raise RuntimeError(f"{self.path}: closed")
            if self._sql is None:
                sql = connect(self.path)
                try:
                    if self._schema:
                        sql.executescript(self._schema)
                    if self._setup is not None:
                        self._setup(sql)
                except sqlite3.Error as error:
                    sql.close()
                    raise DiskError(f"{self.path}: {error}") from error
                except BaseException:
                    sql.close()
                    raise
                if self._secure:
                    _harden(self.path)
                self._sql = sql
            return self._sql

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        """One `BEGIN IMMEDIATE` … `COMMIT`, rolled back on an error; nested blocks join it.

        Yields:
            The connection, inside the transaction.

        Raises:
            DiskError: SQLite failed (a full disk, a lock held past `BUSY_MS`); nothing is kept.
        """
        with self._lock:
            sql = self.connection()
            if sql.in_transaction:
                yield sql
                return
            try:
                sql.execute("BEGIN IMMEDIATE")
                yield sql
                sql.execute("COMMIT")
            except sqlite3.Error as error:
                if sql.in_transaction:
                    sql.execute("ROLLBACK")
                raise DiskError(f"{self.path}: {error}") from error
            except BaseException:
                if sql.in_transaction:
                    sql.execute("ROLLBACK")
                raise

    def write(self, query: str, args: tuple[Any, ...] = ()) -> None:
        """Run one statement that writes, in the caller's transaction or as its own.

        Outside a transaction the statement runs in autocommit mode: SQLite makes it a
        transaction of its own, as whole as one with `BEGIN` and `COMMIT`, in one call.

        Args:
            query: the statement.
            args: its parameters.

        Raises:
            DiskError: SQLite failed; nothing of the statement is kept.
        """
        with self._lock:
            sql = self.connection()
            try:
                sql.execute(query, args)
            except sqlite3.Error as error:
                raise DiskError(f"{self.path}: {error}") from error

    def one(self, query: str, args: tuple[Any, ...]) -> tuple[Any, ...] | None:
        """Run one read and return its first row; None for no row, or a file not made yet.

        Args:
            query: the statement.
            args: its parameters.

        Returns:
            The row's columns, in the statement's order.

        Raises:
            RuntimeError: the `Db` is closed: a bug in the caller.
            DiskError: SQLite failed.
        """
        # The hot path: an open `Db` is already connected, so this is one prepared statement
        # (sqlite3 caches it) and one row.
        with self._lock:
            sql = self._sql
            if sql is None:
                if not self._closed and not self.path.is_file():
                    return None
                sql = self.connection()
            try:
                return sql.execute(query, args).fetchone()
            except sqlite3.Error as error:
                raise DiskError(f"{self.path}: {error}") from error

    def all(self, query: str, args: tuple[Any, ...] = ()) -> list[tuple[Any, ...]]:
        """Run one read and return every row; none for a file not made yet.

        Args:
            query: the statement.
            args: its parameters.

        Returns:
            The rows, each its columns in the statement's order.

        Raises:
            RuntimeError: the `Db` is closed: a bug in the caller.
            DiskError: SQLite failed.
        """
        with self._lock:
            if self._sql is None and not self._closed and not self.path.is_file():
                return []
            try:
                return self.connection().execute(query, args).fetchall()
            except sqlite3.Error as error:
                raise DiskError(f"{self.path}: {error}") from error



