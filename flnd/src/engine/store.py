"""`flnd.db`, schema 3: its tables, the migrations from versions 1 and 2, its rows and queries, and
`check`, which compares the rows with the disk and repairs them. SPEC.md#for-developers
explains it.
"""

from __future__ import annotations

import datetime
import functools
import itertools
import json
import os
import re
import shutil
import sqlite3
import time
from collections.abc import Iterator, Mapping, MutableMapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

import msgspec

from flnd.engine.events import Checked, Record
from flnd.errors import Changed, DiskError, NotFound, UsageError
from flnd.ui.log import LOGS, ui
from flnkit import clock, db, files, text

__all__ = ["FailureRow", "FileRow", "RunRow", "State", "Store", "TargetRow",
           "busy", "check", "erase", "folder_name", "open", "sample", "sweep",
           "target_folder"]

# ---------------------------------------------------------------------------------------------
# The schema
# ---------------------------------------------------------------------------------------------

MEDIA, METADATA = Path("media"), Path("metadata")
DB = METADATA / "flnd.db"
SECRETS = METADATA / "secret.db"
# Where a version-1 or version-2 `flnd.db` is copied, whole, before its migration.
BACKUP = METADATA / "flnd.v1.db"
BACKUP_V2 = METADATA / "flnd.v2.db"
VERSION = "3"
# What a migrated home that still has a `secret.db` is told, once.
LOGINS_NOTICE = "logins are cookie files now: flnd auth login <code> <file>"

SCHEMA = """
CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL) WITHOUT ROWID;  -- version = '3'
CREATE TABLE files (
    key TEXT PRIMARY KEY, target TEXT NOT NULL, plugin TEXT NOT NULL,
    post_id TEXT NOT NULL DEFAULT '', category TEXT NOT NULL DEFAULT '', path TEXT NOT NULL,
    size INTEGER NOT NULL DEFAULT 0, mtime INTEGER, date TEXT,
    estimated INTEGER NOT NULL DEFAULT 0, kind TEXT, files INTEGER, hash BLOB,
    state TEXT NOT NULL DEFAULT 'saved',            -- saved | gone | link
    unpacked TEXT NOT NULL DEFAULT '', folder TEXT, sidecar TEXT, extracted TEXT,
    at TEXT NOT NULL DEFAULT ''
) WITHOUT ROWID;
CREATE INDEX files_post ON files (target, post_id);
CREATE INDEX files_size ON files (size) WHERE state = 'saved';
CREATE INDEX files_hash ON files (hash) WHERE hash IS NOT NULL;
CREATE INDEX files_undated ON files (target) WHERE state = 'saved' AND (date IS NULL OR estimated);
CREATE VIEW posts AS
    SELECT target, post_id, min(category) AS category, max(date) AS date,
           max(count(*), coalesce(max(files), 1)) AS files, max(at) AS at
    FROM files WHERE state = 'saved' GROUP BY target, post_id;
CREATE TABLE targets (
    key TEXT PRIMARY KEY, plugin TEXT NOT NULL, value TEXT NOT NULL, kind TEXT NOT NULL,
    follow INTEGER NOT NULL DEFAULT 0, categories TEXT NOT NULL DEFAULT '[]', query TEXT,
    complete TEXT NOT NULL DEFAULT '{}', cursors TEXT NOT NULL DEFAULT '{}',
    seen TEXT NOT NULL DEFAULT '[]', last_sync TEXT NOT NULL DEFAULT '',
    files INTEGER NOT NULL DEFAULT 0, bytes INTEGER NOT NULL DEFAULT 0,
    heads TEXT NOT NULL DEFAULT '{}', gaps TEXT NOT NULL DEFAULT '{}',
    folder TEXT
) WITHOUT ROWID;
CREATE TABLE target_seen (
    target TEXT NOT NULL, item TEXT NOT NULL, ord INTEGER NOT NULL,
    PRIMARY KEY (target, item)
) WITHOUT ROWID;
CREATE INDEX target_seen_ord ON target_seen (target, ord);
CREATE TABLE runs (
    id INTEGER PRIMARY KEY, command TEXT NOT NULL, started TEXT NOT NULL, ended TEXT,
    saved INTEGER NOT NULL DEFAULT 0, failed INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE failures (
    run INTEGER, key TEXT NOT NULL, target TEXT NOT NULL, post_id TEXT NOT NULL DEFAULT '',
    category TEXT NOT NULL DEFAULT '', file TEXT NOT NULL DEFAULT '',
    url TEXT NOT NULL DEFAULT '', class TEXT NOT NULL, message TEXT NOT NULL,
    hint TEXT NOT NULL DEFAULT '', attempts INTEGER NOT NULL DEFAULT 1, at TEXT NOT NULL,
    cursor TEXT, retryable INTEGER NOT NULL DEFAULT 1
);
CREATE INDEX failures_target ON failures (target, key);
CREATE INDEX failures_run ON failures (run);
CREATE UNIQUE INDEX failures_key ON failures (key);
CREATE TABLE state (plugin TEXT NOT NULL, key TEXT NOT NULL, value TEXT NOT NULL,
                    PRIMARY KEY (plugin, key)) WITHOUT ROWID;
"""
# Executed one by one: `executescript` would commit the migration's transaction halfway.
STATEMENTS = tuple(part for part in SCHEMA.split(";") if "CREATE" in part)

# The tables version 1 had that schema 3 rebuilds, with each new column's old name.
REBUILT = {"meta": {}, "files": {"plugin": "source"}, "targets": {"plugin": "source"},
           "runs": {}, "failures": {}, "state": {"plugin": "source"}}
V1_DROPPED = ("logins", "probes")
# A column an old version-1 table lacks, whose version-1 default isn't schema 3's: a failure
# from before `retryable` was recorded wasn't retryable.
V1_DEFAULTS = {("failures", "retryable"): "0"}
V1_INDEXES = ("files_post", "files_hash", "files_undated", "failures_target", "failures_run",
              "failures_key")

# The loop's connection waits for no lock (a busy commit is queued again); any other waits 30 s.
LOOP_BUSY_MS, OTHER_BUSY_MS = 0, 30_000
# A negative size is in KiB: 4 MiB, so a home of 100,000 files (20 MiB) stays in memory.
CACHE_KIB = 4 * 1024
# Another connection (a read-only command, another process) checkpoints by itself, as today.
OTHER_CHECKPOINT_PAGES = 10_000
# The loop's connection never checkpoints by itself; the run checkpoints after this many rows.
CHECKPOINT_ROWS = 1_000

SAVED, GONE, LINK = "saved", "gone", "link"
RETRIES = 3
ON_DISK = "state IN ('saved', 'link')"
# SQLite takes up to 32,766 values in one statement; 500 rows of 16 is 8,000.
ONE_STATEMENT = 500

FILE_COLUMNS = ("key", "target", "plugin", "post_id", "category", "path", "size", "mtime",
                "date", "estimated", "kind", "state", "at", "files", "hash", "sidecar")
INSERT_FILE = (f"INSERT OR REPLACE INTO files ({', '.join(FILE_COLUMNS)}) "
               f"VALUES ({', '.join('?' * len(FILE_COLUMNS))})")
# The columns `FileRow` reads, in its order.
ROW_SELECT = ("key, target, path, size, mtime, post_id, category, date, estimated, kind, state,"
              " at, files, unpacked, folder, hash, sidecar, extracted")
STEP_COLUMNS = frozenset({"path", "size", "mtime", "unpacked", "folder", "hash", "sidecar",
                          "extracted"})
TARGET_COLUMNS = ("key", "plugin", "value", "kind", "follow", "categories", "query", "complete",
                  "cursors", "seen", "last_sync", "files", "bytes", "heads", "gaps", "folder")
