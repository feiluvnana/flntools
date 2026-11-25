"""src/engine/store.py: `flnd.db` schema 3, the migrations of a version-1 and a version-2 home,
its pragmas, the busy signal, and `check`.
"""

import datetime
import io
import os
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from plugin_kit import fake_clock

from flnd.engine import store
from flnd.errors import Changed, DiskError, NetworkError, NotFound, UsageError
from flnd.plugin import Target
from flnd.ui.log import ui

# Version 1 as a home made it: its schema text, then what its `_upgrade` added.
V1 = """
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
ALTER TABLE failures ADD COLUMN retryable INTEGER NOT NULL DEFAULT 0;
CREATE UNIQUE INDEX IF NOT EXISTS failures_key ON failures (key);
INSERT INTO meta VALUES ('version', '1');
INSERT INTO files (key, target, source, post_id, category, path, size, mtime, date, estimated,
                   kind, files, hash, state, shrink, before, format, tools, at) VALUES
    ('ins:1_0', 'ins:example_user', 'ins', '1', 'posts', 'media/ins/example_user/1_0.jpg', 10,
     5, '2026-01-02', 0, 'image', 2, x'aa', 'saved', 'compressed', 20, 'jpg', 'jpegli',
     '2026-01-02T00:00:00+00:00'),
    ('ins:1_1', 'ins:example_user', 'ins', '1', 'posts', 'media/ins/example_user/1_1.mp4', 30,
     6, NULL, 1, 'video', NULL, NULL, 'saved', '', NULL, NULL, NULL, '2026-01-03T00:00:00+00:00'),
    ('ins:1_0@reels', 'ins:example_user', 'ins', '1', 'reels', 'media/ins/example_user/r.jpg',
     10, 5, '2026-01-02', 0, 'image', NULL, x'aa', 'link', '', NULL, NULL, NULL, ''),
    ('twt:9', 'twt:example_creator', 'twt', '9', '', 'media/twt/example_creator/9.png', 0,
     NULL, NULL, 0, 'image', NULL, NULL, 'gone', '', NULL, NULL, NULL, '');
INSERT INTO targets (key, source, value, kind, follow, options, query, complete, cursors, seen,
                     last_sync, files, bytes, heads, gaps) VALUES
    ('ins:example_user', 'ins', 'example_user', 'user', 1, '{"only": ["posts", "reels"]}',
     NULL, '{"posts": "2026-01-02"}', '{"reels": "c1"}', '[]', '2026-01-03', 2, 40,
     '{"posts": "1"}', '{"reels": ["c0", ""]}'),
    ('ins:cats', 'ins', 'cats', 'query', 1, '{"categories": ["reels"]}', 'cats', '{}', '{}',
     '["ins:example_user"]', '', 0, 0, '{}', '{}'),
    ('twt:example_creator', 'twt', 'example_creator', 'user', 0, '{}', NULL, '{}', '{}', '[]',
     '', 0, 0, '{}', '{}'),
    ('tik:example_handle', 'tik', 'example_handle', 'user', 0, 'not json', NULL, '{}', '{}',
     '[]', '', 0, 0, '{}', '{}');
INSERT INTO runs VALUES (4, 'sync', '2026-01-03T00:00:00+00:00', NULL, 2, 1);
INSERT INTO failures (run, key, target, post_id, category, file, url, class, message, hint,
                      attempts, at, cursor, retryable) VALUES
    (4, 'ins:2_0', 'ins:example_user', '2', 'posts', 'media/ins/example_user/2_0.jpg',
     'https://example.com/2.jpg', 'NetworkError', 'reset', '', 2, '2026-01-03', 'c1', 1),
    (4, 'ins:example_user', 'ins:example_user', '', '', '', '', 'Blocked', 'wait', 'later', 1,
     '2026-01-03', NULL, 0);
INSERT INTO state VALUES ('tlg', 'offset', '12'), ('ins', 'pace', '{"s": 2}');
INSERT INTO logins VALUES ('ins', 'Chrome', '2026-01-01T00:00:00+00:00'),
                          ('twt', 'example_cookies.txt', '2026-01-01T00:00:00+00:00'),
                          ('tlg', 'qr', '2026-01-01T00:00:00+00:00');
INSERT INTO probes VALUES ('media/ins/example_user/1_1.mp4', 30, 6, '{}');
"""


# Version 2 as a home made it: its schema text, with `accounts` and `targets.account`.
V2_SCHEMA = """
CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL) WITHOUT ROWID;  -- version = '2'
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
    heads TEXT NOT NULL DEFAULT '{}', gaps TEXT NOT NULL DEFAULT '{}', account TEXT,
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
CREATE TABLE accounts (
    id INTEGER PRIMARY KEY, plugin TEXT NOT NULL, name TEXT NOT NULL,
    origin TEXT NOT NULL,                           -- browser | chrome | file | … | qr | phone
    at TEXT NOT NULL, is_default INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL DEFAULT 'ok',              -- ok | held | expired
    held_until TEXT, expires TEXT, UNIQUE (plugin, name)
);
"""


def schema(path):
    """A database file's schema, as SQLite keeps it."""
    with sqlite3.connect(path) as sql:
        found = sorted(sql.execute("SELECT type, name, tbl_name, sql FROM sqlite_master"))
    sql.close()
    return found


