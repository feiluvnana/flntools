"""The home: its layout, `flnd.db` (what the home remembers) and the logins in its vault.

The home folder holds `media/` (the downloads), `metadata/` (`flnd.db` and `secret.db`), and `log/`
(crash files and each run's samples), and an optional `config.toml`; `cache/` is reserved. This
module is flnd's own storage, not a library: it knows flnd's tables, targets and sources. It is
built on three library modules: `flnkit.db` (a SQLite store), `flnkit.vault` (source secrets in
`secret.db`) and `flnkit.cookies` (cookie jars). `src/docs/storage` is the guide to it.

What `flnd.db` holds (the storage guide in `src/docs/` shows every column):
- `files`: one row per saved file, keyed `<code>:<key>` (the source's `key` template), with its
  target, post, path, size, day, its bytes' SHA-256 (`hash`), and `state`: `saved`, `gone` (the
  site answered 404 or 410), or `link` (a second name for a saved file, in another category's
  folder, keyed `<file's key>@<category>`). A file that failed has no row here (its record is
  in `failures`), unless the site said it is gone. The steps after a file lands fill in
  `shrink`, `tools`, `before`, `unpacked`, `folder` and `sidecar` (`flnd.engine.save`).
- `posts`: a view of `files`, one row per post of a target with its category, day and how
  many files it has. The first file of a post of several holds the count (`files.files`),
  which tells whether a post listed again is whole: a table of its own would cost every commit
  another statement and another page.
- `targets`: every target the home knows: followed or not, its options, which categories were
  listed to their end, the cursors of cut-short listings, each listing's head (the newest post
  a run that read it from the top saw) and its gaps (where syncs cut short stopped), a search's
  found targets, and its file count and bytes for `flnd ls`.
- `runs`: each run that downloaded, with what it saved and how many failures it left.
- `failures`: one row per file (or target) that failed and isn't saved since: its target, post,
  category, the cursor of the page its post was on, the error's class, message and hint, how
  many runs failed it, and its redacted URL. It is the only record of a failure, so the report
  (`flnd status`), the automatic retry and `flnd retry` can't disagree. No row holds a
  request: a retry lists the post again for fresh URLs (`flnd.engine.job`).
- `state`: each source's own values between runs (`site.state`).
- `logins`: how each source logged in, and when; never a secret (those are in `secret.db`).
- `probes`: what ffmpeg found in a file, kept so it isn't asked twice; safe to empty.
- `meta`: the schema's version.

The logins: a cookie source logs in by `from_browser` or `from_file`; `store` checks the jar
proves a login and `save` stores it in the source's slot of `secret.db` (`secrets`); a run calls
`load` for the jar, and `write_back` saves the cookies the site refreshed.

Who calls it: the engine (`src/engine/core.py`), the job (`src/engine/job.py`), the steps after
landing (`src/engine/save.py`), `check` (`src/engine/check.py`) and the library
(`src/_library.py`) for files,
targets, runs and failures; the engine also for logins (`flnd login`, `flnd logout`, and
`fl.logins`, each source's login state); `SourceRun` (`src/engine/site.py`) for a source's cookies,
state and secrets.

How a home opens (`open`):
1. A home with `metadata/flnd.db` connects to it, and makes any table it lacks.
2. A new home connects on its first write, so reading one (`fl.targets()`, a dry run) leaves
   the folder as it was.

Rules a change must keep:
- One transaction per post: a post's file rows commit together, after every file of it is
  in place (`save`). A commit per file would cost 14 µs each, and a writer thread would add a
  window in which saved rows are invisible. A step that moves several files at once (compress,
  which keeps the links dedupe made) moves their rows in one transaction too (`change`).
- A row is written only after its file: a crash in between leaves a whole file without a row,
  which the next run finds on disk and skips.
- One connection per `Home`, shared by the lister and the download threads behind one lock.
  SQLite serializes writers across processes; a writer waits for another run (`flnkit.db`).
- A secret's value never reaches a message, a log line or `flnd.db`.
"""