FAILURE_SELECT = ("key, target, post_id, category, file, cursor, attempts, class, message, hint,"
                  " retryable, url, run, at")


# ---------------------------------------------------------------------------------------------
# Rows
# ---------------------------------------------------------------------------------------------

class FileRow(msgspec.Struct):
    """One file's row in `files`: a saved file, a gone one, or a link to a saved one."""

    # `<code>:<key>`: the plugin's code, then its `key` template filled for this file.
    key: str
    # The target it was saved for, `<code>:<value>`.
    target: str
    path: str
    size: int = 0
    # Its modified time in nanoseconds when it was saved; None for a gone file.
    mtime: int | None = None
    post_id: str = ""
    category: str = ""
    # Its post's day, `YYYY-MM-DD`; None when the site gave none.
    date: str | None = None
    # Whether the day was guessed from the posts around it.
    estimated: bool = False
    kind: str | None = None
    state: str = SAVED
    at: str = ""
    # How many files its post has; only the first file of a post sets it.
    files: int | None = None
    # For an archive: "", "extracted" or "failed"; and the folder it was extracted to.
    unpacked: str = ""
    folder: str | None = None
    # The SHA-256 of its bytes; None until a file of the same size made it worth hashing.
    hash: bytes | None = None
    sidecar: str | None = None
    # For an archive extracted into its post's folder and deleted: the files it made, as a
    # JSON list of paths; None otherwise.
    extracted: str | None = None

    @property
    def plugin(self) -> str:
        """The plugin's code: the key's part before the first colon."""
        return self.key.partition(":")[0]

    @property
    def day(self) -> datetime.date | None:
        """Its post's day; None when the row holds none, or one that isn't a day."""
        return _date(self.date)


class TargetRow(msgspec.Struct):
    """One target the home knows, in `targets`."""

    key: str
    plugin: str
    value: str
    kind: str
    follow: bool = False
    # The categories a followed target syncs; empty for every category.
    categories: tuple[str, ...] = ()
    # For a target a search found: the search's text.
    query: str | None = None
    # Each category listed to its end, with the day it was.
    complete: dict[str, str] = {}
    # Each category's cursor, kept when its listing was cut short.
    cursors: dict[str, str] = {}
    # For a search: the targets it found, as keys, in order.
    seen: list[str] = []
    last_sync: str = ""
    files: int = 0
    bytes: int = 0
    # Each listing's newest post a run that read it from the top saw, where a sync stops.
    heads: dict[str, str] = {}
    # Each listing's gaps: where cut-short syncs stopped, newest first; "" is a gap with no
    # cursor, which the next sync reads down to the head instead.
    gaps: dict[str, list[str]] = {}
    # For a followed search: the `--folder` its hits go to, which a plain sync reuses.
    folder: str | None = None


class FailureRow(msgspec.Struct):
    """One file (or whole target) that failed and isn't saved since, in `failures`."""

    # The file's key, or the target's key for a failure of the whole target.
    key: str
    target: str
    # The post's id and category; "" for a failure of the whole target.
    post: str = ""
    category: str = ""
    file: str = ""
    # The cursor of the page the post was listed on; None for the first page.
    cursor: str | None = None
    # The runs that tried it and failed.
    attempts: int = 1
    error: str = "Error"
    message: str = ""
    hint: str = ""
    retryable: bool = True
    # The file's URL, redacted.
    url: str = ""
    run: int | None = None
    at: str = ""


class RunRow(msgspec.Struct):
    """One run that downloaded, in `runs`."""

    id: int
    command: str
    started: str
    # "" while it runs, or when it never ended (a crash).
    ended: str = ""
    saved: int = 0
    failed: int = 0


class _Target(Protocol):
    """What the store reads of a `flnd.plugin.Target`."""

    plugin: str
    value: str
    kind: str


def key(target: _Target) -> str:
    """A target's key in `targets` and `files.target`: `<code>:<value>`."""
    return f"{target.plugin}:{target.value}"


def now() -> str:
    """The time now, ISO 8601 in UTC to the second, as every `at` column holds it."""
    return datetime.datetime.now(datetime.UTC).isoformat(timespec="seconds")


def busy(error: BaseException) -> bool:
    """Whether a `DiskError` is another connection holding the write lock: queue it again."""
    cause = error.__cause__
    if not isinstance(cause, sqlite3.OperationalError):
        return False
    # The low byte is the primary code: BUSY_SNAPSHOT (517) and BUSY_RECOVERY (261) are BUSY.
    code = getattr(cause, "sqlite_errorcode", 0) & 0xFF
    return code in (sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED)


# ---------------------------------------------------------------------------------------------
# Opening, and the migrations from versions 1 and 2
# ---------------------------------------------------------------------------------------------

def open(home: Path, *, loop: bool = False) -> Store:
    """Open a home's `flnd.db`, migrating a version-1 or version-2 file in place.

    A home without `flnd.db` is left untouched until the first write.

    Args:
        home: the home folder.
        loop: the run's shared connection, committing on the loop: it never waits for a lock
            and never checkpoints by itself. False for any other (30 s for a lock).

    Raises:
        UsageError: the file is of a later schema, or of none with files in it.
        DiskError: `flnd.db` can't be read or written.
    """
    store = Store(home, loop=loop)
    try:
        if (home / DB).is_file():
            store._db.connection()
    except BaseException:
        store.close()
        raise
    return store


def _setup(sql: sqlite3.Connection, *, home: Path, loop: bool) -> None:
    """Set the pragmas, then make schema 3 or migrate an older one, in one transaction."""
    sql.execute(f"PRAGMA busy_timeout = {OTHER_BUSY_MS}")
    sql.execute(f"PRAGMA cache_size = -{CACHE_KIB}")
    sql.execute(f"PRAGMA wal_autocheckpoint = {0 if loop else OTHER_CHECKPOINT_PAGES}")
    old = _version(sql)
    if old in ("1", "2"):
        _backup(sql, home / (BACKUP if old == "1" else BACKUP_V2), old)
    notice = False
    sql.execute("BEGIN IMMEDIATE")
    try:
        version = _version(sql)
        if version is None:
            for statement in STATEMENTS:
                sql.execute(statement)
            sql.execute("INSERT INTO meta VALUES ('version', ?)", (VERSION,))
            sql.execute("INSERT INTO meta VALUES ('keys', '2')")
        elif version == "1":
            _migrate(sql)
        elif version == "2":
            _drop_accounts(sql)
        elif version != VERSION:
            raise UsageError(f"home schema {version!r} needs a newer flnd",
                             hint="upgrade flnd, or open the home with the flnd that wrote it")
        if version in ("1", "2") and (home / SECRETS).is_file():
            # An old home's sealed logins are left alone, and its person is told once.
            sql.execute("INSERT OR REPLACE INTO meta VALUES ('logins', 'told')")
            notice = True
        if version and not sql.execute("SELECT 1 FROM meta WHERE key = 'keys'").fetchone():
            _rekey(sql)
            sql.execute("INSERT INTO meta VALUES ('keys', '2')")
        if version and "folder" not in _columns(sql, "targets"):
            # A home from before a followed search kept its folder gets the column once.
            sql.execute("ALTER TABLE targets ADD COLUMN folder TEXT")
        if version and "extracted" not in _columns(sql, "files"):
            # A home from before an archive could be extracted into its post's folder.
            sql.execute("ALTER TABLE files ADD COLUMN extracted TEXT")
        if version and "target_seen" not in _tables(sql):
            for statement in STATEMENTS:
                if "target_seen" in statement:
                    sql.execute(statement)
            _backfill_seen(sql)
        sql.execute("COMMIT")
    except BaseException:
        if sql.in_transaction:
            sql.execute("ROLLBACK")
        raise
    if notice:
        ui.warn(LOGINS_NOTICE)
    if loop:
        sql.execute(f"PRAGMA busy_timeout = {LOOP_BUSY_MS}")


