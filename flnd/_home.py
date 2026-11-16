"""The home: its layout, `flnd.db` (what the home remembers) and the logins in its vault.

The home folder holds `flnd.db`, `secrets.db` and `secret.key`, `media/`, `cache/` and `logs/`.
This module is flnd's own storage, built on two library modules: `flnd._db` (a SQLite store) and
`flnd._vault` (sealed secrets).

What `flnd.db` holds (the storage guide in `docs/` shows every column):
- `files`: one row per saved file, keyed `<code>:<key>` (the source's `key` template), with its
  target, post, path, size, day, its bytes' SHA-256 (`hash`), and `state`: `saved`, `gone` (the
  site answered 404 or 410), `failed`, or `link` (a second name for a saved file, in another
  category's folder, keyed `<file's key>@<category>`). The steps after a file lands fill in
  `shrink`, `tools`, `before`, `unpacked`, `folder` and `sidecar` (`flnd._steps`).
- `posts`: a view of `files`, one row per post of a target with its category, day and how
  many files it has. The first file of a post of several holds the count (`files.files`),
  which tells whether a post listed again is whole: a table of its own would cost every commit
  another statement and another page.
- `targets`: every target the home knows: followed or not, its options, which categories were
  listed to their end, the cursors of cut-short listings, a search's found targets, and its
  file count and bytes for `flnd ls`.
- `runs` and `failures`: each run that downloaded, and each failure in it.
- `state`: each source's own values between runs (`site.state`).
- `logins`: how each source logged in, and when; never a secret (those are in `secrets.db`).
- `probes`: what ffmpeg found in a file, kept so it isn't asked twice; safe to empty.
- `meta`: the schema's version and when an old home was migrated.

The logins: a cookie source logs in by `from_browser` or `from_file`; `store` checks the jar
proves a login and `save` seals it in the source's slot of the vault (`secrets`); a run calls
`load` for the jar, and `write_back` saves the cookies the site refreshed.

Who calls it: the engine, the job and the library for files, targets and logins; `SourceRun`
for a source's state and secrets.

How a home opens (`open`):
1. A home with a `flnd.db` connects to it, and makes any table it lacks.
2. A home with an old `metadata/` folder (flnd before `flnd.db`) is migrated first: its
   `history.db`, `targets.db`, `sources.db` and `secrets.db` are read, never written, and
   `flnd.db` and `secrets.db` are written in one `BEGIN IMMEDIATE` transaction (`_migrate`).
3. A new home connects on its first write, so reading one (`fl.targets()`, a dry run) leaves
   the folder as it was.

Rules a change must keep:
- One transaction per post: a post's file rows commit together, after every file of it is
  in place (`save`). A commit per file would cost 14 µs each, and a writer thread would add a
  window in which saved rows are invisible. A step that moves several files at once (compress,
  which keeps the links dedupe made) moves their rows in one transaction too (`change`).
- A row is written only after its file: a crash in between leaves a whole file without a row,
  which the next run finds on disk and skips.
- One connection per `Home`, shared by the lister and the download threads behind one lock.
  SQLite serializes writers across processes; a writer waits for another run (`flnd._db`).
- The old files of a migrated home are never changed: they are opened `immutable` (or
  read-only when an old run left a write-ahead log), so the previous flnd still runs there.
- A secret's value never reaches a message, a log line or `flnd.db`.
"""

from __future__ import annotations

import datetime
import functools
import json
import os
import sqlite3
import time
from collections.abc import Iterable, Iterator, Mapping, MutableMapping, Sequence
from http.cookiejar import Cookie
from pathlib import Path
from typing import TYPE_CHECKING, Any

import msgspec

from flnd import _cookies, _db, _text, _vault
from flnd._ui import ui
from flnd.errors import DiskError, Error, LoginError

if TYPE_CHECKING:
    from flnd.options import Options
    from flnd.source import Source, Target

__all__ = ["FileRow", "Home", "TargetRow", "open", "secrets"]

# ---------------------------------------------------------------------------------------------
# The home's layout
# ---------------------------------------------------------------------------------------------

# Under the home: the database, the vault and its key, the downloads, things safe to delete,
# and each run's log.
DB, SECRETS, KEY = Path("flnd.db"), Path("secrets.db"), Path("secret.key")
MEDIA, CACHE, LOGS = Path("media"), Path("cache"), Path("logs")
# An old home kept its stores in `metadata/`: tables of JSON values, keyed by text.
OLD = Path("metadata")
OLD_HISTORY, OLD_TARGETS, OLD_STATE = OLD / "history.db", OLD / "targets.db", OLD / "sources.db"
OLD_SECRETS, OLD_KEY = OLD / "secrets.db", OLD / "secret.key"
# Each old store's table, by its SQL name: a table `x` was stored as `t_x`.
OLD_TABLES = {OLD_HISTORY: "t_downloads", OLD_TARGETS: "t_targets", OLD_SECRETS: "t_secrets"}
# SQLite's header; an old store without it is a JSON store (`{table: {key: value}}`).
SQLITE_MAGIC = b"SQLite format 3\x00"

# How many pages the log grows to before SQLite checkpoints it into the database (4 KiB each).
CHECKPOINT_PAGES = 10_000
# The schema's version, in `meta`; a later change of a table bumps it and migrates.
VERSION = "1"

# A file's `state`: where it stands with the site; a `link` row is another name of a saved file.
SAVED, GONE, FAILED, LINK = "saved", "gone", "failed", "link"
# Headers that log a request in. A failure's `request` keeps what a retry needs, never these:
# `flnd.db` isn't encrypted like the vault.
SECRET_HEADERS = frozenset({"authorization", "cookie", "proxy-authorization"})

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS files (
    key TEXT PRIMARY KEY, target TEXT NOT NULL, source TEXT NOT NULL,
    post_id TEXT NOT NULL DEFAULT '', category TEXT NOT NULL DEFAULT '', path TEXT NOT NULL,
    size INTEGER NOT NULL DEFAULT 0, mtime INTEGER, date TEXT,
    estimated INTEGER NOT NULL DEFAULT 0, kind TEXT, files INTEGER, hash BLOB,
    state TEXT NOT NULL DEFAULT 'saved', shrink TEXT NOT NULL DEFAULT '',
    unpacked TEXT NOT NULL DEFAULT '', folder TEXT, before INTEGER, format TEXT, tools TEXT,
    sidecar TEXT,
    at TEXT NOT NULL DEFAULT ''
) WITHOUT ROWID;
CREATE INDEX IF NOT EXISTS files_post ON files (target, post_id);
CREATE INDEX IF NOT EXISTS files_hash ON files (hash) WHERE hash IS NOT NULL;
CREATE INDEX IF NOT EXISTS files_undated ON files (target)
    WHERE state = 'saved' AND (date IS NULL OR estimated);
CREATE VIEW IF NOT EXISTS posts AS
    SELECT target, post_id, min(category) AS category, max(date) AS date,
           max(count(*), coalesce(max(files), 1)) AS files, max(at) AS at
    FROM files WHERE state = 'saved' GROUP BY target, post_id;