from __future__ import annotations

import datetime
import functools
import json
import re
import shutil
import sqlite3
import time
from collections.abc import Iterable, Iterator, Mapping, MutableMapping, Sequence
from http.cookiejar import Cookie
from pathlib import Path
from typing import TYPE_CHECKING, Any

import msgspec

from flnd.errors import DiskError, Error, LoginError, UsageError
from flnd.ui.log import ui
from flnkit import clock, cookies, db, text, vault

if TYPE_CHECKING:
    from flnd.engine.options import Options
    from flnd.source import Source, Target

__all__ = ["FailureRow", "FileRow", "Home", "RunRow", "TargetRow", "open", "sample", "secrets",
           "sweep"]

# ---------------------------------------------------------------------------------------------
# The home's layout
# ---------------------------------------------------------------------------------------------

# Under the home: media/ for downloads, metadata/ for stores (flnd.db and secret.db), and
# log/ for run logs and crash reports. A cache/ folder is reserved; nothing writes there yet.
MEDIA, METADATA, LOGS = Path("media"), Path("metadata"), Path("log")
DB = METADATA / "flnd.db"
SECRETS = METADATA / "secret.db"

# How many pages the log grows to before SQLite checkpoints it into the database (4 KiB each).
CHECKPOINT_PAGES = 10_000
# The most memory, in KiB, SQLite's page cache may take for `flnd.db`. Its default (2 MiB) holds
# a tenth of a home of 100,000 files (20 MiB), so most reads of a listed file's row went to the
# log or the file, through the OS: at 64 MiB a point read takes 1.8 µs instead of 2.4. The
# cache only grows as pages are read, so a small home costs what it holds.
CACHE_KIB = 64 * 1024
# The schema's version, in `meta`; a later change of a table bumps it and migrates.
VERSION = "1"