def _backup(sql: sqlite3.Connection, path: Path, version: str) -> None:
    """Copy an old file to `path` with SQLite's online backup, unless a copy is there."""
    if path.is_file():
        return
    part = path.with_name(f"{path.name}.part")
    try:
        copy = sqlite3.connect(part)
        try:
            sql.backup(copy)
        finally:
            copy.close()
        os.replace(part, path)
    except (OSError, sqlite3.Error) as error:
        part.unlink(missing_ok=True)
        raise DiskError(f"{path}: can't keep a copy of version {version} before migrating: {error}",
                        hint="free some disk space, then run flnd again") from error


def _version(sql: sqlite3.Connection) -> str | None:
    """The file's schema version; None for an empty file, "1" for a version-1 one without it."""
    tables = _tables(sql)
    if not tables:
        return None
    if "meta" in tables:
        found = sql.execute("SELECT value FROM meta WHERE key = 'version'").fetchone()
        if found is not None:
            return found[0]
    if "files" in tables and sql.execute("SELECT 1 FROM files LIMIT 1").fetchone() is not None:
        raise UsageError("home has no schema version",
                         hint="upgrade flnd, or open the home with the flnd that wrote it")
    return "1"


def _tables(sql: sqlite3.Connection) -> set[str]:
    return {name for (name,) in sql.execute("SELECT name FROM sqlite_master WHERE type='table'")}


def _drop_accounts(sql: sqlite3.Connection) -> None:
    """Migrate a version-2 file to 3: no `accounts` table, no `targets.account` column, in the
    caller's transaction."""
    sql.execute("DROP TABLE IF EXISTS accounts")
    if "account" in _columns(sql, "targets"):
        for _, index, *_, origin, _ in sql.execute("PRAGMA index_list(targets)").fetchall():
            if origin == "c" and "account" in [
                    row[2] for row in sql.execute(f"PRAGMA index_info({index})")]:
                sql.execute(f"DROP INDEX {index}")
        sql.execute("ALTER TABLE targets DROP COLUMN account")
    sql.execute("INSERT OR REPLACE INTO meta VALUES ('version', ?)", (VERSION,))


def _migrate(sql: sqlite3.Connection) -> None:
    """Rebuild a version-1 file's tables as schema 3, in the caller's transaction."""
    tables = _tables(sql)
    sql.execute("DROP VIEW IF EXISTS posts")
    for index in V1_INDEXES:
        sql.execute(f"DROP INDEX IF EXISTS {index}")
    for table in REBUILT:
        if table in tables:
            sql.execute(f"ALTER TABLE {table} RENAME TO v1_{table}")
    for statement in STATEMENTS:
        sql.execute(statement)
    for table, renames in REBUILT.items():
        if table in tables and table != "targets":
            _copy(sql, table, renames)
    if "targets" in tables:
        _copy_targets(sql)
        _backfill_seen(sql)
    for table in [*(f"v1_{name}" for name in REBUILT if name in tables), *V1_DROPPED]:
        sql.execute(f"DROP TABLE IF EXISTS {table}")
    sql.execute("INSERT OR REPLACE INTO meta VALUES ('version', ?)", (VERSION,))


# A built-in single post's (plugin, kind) as an old home stored it, and its kind now: its key
# was `<code>:<id>`, and is `<code>:<kind>/<id>`, so a post never clashes with a user's key.
REKEYED = {("fbk", "video"): "video", ("fbk", "watch"): "watch", ("fbk", "photo"): "photo",
           ("fbk", "post"): "post", ("ins", "post"): "post", ("twt", "post"): "post",
           ("tik", "video"): "post", ("tik", "post"): "post", ("loi", "post"): "post",
           ("mht", "chapter"): "chapter"}


def _rekey(sql: sqlite3.Connection) -> None:
    """Rewrite the rows of single posts keyed `<code>:<id>` to `<code>:<kind>/<id>`: the target,
    its files' and failures' `target`, and the keys a search lists. A home with none is left
    alone. It runs once per home: `meta` keeps a `keys` row afterwards."""
    kinds = sorted({kind for _, kind in REKEYED})
    rows = sql.execute(f"SELECT key, plugin, value, kind FROM targets WHERE kind IN"
                       f" ({', '.join('?' * len(kinds))})", kinds).fetchall()
    moved: dict[str, str] = {}
    for old, plugin, value, kind in rows:
        new_kind = REKEYED.get((plugin, kind))
        if new_kind is None or value.startswith(f"{new_kind}/"):
            continue
        new = f"{plugin}:{new_kind}/{value}"
        moved[old] = new
        if sql.execute("SELECT 1 FROM targets WHERE key = ?", (new,)).fetchone() is not None:
            sql.execute("DELETE FROM targets WHERE key = ?", (old,))
        else:
            sql.execute("UPDATE targets SET key = ?, value = ?, kind = ? WHERE key = ?",
                        (new, f"{new_kind}/{value}", new_kind, old))
        sql.execute("UPDATE files SET target = ? WHERE target = ?", (new, old))
        sql.execute("UPDATE failures SET target = ? WHERE target = ?", (new, old))
        sql.execute("UPDATE OR REPLACE failures SET key = ? WHERE key = ?", (new, old))
    if not moved:
        return
    for item, seen in sql.execute("SELECT key, seen FROM targets WHERE seen != '[]'").fetchall():
        listed = json.loads(seen)
        if any(found in moved for found in listed):
            sql.execute("UPDATE targets SET seen = ? WHERE key = ?",
                        (_json([moved.get(found, found) for found in listed]), item))
    if "target_seen" in _tables(sql):
        for old, new in moved.items():
            sql.execute("DELETE FROM target_seen WHERE target = ? AND item IN "
                        "(SELECT item FROM target_seen WHERE target = ?)", (old, new))
            sql.execute("UPDATE target_seen SET target = ? WHERE target = ?", (new, old))
            sql.execute("DELETE FROM target_seen WHERE item = ? AND target IN "
                        "(SELECT target FROM target_seen WHERE item = ?)", (old, new))
            sql.execute("UPDATE target_seen SET item = ? WHERE item = ?", (new, old))


def _backfill_seen(sql: sqlite3.Connection) -> None:
    """Populate `target_seen` from legacy `targets.seen` JSON lists, preserving order."""
    for tkey, raw in sql.execute("SELECT key, seen FROM targets WHERE seen != '[]'").fetchall():
        try:
            items = json.loads(raw)
        except (TypeError, ValueError):
            continue
        if not isinstance(items, list):
            continue
        unique = list(dict.fromkeys(x for x in items if isinstance(x, str)))
        sql.executemany(
            "INSERT OR IGNORE INTO target_seen (target, item, ord) VALUES (?, ?, ?)",
            [(tkey, item, i) for i, item in enumerate(unique)],
        )


def _columns(sql: sqlite3.Connection, table: str) -> list[str]:
    return [row[1] for row in sql.execute(f"PRAGMA table_info({table})")]


