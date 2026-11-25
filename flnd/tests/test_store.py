"""src/engine/store.py: `flnd.db` schema 5, old and newer schema refusal, its pragmas, the busy
signal, and `check`.
"""

import datetime
import io
import os
import sqlite3
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from fakes import records
from plugin_kit import fake_clock

from flnd.engine import store
from flnd.errors import Changed, DiskError, NetworkError, NotFound, UsageError
from flnd.plugin import Target
from flnd.ui.log import ui

# Version 1 as a home made it: only what tells it from a newer one, a `meta` without a version.
V1 = """
CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL) WITHOUT ROWID;
CREATE TABLE files (
    key TEXT PRIMARY KEY, target TEXT NOT NULL, source TEXT NOT NULL,
    post_id TEXT NOT NULL DEFAULT '', category TEXT NOT NULL DEFAULT '', path TEXT NOT NULL
) WITHOUT ROWID;
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
        # What the store says (a warning), kept off the terminal.
        self.said = io.StringIO()
        self.enterContext(ui.scope(dest=self.said, width=200, color=False))

    def open(self, **how):
        opened = store.open(self.home, **how)
        self.addCleanup(opened.close)
        return opened

    def v1(self):
        """A version-1 home: its tables, and no version."""
        self.path.parent.mkdir(parents=True)
        with sqlite3.connect(self.path) as sql:
            sql.executescript(V1)
        sql.close()


class SchemaTest(StoreCase):
    def test_an_old_home_is_refused_and_left_as_it_is(self):
        for version in (None, "2", "3", "4"):
            with self.subTest(version=version):
                self.v1()
                if version is not None:
                    with sqlite3.connect(self.path) as sql:
                        sql.execute("INSERT INTO meta VALUES ('version', ?)", (version,))
                    sql.close()
                before = schema(self.path)
                with self.assertRaisesRegex(UsageError, "is from an old flnd; start a new home"):
                    store.open(self.home)
                self.assertEqual(schema(self.path), before)
                self.path.unlink()
                self.path.parent.rmdir()

    def test_a_second_open_changes_nothing(self):
        with self.open() as opened:
            opened.save([store.FileRow("ins:1", "ins:example_user", "media/ins/1.jpg", 10)])
        before = (schema(self.path), rows(self.path, "SELECT * FROM files ORDER BY key"),
                  rows(self.path, "SELECT * FROM meta ORDER BY key"))
        self.open().close()
        self.assertEqual((schema(self.path), rows(self.path, "SELECT * FROM files ORDER BY key"),
                          rows(self.path, "SELECT * FROM meta ORDER BY key")), before)

    def test_a_home_flnd_cannot_read_is_refused(self):
        cases = {"newer": "INSERT INTO meta VALUES ('version', '6')",
                 "unversioned": "DELETE FROM meta; INSERT INTO files (key, target, source, path)"
                                " VALUES ('a:1', 'a:b', 'a', 'p')"}
        for name, change in cases.items():
            with self.subTest(name):
                self.v1()
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
            self.assertEqual((opened.targets(), records(opened),
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
        [saved] = records(opened)
        self.assertEqual((saved.path, saved.size, saved.estimated, saved.day),
                         (str(path), 5, False, datetime.date(2026, 1, 4)))

    def test_records_filter_by_target_and_kind_and_100k_rows_read_fast(self):
        opened = self.open()
        opened.save([row("aaa:1", kind="image"), row("aaa:2", post="p2", kind="video")])
        opened.save([store.FileRow("bbb:1", "bbb:other", "/m/b.jpg", kind="image")])
        self.assertEqual([found.key for found in records(opened, target="aaa:example_user")],
                         ["aaa:1", "aaa:2"])
        self.assertEqual(sorted(found.key for found in records(opened, kind="image")),
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
        [saved] = records(opened)
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
                          records(opened)), ([], [], {}, []))

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
    """A followed search keeps its folder across records."""

    def test_a_search_keeps_its_folder_across_records(self):
        with self.open() as opened:
            opened.record(Target("twt:cats", kind="search"), folder="{query}")
        with self.open() as opened:
            self.assertEqual(opened.targets()["twt:cats"].folder, "{query}")
            # A later record without a folder keeps it.
            opened.record(Target("twt:cats", kind="search"))
            self.assertEqual(opened.targets()["twt:cats"].folder, "{query}")


class TargetSeenTest(StoreCase):
    def test_incremental_o1_append_and_forget(self):
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
            opened.forget("twt:dogs")
            self.assertEqual(
                rows(self.path, "SELECT item FROM target_seen WHERE target = 'twt:dogs'"), [],
            )


if __name__ == "__main__":
    unittest.main()