def columns(path):
    """Each table's column names, and the names of the other objects, as SQLite keeps them."""
    with sqlite3.connect(path) as sql:
        found = {name: [row[1] for row in sql.execute(f"PRAGMA table_info({name})")]
                 for (name,) in sql.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
        found["."] = sorted(name for (name,) in sql.execute(
            "SELECT name FROM sqlite_master WHERE type != 'table'"))
    sql.close()
    return found


def rows(path, query):
    with sqlite3.connect(path) as sql:
        found = sql.execute(query).fetchall()
    sql.close()
    return found


class StoreCase(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.home = Path(folder.name)
        self.path = self.home / store.DB
        # What the store says (a warning), kept off the terminal.
        self.said = io.StringIO()
        self.enterContext(ui.scope(dest=self.said, width=200, color=False))

    def open(self, **how):
        opened = store.open(self.home, **how)
        self.addCleanup(opened.close)
        return opened

    def v1(self, slots=("ins", "loi")):
        """A version-1 home, with a `secret.db` holding a slot per code of `slots`."""
        self.path.parent.mkdir(parents=True)
        with sqlite3.connect(self.path) as sql:
            sql.executescript(V1)
        sql.close()
        if not slots:
            return
        with sqlite3.connect(self.home / store.SECRETS) as sql:
            sql.execute("CREATE TABLE secrets (slot TEXT NOT NULL, name TEXT NOT NULL,"
                        " value TEXT NOT NULL, PRIMARY KEY (slot, name)) WITHOUT ROWID")
            sql.executemany("INSERT INTO secrets VALUES (?, 'cookies', '[]')",
                            [(slot,) for slot in slots])
        sql.close()


class MigrationTest(StoreCase):
    def test_every_row_of_a_version_1_home_moves_to_schema_3(self):
        self.v1()
        with self.open() as opened:
            self.assertEqual(rows(self.path, "SELECT * FROM meta ORDER BY key"),
                             [("keys", "2"), ("logins", "told"), ("version", "3")])
            files = {row.key: row for row in opened.on_disk()}
            self.assertEqual(sorted(files), ["ins:1_0", "ins:1_0@reels", "ins:1_1"])
            self.assertEqual((files["ins:1_0"].plugin, files["ins:1_0"].hash,
                              files["ins:1_0"].files, files["ins:1_1"].estimated),
                             ("ins", b"\xaa", 2, True))
            self.assertEqual(opened.known("twt:9"),
                             ("media/twt/example_creator/9.png", "gone", None))
            self.assertEqual(rows(self.path, "SELECT plugin, count(*) FROM files GROUP BY 1"),
                             [("ins", 3), ("twt", 1)])
            self.assertEqual(rows(self.path, "SELECT post_id, files FROM posts"), [("1", 2)])
            targets = opened.targets()
            self.assertEqual({item: row.categories for item, row in targets.items()},
                             {"ins:example_user": ("posts", "reels"), "ins:cats": ("reels",),
                              "twt:example_creator": (), "tik:example_handle": ()})
            user = targets["ins:example_user"]
            self.assertEqual((user.plugin, user.follow, user.complete, user.cursors, user.heads,
                              user.gaps, user.files, user.bytes),
                             ("ins", True, {"posts": "2026-01-02"}, {"reels": "c1"},
                              {"posts": "1"}, {"reels": ["c0", ""]}, 2, 40))
            self.assertEqual((targets["ins:cats"].query, targets["ins:cats"].seen),
                             ("cats", ["ins:example_user"]))
            self.assertEqual(opened.last_run(),
                             store.RunRow(4, "sync", "2026-01-03T00:00:00+00:00", "", 2, 1))
            self.assertEqual([(row.key, row.cursor, row.attempts, row.retryable)
                              for row in opened.failures(4)],
                             [("ins:2_0", "c1", 2, True), ("ins:example_user", None, 1, False)])
            self.assertEqual(opened.failed(), {"ins:example_user": True})
            self.assertEqual((dict(opened.state("tlg")), dict(opened.state("ins"))),
                             ({"offset": 12}, {"pace": {"s": 2}}))
            self.assertNotIn("accounts", {name for _, name, *_ in schema(self.path)})
        with store.open(self.home / "fresh") as fresh:
            fresh.state("x")["y"] = 1
        self.assertEqual(schema(self.path), schema(fresh.path))

    def test_a_version_1_home_is_kept_whole_beside_it_once(self):
        self.v1(slots=())
        before = rows(self.path, "SELECT * FROM files ORDER BY key")
        self.open().close()
        backup = self.home / store.BACKUP
        self.assertEqual((rows(backup, "SELECT * FROM meta"),
                          rows(backup, "SELECT * FROM files ORDER BY key")),
                         ([("version", "1")], before))
        made = backup.stat().st_mtime_ns
        self.open().close()
        self.assertEqual(backup.stat().st_mtime_ns, made)

    def test_a_crash_in_the_migration_leaves_a_home_that_opens_and_migrates(self):
        self.v1()
        code = ("import os, sys\n"
                "from pathlib import Path\n"
                "from unittest import mock\n"
                "from flnd.engine import store\n"
                "with mock.patch.object(store, '_copy_targets', lambda sql: os._exit(9)):\n"
                "    store.open(Path(sys.argv[1]))\n")
        done = subprocess.run([sys.executable, "-c", code, str(self.home)], check=False)
        self.assertEqual(done.returncode, 9)
        self.assertEqual(rows(self.path, "SELECT * FROM meta"), [("version", "1")])
        with self.open() as opened:
            self.assertEqual((rows(self.path, "SELECT * FROM meta ORDER BY key"),
                              len(opened.on_disk()), len(opened.targets())),
                             ([("keys", "2"), ("logins", "told"), ("version", "3")], 3, 4))

    def test_a_failure_from_before_retryable_was_recorded_is_not_retryable(self):
        self.v1(slots=())
        with sqlite3.connect(self.path) as sql:
            sql.execute("ALTER TABLE failures DROP COLUMN retryable")
        sql.close()
        self.assertEqual([row.retryable for row in self.open().failures(4)], [False, False])

    def test_a_key_that_failed_twice_keeps_its_latest_failure(self):
        self.v1(slots=())
        with sqlite3.connect(self.path) as sql:
            sql.executescript(
                "DROP INDEX failures_key; INSERT INTO failures (run, key, target, class, message,"
                " at) VALUES (4, 'ins:3_0', 'ins:example_user', 'NetworkError', 'first', 'a'),"
                " (4, 'ins:3_0', 'ins:example_user', 'NetworkError', 'latest', 'b')")
        sql.close()
        found = [row.message for row in self.open().failures(4) if row.key == "ins:3_0"]
        self.assertEqual(found, ["latest"])

    def test_a_second_open_changes_nothing(self):
        self.v1()
        self.open().close()
        before = (schema(self.path), rows(self.path, "SELECT * FROM files ORDER BY key"),
                  rows(self.path, "SELECT * FROM meta ORDER BY key"))
        self.open().close()
        self.assertEqual((schema(self.path), rows(self.path, "SELECT * FROM files ORDER BY key"),
                          rows(self.path, "SELECT * FROM meta ORDER BY key")), before)

    def v2(self, secrets=True):
        """A version-2 home, made by the old schema text, with an account and a target that
        remembers it; and a sealed `secret.db` of the vault."""
        self.path.parent.mkdir(parents=True)
        with sqlite3.connect(self.path) as sql:
            sql.executescript(V2_SCHEMA)
            sql.executescript(
                "INSERT INTO meta VALUES ('version', '2'), ('keys', '2');"
                "INSERT INTO targets (key, plugin, value, kind, follow, account) VALUES"
                " ('ins:example_user', 'ins', 'example_user', 'user', 1, 'example_handle');"
                "INSERT INTO files (key, target, plugin, post_id, category, path, size) VALUES"
                " ('ins:1', 'ins:example_user', 'ins', '1', 'posts', 'media/ins/1.jpg', 10);"
                "INSERT INTO accounts (plugin, name, origin, at, is_default) VALUES"
                " ('ins', 'example_handle', 'file', '2026-01-01', 1);")
        sql.close()
        if secrets:
            (self.home / store.SECRETS).write_bytes(b"sealed")

    def test_a_version_2_home_loses_its_accounts_and_keeps_the_rest(self):
        self.v2()
        before = rows(self.path, "SELECT * FROM files")
        with self.open() as opened:
            self.assertEqual(rows(self.path, "SELECT key, value FROM meta ORDER BY key"),
                             [("keys", "2"), ("logins", "told"), ("version", "3")])
            target = opened.targets()["ins:example_user"]
            self.assertEqual((target.plugin, target.follow), ("ins", True))
            self.assertEqual(rows(self.path, "SELECT * FROM files"), before)
        names = {name for _, name, *_ in schema(self.path)}
        self.assertNotIn("accounts", names)
        self.assertNotIn("account", [row[1] for row in rows(
            self.path, "PRAGMA table_info(targets)")])
        with store.open(self.home / "fresh") as fresh:
            fresh.state("x")["y"] = 1
        self.assertEqual(columns(self.path), columns(fresh.path))
        # The old file is kept whole beside it, once; the old vault is left alone.
        backup = self.home / store.BACKUP_V2
        self.assertEqual(rows(backup, "SELECT plugin, name FROM accounts"),
                         [("ins", "example_handle")])
        self.assertEqual(rows(backup, "SELECT value FROM meta WHERE key = 'version'"), [("2",)])
        self.assertEqual((self.home / store.SECRETS).read_bytes(), b"sealed")

    def test_an_index_on_the_account_column_is_dropped_first(self):
        self.v2(secrets=False)
        with sqlite3.connect(self.path) as sql:
            sql.execute("CREATE INDEX targets_account ON targets (account)")
        sql.close()
        self.open().close()
        self.assertNotIn("targets_account", {name for _, name, *_ in schema(self.path)})

    def test_a_home_with_a_vault_is_told_once_how_to_log_in_again(self):
        self.v2()
        self.open().close()
        self.open().close()
        self.assertEqual(self.said.getvalue().count("logins are cookie files now"), 1)
        self.assertIn("flnd auth login <code> <file>", self.said.getvalue())

    def test_a_home_without_a_vault_is_told_nothing(self):
        self.v2(secrets=False)
        self.open().close()
        self.assertEqual(self.said.getvalue(), "")
        self.assertEqual(rows(self.path, "SELECT key FROM meta ORDER BY key"),
                         [("keys",), ("version",)])

    def test_a_home_flnd_cannot_read_is_refused(self):
        cases = {"newer": "INSERT INTO meta VALUES ('version', '4')",
                 "unversioned": "DELETE FROM meta; INSERT INTO files (key, target, source, path)"
                                " VALUES ('a:1', 'a:b', 'a', 'p')"}
        for name, change in cases.items():
            with self.subTest(name):
                self.v1(slots=())
                with sqlite3.connect(self.path) as sql:
                    sql.executescript("DELETE FROM files; DELETE FROM meta;" + change)
                sql.close()
                with self.assertRaises(UsageError):
                    store.open(self.home)
                self.path.unlink()
                self.path.parent.rmdir()


class PragmaTest(StoreCase):
    def test_the_loop_waits_for_no_lock_and_never_checkpoints_by_itself(self):
        expected = {True: {"busy_timeout": 0, "wal_autocheckpoint": 0},
                    False: {"busy_timeout": 30_000, "wal_autocheckpoint": 10_000}}
        for loop, own in expected.items():
            with self.subTest(loop=loop), self.open(loop=loop) as opened:
                opened.state("x")["y"] = 1
                sql = opened._db.connection()
                found = {name: sql.execute(f"PRAGMA {name}").fetchone()[0]
                         for name in [*own, "journal_mode", "synchronous", "cache_size",
                                      "fullfsync"]}
                self.assertEqual(found, {**own, "journal_mode": "wal", "synchronous": 1,
                                         "cache_size": -4096,
                                         "fullfsync": int(sys.platform == "darwin")})

    def test_the_checkpoint_connection_syncs_as_the_store_does(self):
        from flnkit import db

        opened, seen, real = self.open(loop=True), {}, db.connect
        opened.state("x")["y"] = 1

        def spy(path: Path) -> sqlite3.Connection:
            conn = real(path)
            for name in ("journal_mode", "synchronous", "fullfsync", "checkpoint_fullfsync"):
                seen[name] = conn.execute(f"PRAGMA {name}").fetchone()[0]
            return conn

        with mock.patch("flnkit.db.connect", side_effect=spy):
            opened.checkpoint(force=True)
        darwin = int(sys.platform == "darwin")
        self.assertEqual((seen, opened._checkpointer),
                         ({"journal_mode": "wal", "synchronous": 1, "fullfsync": darwin,
                           "checkpoint_fullfsync": darwin}, None))

    def test_a_failed_checkpoint_still_closes(self):
        opened = self.open(loop=True)
        opened.state("x")["y"] = 1
        with mock.patch.object(opened, "checkpoint", side_effect=DiskError("full")), \
                self.assertRaises(DiskError):
            opened.close()
        with self.assertRaises(RuntimeError):
            opened.known("a:1")

    def test_a_commit_while_another_holds_the_lock_is_busy_and_writes_nothing(self):
        opened = self.open(loop=True)
        opened.state("x")["y"] = 1
        other = sqlite3.connect(self.path, isolation_level=None)
        self.addCleanup(other.close)
        other.execute("BEGIN IMMEDIATE")
        with self.assertRaises(DiskError) as raised:
            opened.save([store.FileRow("a:1", "a:b", "p")])
        self.assertTrue(store.busy(raised.exception))
        codes = {"BUSY": (5, True), "LOCKED": (6, True), "BUSY_RECOVERY": (261, True),
                 "BUSY_SNAPSHOT": (517, True), "LOCKED_SHAREDCACHE": (262, True),
                 "FULL": (13, False), "IOERR_WRITE": (778, False)}
        for name, (code, expected) in codes.items():
            with self.subTest(name):
                cause = sqlite3.OperationalError(name)
                cause.sqlite_errorcode = code
                error = DiskError(name)
                error.__cause__ = cause
                self.assertEqual(store.busy(error), expected)
        self.assertFalse(store.busy(DiskError("full")))
        other.execute("ROLLBACK")
        self.assertIsNone(opened.known("a:1"))

    def test_a_checkpoint_is_due_after_1000_committed_rows(self):
        opened = self.open(loop=True)
        opened.save([store.FileRow(f"a:{n}", "a:b", f"p{n}") for n in range(999)])
        self.assertFalse(opened.due)
        opened.save([store.FileRow("a:x", "a:b", "px")])
        self.assertTrue(opened.due)
        opened.checkpoint()
        self.assertFalse(opened.due)
        self.assertEqual(rows(self.path, "SELECT count(*) FROM files"), [(1000,)])


class RowsTest(StoreCase):
    def test_dedupe_finds_saved_files_of_the_same_size(self):
        opened = self.open()
        opened.save([store.FileRow("a:1", "a:b", "p1", 5), store.FileRow("a:2", "a:b", "p2", 5),
                     store.FileRow("a:3", "a:b", "p3", 6)])
        opened.fail(run=None, item="a:4", target="a:b", error=NotFound("gone"), file="p4")
        self.assertEqual([row.key for row in opened.twins(besides="a:1", size=5)], ["a:2"])
        self.assertEqual(opened.twins(besides="a:1", size=0), [])
        for wrong in ({}, {"size": 5, "digest": b"x"}):
            with self.subTest(wrong=wrong), self.assertRaises(ValueError):
                opened.twins(besides="a:1", **wrong)

    def test_a_target_keeps_its_categories(self):
        opened = self.open()
        user = SimpleNamespace(plugin="ins", value="example_user", kind="user")
        opened.record(user, categories=["posts"])
        opened.record(user)
        self.assertEqual(opened.target("ins:example_user").categories, ("posts",))
        opened.record(user, categories=["all"])
        row = opened.target("ins:example_user")
        self.assertEqual((row.categories, row.follow), ((), True))


USER = SimpleNamespace(plugin="aaa", value="example_user", kind="user")


def row(item, post="p1", **fields):
    """A saved file's row of `aaa:example_user`."""
    return store.FileRow(item, "aaa:example_user", f"/m/{item}.jpg", 10, post_id=post,
                         category="posts", at="2026-01-02T00:00:00+00:00", **fields)


class FilesTest(StoreCase):
    def test_reading_a_new_home_creates_nothing(self):
        with store.open(self.home / "new") as opened:
            self.assertIsNone(opened.known("aaa:x"))
            self.assertEqual((opened.targets(), opened.records(),
                              opened.pending("aaa:example_user"), dict(opened.state("aaa"))),
                             ({}, [], [], {}))
        self.assertFalse((self.home / "new").exists())

    def test_wal_with_normal_sync_and_full_syncs_on_macos(self):
        opened = self.open()
        opened.save([row("aaa:1")])
        sql = opened._db.connection()
        self.assertEqual([sql.execute(f"PRAGMA {name}").fetchone()[0]
                          for name in ("journal_mode", "synchronous", "fullfsync")],
                         ["wal", 1, int(sys.platform == "darwin")])

    def test_a_posts_files_commit_once(self):
        opened = self.open()
        opened.save([row("aaa:0")])
        statements = []
        opened._db.connection().set_trace_callback(statements.append)
        opened.save([row("aaa:1", files=3), row("aaa:2"), row("aaa:3")])
        # One statement for the post: SQLite makes it one transaction, so one commit.
        self.assertEqual(len(statements), 1)
        self.assertTrue(statements[0].startswith("INSERT OR REPLACE INTO files"))
        self.assertEqual(opened.known("aaa:2"), ("/m/aaa:2.jpg", "saved", None))
        self.assertEqual([opened.files_of_post(item) for item in ("aaa:1", "aaa:2", "aaa:9")],
                         [3, None, None])

    def test_the_day_a_row_holds_and_redating_it(self):
        opened = self.open()
        opened.save([row("aaa:1", estimated=True)])
        self.assertIsNone(opened.day("aaa:1"))
        self.assertEqual([found.key for found in opened.undated("aaa:example_user")], ["aaa:1"])
        path = self.home / "a.jpg"
        path.write_bytes(b"12345")
        opened.redate("aaa:1", path, datetime.date(2026, 1, 4))
        self.assertEqual(opened.day("aaa:1"), datetime.date(2026, 1, 4))
        self.assertEqual(opened.undated("aaa:example_user"), [])
        [saved] = opened.records()
        self.assertEqual((saved.path, saved.size, saved.estimated, saved.day),
                         (str(path), 5, False, datetime.date(2026, 1, 4)))

    def test_records_filter_by_target_and_kind_and_100k_rows_read_fast(self):
        opened = self.open()
        opened.save([row("aaa:1", kind="image"), row("aaa:2", post="p2", kind="video")])
        opened.save([store.FileRow("bbb:1", "bbb:other", "/m/b.jpg", kind="image")])
        self.assertEqual([found.key for found in opened.records(target="aaa:example_user")],
                         ["aaa:1", "aaa:2"])
        self.assertEqual(sorted(found.key for found in opened.records(kind="image")),
                         ["aaa:1", "bbb:1"])
        rows = [store.FileRow(f"ccc:{n:06d}", "ccc:example_user", f"/m/{n}.jpg", n,
                              post_id=str(n)) for n in range(100_000)]
        for start in range(0, len(rows), 500):
            opened.save(rows[start:start + 500])
        self.assertEqual(opened.known("ccc:099999"), ("/m/99999.jpg", "saved", None))
        self.assertEqual(opened.count("ccc:example_user"), (100_000, sum(range(100_000))))

    def test_a_step_updates_its_columns_only(self):
        opened = self.open()
        opened.save([row("aaa:1")])
        opened.update("aaa:1", unpacked="extracted", folder="/m/set")
        [saved] = opened.records()
        self.assertEqual((saved.unpacked, saved.folder), ("extracted", "/m/set"))
        with self.assertRaises(ValueError):
            opened.update("aaa:1", target="aaa:other")


class FailuresTest(StoreCase):
    def failure(self, opened, item="aaa:1", error=None, run=None, **fields):
        fields = {"post": "p1", "category": "posts", "file": f"/m/{item}.jpg", **fields}
        opened.fail(run=run, item=item, target="aaa:example_user",
                    error=error or NetworkError("reset"), **fields)

    def test_a_not_found_file_is_gone_and_never_pending(self):
        opened = self.open()
        self.failure(opened, error=NotFound("HTTP 404"), file="/m/1.jpg",
                     url="https://cdn.example/1.jpg?sig=secret")
        self.assertEqual(opened.known("aaa:1"), ("/m/1.jpg", "gone", None))
        self.assertEqual((opened.pending("aaa:example_user"),
                          opened.pending("aaa:example_user", every=True), opened.failed(),
                          opened.records()), ([], [], {}, []))

    def test_a_failure_keeps_what_a_retry_lists_again_never_a_url_to_reuse(self):
        opened = self.open()
        run = opened.start("download aaa:example_user")
        self.failure(opened, run=run, url="https://user:pw@cdn.example/1.jpg?token=secret",
                     cursor="c4")
        opened.end(run, saved=0)
        [pending] = opened.pending("aaa:example_user")
        self.assertEqual((pending.key, pending.post, pending.category, pending.cursor,
                          pending.attempts, pending.error, pending.url),
                         ("aaa:1", "p1", "posts", "c4", 1, "NetworkError",
                          "https://cdn.example/1.jpg"))
        found = opened._db.all("SELECT * FROM failures", ())
        self.assertNotIn("secret", repr(found))
        self.assertEqual(opened._db.one("SELECT saved, failed FROM runs", ()), (0, 1))
        self.assertEqual(([row.key for row in opened.failures(run)], opened.last_run().command),
                         (["aaa:1"], "download aaa:example_user"))
        opened.save([row("aaa:1")])
        self.assertEqual((opened.pending("aaa:example_user"), opened.failures(run),
                          opened.failed()), ([], [], {}))

    def test_one_row_per_file_counting_the_runs_and_retried_on_three(self):
        opened = self.open()
        for attempt in range(1, 6):
            self.failure(opened, run=opened.start("sync"), error=NetworkError(f"reset {attempt}"))
            [found] = opened.pending("aaa:example_user", every=True)
            self.assertEqual((found.attempts, found.message), (attempt, f"reset {attempt}"))
        # The first failure and three retries: the automatic retry leaves it to a retry asked.
        self.assertEqual(opened.pending("aaa:example_user"), [])
        self.assertEqual(opened._db.one("SELECT count(*) FROM failures", ()), (1,))
        # A file saved since that fails again starts from 1.
        opened.save([row("aaa:1")])
        self.failure(opened)
        self.assertEqual(opened._db.one("SELECT attempts FROM failures", ()), (1,))

    def test_a_whole_target_failure_is_retried_whole_and_cleared(self):
        opened = self.open()
        opened.fail(run=None, item="aaa:example_user", target="aaa:example_user",
                    error=NetworkError("listing reset"))
        self.assertEqual((opened.pending("aaa:example_user"), opened.failed()),
                         ([], {"aaa:example_user": True}))
        opened.clear("aaa:example_user")
        self.assertEqual(opened.failed(), {})


class TargetsTest(StoreCase):
    def test_record_follow_progress_seen_and_tally(self):
        opened = self.open()
        opened.record(USER, follow=False, categories=("posts",))
        self.assertEqual((opened.target("aaa:example_user").categories,
                          opened.target("aaa:example_user").follow), (("posts",), False))
        # Recorded again without `follow`, it keeps its state; `follow` follows it.
        opened.record(USER)
        self.assertFalse(opened.target("aaa:example_user").follow)
        opened.follow(USER, True)
        opened.progress(USER, cursors={"posts": "c2"}, complete=[])
        opened.progress(USER, cursors={"posts": None}, complete=["posts"])
        opened.seen(USER, ["aaa:a", "aaa:b"])
        opened.seen(USER, ["aaa:b", "aaa:c"])
        opened.save([row("aaa:1"), row("aaa:2")])
        opened.tally(USER)
        saved = opened.targets()["aaa:example_user"]
        self.assertEqual((saved.follow, saved.categories, saved.cursors, list(saved.complete),
                          saved.seen, saved.files, saved.bytes),
                         (True, ("posts",), {}, ["posts"], ["aaa:a", "aaa:b", "aaa:c"], 2, 20))

    @unittest.skipUnless(hasattr(time, "tzset"), "needs time.tzset")
    def test_a_listing_is_stamped_complete_on_the_utc_day(self):
        with mock.patch.dict(os.environ, {"TZ": "EST5"}):
            time.tzset()
            self.addCleanup(time.tzset)
            opened = self.open()
            # 04:30 UTC on 2026-01-01 is 23:30 the day before at UTC-5.
            with fake_clock() as clock:
                clock.advance(4.5 * 3600)
                opened.progress(USER, cursors={}, complete=["posts"])
        self.assertEqual(opened.target("aaa:example_user").complete, {"posts": "2026-01-01"})

    def test_a_plugins_state_is_a_mapping_of_json_values(self):
        opened = self.open()
        state = opened.state("aaa")
        state["user"] = {"id": 42}
        state["asked"] = [1, 2]
        self.assertEqual((state["user"], sorted(state), len(state)),
                         ({"id": 42}, ["asked", "user"], 2))
        del state["asked"]
        self.assertNotIn("asked", state)
        self.assertEqual(dict(opened.state("bbb")), {})
        with self.assertRaises(KeyError):
            del state["asked"]


class CheckTest(StoreCase):
    def test_an_unknown_file_after_fix_is_not_ok(self):
        folder = self.home / store.MEDIA / "ins" / "example_user"
        folder.mkdir(parents=True)
        for name, body in (("a.jpg", b"abc"), ("b.jpg", b"gone soon"), ("lost.jpg", b"orphan")):
            (folder / name).write_bytes(body)
        opened = self.open()
        opened.save([store.FileRow("ins:a", "ins:example_user", str(folder / "a.jpg"), 3),
                     store.FileRow("ins:b", "ins:example_user", str(folder / "b.jpg"), 9)])
        (folder / "b.jpg").unlink()
        with mock.patch("flnd.engine.store.time.time", return_value=time.time() + 120):
            found = store.check(self.home, opened, fix=True)
            self.assertEqual(([path.name for path in found.unknown], found.ok), (["lost.jpg"],
                                                                                 False))
            self.assertIsNone(opened.known("ins:b"))
            (folder / "lost.jpg").unlink()
            self.assertTrue(store.check(self.home, opened).ok)


class SampleTest(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.home = Path(folder.name) / ".flnd"

    def test_samples_saved_at_once_get_their_own_names(self):
        response = SimpleNamespace(url="https://example.com/api", headers={}, content=b"{}")
        barrier = threading.Barrier(2)

        def keep():
            barrier.wait(timeout=5)
            for _ in range(50):
                store.sample(self.home, 1, Changed("bad shape", response=response))

        threads = [threading.Thread(target=keep) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(len(list((self.home / store.LOGS / "1" / "samples").iterdir())), 100)


class FolderColumnTest(StoreCase):
    """A followed search keeps its folder: an older home's `targets` gets the column once."""

    def test_an_old_home_gets_the_column_once_and_keeps_its_rows(self):
        self.path.parent.mkdir(parents=True)
        with sqlite3.connect(self.path) as sql:
            for statement in store.STATEMENTS:
                sql.execute(statement)
            sql.execute("INSERT INTO meta VALUES ('version', ?)", (store.VERSION,))
            sql.execute("INSERT INTO meta VALUES ('keys', '2')")
            sql.execute("INSERT INTO targets (key, plugin, value, kind, follow)"
                        " VALUES ('twt:cats', 'twt', 'cats', 'search', 1)")
            sql.execute("ALTER TABLE targets DROP COLUMN folder")
        sql.close()
        with self.open() as opened:
            row = opened.targets()["twt:cats"]
            self.assertEqual((row.follow, row.folder), (True, None))
            opened.record(Target("twt:cats", kind="search"), folder="{query}")
        with self.open() as opened:
            self.assertEqual(opened.targets()["twt:cats"].folder, "{query}")
            # A later record without a folder keeps it.
            opened.record(Target("twt:cats", kind="search"))
            self.assertEqual(opened.targets()["twt:cats"].folder, "{query}")


class RekeyTest(StoreCase):
    """A single post was keyed `<code>:<id>`, and is `<code>:<kind>/<id>`: old rows follow."""

    OLD = [("ins", "C1abc", "post"), ("twt", "1790", "post"), ("tik", "7301", "video"),
           ("loi", "12", "post"), ("fbk", "123", "video"), ("fbk", "9", "photo"),
           ("fbk", "42_7", "post"), ("fbk", "abc", "watch"), ("mht", "1001", "chapter"),
           # A user of the same code keeps its key.
           ("ins", "C1abd", "user")]

    def old_home(self):
        """A home as the last version left it: single posts keyed by their id."""
        self.path.parent.mkdir(parents=True)
        with sqlite3.connect(self.path) as sql:
            for statement in store.STATEMENTS:
                sql.execute(statement)
            sql.execute("INSERT INTO meta VALUES ('version', ?)", (store.VERSION,))
            sql.executemany(
                "INSERT INTO targets (key, plugin, value, kind, categories, seen)"
                " VALUES (?, ?, ?, ?, '[\"photo\"]', ?)",
                [(f"{plugin}:{value}", plugin, value, kind,
                  '["ins:C1abc", "ins:other"]' if plugin == "loi" else "[]")
                 for plugin, value, kind in self.OLD])
            sql.execute("INSERT INTO files (key, target, plugin, path) VALUES"
                        " ('ins:C1abc_0', 'ins:C1abc', 'ins', 'media/ins/C1abc/a.jpg')")
            sql.execute("INSERT INTO failures (key, target, class, message, at) VALUES"
                        " ('ins:C1abc', 'ins:C1abc', 'Blocked', 'wait', 'a')")
        sql.close()

    def test_old_rows_move_to_their_written_form_and_users_stay(self):
        self.old_home()
        with self.open() as opened:
            found = opened.targets()
            self.assertEqual(sorted(found), [
                "fbk:photo/9", "fbk:post/42_7", "fbk:video/123", "fbk:watch/abc", "ins:C1abd",
                "ins:post/C1abc", "loi:post/12", "mht:chapter/1001", "tik:post/7301",
                "twt:post/1790"])
            self.assertEqual([(found[key].value, found[key].kind) for key in (
                "tik:post/7301", "mht:chapter/1001", "ins:C1abd")],
                [("post/7301", "post"), ("chapter/1001", "chapter"), ("C1abd", "user")])
            self.assertEqual(found["loi:post/12"].seen, ["ins:post/C1abc", "ins:other"])
            self.assertEqual(found["ins:post/C1abc"].categories, ("photo",))
            self.assertEqual([row.target for row in opened.on_disk()], ["ins:post/C1abc"])
        self.assertEqual(rows(self.path, "SELECT key, target FROM failures"),
                         [("ins:post/C1abc", "ins:post/C1abc")])

    def test_a_second_open_changes_nothing(self):
        self.old_home()
        self.open().close()
        query = "SELECT key, value, kind, seen FROM targets ORDER BY key"
        before = rows(self.path, query)
        self.open().close()
        self.assertEqual(rows(self.path, query), before)

    def test_the_rewrite_runs_once_per_home(self):
        self.old_home()
        with mock.patch.object(store, "_rekey", wraps=store._rekey) as rekey:
            self.open().close()
            self.open().close()
        self.assertEqual(rekey.call_count, 1)
        # A new home is made with the row, so it never runs.
        self.path.unlink()
        with mock.patch.object(store, "_rekey") as rekey:
            self.open().close()
            with store.Store(self.home) as s: s.record(Target("ins:example_user", kind="user"))
            self.open().close()
        self.assertEqual(rekey.call_count, 0)

    def test_an_old_row_is_found_by_the_written_form_and_runs_as_its_route(self):
        from fakes import Engine

        from flnd.engine.plugins import PLUGINS
        self.old_home()
        engine = Engine(home=self.home, sources=PLUGINS)
        for text, key in [("loi:post/12", "loi:post/12"), ("loi:12", "loi:post/12"),
                          ("tik:7301234567890123456", "tik:post/7301234567890123456")]:
            with self.subTest(text=text):
                self.assertEqual(engine.chosen([text])[0].target.text, key)
        # The rewritten row is the one the written form finds: its categories apply.
        self.assertEqual(engine.chosen(["loi:post/12"])[0].categories, ("photo",))
        recorded = engine._recorded(engine._rows())
        for target in (Target("tik:post/7301", kind="post"), Target("loi:post/12", kind="post")):
            self.assertIn(target, recorded)


class TargetSeenTest(StoreCase):
    def test_target_seen_auto_migration_and_incremental_o1_append(self):
        # 1. Existing schema-2 home without target_seen auto-migrates from targets.seen JSON.
        fresh = self.home / "fresh.db"
        with store.Store(self.home) as s:
            s.record(Target("twt:cats", kind="search"), query="cats")
        sql = sqlite3.connect(self.path)
        try:
            fresh_sql = sqlite3.connect(fresh)
            try:
                fresh_sql.executescript(store.SCHEMA)
                fresh_schema = fresh_sql.execute(
                    "SELECT type, name, sql FROM sqlite_master "
                    "WHERE sql IS NOT NULL ORDER BY name"
                ).fetchall()
            finally:
                fresh_sql.close()
            sql.execute("DROP INDEX IF EXISTS target_seen_ord")
            sql.execute("DROP TABLE IF EXISTS target_seen")
            sql.execute("UPDATE targets SET seen = ? WHERE key = ?",
                        ('["twt:post/3", "twt:post/1", "twt:post/2"]', "twt:cats"))
            sql.commit()
        finally:
            sql.close()

        with store.Store(self.home) as opened:
            row = opened.target("twt:cats")
            self.assertEqual(row.seen, ["twt:post/3", "twt:post/1", "twt:post/2"])
            self.assertEqual(opened.seen_keys("twt:cats"),
                             {"twt:post/3", "twt:post/1", "twt:post/2"})
        sql = sqlite3.connect(self.path)
        try:
            migrated_schema = sql.execute(
                "SELECT type, name, sql FROM sqlite_master WHERE sql IS NOT NULL ORDER BY name"
            ).fetchall()
        finally:
            sql.close()
        self.assertEqual(migrated_schema, fresh_schema)

        # 2. Incremental seen() appends O(1) without touching targets.seen JSON or progress().
        q = Target("twt:dogs", kind="search")
        with store.Store(self.home) as opened:
            opened.record(q, query="dogs")
            opened.progress(
                q, cursors={"max_id": "99"}, complete=[], heads={"search": "twt:post/9"},
            )
            opened.seen(q, ["twt:post/9", "twt:post/8", "twt:post/9"])
            opened.seen(q, ["twt:post/8", "twt:post/7"])
            row = opened.target("twt:dogs")
            self.assertEqual(row.seen, ["twt:post/9", "twt:post/8", "twt:post/7"])
            self.assertEqual(row.cursors, {"max_id": "99"})
            self.assertEqual(row.heads, {"search": "twt:post/9"})
            # targets.seen column was not rewritten with the incremental appends.
            self.assertEqual(rows(self.path, "SELECT seen FROM targets WHERE key = 'twt:dogs'"),
                             [("[]",)])
            opened.forget("twt:dogs")
            self.assertEqual(
                rows(self.path, "SELECT item FROM target_seen WHERE target = 'twt:dogs'"), [],
            )


if __name__ == "__main__":
    unittest.main()