def _copy(sql: sqlite3.Connection, table: str, renames: Mapping[str, str]) -> None:
    """Fill a new table from `v1_<table>`, by column; a column the old one lacks takes its
    version-1 default (`V1_DEFAULTS`, else schema 3's), and one the new one lacks is dropped."""
    old = set(_columns(sql, f"v1_{table}"))
    pairs = [(new, renames.get(new, new)) for new in _columns(sql, table)]
    pairs = [(new, was if was in old else V1_DEFAULTS[table, new]) for new, was in pairs
             if was in old or (table, new) in V1_DEFAULTS]
    into, picked = ", ".join(new for new, _ in pairs), ", ".join(was for _, was in pairs)
    # A failure is one row per key: an old table may hold several; the latest wins.
    latest = (" WHERE rowid IN (SELECT max(rowid) FROM v1_failures GROUP BY key) ORDER BY rowid"
              if table == "failures" else "")
    sql.execute(f"INSERT INTO {table} ({into}) SELECT {picked} FROM v1_{table}{latest}")


def _copy_targets(sql: sqlite3.Connection) -> None:
    """Fill `targets` from `v1_targets`; its `options` JSON becomes the `categories` list."""
    old = set(_columns(sql, "v1_targets"))
    kept = [name for name in TARGET_COLUMNS if name in old and name != "plugin"]
    found = sql.execute(f"SELECT source, options, {', '.join(kept)} FROM v1_targets").fetchall()
    sql.executemany(
        f"INSERT INTO targets (plugin, categories, {', '.join(kept)})"
        f" VALUES (?, ?, {', '.join('?' * len(kept))})",
        [(source, _json(_categories(options)), *rest) for source, options, *rest in found])


def _categories(options: str) -> list[str]:
    """A version-1 target's categories: its options' `only` list, else `categories`, else []."""
    try:
        settings = json.loads(options)
    except (TypeError, ValueError):
        return []
    if not isinstance(settings, dict):
        return []
    names = settings.get("only") or settings.get("categories") or []
    return [name for name in names if isinstance(name, str)] if isinstance(names, list) else []


# ---------------------------------------------------------------------------------------------
# The store
# ---------------------------------------------------------------------------------------------