# A file's `state`: where it stands with the site; a `link` row is another name of a saved file.
# A file that failed has no `files` row at all: its record is in `failures`.
SAVED, GONE, LINK = "saved", "gone", "link"
# The runs a failed file is retried on by itself, at the start of its target's next downloads and
# syncs; past them it waits for `flnd retry`, which always tries (src/docs/errors).
RETRIES = 3
# Logs and samples older than this are swept at the start of each run that downloads.
LOG_DAYS = 30
# A response whose URL looks like this is a login's, and is never kept as a sample: its body may
# hold the session it just made.
LOGIN_URL = re.compile(r"login|logon|signin|sign_in|oauth|auth|session|token|password|account",
                       re.IGNORECASE)

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
    files INTEGER NOT NULL DEFAULT 0, bytes INTEGER NOT NULL DEFAULT 0,
    heads TEXT NOT NULL DEFAULT '{}', gaps TEXT NOT NULL DEFAULT '{}'
) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS runs (
    id INTEGER PRIMARY KEY, command TEXT NOT NULL, started TEXT NOT NULL, ended TEXT,
    saved INTEGER NOT NULL DEFAULT 0, failed INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS failures (
    run INTEGER, key TEXT NOT NULL, target TEXT NOT NULL, post_id TEXT NOT NULL DEFAULT '',
    category TEXT NOT NULL DEFAULT '', file TEXT NOT NULL DEFAULT '',
    url TEXT NOT NULL DEFAULT '', class TEXT NOT NULL, message TEXT NOT NULL,
    hint TEXT NOT NULL DEFAULT '', attempts INTEGER NOT NULL DEFAULT 1, at TEXT NOT NULL,
    cursor TEXT
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
# The most files `save` writes in one statement; SQLite takes up to 32,766 values in one.
ONE_STATEMENT = 500
TARGET_COLUMNS = ("key", "source", "value", "kind", "follow", "options", "query", "complete",
                  "cursors", "seen", "last_sync", "files", "bytes", "heads", "gaps")
# The columns `FailureRow` reads, in its order.
FAILURE_SELECT = ("key, target, post_id, category, file, cursor, attempts, class, message, hint,"
                  " retryable, url, run, at")


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
    # Each `newest_first` listing's head: the newest post the last run that read it from the
    # top saw, where a later sync stops (`flnd.engine.job`); "" for a route's own listing.
    heads: dict[str, str] = {}
    # Each listing's gaps: where syncs that were cut short stopped, newest first, for the next
    # sync to jump to once it meets saved posts at the top. "" is a gap with no cursor (a feed
    # that isn't paged by cursor, or one cut on its first page): the next sync reads down to
    # the head instead.
    gaps: dict[str, list[str]] = {}


class FailureRow(msgspec.Struct):
    """One failure the home keeps, as `pending` and `failures` read it.

    Example:
        >>> row = FailureRow("ins:9001_0", "ins:example_user", "9001", "posts")
        >>> row.attempts, row.error
        (1, 'Error')
    """

    # The file's key, or the target's key for a failure of the whole target.
    key: str
    target: str
    # The post's id and category; "" for a failure of the whole target.
    post: str = ""
    category: str = ""
    # Where the file would have gone; "" for a failure of the whole target.
    file: str = ""
    # The cursor of the page the post was listed on; None for the first page, or a listing
    # that isn't paged by cursor.
    cursor: str | None = None
    # The runs that tried it and failed: 1 after the first.
    attempts: int = 1
    # The error's class name, message and hint.
    error: str = "Error"
    message: str = ""
    hint: str = ""
    retryable: bool = False
    # The file's URL, redacted (no query, no user info); "" for none.
    url: str = ""
    # The run it last failed in; None outside a recorded run.
    run: int | None = None
    # When it last failed, ISO 8601 in UTC.
    at: str = ""


class RunRow(msgspec.Struct):
    """One run that downloaded, as `last_run` reads it.

    Example:
        >>> RunRow(7, "sync", "2026-11-16T10:00:00+00:00").saved
        0
    """

    id: int
    # What ran: the command and its first targets (`download ins:example_user`).
    command: str
    started: str
    # When it ended; "" while it runs, or when it never ended (a crash).
    ended: str = ""
    saved: int = 0
    # Failures it left, counted when it ended.
    failed: int = 0


def key(target: Target) -> str:
    """A target's key in `targets` and `files.target`: `<code>:<value>`.

    Args:
        target: the target, resolved.

    Returns:
        Its key; a search's is its words (`ins:cats`), as the old `targets.db` keyed it.

    Example:
        >>> from flnd.source import Target
        >>> key(Target("ins:example_user", kind="user"))
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
        >>> target_folder(Path("home"), Target("ins:example_user", kind="user")).as_posix()
        'home/media/ins/example_user'
    """
    return home / MEDIA / target.source / text.filename(target.value)


def now() -> str:
    """The time now, ISO 8601 in UTC to the second, as every `at` column holds it."""
    return datetime.datetime.now(datetime.UTC).isoformat(timespec="seconds")


# ---------------------------------------------------------------------------------------------
# The database
# ---------------------------------------------------------------------------------------------

def open(home: Path) -> Home:
    """Open a home's `flnd.db`.

    A home without `flnd.db` is left untouched: the `Db` connects on its first write, and
    reads it as empty until then.

    Args:
        home: the home folder.

    Returns:
        The `Db`, to use in a `with` block.

    Raises:
        DiskError: `flnd.db` can't be read or written.

    Example:
        >>> import tempfile
        >>> with open(Path(tempfile.mkdtemp())) as db:
        ...     db.targets()
        {}
    """
    db = Home(home)
    try:
        if (home / DB).is_file():
            sql = db._store.connection()
            _require_version(sql)
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
        # The store: one connection, its tables made on first use.
        self._store = db.Db(self.path, schema=SCHEMA, setup=self._setup)

    def __enter__(self) -> Home:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def close(self) -> None:
        """Close the connection; closing twice is harmless."""
        self._store.close()

    def ready(self) -> Path:
        """Make `flnd.db` and its tables now, for a connection of another's (the save's), and
        return its path."""
        self._store.connection()
        return self.path

    def _setup(self, sql: sqlite3.Connection) -> None:
        """Set the connection up, and mark the schema's version.

        Args:
            sql: the new connection, its tables made, before anything else uses it.
        """
        # A checkpoint copies the log into the database and syncs it; on macOS that sync is a
        # full one, which costs milliseconds, so they come every `CHECKPOINT_PAGES` pages (a
        # 40 MB log) rather than SQLite's 1,000. A commit per post writes about 6 pages.
        sql.execute(f"PRAGMA wal_autocheckpoint = {CHECKPOINT_PAGES}")
        # A negative size is in KiB, not pages.
        sql.execute(f"PRAGMA cache_size = -{CACHE_KIB}")
        sql.execute("INSERT OR IGNORE INTO meta VALUES ('version', ?)", (VERSION,))
        _upgrade(sql)

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

    def post_saved(self, target: str, post: str, category: str) -> bool:
        """Whether a post of a target has saved files in a category: the read by post, not key.

        For a lazy post whose keys can't be made before its files are listed (a key that is the
        file's path), so a post saved before isn't listed again. One read of `files_post`.

        Args:
            target: the target's key.
            post: the post's id.
            category: the category it was saved from; "" for a route's own posts.

        Returns:
            True when at least one of its files is saved.
        """
        found = self._store.one("SELECT 1 FROM files WHERE target = ? AND post_id = ? AND"
                                " category = ? AND state = 'saved' LIMIT 1",
                                (target, post, category))
        return found is not None

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

        Used for what a step after a file did (unpack sets `unpacked` and `folder`; the sidecar
        step sets `sidecar`); compress, which is gone, set `shrink`, `tools`, `before` and `hash`.

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
             post: str = "", category: str = "", file: str = "", url: str = "",
             cursor: str | None = None) -> None:
        """Write one failure, and mark a file the site no longer has as gone.

        A file has one row, however often it fails: a later failure replaces the message and
        counts one more run in `attempts`, which the automatic retry reads (`RETRIES`). A file
        saved since starts again from 1. A `NotFound` (a 404 or 410) also records the file as
        `gone`, so later runs skip it until they are told to ask again (`--again`). Its own
        transaction: a failure is rare, and its row must outlive a run that stops right after.

        Args:
            run: the run it happened in; None outside one.
            item: the file's key, or the target's key for a failure of the whole target.
            target: the target's key.
            error: what failed; a `flnd.Error` gives its hint.
            post: the post's id; "" for a failure of the whole target.
            category: the category the post was listed in; "" for a route's own posts.
            file: where the file would have gone; "" for a failure of the whole target.
            url: the file's URL; only its redacted form is kept.
            cursor: the cursor of the page the post was listed on, where a retry starts
                listing; None for the first page.

        Raises:
            DiskError: the database can't be written.
        """
        from flnd.errors import NotFound

        at = now()
        with self._store.transaction() as sql:
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

    def clear(self, item: str) -> None:
        """Drop a failure's row: a target that failed whole, and has now run to its end."""
        self._store.write("DELETE FROM failures WHERE key = ?", (item,))

    def pending(self, target: str, *, every: bool = False) -> list[FailureRow]:
        """The files of a target that failed and may be retried: not saved or gone since.

        Args:
            target: the target's key.
            every: take every one, as `flnd retry` does; False takes only those failed on at
                most `RETRIES` runs, as the automatic retry does. A `NotFound` is never taken.

        Returns:
            The rows, in the order they first failed.
        """
        limit = "" if every else f" AND f.attempts <= {RETRIES}"
        found = self._store.all(f"SELECT {FAILURE_SELECT} FROM failures f WHERE f.target = ?"
                                " AND f.post_id != '' AND f.class != 'NotFound'" + limit +
                                " AND NOT EXISTS (SELECT 1 FROM files WHERE files.key = f.key)"
                                " ORDER BY f.rowid", (target,))
        return [FailureRow(*row) for row in found]

    def failed(self) -> dict[str, bool]:
        """The targets `flnd retry` takes: those with a failure that isn't gone or saved since.

        Returns:
            Each target's key, with whether the whole target failed (its listing, not only
            some files), in the order they first failed.
        """
        found = self._store.all("SELECT target, max(post_id = '') FROM failures f"
                                " WHERE f.class != 'NotFound' AND NOT EXISTS"
                                " (SELECT 1 FROM files WHERE files.key = f.key)"
                                " GROUP BY target ORDER BY min(f.rowid)")
        return {target: bool(whole) for target, whole in found}

    def failures(self, run: int) -> list[FailureRow]:
        """The failures a run left: what `flnd status` shows.

        Args:
            run: the run's id.

        Returns:
            Its rows that still stand (a file saved since is left out), in the order they
            first failed.
        """
        found = self._store.all(f"SELECT {FAILURE_SELECT} FROM failures f WHERE f.run = ?"
                                " AND NOT EXISTS (SELECT 1 FROM files WHERE files.key = f.key"
                                " AND files.state = 'saved') ORDER BY f.rowid", (run,))
        return [FailureRow(*row) for row in found]

    def last_run(self) -> RunRow | None:
        """The latest run that downloaded; None when the home has none."""
        found = self._store.one("SELECT id, command, started, coalesce(ended, ''), saved, failed"
                                " FROM runs ORDER BY id DESC LIMIT 1", ())
        return RunRow(*found) if found else None

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
            keep: whether to save each target's categories for later syncs (`follow`);
                otherwise the saved ones stay.
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
                 complete: Sequence[str], heads: Mapping[str, str] | None = None,
                 gaps: Mapping[str, Sequence[str]] | None = None) -> None:
        """Keep where a target's listings stopped, after its run.

        Args:
            target: the target.
            cursors: each category's cursor to resume from; None drops it (listed to the end).
            complete: the categories listed to their end today.
            heads: each listing's newest post, for the listings this run read from the top;
                None or empty keeps the heads the row has.
            gaps: each listing's gaps after this run, newest first; an empty list drops them.
                A listing left out keeps the gaps it has.
        """
        if not cursors and not complete and not heads and not gaps:
            return
        today = datetime.datetime.fromtimestamp(clock.wall(), datetime.UTC).date().isoformat()
        with self._store.transaction():
            # A target nothing recorded (a run of the engine's own, a test) follows, as a row of
            # an old home without a `sync` field did.
            row = self.target(target) or TargetRow(key(target), target.source, target.value,
                                                   target.kind, follow=True)
            merged = {**row.cursors, **cursors}
            row.cursors = {name: value for name, value in merged.items() if value}
            row.complete = {**row.complete, **dict.fromkeys(complete, today)}
            row.heads = {**row.heads, **(heads or {})}
            kept = {**row.gaps, **{name: list(found) for name, found in (gaps or {}).items()}}
            row.gaps = {name: found for name, found in kept.items() if found}
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
        self._store.write(f"INSERT OR REPLACE INTO targets ({', '.join(TARGET_COLUMNS)})"
                    f" VALUES ({', '.join('?' * len(TARGET_COLUMNS))})",
                    (row.key, row.source, row.value, row.kind, int(row.follow), _json(options),
                     row.query, _json(row.complete), _json(row.cursors), _json(row.seen),
                     row.last_sync, row.files, row.bytes, _json(row.heads),
                     _json(row.gaps)))

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