CREATE TABLE IF NOT EXISTS targets (
    key TEXT PRIMARY KEY, source TEXT NOT NULL, value TEXT NOT NULL, kind TEXT NOT NULL,
    follow INTEGER NOT NULL DEFAULT 0, options TEXT NOT NULL DEFAULT '{}', query TEXT,
    complete TEXT NOT NULL DEFAULT '{}', cursors TEXT NOT NULL DEFAULT '{}',
    seen TEXT NOT NULL DEFAULT '[]', last_sync TEXT NOT NULL DEFAULT '',
    files INTEGER NOT NULL DEFAULT 0, bytes INTEGER NOT NULL DEFAULT 0
) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS runs (
    id INTEGER PRIMARY KEY, command TEXT NOT NULL, started TEXT NOT NULL, ended TEXT,
    saved INTEGER NOT NULL DEFAULT 0, failed INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS failures (
    run INTEGER, key TEXT NOT NULL, target TEXT NOT NULL, post_id TEXT NOT NULL DEFAULT '',
    file TEXT NOT NULL DEFAULT '', url TEXT NOT NULL DEFAULT '', class TEXT NOT NULL,
    message TEXT NOT NULL, hint TEXT NOT NULL DEFAULT '', attempts INTEGER NOT NULL DEFAULT 1,
    at TEXT NOT NULL, request TEXT
);
CREATE INDEX IF NOT EXISTS failures_target ON failures (target, key);
CREATE INDEX IF NOT EXISTS failures_run ON failures (run);
CREATE TABLE IF NOT EXISTS state (
    source TEXT NOT NULL, key TEXT NOT NULL, value TEXT NOT NULL, PRIMARY KEY (source, key)
) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS logins (
    source TEXT PRIMARY KEY, origin TEXT NOT NULL DEFAULT '', at TEXT NOT NULL DEFAULT ''
) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS probes (
    path TEXT PRIMARY KEY, size INTEGER, mtime INTEGER, data TEXT NOT NULL
) WITHOUT ROWID;
"""

# The columns `save` writes, in order.
FILE_COLUMNS = ("key", "target", "source", "post_id", "category", "path", "size", "mtime",
                "date", "estimated", "kind", "state", "at", "files", "hash")
INSERT_FILE = (f"INSERT OR REPLACE INTO files ({', '.join(FILE_COLUMNS)}) "
               f"VALUES ({', '.join('?' * len(FILE_COLUMNS))})")
# The columns `FileRow` reads, in its order.
ROW_SELECT = ("key, target, path, size, mtime, post_id, category, date, estimated, kind, state,"
              " at, files, shrink, tools, before, unpacked, folder, hash, sidecar")
# The columns the steps after landing write (`Home.update`, `Home.change`).
STEP_COLUMNS = frozenset({"path", "size", "mtime", "shrink", "tools", "before", "unpacked",
                          "folder", "hash", "sidecar"})
# The rows that name a file on disk: saved files and their links.
ON_DISK = "state IN ('saved', 'link')"
# The vault name an archive's password is kept under, before its file's key.
PASSWORD = "password:"
# The most files `save` writes in one statement; SQLite takes up to 32,766 values in one.
ONE_STATEMENT = 500
TARGET_COLUMNS = ("key", "source", "value", "kind", "follow", "options", "query", "complete",
                  "cursors", "seen", "last_sync", "files", "bytes")


# ---------------------------------------------------------------------------------------------
# Rows
# ---------------------------------------------------------------------------------------------

class FileRow(msgspec.Struct):
    """One saved file's row, as `save` writes it and `records` reads it.

    Example:
        >>> row = FileRow("ins:9001_0", "ins:example_user", "media/ins/example_user/9001.jpg")
        >>> row.source, row.state
        ('ins', 'saved')
    """

    # `<code>:<key>`: the source's code, then its `key` template filled for this file.
    key: str
    # The target it was saved for, `<code>:<value>`.
    target: str
    # Where the file is, as it was saved (relative to the folder flnd ran in, or absolute).
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
    # "image", "video" or "audio" by its name; None for anything else.
    kind: str | None = None
    state: str = SAVED
    # When it was saved, ISO 8601 in UTC.
    at: str = ""
    # How many files its post has; only the first file of a post sets it.
    files: int | None = None
    # What `--compress` did: "", "compressed", "kept" (nothing smaller passed) or "failed".
    shrink: str = ""
    # The tools and settings a "kept" or "failed" result was reached with.
    tools: str | None = None
    # Its size before it was compressed.
    before: int | None = None
    # For an archive: "", "extracted" or "failed"; and the folder it was extracted to.
    unpacked: str = ""
    folder: str | None = None
    # The SHA-256 of its bytes (32 bytes), from the download; None for a row from before.
    hash: bytes | None = None
    # Its metadata sidecar, `<path>.json`, when sidecars are on; None for none.
    sidecar: str | None = None

    @property
    def source(self) -> str:
        """The source's code: the key's part before the first colon."""
        return self.key.partition(":")[0]


class TargetRow(msgspec.Struct):
    """One target the home knows, as `targets` reads it.

    Example:
        >>> row = TargetRow("ins:example_user", "ins", "example_user", "user", follow=True)
        >>> row.only is None, row.follow
        (True, True)
    """

    # `<code>:<value>`, as `key(target)` makes it.
    key: str
    source: str
    value: str
    # The route that reads it: `user`, `post`, `query`, …
    kind: str
    # Whether `sync` takes it.
    follow: bool = False
    # The categories a followed target syncs; None for every category.
    only: tuple[str, ...] | None = None
    # For a target a search found: the search's text.
    query: str | None = None
    # Each category listed to its end, with the day it was.
    complete: dict[str, str] = {}
    # Each category's cursor, kept when its listing was cut short.
    cursors: dict[str, str] = {}
    # For a search: the targets it found, as keys, in order.
    seen: list[str] = []
    # When it was last downloaded or synced, ISO 8601 in UTC.
    last_sync: str = ""
    # Its saved files and their bytes, as of its last run.
    files: int = 0
    bytes: int = 0
    # Whether `sync` compresses its new files (`follow(compress=True)`).
    compress: bool = False


def key(target: Target) -> str:
    """A target's key in `targets` and `files.target`: `<code>:<value>`.

    Args:
        target: the target, resolved.

    Returns:
        Its key; a search's is its words (`ins:cats`), as the old `targets.db` keyed it.

    Example:
        >>> from flnd.source import Target
        >>> key(Target("ins", "user", "example_user"))
        'ins:example_user'
    """
    return f"{target.source}:{target.value}"


def target_folder(home: Path, target: Target) -> Path:
    """A target's folder: `<home>/media/<code>/<value>`, with the value made a safe name.

    Args:
        home: the home folder.
        target: the target, resolved.

    Returns:
        The folder; nothing is made on disk.

    Example:
        >>> from flnd.source import Target
        >>> target_folder(Path("home"), Target("ins", "user", "example_user")).as_posix()
        'home/media/ins/example_user'
    """
    return home / MEDIA / target.source / _text.filename(target.value)


def now() -> str:
    """The time now, ISO 8601 in UTC to the second, as every `at` column holds it."""
    return datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")


# ---------------------------------------------------------------------------------------------
# The database
# ---------------------------------------------------------------------------------------------

def open(home: Path) -> Home:
    """Open a home's `flnd.db`, migrating an old home first.

    A home with neither `flnd.db` nor an old `metadata/` is left untouched: the `Db` connects
    on its first write, and reads it as empty until then.

    Args:
        home: the home folder.

    Returns:
        The `Db`, to use in a `with` block.

    Raises:
        DiskError: `flnd.db` or an old store can't be read or written.

    Example:
        >>> import tempfile
        >>> with open(Path(tempfile.mkdtemp())) as db:
        ...     db.targets()
        {}
    """
    db = Home(home)
    try:
        if (home / DB).is_file() or _old(home):
            db._store.connection()
    except BaseException:
        db.close()
        raise
    return db


class Home:
    """One home's `flnd.db`: its files, posts, targets, runs, failures and sources' state.

    Every method is safe from any thread: they share one connection behind a lock. Writes that
    belong together run in one transaction.

    Args:
        home: the home folder; `flnd.db` is made in it on the first write.
    """

    def __init__(self, home: Path) -> None:
        self.home = home
        self.path = home / DB
        # The store: one connection, its tables made, and an old home migrated, on first use.
        self._store = _db.Db(self.path, schema=SCHEMA, setup=self._setup)

    def __enter__(self) -> Home:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def close(self) -> None:
        """Close the connection; closing twice is harmless."""
        self._store.close()

    def _setup(self, sql: sqlite3.Connection) -> None:
        """Set the connection up, mark the schema's version, and migrate an old home.

        Args:
            sql: the new connection, its tables made, before anything else uses it.
        """
        # A checkpoint copies the log into the database and syncs it; on macOS that sync is a
        # full one, which costs milliseconds, so they come every `CHECKPOINT_PAGES` pages (a
        # 40 MB log) rather than SQLite's 1,000. A commit per post writes about 6 pages.
        sql.execute(f"PRAGMA wal_autocheckpoint = {CHECKPOINT_PAGES}")
        sql.execute("INSERT OR IGNORE INTO meta VALUES ('version', ?)", (VERSION,))
        if _old(self.home):
            _migrate(sql, self.home)

    # -- files ------------------------------------------------------------------------------

    def held(self, item: str) -> tuple[str, str, str | None] | None:
        """Where a file's row says it is, and its state; the hot read of every listed file.

        Args:
            item: the file's key, `<code>:<key>`.

        Returns:
            `(path, state, folder)`, `folder` being where an archive was extracted to; None
            when the home has no row for it.
        """
        found = self._store.one("SELECT path, state, folder FROM files WHERE key = ?", (item,))
        return found  # type: ignore[return-value]

    def day(self, item: str) -> datetime.date | None:
        """The post's day a saved file's row holds; None for no row or no day."""
        found = self._store.one("SELECT date FROM files WHERE key = ?", (item,))
        return _date(found[0]) if found else None

    def files_of_post(self, item: str) -> int | None:
        """How many files the post of a saved file has, so a post listed again is known whole.

        Args:
            item: the key of one of the post's files.

        Returns:
            The count the post's first file holds; None when it holds none (a post of one
            file) or the home has no row for it.
        """
        found = self._store.one("SELECT files FROM files WHERE key = ?", (item,))
        return found[0] if found else None

    def save(self, rows: Sequence[FileRow]) -> None:
        """Write one post's file rows in one transaction.

        Call it once every file of the post is in place: a row is never written before its file.
        A file saved again replaces its row (a gone file found again, a redownload).

        Args:
            rows: the post's files; empty does nothing.

        Raises:
            DiskError: the database can't be written.
        """
        if not rows:
            return
        if len(rows) <= ONE_STATEMENT:
            # One statement with a row of values per file is its own transaction: no BEGIN and
            # COMMIT to send. Each call into SQLite lets go of the GIL and waits to take it
            # back, which costs most while 8 download threads want it, so one call per post
            # beats three.
            values = tuple(value for row in rows for value in _file_values(row))
            self._store.write(_insert_files(len(rows)), values)
            return
        with self._store.transaction() as sql:
            sql.executemany(INSERT_FILE, [_file_values(row) for row in rows])

    def undated(self, target: str) -> list[FileRow]:
        """A target's saved files with no day, or a guessed one, for the source's `date` hook.

        Args:
            target: the target's key.

        Returns:
            The rows, read through a partial index, so a target with none costs nothing.
        """
        found = self._store.all(f"SELECT {ROW_SELECT} FROM files INDEXED BY files_undated"
                                " WHERE target = ? AND state = 'saved'"
                                " AND (date IS NULL OR estimated)", (target,))
        return [_file_row(row) for row in found]

    def redate(self, item: str, path: Path, day: datetime.date) -> None:
        """Record the day a saved file was dated with, and where it is now.

        Args:
            item: the file's key.
            path: where the file is after the rename the day may have caused.
            day: the post's day; it is no longer a guess.

        Raises:
            DiskError: the file can't be stat'ed, or the database written.
        """
        size, mtime = stamp(path)
        self._store.write("UPDATE files SET path = ?, date = ?, estimated = 0, size = ?, mtime = ?"
                          " WHERE key = ?", (str(path), day.isoformat(), size, mtime, item))

    def records(self, *, target: str | None = None, kind: str | None = None) -> list[FileRow]:
        """The saved files, oldest saved first, for `fl.files()`.

        Args:
            target: only this target's files (its key); None for all.
            kind: only "image", "video" or "audio" files; None for all.

        Returns:
            The rows of files in the `saved` state.
        """
        where, args = ["state = 'saved'"], []
        if target is not None:
            where.append("target = ?")
            args.append(target)
        if kind is not None:
            where.append("kind = ?")
            args.append(kind)
        found = self._store.all(f"SELECT {ROW_SELECT} FROM files WHERE {' AND '.join(where)}"
                                " ORDER BY at, key", tuple(args))
        return [_file_row(row) for row in found]

    def unfinished(self, targets: Sequence[str]) -> list[FileRow]:
        """The targets' saved files the steps after landing may still have work on.

        A compressed file is done for good, so it is left out in SQL; whether a kept or failed
        one is done depends on today's tools, which the steps judge.

        Args:
            targets: the targets' keys.

        Returns:
            The rows, with their `shrink`, `tools`, `before` and `unpacked` results.
        """
        if not targets:
            return []
        marks = ", ".join("?" * len(targets))
        found = self._store.all(f"SELECT {ROW_SELECT} FROM files WHERE target IN ({marks})"
                                " AND state = 'saved' AND shrink != 'compressed'", tuple(targets))
        return [_file_row(row) for row in found]

    def shrunk(self) -> list[FileRow]:
        """Every file `--compress` compressed or kept, for what compressing has saved so far."""
        found = self._store.all(f"SELECT {ROW_SELECT} FROM files"
                                " WHERE shrink IN ('compressed', 'kept')")
        return [_file_row(row) for row in found]

    def row(self, item: str) -> FileRow | None:
        """A file's whole row, by its key; None when the home has none.

        Args:
            item: the file's key.

        Returns:
            The row, in whatever state it is.
        """
        found = self._store.one(f"SELECT {ROW_SELECT} FROM files WHERE key = ?", (item,))
        return _file_row(found) if found else None

    def twins(self, digest: bytes, *, besides: str, limit: int = -1) -> list[FileRow]:
        """The files on disk with the same bytes as another: what dedupe links to, and what
        compress must keep linked.

        Args:
            digest: the bytes' SHA-256.
            besides: the key of the file asking, which is left out.
            limit: at most this many rows; -1 for all. Dedupe needs one holder, and a file
                saved thousands of times must not cost it thousands of rows per landing.

        Returns:
            The saved and link rows with that hash, through the `hash` index.
        """
        found = self._store.all(f"SELECT {ROW_SELECT} FROM files WHERE hash = ? AND key != ?"
                                f" AND {ON_DISK} LIMIT ?", (digest, besides, limit))
        return [_file_row(row) for row in found]

    def link(self, holder: str, path: Path, category: str) -> None:
        """Record a second name of a saved file, made in another category's folder.

        The row copies the holder's, under the key `<holder>@<category>` and the state `link`,
        so compress finds it by its hash and keeps it linked, and `check` knows the file. It
        counts toward no post and no target: those read `saved` rows only.

        Args:
            holder: the saved file's key; with no row for it, nothing is recorded.
            path: where the link is.
            category: the category whose folder holds the link.

        Raises:
            DiskError: the database can't be written.
        """
        self._store.write(
            "INSERT OR REPLACE INTO files (key, target, source, post_id, category, path, size,"
            " mtime, date, estimated, kind, state, at, hash) SELECT ?, target, source, post_id,"
            " ?, ?, size, mtime, date, estimated, kind, ?, ?, hash FROM files WHERE key = ?",
            (f"{holder}@{category}", category, str(path), LINK, now(), holder))

    def on_disk(self) -> list[FileRow]:
        """Every row that names a file on disk (saved files and their links), for `check`."""
        found = self._store.all(f"SELECT {ROW_SELECT} FROM files WHERE {ON_DISK}")
        return [_file_row(row) for row in found]

    def failed_at(self) -> dict[str, tuple[str, str, str]]:
        """The key, target and post of each failed file, by the path it was to have.

        `check` adopts a file found on disk with no row through this: a file a run renamed into
        place and then crashed before its row (its failure, if any, names it).

        Returns:
            `{path: (key, target, post_id)}`; the latest failure wins.
        """
        found = self._store.all("SELECT file, key, target, post_id FROM failures"
                                " WHERE file != '' ORDER BY at")
        return {path: (item, target, post) for path, item, target, post in found}

    def drop(self, items: Sequence[str]) -> None:
        """Remove files' rows, in one transaction: `check --fix` for files gone from the disk.

        Args:
            items: the keys.

        Raises:
            DiskError: the database can't be written.
        """
        with self._store.transaction() as sql:
            sql.executemany("DELETE FROM files WHERE key = ?", [(item,) for item in items])

    def change(self, changes: Sequence[tuple[str, Mapping[str, Any]]]) -> None:
        """Record what a step did to several files, in one transaction.

        Compress uses it to move a file and every hard link of it together, so no row ever
        names a file the others no longer share.

        Args:
            changes: each file's key with the columns to set, of `STEP_COLUMNS`.

        Raises:
            ValueError: a field isn't one of them: a bug in the caller.
            DiskError: the database can't be written.
        """
        for _, fields in changes:
            unknown = set(fields) - STEP_COLUMNS
            if unknown:
                raise ValueError(f"no step writes {', '.join(sorted(unknown))}")
        with self._store.transaction() as sql:
            for item, fields in changes:
                if fields:
                    sets = ", ".join(f"{name} = ?" for name in fields)
                    sql.execute(f"UPDATE files SET {sets} WHERE key = ?",
                                (*fields.values(), item))

    def update(self, item: str, **fields: Any) -> None:
        """Record what a step after landing did to a saved file, in one statement.

        This is how the steps (`flnd._steps`) write: compress moves a file and sets `shrink`,
        `tools`, `before` and its new `hash`; unpack sets `unpacked` and `folder`; the sidecar
        step sets `sidecar`.

        Args:
            item: the file's key; a key with no row changes nothing.
            **fields: the columns to set, of `STEP_COLUMNS`.

        Raises:
            ValueError: a field isn't one of them: a bug in the caller.
            DiskError: the database can't be written.
        """
        unknown = set(fields) - STEP_COLUMNS
        if unknown:
            raise ValueError(f"no step writes {', '.join(sorted(unknown))}")
        if fields:
            sets = ", ".join(f"{name} = ?" for name in fields)
            self._store.write(f"UPDATE files SET {sets} WHERE key = ?", (*fields.values(), item))

    # -- failures ---------------------------------------------------------------------------

    def fail(self, *, run: int | None, item: str, target: str, error: BaseException,
             post: str = "", file: str = "", url: str = "",
             request: Mapping[str, Any] | None = None) -> None:
        """Write one failure, and mark a file the site no longer has as gone.

        A `NotFound` (a 404 or 410) records the file as `gone`, so later runs skip it until
        they are told to ask again (`--again`). Its own transaction: a failure is rare, and its
        row must outlive a run that stops right after.

        Args:
            run: the run it happened in; None outside one.
            item: the file's key, or the target's key for a failure of the whole target.
            target: the target's key.
            error: what failed; a `flnd.Error` gives its hint and `attempts`.
            post: the post's id; "" for a failure of the whole target.
            file: where the file would have gone; "" for a failure of the whole target.
            url: the file's URL; only its redacted form is kept.
            request: what a retry needs (the URL, its headers, the post's fields); login
                headers are dropped first, and an archive's password goes to the vault, never
                to `flnd.db`. None when it can't be retried from here.
        """
        from flnd.errors import NotFound

        kept = None
        if request is not None:
            password = request.get("password")
            if password:
                with secrets(self.home, item.partition(":")[0]) as vault:
                    vault[PASSWORD + item] = password
            kept = _request({**request, "password": bool(password)})
        at = now()
        with self._store.transaction() as sql:
            sql.execute("INSERT INTO failures (run, key, target, post_id, file, url, class,"
                        " message, hint, attempts, at, request)"
                        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                        (run, item, target, post, file, _text.redact(url) if url else "",
                         type(error).__name__, str(error), getattr(error, "hint", "") or "",
                         getattr(error, "attempts", 1) or 1, at, kept))
            if isinstance(error, NotFound) and file:
                sql.execute(INSERT_FILE, _file_values(FileRow(
                    item, target, file, post_id=post, state=GONE, at=at)))

    def pending(self, target: str) -> dict[str, dict[str, Any]]:
        """The files of a target that failed and may be retried: not saved or gone since.

        Args:
            target: the target's key.

        Returns:
            Each file's latest retry request, by its key, with an archive's password read back
            from the vault.
        """
        found = self._store.all("SELECT f.key, f.request FROM failures f WHERE f.target = ?"
                          " AND f.request IS NOT NULL AND NOT EXISTS"
                          " (SELECT 1 FROM files WHERE files.key = f.key) ORDER BY f.rowid",
                          (target,))
        pending = {item: json.loads(request) for item, request in found}
        for item, request in pending.items():
            if request.get("password") is True:
                with secrets(self.home, item.partition(":")[0]) as vault:
                    request["password"] = vault.get(PASSWORD + item)
            else:
                request["password"] = None
        return pending

    # -- targets ----------------------------------------------------------------------------

    def targets(self) -> dict[str, TargetRow]:
        """Every target the home knows, by key."""
        found = self._store.all(f"SELECT {', '.join(TARGET_COLUMNS)} FROM targets ORDER BY key")
        return {row[0]: _target_row(row) for row in found}

    def target(self, target: Target) -> TargetRow | None:
        """One target's row; None when the home doesn't know it."""
        found = self._store.one(f"SELECT {', '.join(TARGET_COLUMNS)} FROM targets WHERE key = ?",
                          (key(target),))
        return _target_row(found) if found else None

    def record(self, chosen: Sequence[tuple[Target, Options]], *, follow: bool | None = None,
               keep: bool = False) -> None:
        """Remember each target of a run, keeping what earlier runs learned about it.

        Args:
            chosen: each target with its options.
            follow: True follows the targets; None keeps each one's state (a new one follows);
                False leaves a new one unfollowed.
            keep: whether to save each target's categories and `compress` for later syncs
                (`follow`); otherwise the saved ones stay.
        """
        if not chosen:
            return
        with self._store.transaction():
            for target, options in chosen:
                old = self.target(target)
                row = old or TargetRow(key(target), target.source, target.value, target.kind)
                row.kind = target.kind
                if follow is True or old is None:
                    row.follow = follow is not False
                if keep:
                    names = options.categories
                    row.only = tuple(names) if names and "all" not in names else None
                    row.compress = options.compress
                if options.query:
                    row.query = options.query
                row.last_sync = now()
                self._put(row)

    def follow(self, target: Target, on: bool) -> None:
        """Follow a target, or stop following it; a target the home doesn't know is added.

        Args:
            target: the target.
            on: True follows it, so `sync` takes it; False stops following it.
        """
        with self._store.transaction():
            row = self.target(target) or TargetRow(key(target), target.source, target.value,
                                                   target.kind, last_sync=now())
            row.follow = on
            self._put(row)

    def forget(self, target: Target) -> bool:
        """Remove a target's row; its files and their rows stay. Whether there was one."""
        with self._store.transaction() as sql:
            return sql.execute("DELETE FROM targets WHERE key = ?", (key(target),)).rowcount > 0

    def seen(self, target: Target, found: Sequence[str]) -> None:
        """Add targets a search found to its row, in order, each once.

        Args:
            target: the search.
            found: the keys of the targets it found.
        """
        if not found:
            return
        with self._store.transaction():
            row = self.target(target) or TargetRow(key(target), target.source, target.value,
                                                   target.kind, follow=True, last_sync=now())
            row.seen = list(dict.fromkeys([*row.seen, *found]))
            self._put(row)

    def progress(self, target: Target, *, cursors: Mapping[str, str | None],
                 complete: Sequence[str]) -> None:
        """Keep where a target's listings stopped, after its run.

        Args:
            target: the target.
            cursors: each category's cursor to resume from; None drops it (listed to the end).
            complete: the categories listed to their end today.
        """
        if not cursors and not complete:
            return
        today = datetime.date.today().isoformat()
        with self._store.transaction():
            # A target nothing recorded (a run of the engine's own, a test) follows, as a row of
            # an old home without a `sync` field did.
            row = self.target(target) or TargetRow(key(target), target.source, target.value,
                                                   target.kind, follow=True)
            merged = {**row.cursors, **cursors}
            row.cursors = {name: value for name, value in merged.items() if value}
            row.complete = {**row.complete, **dict.fromkeys(complete, today)}
            self._put(row)

    def tally(self, target: Target) -> None:
        """Count a target's saved files and bytes into its row, for `flnd ls`."""
        with self._store.transaction() as sql:
            sql.execute("UPDATE targets SET (files, bytes) = (SELECT count(*), coalesce(sum(size),"
                        " 0) FROM files WHERE target = ?1 AND state = 'saved') WHERE key = ?1",
                        (key(target),))

    def count(self, target: str) -> tuple[int, int]:
        """A target's saved files and their bytes, counted now; for a target with no row."""
        found = self._store.one("SELECT count(*), coalesce(sum(size), 0) FROM files"
                          " WHERE target = ? AND state = 'saved'", (target,))
        return (found[0], found[1]) if found else (0, 0)

    def _put(self, row: TargetRow) -> None:
        """Write a target's whole row, replacing the one there; the caller's transaction joins.

        Args:
            row: the row, as `target` read it and the caller changed it.
        """
        options: dict[str, Any] = {"only": list(row.only)} if row.only else {}
        if row.compress:
            options["compress"] = True
        self._store.write(f"INSERT OR REPLACE INTO targets ({', '.join(TARGET_COLUMNS)})"
                    f" VALUES ({', '.join('?' * len(TARGET_COLUMNS))})",
                    (row.key, row.source, row.value, row.kind, int(row.follow), _json(options),
                     row.query, _json(row.complete), _json(row.cursors), _json(row.seen),
                     row.last_sync, row.files, row.bytes))

    # -- runs, logins and sources' state ----------------------------------------------------

    def start(self, command: str) -> int:
        """Record a run that downloads, and return its id for its failures."""
        with self._store.transaction() as sql:
            made = sql.execute("INSERT INTO runs (command, started) VALUES (?, ?)",
                               (command, now()))
            return int(made.lastrowid or 0)

    def end(self, run: int, *, saved: int) -> None:
        """Record that a run ended, with the files it saved and how many failed."""
        self._store.write("UPDATE runs SET ended = ?, saved = ?, failed ="
                    " (SELECT count(*) FROM failures WHERE run = ?) WHERE id = ?",
                    (now(), saved, run, run))

    def login(self, source: str, origin: str | None) -> None:
        """Record how a source logged in, or forget it when it logs out.

        Args:
            source: the source's code.
            origin: where the login came from ("Chrome", "cookies.txt", "qr"); None forgets it.
        """
        if origin is None:
            self._store.write("DELETE FROM logins WHERE source = ?", (source,))
        else:
            self._store.write("INSERT OR REPLACE INTO logins (source, origin, at) VALUES (?, ?, ?)",
                        (source, origin, now()))

    def origin(self, source: str) -> str | None:
        """How a source logged in, as recorded; None when nothing was."""
        found = self._store.one("SELECT origin FROM logins WHERE source = ?", (source,))
        return found[0] if found else None

    def state(self, source: str) -> State:
        """A source's own values between runs, as a mapping (`site.state`)."""
        return State(self, source)

    # -- the connection ---------------------------------------------------------------------

class State(MutableMapping[str, Any]):
    """One source's values between runs: `site.state`, a mapping of text to JSON values.

    Each read and write is one statement on the home's `state` table, so values survive a run
    that stops at any point.

    Args:
        db: the home's database.
        source: the source's code; another source's values aren't visible.
    """

    def __init__(self, db: Home, source: str) -> None:
        self._db, self._source = db, source

    def __getitem__(self, name: str) -> Any:
        found = self._db._store.one("SELECT value FROM state WHERE source = ? AND key = ?",
                              (self._source, name))
        if found is None:
            raise KeyError(name)
        return msgspec.json.decode(found[0])

    def __setitem__(self, name: str, value: Any) -> None:
        self._db._store.write("INSERT OR REPLACE INTO state VALUES (?, ?, ?)",
                        (self._source, name, _json(value)))

    def __delitem__(self, name: str) -> None:
        if name not in self:
            raise KeyError(name)
        self._db._store.write("DELETE FROM state WHERE source = ? AND key = ?",
                              (self._source, name))

    def __iter__(self) -> Iterator[str]:
        found = self._db._store.all("SELECT key FROM state WHERE source = ? ORDER BY key",
                              (self._source,))
        return iter([name for (name,) in found])

    def __len__(self) -> int:
        found = self._db._store.one("SELECT count(*) FROM state WHERE source = ?", (self._source,))
        return found[0] if found else 0


def stamp(path: Path) -> tuple[int, int]:
    """A finished file's size and modified time in nanoseconds, as its row keeps them.

    Args:
        path: the file, in place.

    Returns:
        `(size, mtime_ns)`, from one `stat`.

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


def _file_values(row: FileRow) -> tuple[Any, ...]:
    return (row.key, row.target, row.source, row.post_id, row.category, row.path, row.size,
            row.mtime, row.date, int(row.estimated), row.kind, row.state, row.at, row.files,
            row.hash)


def _target_row(found: tuple[Any, ...]) -> TargetRow:
    """A `targets` row as a `TargetRow`, with its JSON columns decoded.

    Args:
        found: the row's columns, in `TARGET_COLUMNS` order.

    Returns:
        The row.
    """
    (item, source, value, kind, follow, options, query, complete, cursors, seen, last_sync,
     files, size) = found
    settings = json.loads(options)
    only = settings.get("only")
    return TargetRow(item, source, value, kind, bool(follow), tuple(only) if only else None,
                     query, json.loads(complete), json.loads(cursors), json.loads(seen),
                     last_sync, files, size, compress=settings.get("compress") is True)


def _file_row(found: tuple[Any, ...]) -> FileRow:
    """A `files` row read with `ROW_SELECT`, as a `FileRow`.

    Args:
        found: the row's columns, in `ROW_SELECT`'s order.

    Returns:
        The row.
    """
    (item, target, path, size, mtime, post, category, date, estimated, kind, state, at, files,
     shrink, tools, before, unpacked, folder, digest, sidecar) = found
    return FileRow(item, target, path, size, mtime, post, category, date, bool(estimated), kind,
                   state, at, files, shrink, tools, before, unpacked, folder, digest, sidecar)


def _request(request: Mapping[str, Any]) -> str:
    """A failed file's retry request as JSON, without the headers that log a request in.

    Args:
        request: the URL, its headers and the post's fields, as the job keeps them.

    Returns:
        The JSON; `headers` is None when none is left.
    """
    if isinstance(request.get("password"), str):
        # An archive's password lives in the vault (`Home.fail`); a plain one is dropped here.
        request = {**request, "password": False}
    headers = request.get("headers")
    clean = {name: value for name, value in (headers.items() if isinstance(headers, dict) else ())
             if str(name).lower() not in SECRET_HEADERS}
    return _json({**request, "headers": clean or None})


def _json(value: Any) -> str:
    return msgspec.json.encode(value).decode()


def _date(text: Any) -> datetime.date | None:
    try:
        return datetime.date.fromisoformat(text) if isinstance(text, str) else None
    except ValueError:
        return None


# ---------------------------------------------------------------------------------------------
# Logins, in the vault
# ---------------------------------------------------------------------------------------------

# Browsers `from_browser` reads cookies from, by `flnd._cookies` name with display name.
BROWSERS = {
    "chrome": "Chrome", "firefox": "Firefox", "edge": "Edge",
    "brave": "Brave", "chromium": "Chromium",
}
# The folder of the plain cookie files of much older versions, moved into the vault on first use.
COOKIE_FOLDER = "cookies"
# A plain cookie file was `<code>.txt`, or `<code>.json`, the older form; the first one found wins.
COOKIE_SUFFIXES = (".txt", ".json")
# The secret that holds a cookie source's jar, as JSON: a list of cookie dicts.
COOKIES = "cookies"
# The secret an old home kept a login's origin in; `flnd.db`'s `logins` table holds it now.
ORIGIN = "origin"
# The secret that proves a login for a source with its own (tlg): the session its `login` stores.
# A cookie source's jar (`COOKIES`) proves one too. Any other secret, such as tlg's `api_id` and
# `api_hash`, is only the source's API key.
LOGIN_SECRET = "session"
LOGIN_SECRETS = frozenset({COOKIES, LOGIN_SECRET})
# What a login that can't be read says to do: the vault holds logins only.
LOGIN_HINT = "log in again: flnd login <source> (a damaged secret.key: delete it first)"


def from_browser(source: Source, *, browser: str, profile: str | None = None) -> _cookies.Jar:
    """The source's cookies from an installed browser, for each of its domains.

    Args:
        source: the source; only cookies of its domains and their subdomains are kept.
        browser: a key of `BROWSERS`.
        profile: the browser's profile folder; None for its default.

    Returns:
        The cookies.

    Raises:
        LoginError: the browser's cookies can't be read; the message suggests a cookie file.
    """
    try:
        return _own(source, _cookies.load(browser, profile=profile))
    # Every way reading a browser fails: missing file, platform refusal, bad decrypt, no crypto.
    except (OSError, RuntimeError, ValueError, ImportError, Error) as error:
        raise LoginError(
            f"{source.code}: can't read {BROWSERS.get(browser, browser)}'s cookies: {error}; "
            "try a cookie file instead"
        ) from None


def from_file(source: Source, path: Path) -> _cookies.Jar:
    """The source's cookies from a Netscape or JSON cookie file; a whole browser export works.

    Args:
        source: the source; only cookies of its domains and their subdomains are kept.
        path: the file; it is read once and never copied.

    Returns:
        The cookies.

    Raises:
        LoginError: the file can't be read, or isn't a cookie file.
    """
    try:
        return _own(source, _cookies.load(path))
    except (OSError, ValueError, Error) as error:
        raise LoginError(f"{source.code}: can't read {path}: {error}") from None


def store(source: Source, jar: _cookies.Jar, *, home: Path, origin: str) -> str:
    """Check that the jar proves a login, seal it in the source's vault, and return a summary.

    A plain cookie file a much older version left for the source is deleted once the new login
    is sealed, so it can never come back.

    Args:
        source: the source logging in; one of its `needs` cookies must be in the jar.
        jar: the cookies, from `from_browser` or `from_file`.
        home: the home folder.
        origin: where the jar came from, for the messages: a browser's name or the file's.

    Returns:
        One line saying where the login came from, and how many cookies it has.

    Raises:
        LoginError: none of the source's `needs` cookies is in the jar.
        DiskError: the key file is damaged, or the vault can't be written.
    """
    if source.needs and not any(name in jar for name in source.needs):
        raise LoginError(
            f"{source.code}: no {' or '.join(source.needs)} for {source.domains[0]} in {origin}; "
            "log in there first"
        )
    save(home, source.code, jar, origin=origin)
    for path in _cookie_files(home, source.code):
        path.unlink(missing_ok=True)
    _remove_if_empty(home / COOKIE_FOLDER)
    return f"{source.code}: logged in from {origin} ({_text.count(len(jar), 'cookie')})"


def save(home: Path, code: str, jar: _cookies.Jar, *, origin: str | None = None) -> None:
    """Seal a source's cookie jar in its vault as `cookies`, replacing the one there.

    Args:
        home: the home folder; `secrets.db` and the key file are made when missing.
        code: the source's code; the value is bound to its row.
        jar: the cookies; nothing checks that they prove a login (`store` does).
        origin: how it logged in, recorded in `flnd.db`; None leaves the record as it is.

    Raises:
        DiskError: the key file is damaged, or the vault can't be written.
    """
    with secrets(home, code) as vault:
        vault[COOKIES] = json.dumps(fix_domains(jar).list)
    if origin is not None:
        record_origin(home, code, origin)


def record_origin(home: Path, code: str, origin: str) -> None:
    """Record how a source logged in ("qr", "phone", "Chrome") in `flnd.db`'s `logins`."""
    with open(home) as db:
        db.login(code, origin)


def origin(home: Path, code: str) -> str:
    """How a source logged in, or "" when nothing was recorded.

    A home migrated from before `flnd.db` kept it as a secret; that is read when `flnd.db` has
    no row.

    Args:
        home: the home folder.
        code: the source's code.

    Returns:
        The origin, as `store` or a source's own login recorded it.
    """
    with open(home) as db:
        found = db.origin(code)
    if found is not None:
        return found
    if not (home / SECRETS).is_file():
        return ""
    with secrets(home, code) as vault:
        return vault.get(ORIGIN, "")


def expiring(home: Path, source: Source, *, within: float = 24 * 3600) -> list[tuple[str, float]]:
    """Login cookies of `source.needs` that expire soon, so a run can warn before the wall.

    Args:
        home: the home folder.
        source: the source; only its `needs` cookies count.
        within: how soon, in seconds; a day by default.

    Returns:
        Each such cookie's name and its seconds left (negative once expired).
    """
    if not source.needs or not logged_in(home, source.code):
        return []
    now = time.time()
    found: list[tuple[str, float]] = []
    for cookie in load(home, source.code):
        expires = cookie.get("expires", -1)
        if cookie.get("name") in source.needs and isinstance(expires, (int, float)) and expires > 0:
            remaining = expires - now
            if remaining <= within:
                found.append((str(cookie["name"]), remaining))
    return found


def write_back(home: Path, code: str, current: Iterable[Cookie]) -> list[str]:
    """Save the cookies a site refreshed into the vault's jar, for names already there.

    A failure is a warning, never an error: the run's files are saved, and the next run logs in
    with the cookies it had.

    Args:
        home: the home folder.
        code: the source's code.
        current: the cookies the run's client holds now.

    Returns:
        The names of the cookies that changed.
    """
    fresh = list(current)
    if not fresh or not _has_vault(home):
        return []
    try:
        saved = load(home, code).list
        changed = [
            name for cookie in saved if (name := _refresh_cookie(cookie, fresh)) is not None
        ]
        if changed:
            save(home, code, _cookies.Jar(saved))
            ui.debug("{code}: refreshed {names}", code=code, names=", ".join(changed))
        return changed
    except (OSError, ValueError, Error) as error:
        ui.warn(
            "{code}: could not save refreshed cookies ({kind})",
            code=code, kind=type(error).__name__,
        )
        return []


def _refresh_cookie(cookie: dict, fresh: list[Cookie]) -> str | None:
    """Take a saved cookie's new value from the run's cookies, when the site changed it.

    The same name on the same domain and path wins; else the same name on a subdomain.

    Args:
        cookie: a saved cookie, changed in place.
        fresh: the cookies the run's client holds now.

    Returns:
        The cookie's name when it changed; None when it didn't.
    """
    domain, path = str(cookie.get("domain", "")).lstrip("."), cookie.get("path") or "/"
    same = [
        found for found in fresh if found.name == cookie["name"]
        and (found.domain.lstrip(".") == domain
             or found.domain.lstrip(".").endswith("." + domain))
    ]
    exact = [
        found for found in same
        if found.domain.lstrip(".") == domain and found.path == path
    ]
    matched = exact or same
    if not matched or (new := matched[-1]).value in (None, "", cookie["value"]):
        return None
    cookie["value"] = new.value
    if new.expires is not None and isinstance(cookie.get("expires"), (int, float)):
        cookie["expires"] = new.expires
    return str(cookie["name"])


def load(home: Path, code: str) -> _cookies.Jar:
    """The source's saved cookies, or an empty jar when it never logged in.

    A plain cookie file of a much older version is moved into the vault first.

    Args:
        home: the home folder.
        code: the source's code.

    Returns:
        The jar, with `fix_domains` applied.

    Raises:
        DiskError: the key file is damaged.
        LoginError: the sealed jar doesn't open: it was copied from another row, or sealed
            with another key.
    """
    _move(home, code)
    if not _has_vault(home):
        return _cookies.Jar()
    with secrets(home, code) as vault:
        text = vault.get(COOKIES)
    return fix_domains(_cookies.Jar(json.loads(text))) if text else _cookies.Jar()


def logged_in(home: Path, code: str) -> bool:
    """Whether the source has a saved login in the vault: cookies or a session."""
    _move(home, code)
    return bool(LOGIN_SECRETS & _secret_names(home, code))


def api_key_saved(home: Path, code: str) -> bool:
    """Whether the source has secrets but no login: its API key is stored, the login isn't."""
    return not logged_in(home, code) and bool(_secret_names(home, code))


def logout(home: Path, code: str) -> bool:
    """Remove every secret of the source, and its login record.

    Args:
        home: the home folder; a home with no vault is left without one.
        code: the source's code.

    Returns:
        Whether there was a login, an API key or a plain cookie file to remove.
    """
    removed = logged_in(home, code) or api_key_saved(home, code)
    # Opening the vault would create it, so a home that has none is left without one.
    if _has_vault(home):
        with secrets(home, code) as saved:
            saved.clear()
    if (home / DB).is_file():
        with open(home) as db:
            db.login(code, None)
    for path in _cookie_files(home, code):
        if path.is_file():
            path.unlink()
            removed = True
    _remove_if_empty(home / COOKIE_FOLDER)
    return removed


def _has_vault(home: Path) -> bool:
    """Whether the home has a vault: its own, or an old one the migration will copy."""
    return (home / SECRETS).is_file() or (home / OLD_SECRETS).is_file()


def _secret_names(home: Path, code: str) -> set[str]:
    """The names of the source's secrets, without creating `secrets.db` or the key file."""
    if not _has_vault(home):
        return set()
    with secrets(home, code) as saved:
        return set(saved)


def _cookie_files(home: Path, code: str) -> list[Path]:
    """Plain cookie files a much older version may have left for a source."""
    return [home / COOKIE_FOLDER / f"{code}{suffix}" for suffix in COOKIE_SUFFIXES]


def _move(home: Path, code: str) -> None:
    """Move a source's plain cookie file into its vault on first access.

    Args:
        home: the home folder.
        code: the source's code.

    The first file found (`.txt`, then `.json`) is sealed as `cookies`, unless the vault holds a
    jar already, which is the newer login. Each file is deleted only after that, so a vault write
    that fails (a damaged key, a full disk) keeps the file, warned of, for the next run to try
    again. The folder is removed once empty. Sources run on threads of their own, each calling
    this for its own code, so two threads never touch one file.
    """
    folder = home / COOKIE_FOLDER
    if not folder.is_dir():
        return
    for path in _cookie_files(home, code):
        if not path.is_file():
            continue
        try:
            jar = _cookies.load(path)
            with secrets(home, code) as vault:
                if COOKIES not in vault:
                    vault[COOKIES] = json.dumps(fix_domains(jar).list)
        # A file that can't be read or isn't a cookie file (a LoginError), a damaged key (a
        # DiskError), or a vault that can't be opened or written (OSError, DiskError): the plain
        # file is the only copy of the login, so it stays. Each of these names the file or the
        # key, never a cookie's value.
        except (OSError, ValueError, Error) as error:
            ui.warn(
                "{code}: can't move {name} into the vault: {error}; kept it",
                code=code, name=path.name, error=error,
            )
            continue
        path.unlink(missing_ok=True)
    _remove_if_empty(folder)


def _remove_if_empty(folder: Path) -> None:
    """Remove the old cookie folder once nothing is left in it.

    Args:
        folder: the folder; missing, or not empty, it is left as it is.
    """
    try:
        folder.rmdir()
    # Missing, or still holding another source's file (or anything else): left as it is.
    except OSError:
        pass


def fix_domains(jar: _cookies.Jar) -> _cookies.Jar:
    """Give every domain a leading dot, and every path `/` when it has none.

    Args:
        jar: the cookies, as read or saved.

    Returns:
        A new jar; the one given is unchanged.
    """
    # Exported files often hold host-only domains; the dot lets every subdomain see the cookie.
    return _cookies.Jar(
        {
            **cookie,
            "domain": "." + cookie.get("domain", "").lstrip("."),
            "path": cookie.get("path") or "/",
        }
        for cookie in jar
    )


def _own(source: Source, jar: _cookies.Jar) -> _cookies.Jar:
    """The cookies of `jar` for the source's domains and their subdomains, domain by domain."""
    return jar.filter(domains=source.domains)




def secrets(home: Path, code: str) -> _vault.Vault:
    """The secrets of one source, as a mapping of `str` to `str`, for a `with` block.

    An old home is migrated first, so the logins of its old vault are there.

    Args:
        home: the home folder; `secrets.db` and `secret.key` are made in it on the first write.
        code: the source's code; another source's secrets aren't visible.

    Returns:
        The `flnd._vault.Vault`; leaving the `with` closes it.

    Example:
        >>> import tempfile
        >>> with secrets(Path(tempfile.mkdtemp()), "tlg") as saved:
        ...     dict(saved)
        {}
    """
    if _old(home):
        open(home).close()
    return _vault.Vault(home / SECRETS, home / KEY, code, hint=LOGIN_HINT)


# ---------------------------------------------------------------------------------------------
# Migrating an old home
# ---------------------------------------------------------------------------------------------
# An old home (flnd before flnd.db) kept four stores in `metadata/`, each a table of JSON values
# keyed by text: the history (`downloads`, by file key), the targets (`targets`, by target key),
# each source's state (a table per source) and the vault (`secrets`, base64 of sealed values).
# The migration reads them and never writes them, so the old flnd still runs on the home. It can
# be deleted one release after it ships.

def _old(home: Path) -> bool:
    """Whether a home has an old store; `_migrate` then checks whether it was copied yet."""
    return any((home / path).is_file() for path in (*OLD_TABLES, OLD_STATE))


def _migrate(sql: sqlite3.Connection, home: Path) -> None:
    """Copy an old home's stores into `flnd.db` and `secrets.db`, once.

    Steps:
    1. Attach `secrets.db` when there is an old vault, so one transaction writes both files.
    2. `BEGIN IMMEDIATE` takes the write lock; a second run opening the same home waits here,
       then finds the migration done and does nothing.
    3. Copy the vault's key to `<home>/secret.key` (0600), unless one is there: the sealed values
       are copied as they are, which needs the same key, and no value is ever decrypted here.
    4. Read each old store (`_read_old`) and insert its rows; `INSERT OR IGNORE`, so a home
       half-written by a crash between the two files' commits is completed, never doubled.
    5. Count each target's files, mark the migration done in `meta`, and commit.

    Args:
        sql: the new `flnd.db`'s connection, outside any transaction.
        home: the home folder.

    Raises:
        DiskError: an old store can't be read, or a new file written.
    """
    if sql.execute("SELECT 1 FROM meta WHERE key = 'migrated'").fetchone():
        return
    vault = (home / OLD_SECRETS).is_file()
    if vault:
        sql.execute("ATTACH DATABASE ? AS vault", (str(home / SECRETS),))
    try:
        sql.execute("BEGIN IMMEDIATE")
        try:
            if not sql.execute("SELECT 1 FROM meta WHERE key = 'migrated'").fetchone():
                if (home / OLD_KEY).is_file():
                    _copy_key(home / OLD_KEY, home / KEY)
                _copy(sql, home, vault)
                sql.execute("INSERT INTO meta VALUES ('migrated', ?)", (now(),))
            sql.execute("COMMIT")
        except BaseException:
            if sql.in_transaction:
                sql.execute("ROLLBACK")
            raise
    finally:
        if vault:
            sql.execute("DETACH DATABASE vault")


def _copy(sql: sqlite3.Connection, home: Path, vault: bool) -> None:
    """Insert every old row into the new tables, inside the migration's transaction.

    Args:
        sql: the new `flnd.db`'s connection, in the migration's transaction.
        home: the home folder.
        vault: whether an old vault is there; its rows go to the attached `vault` database.
    """
    targets = _read_old(home / OLD_TARGETS, "targets")
    folders = {}
    for item, row in targets.items():
        code, _, value = item.partition(":")
        if not isinstance(row, dict) or not value:
            continue
        folders[(code, _text.filename(value))] = item
        complete = row.get("complete") if isinstance(row.get("complete"), dict) else {}
        cursors = row.get("cursors") if isinstance(row.get("cursors"), dict) else {}
        seen = row.get("seen") if isinstance(row.get("seen"), list) else []
        only = row.get("only") if isinstance(row.get("only"), list) else None
        query = row.get("query") if isinstance(row.get("query"), str) else None
        sql.execute(f"INSERT OR IGNORE INTO targets ({', '.join(TARGET_COLUMNS)})"
                    f" VALUES ({', '.join('?' * len(TARGET_COLUMNS))})",
                    (item, code, value, str(row.get("kind") or ""),
                     int(row.get("sync", True) is True), _json({"only": only} if only else {}),
                     query, _json(complete), _json(cursors), _json(seen),
                     str(row.get("at") or ""), 0, 0))
        failed = row.get("failed")
        for file_key, entry in (failed.items() if isinstance(failed, dict) else ()):
            if isinstance(entry, dict):
                sql.execute("INSERT INTO failures (key, target, post_id, file, url, class,"
                            " message, at, request) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                            (file_key, item, str(entry.get("id") or ""),
                             str(entry.get("dest") or ""),
                             _text.redact(str(entry.get("url") or "")), "Error",
                             "failed before flnd.db", str(row.get("at") or ""), _request(entry)))
    for item, row in _read_old(home / OLD_HISTORY, "downloads").items():
        if not isinstance(row, dict) or not isinstance(row.get("path"), str):
            continue
        target = _owner(item, row["path"], folders)
        name = Path(row["path"]).name
        # An old row held its post's count on the post's first file, as `files` does now.
        sql.execute(INSERT_FILE.replace("OR REPLACE", "OR IGNORE"), _file_values(FileRow(
            item, target, row["path"], int(row.get("size") or 0), _int(row.get("mtime")),
            str(row.get("id") or ""), str(row.get("category") or ""),
            row["date"] if isinstance(row.get("date"), str) else None,
            row.get("estimated") is True, _kind(name), SAVED, str(row.get("at") or ""),
            _int(row.get("files")))))
        # What the steps after landing recorded in the old row (`--compress`, extraction).
        steps = {name: row[name] for name in ("shrink", "tools", "before", "unpacked", "folder")
                 if isinstance(row.get(name), (str, int)) and not isinstance(row[name], bool)}
        if steps:
            sets = ", ".join(f"{name} = ?" for name in steps)
            sql.execute(f"UPDATE files SET {sets} WHERE key = ?", (*steps.values(), item))
    for code, table in _read_old_state(home / OLD_STATE).items():
        sql.executemany("INSERT OR IGNORE INTO state VALUES (?, ?, ?)",
                        [(code, name, _json(value)) for name, value in table.items()])
    if vault:
        import base64

        sql.execute(_vault.SCHEMA.format("vault."))
        rows = []
        for slot, sealed in _read_old(home / OLD_SECRETS, "secrets").items():
            code, _, name = slot.partition(":")
            if isinstance(sealed, str) and name:
                rows.append((code, name, base64.b64decode(sealed)))
        sql.executemany("INSERT OR IGNORE INTO vault.secrets VALUES (?, ?, ?)", rows)
    sql.execute("UPDATE targets SET (files, bytes) = (SELECT count(*), coalesce(sum(size), 0)"
                " FROM files WHERE files.target = targets.key AND state = 'saved')")