class Store:
    """One home's `flnd.db`, behind one connection that every thread shares.

    Args:
        home: the home folder; `flnd.db` is made in it on the first write.
        loop: the run's shared connection (see `open`).
    """

    def __init__(self, home: Path, *, loop: bool = False) -> None:
        self.home = home
        self.path = home / DB
        self._loop = loop
        self._db = db.Db(self.path, setup=functools.partial(_setup, home=home, loop=loop))
        # Rows committed since the last checkpoint; read and reset from any thread.
        self._rows = 0
        self._checkpointer: sqlite3.Connection | None = None

    def __enter__(self) -> Store:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def close(self) -> None:
        """Checkpoint the loop's connection, then close; closing twice is harmless."""
        try:
            if self._loop and self._db._sql is not None:
                self.checkpoint(force=True)
        finally:
            if self._checkpointer is not None:
                self._checkpointer.close()
                self._checkpointer = None
            self._db.close()

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        """One transaction: the store's writes inside the block join it."""
        with self._db.transaction() as sql:
            yield sql

    def ready(self) -> None:
        """Make `flnd.db` (or migrate it) now, off the loop, so no first write on it waits.

        Raises:
            DiskError: the file can't be made or opened.
        """
        self._db.connection()

    @property
    def due(self) -> bool:
        """Whether the loop's connection has committed enough rows to checkpoint."""
        return self._rows >= CHECKPOINT_ROWS

    def checkpoint(self, *, force: bool = False) -> None:
        """Copy the log into the database without waiting for any lock: a `CALL` job's work.

        It runs on a connection of its own, so the loop's commits never wait for it.

        Args:
            force: checkpoint even when it isn't `due`, as closing does.
        """
        if not (force or self.due):
            return
        self._rows = 0
        if not self.path.is_file():
            return
        try:
            if self._checkpointer is None:
                # The same pragmas as the store's: on macOS, its syncs reach the disk.
                self._checkpointer = db.connect(self.path)
            try:
                self._checkpointer.execute("PRAGMA wal_checkpoint(PASSIVE)")
                self._checkpointer.execute("PRAGMA shrink_memory")
            finally:
                self._checkpointer.close()
                self._checkpointer = None
            self._db.connection().execute("PRAGMA shrink_memory")
        except sqlite3.Error as error:
            raise DiskError(f"{self.path}: {error}") from error

    # -- files ------------------------------------------------------------------------------

    def known(self, item: str) -> tuple[str, str, str | None] | None:
        """`(path, state, folder)` of a file's row, the read of every listed file; None for none."""
        return self._db.one("SELECT path, state, folder FROM files WHERE key = ?",
                            (item,))  # type: ignore[return-value]

    def day(self, item: str) -> datetime.date | None:
        """The post's day a file's row holds; None for no row or no day."""
        found = self._db.one("SELECT date FROM files WHERE key = ?", (item,))
        return _date(found[0]) if found else None

    def files_of_post(self, item: str) -> int | None:
        """How many files the post of a saved file has; None when its first row holds none."""
        found = self._db.one("SELECT files FROM files WHERE key = ?", (item,))
        return found[0] if found else None

    def post_saved(self, target: str, post: str, category: str) -> bool:
        """Whether a post of a target has a saved file in a category, for a lazy post's keys."""
        return self._db.one("SELECT 1 FROM files WHERE target = ? AND post_id = ? AND"
                            " category = ? AND state = 'saved' LIMIT 1",
                            (target, post, category)) is not None

    def save(self, rows: Sequence[FileRow]) -> None:
        """Write one post's file rows in one transaction, once every file of it is in place.

        Raises:
            DiskError: the database can't be written; `busy(error)` when another connection
                holds the lock, and nothing was written.
        """
        if not rows:
            return
        if len(rows) <= ONE_STATEMENT:
            # One statement is its own transaction: no BEGIN and COMMIT to send.
            self._db.write(_insert_files(len(rows)), _values(rows))
        else:
            with self._db.transaction() as sql:
                for start in range(0, len(rows), ONE_STATEMENT):
                    part = rows[start:start + ONE_STATEMENT]
                    sql.execute(_insert_files(len(part)), _values(part))
        self._rows += len(rows)

    def commit(self, rows: Sequence[FileRow], *, hashes: Mapping[str, bytes] | None = None,
               failures: Sequence[Mapping[str, Any]] = (), run: int | None = None) -> None:
        """Write a post's rows, the hashes dedupe found and its files' failures, in one
        transaction: the save's commit, on the loop.

        Args:
            rows: the post's files that landed.
            hashes: other saved files' SHA-256, by key, that dedupe read on the way.
            failures: each failed file's `fail` arguments, without `run`.
            run: the run's id, for the failures.

        Raises:
            DiskError: the database can't be written; `busy(error)` when another connection
                holds the lock, and nothing was written.
        """
        at = now()
        with self._db.transaction() as sql:
            for start in range(0, len(rows), ONE_STATEMENT):
                part = rows[start:start + ONE_STATEMENT]
                sql.execute(_insert_files(len(part)), _values(part))
            sql.executemany("UPDATE files SET hash = ? WHERE key = ?",
                            [(digest, item) for item, digest in (hashes or {}).items()])
            for failure in failures:
                _fail(sql, run=run, at=at, **failure)
        self._rows += len(rows)

    def undated(self, target: str) -> list[FileRow]:
        """A target's saved files with no day or a guessed one, through a partial index."""
        found = self._db.all(f"SELECT {ROW_SELECT} FROM files INDEXED BY files_undated"
                             " WHERE target = ? AND state = 'saved'"
                             " AND (date IS NULL OR estimated)", (target,))
        return [_file_row(row) for row in found]

    def redate(self, item: str, path: Path, day: datetime.date) -> None:
        """Record the day a file was dated with, and where its rename put it."""
        size, mtime = stamp(path)
        self._db.write("UPDATE files SET path = ?, date = ?, estimated = 0, size = ?, mtime = ?"
                       " WHERE key = ?", (str(path), day.isoformat(), size, mtime, item))

    def records(self, *, target: str | None = None, kind: str | None = None) -> list[FileRow]:
        """The saved files, oldest saved first.

        Args:
            target: only this target's files (its key); None for all.
            kind: only "image", "video" or "audio" files; None for all.
        """
        where, args = ["state = 'saved'"], []
        if target is not None:
            where.append("target = ?")
            args.append(target)
        if kind is not None:
            where.append("kind = ?")
            args.append(kind)
        found = self._db.all(f"SELECT {ROW_SELECT} FROM files WHERE {' AND '.join(where)}"
                             " ORDER BY at, key", tuple(args))
        return [_file_row(row) for row in found]

    def row(self, item: str) -> FileRow | None:
        """A file's whole row, in whatever state; None when the home has none."""
        found = self._db.one(f"SELECT {ROW_SELECT} FROM files WHERE key = ?", (item,))
        return _file_row(found) if found else None

    def twins(self, *, besides: str, digest: bytes | None = None, size: int | None = None,
              limit: int = -1) -> list[FileRow]:
        """The files that may hold the same bytes as `besides`, at most `limit` (-1 for all).

        Args:
            digest: the files on disk (saved or links) with this SHA-256.
            size: the saved files of this size, through `files_size`: what dedupe hashes
                against.

        Raises:
            ValueError: neither `digest` nor `size` is given, or both are: a bug in the caller.
        """
        if (digest is None) == (size is None):
            raise ValueError("twins needs one of digest and size")
        if digest is not None:
            query, value = f"hash = ? AND {ON_DISK}", digest
        else:
            query, value = "size = ? AND state = 'saved'", size
        found = self._db.all(f"SELECT {ROW_SELECT} FROM files WHERE {query} AND key != ?"
                             " LIMIT ?", (value, besides, limit))
        return [_file_row(row) for row in found]

    def link(self, holder: str, path: Path, category: str) -> None:
        """Record a second name of a saved file in another category's folder, keyed
        `<holder>@<category>`; nothing when the holder has no row."""
        self._db.write(
            "INSERT OR REPLACE INTO files (key, target, plugin, post_id, category, path, size,"
            " mtime, date, estimated, kind, state, at, hash) SELECT ?, target, plugin, post_id,"
            " ?, ?, size, mtime, date, estimated, kind, ?, ?, hash FROM files WHERE key = ?",
            (f"{holder}@{category}", category, str(path), LINK, now(), holder))

    def on_disk(self) -> list[FileRow]:
        """Every row that names a file on disk: saved files and their links."""
        return [_file_row(row) for row in self._db.all(
            f"SELECT {ROW_SELECT} FROM files WHERE {ON_DISK}")]

    def drop(self, items: Sequence[str]) -> None:
        """Remove files' rows, in one transaction."""
        with self._db.transaction() as sql:
            sql.executemany("DELETE FROM files WHERE key = ?", [(item,) for item in items])

    def change(self, changes: Sequence[tuple[str, Mapping[str, Any]]]) -> None:
        """Set columns of several files' rows in one transaction.

        Args:
            changes: each file's key with the columns to set, of `STEP_COLUMNS`.

        Raises:
            ValueError: a column isn't one of them: a bug in the caller.
        """
        for _, fields in changes:
            unknown = set(fields) - STEP_COLUMNS
            if unknown:
                raise ValueError(f"no step writes {', '.join(sorted(unknown))}")
        with self._db.transaction() as sql:
            for item, fields in changes:
                if fields:
                    sets = ", ".join(f"{name} = ?" for name in fields)
                    sql.execute(f"UPDATE files SET {sets} WHERE key = ?",
                                (*fields.values(), item))

    def update(self, item: str, **fields: Any) -> None:
        """Set columns of one file's row (`STEP_COLUMNS`): unpack, sidecar, a hash."""
        self.change([(item, fields)])

    # -- failures and runs ------------------------------------------------------------------

    def fail(self, *, run: int | None, item: str, target: str, error: BaseException,
             post: str = "", category: str = "", file: str = "", url: str = "",
             cursor: str | None = None) -> None:
        """Write one failure, counting one more run, and record a `NotFound` file as gone.

        A file saved since its last failure starts again from 1. `url` is kept redacted.
        """
        with self._db.transaction() as sql:
            _fail(sql, run=run, item=item, target=target, error=error, post=post,
                  category=category, file=file, url=url, cursor=cursor, at=now())

    def clear(self, item: str) -> None:
        """Drop a failure's row: a target that failed whole has now run to its end."""
        self._db.write("DELETE FROM failures WHERE key = ?", (item,))

    def pending(self, target: str, *, every: bool = False) -> list[FailureRow]:
        """A target's failed files to retry, not saved or gone since, in the order they failed.

        Args:
            every: every one, as a retry asked for does; False only those failed on at most
                `RETRIES` runs. A `NotFound` is never taken.
        """
        limit = "" if every else f" AND f.attempts <= {RETRIES}"
        found = self._db.all(f"SELECT {FAILURE_SELECT} FROM failures f WHERE f.target = ?"
                             " AND f.post_id != '' AND f.class != 'NotFound'" + limit +
                             " AND NOT EXISTS (SELECT 1 FROM files WHERE files.key = f.key)"
                             " ORDER BY f.rowid", (target,))
        return [_failure_row(row) for row in found]

    def failed(self) -> dict[str, bool]:
        """Each target with a failure to retry, with whether the whole target failed."""
        found = self._db.all("SELECT target, max(post_id = '') FROM failures f"
                             " WHERE f.class != 'NotFound' AND NOT EXISTS"
                             " (SELECT 1 FROM files WHERE files.key = f.key)"
                             " GROUP BY target ORDER BY min(f.rowid)")
        return {target: bool(whole) for target, whole in found}

    def target_failures(self) -> dict[str, tuple[bool, int, int]]:
        """Each target's pending failures: `(whole_target, failed_files, exhausted_files)`."""
        found = self._db.all(
            "SELECT target, max(post_id = ''), sum(post_id != ''),"
            f" sum(post_id != '' AND attempts > {RETRIES}) FROM failures f"
            " WHERE f.class != 'NotFound' AND NOT EXISTS"
            " (SELECT 1 FROM files WHERE files.key = f.key)"
            " GROUP BY target"
        )
        return {
            target: (bool(whole), int(files or 0), int(exhausted or 0))
            for target, whole, files, exhausted in found
        }

    def failures(self, run: int) -> list[FailureRow]:
        """The failures a run left that still stand, in the order they first failed."""
        found = self._db.all(f"SELECT {FAILURE_SELECT} FROM failures f WHERE f.run = ?"
                             " AND NOT EXISTS (SELECT 1 FROM files WHERE files.key = f.key"
                             " AND files.state = 'saved') ORDER BY f.rowid", (run,))
        return [_failure_row(row) for row in found]

    def start(self, command: str) -> int:
        """Record a run that downloads, and return its id."""
        with self._db.transaction() as sql:
            made = sql.execute("INSERT INTO runs (command, started) VALUES (?, ?)",
                               (command, now()))
            return int(made.lastrowid or 0)

    def end(self, run: int, *, saved: int) -> None:
        """Record that a run ended, with the files it saved; its failures are counted."""
        self._db.write("UPDATE runs SET ended = ?, saved = ?, failed ="
                       " (SELECT count(*) FROM failures WHERE run = ?) WHERE id = ?",
                       (now(), saved, run, run))

    def last_run(self) -> RunRow | None:
        """The latest run that downloaded; None when the home has none."""
        found = self._db.one("SELECT id, command, started, coalesce(ended, ''), saved, failed"
                             " FROM runs ORDER BY id DESC LIMIT 1", ())
        return RunRow(*found) if found else None

    # -- targets ----------------------------------------------------------------------------

    def targets(self) -> dict[str, TargetRow]:
        """Every target the home knows, by key."""
        found = self._db.all(f"SELECT {', '.join(TARGET_COLUMNS)} FROM targets ORDER BY key")
        rows_by_key = {row[0]: _target_row(row) for row in found}
        seen_map: dict[str, list[str]] = {}
        for tkey, item in self._db.all("SELECT target, item FROM target_seen ORDER BY target, ord"):
            seen_map.setdefault(tkey, []).append(item)
        for tkey, items in seen_map.items():
            if tkey in rows_by_key:
                rows_by_key[tkey].seen = items
        return rows_by_key

    def target(self, item: str) -> TargetRow | None:
        """One target's row, by key; None when the home doesn't know it."""
        found = self._db.one(f"SELECT {', '.join(TARGET_COLUMNS)} FROM targets WHERE key = ?",
                             (item,))
        if not found:
            return None
        row = _target_row(found)
        seen_rows = self._db.all("SELECT item FROM target_seen WHERE target = ? ORDER BY ord",
                                 (item,))
        if seen_rows:
            row.seen = [r[0] for r in seen_rows]
        return row

    def seen_keys(self, item: str) -> set[str]:
        """The set of seen child keys for `item`."""
        return {r[0] for r in self._db.all("SELECT item FROM target_seen WHERE target = ?",
                                           (item,))}

    def record(self, target: _Target, *, follow: bool | None = None,
               categories: Sequence[str] | None = None, query: str | None = None,
               folder: str | None = None) -> None:
        """Remember a target of a run, keeping what earlier runs learned about it.

        Args:
            follow: True follows it; None keeps its state (a new one follows); False leaves
                a new one unfollowed.
            categories: the categories to keep for later syncs (`all` keeps every one); None
                keeps the stored ones.
            query: the search that found it; None keeps the stored one.
            folder: a followed search's `--folder`; None keeps the stored one.
        """
        with self._db.transaction():
            old = self.target(key(target))
            row = old or TargetRow(key(target), target.plugin, target.value, target.kind)
            row.kind = target.kind
            if follow is True or old is None:
                row.follow = follow is not False
            if categories is not None:
                row.categories = () if "all" in categories else tuple(categories)
            if query:
                row.query = query
            if folder is not None:
                row.folder = folder
            row.last_sync = now()
            self._put(row)

    def follow(self, target: _Target, on: bool) -> None:
        """Follow a target, or stop following it; a target the home doesn't know is added."""
        with self._db.transaction():
            row = self.target(key(target)) or TargetRow(
                key(target), target.plugin, target.value, target.kind, last_sync=now())
            row.follow = on
            self._put(row)

    def forget(self, item: str) -> list[FileRow]:
        """Delete a target's row, its files rows and its failures, in one transaction.

        Returns:
            The rows of its files on disk (saved files and links), for `erase`.
        """
        with self._db.transaction() as sql:
            gone = [_file_row(row) for row in sql.execute(
                f"SELECT {ROW_SELECT} FROM files WHERE target = ? AND {ON_DISK}", (item,))]
            for table in ("files", "failures", "target_seen", "targets"):
                sql.execute(f"DELETE FROM {table} WHERE "
                            f"{'key' if table == 'targets' else 'target'} = ?", (item,))
        return gone

    def retry(self, target: str) -> int:
        """Make a target's failed files, past their automatic retries, retryable again.

        Returns:
            How many files it made retryable.
        """
        with self._db.transaction() as sql:
            return sql.execute("UPDATE failures SET attempts = 1 WHERE target = ? AND attempts > ?"
                               " AND class != 'NotFound'", (target, RETRIES)).rowcount

    def seen(self, target: _Target, found: Sequence[str]) -> None:
        """Add the targets a search found to `target_seen`, in order, each once."""
        if not found:
            return
        unique = list(dict.fromkeys(found))
        tkey = key(target)
        with self._db.transaction() as sql:
            sql.execute(
                "INSERT OR IGNORE INTO targets (key, plugin, value, kind, follow, last_sync) "
                "VALUES (?, ?, ?, ?, 1, ?)",
                (tkey, target.plugin, target.value, target.kind, now()),
            )
            start = sql.execute(
                "SELECT coalesce(max(ord), -1) + 1 FROM target_seen WHERE target = ?",
                (tkey,),
            ).fetchone()[0]
            sql.executemany(
                "INSERT OR IGNORE INTO target_seen (target, item, ord) VALUES (?, ?, ?)",
                [(tkey, item, start + i) for i, item in enumerate(unique)],
            )

    def progress(self, target: _Target, *, cursors: Mapping[str, str | None],
                 complete: Sequence[str], heads: Mapping[str, str] | None = None,
                 gaps: Mapping[str, Sequence[str]] | None = None) -> None:
        """Keep where a target's listings stopped, after its run.

        Args:
            cursors: each category's cursor to resume from; None drops it.
            complete: the categories listed to their end today.
            heads: each listing's newest post, for listings read from the top; None keeps.
            gaps: each listing's gaps, newest first; an empty list drops them, and a listing
                left out keeps its own.
        """
        if not cursors and not complete and not heads and not gaps:
            return
        today = datetime.datetime.fromtimestamp(clock.wall(), datetime.UTC).date().isoformat()
        with self._db.transaction():
            row = self.target(key(target)) or TargetRow(
                key(target), target.plugin, target.value, target.kind, follow=True)
            merged = {**row.cursors, **cursors}
            row.cursors = {name: value for name, value in merged.items() if value}
            row.complete = {**row.complete, **dict.fromkeys(complete, today)}
            row.heads = {**row.heads, **(heads or {})}
            kept = {**row.gaps, **{name: list(found) for name, found in (gaps or {}).items()}}
            row.gaps = {name: found for name, found in kept.items() if found}
            self._put(row)

    def tally(self, target: _Target) -> None:
        """Count a target's saved files and bytes into its row."""
        self._db.write("UPDATE targets SET (files, bytes) = (SELECT count(*), coalesce(sum(size),"
                       " 0) FROM files WHERE target = ?1 AND state = 'saved') WHERE key = ?1",
                       (key(target),))

    def count(self, target: str) -> tuple[int, int]:
        """A target's saved files and their bytes, counted now."""
        found = self._db.one("SELECT count(*), coalesce(sum(size), 0) FROM files"
                             " WHERE target = ? AND state = 'saved'", (target,))
        return (found[0], found[1]) if found else (0, 0)

    def _put(self, row: TargetRow) -> None:
        """Write a target's whole row; the caller's transaction joins."""
        self._db.write(f"INSERT OR REPLACE INTO targets ({', '.join(TARGET_COLUMNS)})"
                       f" VALUES ({', '.join('?' * len(TARGET_COLUMNS))})",
                       (row.key, row.plugin, row.value, row.kind, int(row.follow),
                        _json(list(row.categories)), row.query, _json(row.complete),
                        _json(row.cursors), _json(row.seen), row.last_sync, row.files,
                        row.bytes, _json(row.heads), _json(row.gaps), row.folder))

    # -- plugins' state ---------------------------------------------------------------------

    def state(self, plugin: str) -> State:
        """A plugin's own values between runs, as a mapping."""
        return State(self, plugin)


