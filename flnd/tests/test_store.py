"""src/engine/store.py: `flnd.db` schema 2, the migration of a version-1 home, its pragmas,
accounts, the busy signal, and `check`.
"""

import sqlite3
import sys
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from flnd.engine import store
from flnd.errors import DiskError, NotFound, UsageError
from flnkit import vault

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
        for slot in slots:
            with vault.Vault(self.home / store.SECRETS, slot) as saved:
                saved["cookies"] = "[]"


class MigrationTest(StoreCase):
    def test_every_row_of_a_version_1_home_moves_to_schema_2(self):
        self.v1()
        sealed = []
        with self.open(seal=lambda sql, accounts: sealed.append(dict(accounts))) as opened:
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
        self.assertEqual(sealed, [{row[0]: row[1] for row in rows(
            self.path, "SELECT plugin, id FROM accounts")}])
        with store.open(self.home / "fresh") as fresh:
            fresh.state("x")["y"] = 1
        self.assertEqual(schema(self.path), schema(fresh.path))

    def test_the_version_1_text_is_what_a_version_1_home_holds(self):
        from flnd.engine import home

        self.v1(slots=())
        with home.open(self.home / "made") as made:
            made.state("x")["y"] = 1
        self.assertEqual(schema(self.path), schema(made.path))

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
        sealed = []
        self.open(seal=lambda sql, accounts: sealed.append(accounts)).close()
        self.assertEqual((schema(self.path), rows(self.path, "SELECT * FROM files ORDER BY key"),
                          rows(self.path, "SELECT * FROM accounts")), before)
        self.assertEqual(sealed, [])

    def test_a_failing_seal_leaves_version_1_whole(self):
        self.v1()
        before = schema(self.path)

        def seal(sql, accounts):
            raise DiskError("no key")

        with self.assertRaises(DiskError):
            store.open(self.home, seal=seal)
        self.assertEqual((schema(self.path), rows(self.path, "SELECT * FROM meta")),
                         (before, [("version", "1")]))

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


if __name__ == "__main__":
    unittest.main()