def _owner(item: str, path: str, folders: Mapping[tuple[str, str], str]) -> str:
    """The target an old history row belongs to, from where its file was saved.

    An old row didn't name its target, but its path did: `…/media/<code>/<folder>/…`, where
    the folder is the target's value made a safe name. A folder no target row names (a target
    deleted from the old `targets.db`) becomes `<code>:<folder>`, as `flnd ls` reads it.

    Args:
        item: the row's key, `<code>:<key>`.
        path: where its file was saved.
        folders: each old target's key, by `(code, folder name)`.

    Returns:
        The target's key; `<code>:` when the path names no target's folder.
    """
    code = item.partition(":")[0]
    parts = Path(path).parts
    for at in range(len(parts) - 2, -1, -1):
        if parts[at] == MEDIA.name and parts[at + 1] == code and at + 2 < len(parts) - 1:
            folder = parts[at + 2]
            return folders.get((code, folder), f"{code}:{folder}")
    return f"{code}:"


def _read_old(path: Path, table: str) -> dict[str, Any]:
    """One table of an old store, without changing the file: `{key: value}`.

    Args:
        path: the old store; missing reads as empty.
        table: the table's name, without its `t_` prefix.

    Returns:
        Each row's value, decoded, by its key.

    Raises:
        DiskError: the file is neither SQLite nor JSON, or can't be read.
    """
    if not path.is_file():
        return {}
    if not _is_sqlite(path):
        found = _read_json(path).get(table)
        return dict(found) if isinstance(found, dict) else {}
    with _db.read_only(path) as old:
        names = {name for (name,) in old.execute("SELECT name FROM sqlite_master")}
        if f"t_{table}" not in names:
            return {}
        rows = old.execute(f'SELECT key, value FROM "t_{table}"').fetchall()
    return {item: json.loads(value) for item, value in rows}