class State(MutableMapping[str, Any]):
    """One plugin's values between runs: text to JSON values, one statement per read or write.

    Args:
        store: the home's store.
        plugin: the plugin's code; another plugin's values aren't visible.
    """

    def __init__(self, store: Store, plugin: str) -> None:
        self._db, self._plugin = store._db, plugin

    def __getitem__(self, name: str) -> Any:
        found = self._db.one("SELECT value FROM state WHERE plugin = ? AND key = ?",
                             (self._plugin, name))
        if found is None:
            raise KeyError(name)
        return msgspec.json.decode(found[0])

    def __setitem__(self, name: str, value: Any) -> None:
        self._db.write("INSERT OR REPLACE INTO state VALUES (?, ?, ?)",
                       (self._plugin, name, _json(value)))

    def __delitem__(self, name: str) -> None:
        if name not in self:
            raise KeyError(name)
        self._db.write("DELETE FROM state WHERE plugin = ? AND key = ?", (self._plugin, name))

    def __iter__(self) -> Iterator[str]:
        found = self._db.all("SELECT key FROM state WHERE plugin = ? ORDER BY key",
                             (self._plugin,))
        return iter([name for (name,) in found])

    def __len__(self) -> int:
        found = self._db.one("SELECT count(*) FROM state WHERE plugin = ?", (self._plugin,))
        return found[0] if found else 0