def _require_version(sql: sqlite3.Connection) -> None:
    """Refuse a home whose schema version in `meta` is missing or not today's.

    Args:
        sql: an open `flnd.db`.

    Raises:
        UsageError: the version is absent on a non-empty home, or not `VERSION`.
    """
    from flnd.errors import UsageError

    row = sql.execute("SELECT value FROM meta WHERE key = 'version'").fetchone()
    if row is not None and row[0] == VERSION:
        return
    if row is not None and row[0] != VERSION:
        raise UsageError(f"home schema {row[0]!r} needs a newer flnd",
                         hint="upgrade flnd, or open the home with the flnd that wrote it")
    tables = sql.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
    if "files" in {name for (name,) in tables}:
        count = sql.execute("SELECT count(*) FROM files").fetchone()
        if count and count[0]:
            raise UsageError("home has no schema version",
                             hint="upgrade flnd, or open the home with the flnd that wrote it")


def _upgrade(sql: sqlite3.Connection) -> None:
    """Bring the tables of a `flnd.db` an earlier flnd made up to today's columns.

    1. `targets` gains `heads` and `gaps`, empty, when it lacks them.
    2. `failures` gains `category` and `cursor`. A table that still has `request` (each
       failure's URL and headers, kept for a retry before retries listed the post again)
       keeps only each key's latest row, and loses the column: an unredacted URL never stays
       in the database.
    3. `failures` is keyed by its file: one row per key, however often it fails.

    Two runs may open an old database at once, so a column the other one added first is not an
    error.

    Args:
        sql: the connection, its tables made, in `Home._setup`.
    """
    def add(table: str, column: str, kind: str) -> None:
        try:
            sql.execute(f"ALTER TABLE {table} ADD COLUMN {column} {kind}")
        except sqlite3.OperationalError as error:
            if "duplicate column" not in str(error):
                raise

    targets = {row[1] for row in sql.execute("PRAGMA table_info(targets)")}
    for column in ("heads", "gaps"):
        if column not in targets:
            add("targets", column, "TEXT NOT NULL DEFAULT '{}'")
    failures = {row[1] for row in sql.execute("PRAGMA table_info(failures)")}
    if "category" not in failures:
        add("failures", "category", "TEXT NOT NULL DEFAULT ''")
    if "cursor" not in failures:
        add("failures", "cursor", "TEXT")
    if "retryable" not in failures:
        add("failures", "retryable", "INTEGER NOT NULL DEFAULT 0")
    if "request" in failures:
        sql.execute("DELETE FROM failures WHERE rowid NOT IN"
                    " (SELECT max(rowid) FROM failures GROUP BY key)")
        try:
            sql.execute("ALTER TABLE failures DROP COLUMN request")
        except sqlite3.OperationalError:
            # Another run dropped it first, or an SQLite older than 3.35 can't: the values
            # go either way.
            if "request" in {row[1] for row in sql.execute("PRAGMA table_info(failures)")}:
                sql.execute("UPDATE failures SET request = NULL")
    sql.execute("CREATE UNIQUE INDEX IF NOT EXISTS failures_key ON failures (key)")