def _read_old_state(path: Path) -> dict[str, dict[str, Any]]:
    """Every source's table of an old `sources.db`, by the source's code.

    Args:
        path: the old store; missing reads as empty.

    Returns:
        Each source's values, by name, by the source's code.
    """
    if not path.is_file():
        return {}
    if not _is_sqlite(path):
        return {code: dict(table) for code, table in _read_json(path).items()
                if isinstance(table, dict)}
    with _db.read_only(path) as old:
        names = [name for (name,) in old.execute("SELECT name FROM sqlite_master"
                                                 " WHERE type = 'table'") if name.startswith("t_")]
        return {name[2:]: {item: json.loads(value) for item, value in
                           old.execute(f'SELECT key, value FROM "{name}"')} for name in names}


def _read_json(path: Path) -> dict[str, Any]:
    """An old JSON store, read whole.

    Args:
        path: the store, a JSON object of tables: `{table: {key: value}}`.

    Returns:
        The tables.

    Raises:
        DiskError: the file can't be read, or isn't a JSON object.
    """
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise DiskError(f"{path}: cannot read: {error}") from error
    if not isinstance(data, dict):
        raise DiskError(f"{path}: expected a JSON object")
    return data


def _is_sqlite(path: Path) -> bool:
    with path.open("rb") as handle:
        return handle.read(len(SQLITE_MAGIC)) == SQLITE_MAGIC


def _copy_key(old: Path, new: Path) -> None:
    """Copy the vault's key file, mode 0600, unless the new home has one already.

    Args:
        old: the old key, `metadata/secret.key`.
        new: where the home keeps it now, `secret.key`.

    Raises:
        DiskError: the new file can't be made.
    """
    secret = old.read_bytes()
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0)
    try:
        handle = os.open(new, flags, 0o600)
    except FileExistsError:
        return
    except OSError as error:
        raise DiskError(f"{new}: {error.strerror or error}") from error
    with os.fdopen(handle, "wb") as file:
        file.write(secret)


def _int(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _kind(name: str) -> str | None:
    from flnd.common.paths import kind_of

    return kind_of(name)