def _fail(sql: sqlite3.Connection, *, run: int | None, item: str, target: str,
          error: BaseException, post: str = "", category: str = "", file: str = "",
          url: str = "", cursor: str | None = None, at: str) -> None:
    """One failure's row, in the caller's transaction (`Store.fail`, `Store.commit`)."""
    sql.execute(
        "INSERT INTO failures (run, key, target, post_id, category, file, url, class,"
        " message, hint, retryable, attempts, at, cursor)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?)"
        " ON CONFLICT (key) DO UPDATE SET run = excluded.run, target = excluded.target,"
        " post_id = excluded.post_id, category = excluded.category,"
        " file = excluded.file, url = excluded.url, class = excluded.class,"
        " message = excluded.message, hint = excluded.hint,"
        " retryable = excluded.retryable, at = excluded.at,"
        " cursor = excluded.cursor, attempts = CASE WHEN EXISTS (SELECT 1 FROM files"
        " WHERE files.key = excluded.key AND files.state = 'saved') THEN 1"
        " ELSE failures.attempts + 1 END",
        (run, item, target, post, category, file, text.redact(url) if url else "",
         type(error).__name__, str(error), getattr(error, "hint", "") or "",
         int(getattr(error, "retryable", False)), at, cursor))
    if isinstance(error, NotFound) and file:
        sql.execute(INSERT_FILE, _file_values(FileRow(
            item, target, file, post_id=post, category=category, state=GONE, at=at)))


def stamp(path: Path) -> tuple[int, int]:
    """A file's `(size, mtime_ns)`, from one `stat`.

    Raises:
        DiskError: the file can't be stat'ed.
    """
    try:
        found = path.stat()
    except OSError as error:
        raise DiskError(f"{path}: {error.strerror or error}") from error
    return found.st_size, found.st_mtime_ns


@functools.lru_cache(maxsize=64)
def _insert_files(count: int) -> str:
    """`INSERT_FILE` with `count` rows of values, made once for each count."""
    row = f"({', '.join('?' * len(FILE_COLUMNS))})"
    return f"{INSERT_FILE.partition(' VALUES ')[0]} VALUES {', '.join([row] * count)}"


def _values(rows: Sequence[FileRow]) -> tuple[Any, ...]:
    """Every row's values, one after another, for one `INSERT` of several rows."""
    return tuple(itertools.chain.from_iterable(map(_file_values, rows)))


def _file_values(row: FileRow) -> tuple[Any, ...]:
    return (row.key, row.target, row.plugin, row.post_id, row.category, row.path, row.size,
            row.mtime, row.date, int(row.estimated), row.kind, row.state, row.at, row.files,
            row.hash, row.sidecar)


def _file_row(found: tuple[Any, ...]) -> FileRow:
    (item, target, path, size, mtime, post, category, date, estimated, kind, state, at, count,
     unpacked, folder, digest, sidecar, extracted) = found
    return FileRow(item, target, path, size, mtime, post, category, date, bool(estimated), kind,
                   state, at, count, unpacked, folder, digest, sidecar, extracted)


def _target_row(found: tuple[Any, ...]) -> TargetRow:
    (item, plugin, value, kind, follow, categories, query, complete, cursors, seen, last_sync,
     count, size, heads, gaps, folder) = found
    return TargetRow(item, plugin, value, kind, bool(follow), tuple(json.loads(categories)),
                     query, json.loads(complete), json.loads(cursors), json.loads(seen),
                     last_sync, count, size, json.loads(heads), json.loads(gaps), folder)


def _failure_row(found: tuple[Any, ...]) -> FailureRow:
    (item, target, post, category, file, cursor, attempts, error, message, hint, retryable, url,
     run, at) = found
    return FailureRow(item, target, post, category, file, cursor, attempts, error, message, hint,
                      bool(retryable), url, run, at)


def _json(value: Any) -> str:
    return msgspec.json.encode(value).decode()


def _date(value: Any) -> datetime.date | None:
    try:
        return datetime.date.fromisoformat(value) if isinstance(value, str) else None
    except ValueError:
        return None


# ---------------------------------------------------------------------------------------------
# Checking the rows against the disk
# ---------------------------------------------------------------------------------------------

# A file renamed into place this recently may be waiting for its row from another run.
SETTLING = 60.0


@dataclass
class _Seen:
    """What the rows say is on disk, for the walk of `media/`."""

    paths: set[str] = field(default_factory=set)
    inodes: set[tuple[int, int]] = field(default_factory=set)
    folders: set[str] = field(default_factory=set)


def check(home: Path, store: Store, *, fix: bool = False) -> Checked:
    """Compare the rows with the disk; with `fix`, drop missing rows and take sizes from disk.

    An unknown file is never adopted, so a home with one is not ok, fixed or not.

    Raises:
        DiskError: `media/` is missing when `fix` is set, or the database can't be written.
    """
    rows = store.on_disk()
    seen = _Seen()
    missing: list[FileRow] = []
    resized: list[tuple[FileRow, os.stat_result]] = []
    for row in rows:
        _look(row, seen, missing, resized)
    unknown = _unknown(home / MEDIA, seen)
    if fix:
        if not (home / MEDIA).is_dir():
            raise DiskError(f"{home / MEDIA}: not found",
                            hint="create the media folder or pick another home")
        _fix(store, missing, resized)
    return Checked(
        files=len(rows),
        missing=tuple(_record(row) for row in missing),
        unknown=tuple(unknown),
        resized=tuple(_record(row) for row, _ in resized),
        # The history matches the disk only once no unknown file is left.
        fixed=fix and bool(missing or resized) and not unknown,
    )


def _look(row: FileRow, seen: _Seen, missing: list[FileRow],
          resized: list[tuple[FileRow, os.stat_result]]) -> None:
    """Stat one row's file, noting it in `seen`, and in `missing` or `resized` when wrong."""
    if row.folder:
        seen.folders.add(_norm(row.folder))
    if row.sidecar:
        seen.paths.add(_norm(row.sidecar))
    made = _made(row)
    seen.paths.update(_norm(name) for name in made)
    path = Path(row.path)
    try:
        found = path.stat()
    except FileNotFoundError:
        extracted = row.unpacked == "extracted" and (
            (row.folder and Path(row.folder).is_dir()) or any(map(os.path.lexists, made)))
        if not extracted:
            missing.append(row)
        return
    except OSError:
        return
    seen.paths.add(_norm(path))
    seen.inodes.add((found.st_dev, found.st_ino))
    if row.state == SAVED and found.st_size != row.size:
        resized.append((row, found))


def _made(row: FileRow) -> list[str]:
    """The files an archive made in its post's folder (`FileRow.extracted`)."""
    return json.loads(row.extracted) if row.extracted else []