# ---------------------------------------------------------------------------------------------
# Logs and samples
# ---------------------------------------------------------------------------------------------

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
    from flnd.errors import Changed

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
     files, size, heads, gaps) = found
    settings = json.loads(options)
    only = settings.get("only")
    return TargetRow(item, source, value, kind, bool(follow), tuple(only) if only else None,
                     query, json.loads(complete), json.loads(cursors), json.loads(seen),
                     last_sync, files, size, heads=json.loads(heads), gaps=json.loads(gaps))


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

# Browsers `from_browser` reads cookies from, by `flnkit.cookies` name with display name.
BROWSERS = {
    "chrome": "Chrome", "firefox": "Firefox", "edge": "Edge",
    "brave": "Brave", "chromium": "Chromium", "safari": "Safari",
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


def from_browser(source: Source, *, browser: str, profile: str | None = None) -> cookies.Jar:
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
        return _own(source, cookies.load(browser, profile=profile))
    # Safari off macOS: a usage error as it is.
    except UsageError:
        raise
    # Every way reading a browser fails: missing file, platform refusal, bad decrypt, no crypto.
    # An error with its own advice (Safari's Full Disk Access) keeps it.
    except (OSError, RuntimeError, ValueError, ImportError, Error) as error:
        hint = getattr(error, "hint", "") or "try a cookie file instead"
        raise LoginError(
            f"{source.code}: can't read {BROWSERS.get(browser, browser)}'s cookies: {error}",
            hint=hint) from None


def from_file(source: Source, path: Path) -> cookies.Jar:
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
        return _own(source, cookies.load(path))
    except (OSError, ValueError, Error) as error:
        raise LoginError(f"{source.code}: can't read {path}: {error}") from None


def store(source: Source, jar: cookies.Jar, *, home: Path, origin: str) -> str:
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
        LoginError: none of the cookies the source's login names is in the jar.
        DiskError: the key file is damaged, or the vault can't be written.
    """
    names = _login_names(source)
    if names and not any(name in jar for name in names):
        raise LoginError(
            f"{source.code}: no {' or '.join(names)} for {source.domains[0]} in {origin}; "
            "log in there first"
        )
    save(home, source.code, jar, origin=origin)
    for path in _cookie_files(home, source.code):
        path.unlink(missing_ok=True)
    _remove_if_empty(home / COOKIE_FOLDER)
    return f"{source.code}: logged in from {origin} ({text.count(len(jar), 'cookie')})"


def save(home: Path, code: str, jar: cookies.Jar, *, origin: str | None = None) -> None:
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
    """Login cookies (the names its `login` gives) that expire soon, to warn before the wall.

    Args:
        home: the home folder.
        source: the source; only the cookies its login names count.
        within: how soon, in seconds; a day by default.

    Returns:
        Each such cookie's name and its seconds left (negative once expired).
    """
    names = _login_names(source)
    if not names or not logged_in(home, source.code):
        return []
    now = time.time()
    found: list[tuple[str, float]] = []
    for cookie in load(home, source.code):
        expires = cookie.get("expires", -1)
        if cookie.get("name") in names and isinstance(expires, (int, float)) and expires > 0:
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
            save(home, code, cookies.Jar(saved, error=LoginError))
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


def load(home: Path, code: str) -> cookies.Jar:
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
        return cookies.Jar()
    with secrets(home, code) as vault:
        text = vault.get(COOKIES)
    return fix_domains(cookies.Jar(json.loads(text), error=LoginError)) if text else cookies.Jar()


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
    """Whether the home has a vault in `metadata/secret.db`."""
    return (home / SECRETS).is_file()


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
            jar = cookies.load(path)
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


def fix_domains(jar: cookies.Jar) -> cookies.Jar:
    """Give every domain a leading dot, and every path `/` when it has none.

    Args:
        jar: the cookies, as read or saved.

    Returns:
        A new jar; the one given is unchanged.
    """
    # Exported files often hold host-only domains; the dot lets every subdomain see the cookie.
    return cookies.Jar(
        ({
            **cookie,
            "domain": "." + cookie.get("domain", "").lstrip("."),
            "path": cookie.get("path") or "/",
        }
        for cookie in jar),
        error=LoginError,
    )


def _own(source: Source, jar: cookies.Jar) -> cookies.Jar:
    """The cookies of `jar` for the source's domains and their subdomains, domain by domain."""
    return jar.filter(domains=source.domains)




def secrets(home: Path, code: str) -> vault.Vault:
    """The secrets of one source, as a mapping of `str` to `str`, for a `with` block.

    Args:
        home: the home folder; `metadata/secret.db` is made in it on the first write.
        code: the source's code; another source's secrets aren't visible.

    Returns:
        The `flnkit.vault.Vault`; leaving the `with` closes it.

    Example:
        >>> import tempfile
        >>> with secrets(Path(tempfile.mkdtemp()), "tlg") as saved:
        ...     dict(saved)
        {}
    """
    return vault.Vault(home / SECRETS, code, hint=LOGIN_HINT)


def _login_names(source: Source) -> tuple[str, ...]:
    """The cookies that prove a source's login (`cookies(...)`'s names); none for other logins."""
    return source.login.names if source.login is not None else ()
