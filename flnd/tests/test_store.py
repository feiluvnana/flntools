"""src/engine/store.py: `flnd.db` schema 2, the migration of a version-1 home, its pragmas,
accounts, the busy signal, and `check`.
"""

import datetime
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

from flnd.engine import store
from flnd.errors import Changed, DiskError, NetworkError, NotFound, UsageError
from flnd.testing import fake_clock

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


def schema(path):
    """A database file's schema, as SQLite keeps it."""
    with sqlite3.connect(path) as sql:
        found = sorted(sql.execute("SELECT type, name, tbl_name, sql FROM sqlite_master"))
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
    def test_every_row_of_a_version_1_home_moves_to_schema_2(self):
        self.v1()
        with self.open() as opened:
            self.assertEqual(rows(self.path, "SELECT * FROM meta"), [("version", "2")])
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
                              user.gaps, user.files, user.bytes, user.account),
                             ("ins", True, {"posts": "2026-01-02"}, {"reels": "c1"},
                              {"posts": "1"}, {"reels": ["c0", ""]}, 2, 40, None))
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
            accounts = [(row.plugin, row.name, row.origin, row.default, row.status)
                        for row in opened.accounts()]
            self.assertEqual(accounts, [("ins", "default", "chrome", True, "ok"),
                                        ("loi", "default", "", True, "ok"),
                                        ("tlg", "default", "qr", True, "ok"),
                                        ("twt", "default", "file", True, "ok")])
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
            self.assertEqual((rows(self.path, "SELECT * FROM meta"), len(opened.on_disk()),
                              len(opened.targets()), len(opened.accounts())),
                             ([("version", "2")], 3, 4, 4))

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
                  rows(self.path, "SELECT * FROM accounts"))
        self.open().close()
        self.assertEqual((schema(self.path), rows(self.path, "SELECT * FROM files ORDER BY key"),
                          rows(self.path, "SELECT * FROM accounts")), before)

    def test_a_home_flnd_cannot_read_is_refused(self):
        cases = {"newer": "INSERT INTO meta VALUES ('version', '3')",
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
        expected = {
            True: {"busy_timeout": 0, "wal_autocheckpoint": 0},
            False: {"busy_timeout": 30_000, "wal_autocheckpoint": 10_000},
        }
        for loop, own in expected.items():
            with self.subTest(loop=loop), self.open(loop=loop) as opened:
                opened.state("x")["y"] = 1
                sql = opened._db.connection()
                found = {name: sql.execute(f"PRAGMA {name}").fetchone()[0]
                         for name in [*own, "journal_mode", "synchronous", "cache_size",
                                      "fullfsync"]}
                self.assertEqual(found, {**own, "journal_mode": "wal", "synchronous": 1,
                                         "cache_size": -65536,
                                         "fullfsync": int(sys.platform == "darwin")})

    def test_the_checkpoint_connection_syncs_as_the_store_does(self):
        opened = self.open(loop=True)
        opened.state("x")["y"] = 1
        opened.checkpoint(force=True)
        sql = opened._checkpointer
        found = {name: sql.execute(f"PRAGMA {name}").fetchone()[0]
                 for name in ("journal_mode", "synchronous", "fullfsync", "checkpoint_fullfsync")}
        darwin = int(sys.platform == "darwin")
        self.assertEqual(found, {"journal_mode": "wal", "synchronous": 1, "fullfsync": darwin,
                                 "checkpoint_fullfsync": darwin})

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

    def test_a_target_keeps_its_categories_and_the_account_that_worked(self):
        opened = self.open()
        user = SimpleNamespace(plugin="ins", value="example_user", kind="user")
        opened.record(user, categories=["posts"])
        opened.record(user)
        self.assertEqual(opened.target("ins:example_user").categories, ("posts",))
        opened.record(user, categories=["all"])
        opened.progress(user, cursors={}, complete=[], account="example_handle")
        row = opened.target("ins:example_user")
        self.assertEqual((row.categories, row.follow, row.account), ((), True, "example_handle"))

    def test_accounts_log_in_out_and_hold(self):
        opened = self.open()
        first = opened.login("ins", "example_user", origin="chrome")
        second = opened.login("ins", "example_handle", origin="file")
        opened.mark(first.id, status="held", held_until="2026-01-02T00:00:00+00:00")
        self.assertEqual(opened.login("ins", "example_user", origin="browser").status, "ok")
        self.assertEqual([(row.name, row.default) for row in opened.accounts("ins")],
                         [("example_user", True), ("example_handle", False)])
        opened.logout(first.id)
        self.assertEqual([(row.id, row.default) for row in opened.accounts("ins")],
                         [(second.id, True)])


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


if __name__ == "__main__":
    unittest.main()