def _unknown(media: Path, seen: _Seen) -> list[Path]:
    """Files under `media/` no row names, in walk order."""
    if not media.is_dir():
        return []
    started = time.time()
    found = []
    for entry in files.walk(media):
        if entry.name.startswith(".") or entry.is_symlink():
            continue
        name = _norm(entry.path)
        if name in seen.paths or _inside(name, seen.folders):
            continue
        if entry.name.endswith(".json") and name[: -len(".json")] in seen.paths:
            continue
        stat = entry.stat(follow_symlinks=False)
        if stat.st_ino == 0 and os.name == "nt":
            stat = os.stat(entry.path)
        if (stat.st_dev, stat.st_ino) in seen.inodes:
            continue
        if started - stat.st_ctime < SETTLING:
            continue
        found.append(Path(entry.path))
    return found


def _inside(name: str, folders: set[str]) -> bool:
    """Whether a normalised path is inside one of `folders`, at any depth."""
    if not folders:
        return False
    parent = os.path.dirname(name)
    while True:
        if parent in folders:
            return True
        up = os.path.dirname(parent)
        if up == parent:
            return False
        parent = up


def _fix(store: Store, missing: list[FileRow],
         resized: list[tuple[FileRow, os.stat_result]]) -> None:
    """Drop missing rows, and take resized files' size, time and hash from the disk."""
    if missing:
        store.drop([row.key for row in missing])
    if resized:
        store.change([(row.key, {"size": found.st_size, "mtime": found.st_mtime_ns,
                                 "hash": files.digest(row.path)}) for row, found in resized])


def _record(row: FileRow) -> Record:
    return Record(row.key, Path(row.path), row.size, post=row.post_id, category=row.category,
                  date=row.day, at=row.at)



def _norm(path: str | os.PathLike[str]) -> str:
    """A path as one spelling: absolute, and case-folded where the platform folds names."""
    return os.path.normcase(os.path.abspath(path))


# ---------------------------------------------------------------------------------------------
# The rest of the home: a target's folder, the logs and their samples
# ---------------------------------------------------------------------------------------------

# Logs and samples older than this are swept at the start of each run that downloads.
LOG_DAYS = 30
# A response whose URL looks like this is a login's, and is never kept as a sample: its body may
# hold the session it just made.
LOGIN_URL = re.compile(r"login|logon|signin|sign_in|oauth|auth|session|token|password|account",
                       re.IGNORECASE)


# The kinds of a single post, written `<kind>/<id>`: a folder is named by the id alone.
POST_KINDS = ("post", "video", "photo", "watch")


def folder_name(target: _Target) -> str:
    """What names a target's folder and fills `{target}`: its value, without a single post's
    `post/`, so `ins:post/C1abc` is filed in `media/ins/C1abc`."""
    if target.kind in POST_KINDS:
        return target.value.removeprefix(f"{target.kind}/")
    return target.value


def target_folder(home: Path, target: _Target) -> Path:
    """A target's folder: `<home>/media/<code>/<value>`, with the value made a safe name.

    Args:
        home: the home folder.
        target: the target, resolved.

    Returns:
        The folder; nothing is made on disk.

    Example:
        >>> from flnd.plugin import Target
        >>> target_folder(Path("home"), Target("ins:example_user", kind="user")).as_posix()
        'home/media/ins/example_user'
    """
    return home / MEDIA / target.plugin / text.filename(folder_name(target))


def erase(home: Path, rows: Sequence[FileRow]) -> int:
    """Delete the files the rows name: each file, its sidecar and extracted folder, and the
    folders under `media/` this leaves empty.

    Returns:
        The bytes freed by the files deleted.
    """
    media = (home / MEDIA).resolve()
    freed, parents = 0, set()
    for row in rows:
        for name in (row.path, row.sidecar):
            path = Path(name) if name else None
            if path is not None and path.is_file() and path.resolve().is_relative_to(media):
                if name == row.path and row.state == SAVED:
                    freed += path.stat().st_size
                path.unlink()
                parents.add(path.parent)
        for name in _made(row):
            path = Path(name)
            if path.is_file() and path.resolve().is_relative_to(media):
                path.unlink()
                parents.add(path.parent)
        folder = Path(row.folder) if row.folder else None
        if folder is not None and folder.is_dir() and folder.resolve().is_relative_to(media):
            shutil.rmtree(folder, ignore_errors=True)
            parents.add(folder.parent)
    for parent in sorted(parents, key=lambda path: len(path.parts), reverse=True):
        for folder in (parent, *parent.parents):
            if folder.resolve() == media or not folder.resolve().is_relative_to(media):
                break
            try:
                folder.rmdir()
            except OSError:
                break
    return freed


def sample(home: Path, run: int | None, error: BaseException) -> Path | None:
    """Keep the answer a `Changed` error was raised on, scrubbed, in `logs/<run>/samples/`.

    A parse failure is a site that changed or a source's bug, and the answer is what shows
    which. It is kept only when it is safe to:
    - values under keys that look like secrets (`token`, `csrf`, `session`, `cookie`, `auth`,
      `password`, `key`, …) become "…" (`flnkit.text.scrub`);
    - a login's answer is never kept: one whose URL looks like a login (`LOGIN_URL`), or that
      sets a cookie, may hold the session it made;
    - the file is named by a count, never by anything the site sent.

    Args:
        home: the home folder.
        run: the run's id; None (outside a recorded run) keeps nothing.
        error: what failed; anything but a `Changed` with a response keeps nothing.

    Returns:
        The sample's path; None when nothing was kept.
    """
    response = getattr(error, "response", None)
    if run is None or not isinstance(error, Changed) or response is None:
        return None
    url = str(getattr(response, "url", "") or "")
    headers = getattr(response, "headers", None) or {}
    if LOGIN_URL.search(text.redact(url)) or "set-cookie" in {str(k).lower() for k in headers}:
        return None
    content = getattr(response, "content", b"")
    body = content.decode("utf-8", "replace") if isinstance(content, bytes) else str(content)
    folder = home / LOGS / str(run) / "samples"
    try:
        folder.mkdir(parents=True, exist_ok=True)
        count = len(list(folder.iterdir()))
        while True:
            count += 1
            path = folder / f"{count}.txt"
            try:
                # An exclusive create, so two samples kept at once never take the same name.
                with path.open("x", encoding="utf-8") as out:
                    out.write(f"{text.redact(url)}\n\n{text.scrub(body)}")
                break
            except FileExistsError:
                continue
    except OSError as failed:
        # A sample is a help for a bug report, never worth failing the run over.
        ui.debug("sample not kept: {error}", error=failed)
        return None
    return path


def sweep(home: Path, *, days: int = LOG_DAYS) -> int:
    """Remove the logs older than `days`: each run's folder (its samples) and crash files.

    Called at the start of each run that downloads, so a home that runs every day keeps a
    month of them. An entry that can't be removed is left for the next sweep.

    Args:
        home: the home folder.
        days: how old, by its modified time, an entry may get.

    Returns:
        How many entries were removed.

    Example:
        >>> import tempfile
        >>> sweep(Path(tempfile.mkdtemp()))
        0
    """
    logs = home / LOGS
    if not logs.is_dir():
        return 0
    oldest = time.time() - days * 86_400
    removed = 0
    for entry in logs.iterdir():
        try:
            if entry.lstat().st_mtime >= oldest:
                continue
            if entry.is_dir() and not entry.is_symlink():
                shutil.rmtree(entry)
            else:
                entry.unlink()
            removed += 1
        except OSError as error:
            ui.debug("{name}: not swept: {error}", name=entry.name, error=error)
    return removed
