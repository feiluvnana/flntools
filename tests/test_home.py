"""flnd/_home.py: the home's `flnd.db`, one transaction per post, gone files, failures, targets, and
the migration of an old home, which must never change the old files.

Every test runs on a temporary home. An old home is written here the way flnd wrote it before
`flnd.db`: SQLite stores of `t_<table>` rows of JSON values, in `metadata/`.
"""

import base64
import datetime
import hashlib
import json
import os
import shutil
import sqlite3
import stat
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from test_engine import CDN, EngineCase, make_source, noise

from flnd import _home, _net, _vault
from flnd.engine import Engine
from flnd.errors import NetworkError, NotFound
from flnd.options import Options
from flnd.source import File, Target

USER = Target("aaa:example_user", kind="user")


def row(item, post="p1", **fields):
    """A saved file's row of `aaa:example_user`."""
    return _home.FileRow(item, "aaa:example_user", f"/m/{item}.jpg", 10, post_id=post,
                       category="posts", at="2026-01-02T00:00:00+00:00", **fields)


class DbCase(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.home = Path(folder.name) / ".flnd"

    def open(self):
        db = _home.open(self.home)
        self.addCleanup(db.close)
        return db


class FilesTest(DbCase):
    def test_reading_a_new_home_creates_nothing(self):
        with _home.open(self.home) as db:
            self.assertIsNone(db.held("aaa:x"))
            self.assertEqual((db.targets(), db.records(), db.pending("aaa:example_user")), ({}, [], []))
            self.assertEqual(dict(db.state("aaa")), {})
        self.assertFalse(self.home.exists())

    def test_wal_with_normal_sync_and_full_syncs_on_macos(self):
        db = self.open()
        db.save([row("aaa:1")])
        sql = db._store.connection()
        self.assertEqual(sql.execute("PRAGMA journal_mode").fetchone()[0], "wal")
        self.assertEqual(sql.execute("PRAGMA synchronous").fetchone()[0], 1)
        self.assertEqual(sql.execute("PRAGMA fullfsync").fetchone()[0], int(sys.platform == "darwin"))

    def test_a_posts_files_commit_once(self):
        db = self.open()
        db.save([row("aaa:0")])
        statements = []
        db._store.connection().set_trace_callback(statements.append)
        db.save([row("aaa:1", files=3), row("aaa:2"), row("aaa:3")])
        # One statement for the post: SQLite makes it one transaction, so one commit.
        self.assertEqual(len(statements), 1)
        self.assertTrue(statements[0].startswith("INSERT OR REPLACE INTO files"))
        self.assertEqual(db.held("aaa:2"), ("/m/aaa:2.jpg", "saved", None))
        self.assertEqual(db.files_of_post("aaa:1"), 3)
        self.assertIsNone(db.files_of_post("aaa:2"))
        self.assertIsNone(db.files_of_post("aaa:9"))

    def test_the_day_a_row_holds_and_redating_it(self):
        db = self.open()
        db.save([row("aaa:1", estimated=True)])
        self.assertIsNone(db.day("aaa:1"))
        self.assertEqual([r.key for r in db.undated("aaa:example_user")], ["aaa:1"])
        path = self.home / "a.jpg"
        path.write_bytes(b"12345")
        db.redate("aaa:1", path, datetime.date(2026, 1, 4))
        self.assertEqual(db.day("aaa:1"), datetime.date(2026, 1, 4))
        self.assertEqual(db.undated("aaa:example_user"), [])
        [saved] = db.records()
        self.assertEqual((saved.path, saved.size, saved.estimated), (str(path), 5, False))

    def test_records_filter_by_target_and_kind_in_sql(self):
        db = self.open()
        db.save([row("aaa:1", kind="image"), row("aaa:2", post="p2", kind="video")])
        db.save([_home.FileRow("bbb:1", "bbb:other", "/m/b.jpg", kind="image")])
        self.assertEqual([r.key for r in db.records(target="aaa:example_user")], ["aaa:1", "aaa:2"])
        self.assertEqual(sorted(r.key for r in db.records(kind="image")), ["aaa:1", "bbb:1"])

    def test_100k_rows_read_by_key_fast(self):
        db = self.open()
        rows = [_home.FileRow(f"aaa:{n:06d}", "aaa:example_user", f"/m/{n}.jpg", n, post_id=str(n))
                for n in range(100_000)]
        for start in range(0, len(rows), 500):
            db.save(rows[start:start + 500])
        self.assertEqual(db.held("aaa:099999"), ("/m/99999.jpg", "saved", None))
        self.assertEqual(db.count("aaa:example_user"), (100_000, sum(range(100_000))))


class FailuresTest(DbCase):
    def failure(self, db, item="aaa:1", error=None, run=None, **fields):
        fields = {"post": "p1", "category": "posts", "file": f"/m/{item}.jpg", **fields}
        db.fail(run=run, item=item, target="aaa:example_user",
                error=error or NetworkError("reset"), **fields)

    def test_a_not_found_file_is_gone_and_never_pending(self):
        db = self.open()
        self.failure(db, error=NotFound("HTTP 404"), file="/m/1.jpg",
                  url="https://cdn.example/1.jpg?sig=secret")
        self.assertEqual(db.held("aaa:1"), ("/m/1.jpg", "gone", None))
        self.assertEqual(db.pending("aaa:example_user"), [])
        self.assertEqual(db.pending("aaa:example_user", every=True), [])
        self.assertEqual(db.failed(), {})
        # A gone file isn't a saved one.
        self.assertEqual(db.records(), [])

    def test_a_failure_keeps_what_a_retry_lists_again_never_a_url_to_reuse(self):
        db = self.open()
        run = db.start("download aaa:example_user")
        self.failure(db, run=run, url="https://user:pw@cdn.example/1.jpg?token=secret", cursor="c4")
        db.end(run, saved=0)
        [pending] = db.pending("aaa:example_user")
        self.assertEqual((pending.key, pending.post, pending.category, pending.cursor,
                          pending.attempts, pending.error, pending.url),
                         ("aaa:1", "p1", "posts", "c4", 1, "NetworkError", "https://cdn.example/1.jpg"))
        sql = db._store.connection()
        self.assertNotIn("secret", repr(sql.execute("SELECT * FROM failures").fetchall()))
        self.assertNotIn("request", {row[1] for row in sql.execute("PRAGMA table_info(failures)")})
        self.assertEqual(sql.execute("SELECT saved, failed FROM runs").fetchone(), (0, 1))
        self.assertEqual([row.key for row in db.failures(run)], ["aaa:1"])
        self.assertEqual(db.last_run().command, "download aaa:example_user")
        db.save([row("aaa:1")])
        self.assertEqual((db.pending("aaa:example_user"), db.failures(run), db.failed()), ([], [], {}))

    def test_one_row_per_file_counting_the_runs_and_retried_on_three(self):
        db = self.open()
        for attempt in range(1, 6):
            self.failure(db, run=db.start("sync"), error=NetworkError(f"reset {attempt}"))
            [found] = db.pending("aaa:example_user", every=True)
            self.assertEqual((found.attempts, found.message), (attempt, f"reset {attempt}"))
        # The first failure and three retries: the automatic retry leaves it to `flnd retry`.
        self.assertEqual(db.pending("aaa:example_user"), [])
        self.assertEqual(db._store.one("SELECT count(*) FROM failures", ())[0], 1)
        # A file saved since, failing again (`--again`), starts from 1.
        db.save([row("aaa:1")])
        self.failure(db)
        self.assertEqual(db._store.one("SELECT attempts FROM failures", ())[0], 1)

    def test_a_whole_target_failure_is_retried_whole_and_cleared(self):
        db = self.open()
        db.fail(run=None, item="aaa:example_user", target="aaa:example_user",
                error=NetworkError("listing reset"))
        self.assertEqual(db.pending("aaa:example_user"), [])
        self.assertEqual(db.failed(), {"aaa:example_user": True})
        db.clear("aaa:example_user")
        self.assertEqual(db.failed(), {})

    def test_an_old_failures_table_loses_its_request_column(self):
        sql = sqlite3.connect(self.home.parent / "old.db")
        sql.executescript(_home.SCHEMA.replace("    cursor TEXT\n", "    request TEXT\n")
                          .replace("    category TEXT NOT NULL DEFAULT '', file", "    file"))
        for at in ("1", "2"):
            sql.execute("INSERT INTO failures (key, target, post_id, class, message, at, request)"
                        " VALUES ('aaa:1', 'aaa:example_user', 'p1', 'Error', 'x', ?,"
                        " '{\"url\": \"https://cdn.example/1.jpg?token=secret\"}')", (at,))
        sql.commit()
        sql.close()
        self.home.mkdir()
        os.replace(self.home.parent / "old.db", self.home / _home.DB)
        db = self.open()
        rows = db._store.all("SELECT * FROM failures", ())
        self.assertEqual(len(rows), 1)
        self.assertNotIn("secret", repr(rows))
        self.assertEqual(db.failed(), {"aaa:example_user": False})


class TargetsTest(DbCase):
    def test_follow_keeps_compress_for_later_syncs(self):
        db = self.open()
        db.record([(USER, Options(compress=True))], follow=True, keep=True)
        self.assertTrue(db.target(USER).compress)
        db.record([(USER, Options())])
        self.assertTrue(db.target(USER).compress)
        db.record([(USER, Options())], follow=True, keep=True)
        self.assertFalse(db.target(USER).compress)

    def test_a_step_updates_its_columns_only(self):
        db = self.open()
        db.save([row("aaa:1")])
        db.update("aaa:1", shrink="kept", tools="pillow 1", unpacked="", folder=None)
        [saved] = db.records()
        self.assertEqual((saved.shrink, saved.tools), ("kept", "pillow 1"))
        with self.assertRaises(ValueError):
            db.update("aaa:1", target="aaa:other")

    def test_record_follow_progress_seen_tally_and_forget(self):
        db = self.open()
        db.record([(USER, Options(categories=("posts",)))], follow=False, keep=True)
        self.assertEqual(db.target(USER).only, ("posts",))
        self.assertFalse(db.target(USER).follow)
        # Recorded again without `follow`, it keeps its state; `follow=True` follows it.
        db.record([(USER, Options())])
        self.assertFalse(db.target(USER).follow)
        db.follow(USER, True)
        db.progress(USER, cursors={"posts": "c2"}, complete=[])
        db.progress(USER, cursors={"posts": None}, complete=["posts"])
        db.seen(USER, ["aaa:a", "aaa:b"])
        db.seen(USER, ["aaa:b", "aaa:c"])
        db.save([row("aaa:1"), row("aaa:2")])
        db.tally(USER)
        saved = db.targets()["aaa:example_user"]
        self.assertEqual((saved.follow, saved.only, saved.cursors, list(saved.complete)),
                         (True, ("posts",), {}, ["posts"]))
        self.assertEqual((saved.seen, saved.files, saved.bytes), (["aaa:a", "aaa:b", "aaa:c"], 2, 20))
        self.assertTrue(db.forget(USER))
        self.assertFalse(db.forget(USER))
        self.assertEqual(len(db.records()), 2)

    def test_a_sources_state_is_a_mapping_of_json_values(self):
        db = self.open()
        state = db.state("aaa")
        state["user"] = {"id": 42}
        state["asked"] = [1, 2]
        self.assertEqual((state["user"], sorted(state), len(state)), ({"id": 42}, ["asked", "user"], 2))
        del state["asked"]
        self.assertNotIn("asked", state)
        self.assertEqual(dict(db.state("bbb")), {})
        with self.assertRaises(KeyError):
            del state["asked"]


# ---------------------------------------------------------------------------------------------
# An old home
# ---------------------------------------------------------------------------------------------

def old_store(path, tables, *, json_store=False):
    """Write an old store as flnd wrote it before `flnd.db`: tables of JSON values by key."""
    path.parent.mkdir(parents=True, exist_ok=True)
    if json_store:
        path.write_text(json.dumps(tables))
        return
    db = sqlite3.connect(path, isolation_level=None)
    db.execute("PRAGMA journal_mode = WAL")
    for name, rows in tables.items():
        db.execute(f'CREATE TABLE "t_{name}" (key TEXT PRIMARY KEY, value TEXT NOT NULL) WITHOUT ROWID')
        db.executemany(f'INSERT INTO "t_{name}" VALUES (?, ?)',
                       [(key, json.dumps(value)) for key, value in rows.items()])
    db.close()


def hashes(folder):
    """Every file under `folder` with its SHA-256, so a test sees any change at all."""
    return {path.relative_to(folder).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in sorted(folder.rglob("*")) if path.is_file()}


class MigrationTest(DbCase):
    def setUp(self):
        super().setUp()
        old = self.home / "metadata"
        media = ".flnd/media/aaa"
        old_store(old / "targets.db", {"targets": {
            # A row from before `sync` existed counts as followed.
            "aaa:example_user": {"kind": "user", "at": "2026-01-01T00:00:00+00:00",
                                 "complete": {"posts": "2026-01-01"}, "only": ["posts"],
                                 "failed": {"aaa:9": {"url": "https://cdn.example/9.jpg?sig=s",
                                                      "dest": f"{media}/example_user/9.jpg",
                                                      "id": "p9", "headers": {"Cookie": "c=secret"}}}},
            "aaa:other": {"kind": "user", "sync": False, "cursors": {"posts": "c3"}},
            "aaa:cats": {"kind": "query", "seen": ["aaa:example_user"]},
        }})
        old_store(old / "history.db", {"downloads": {
            "aaa:1": {"path": f"{media}/example_user/posts/1.jpg", "size": 3, "mtime": 5,
                      "at": "2026-01-02T00:00:00+00:00", "id": "p1", "category": "posts",
                      "date": "2026-01-02", "files": 2},
            "aaa:2": {"path": f"{media}/example_user/posts/2.mp4", "size": 4,
                      "at": "2026-01-02T00:00:00+00:00", "id": "p1", "category": "posts"},
            # A folder no target row names any more.
            "aaa:3": {"path": f"{media}/gone_user/3.jpg", "size": 1, "id": "p3",
                      "estimated": True, "date": "2026-01-01"},
        }})
        old_store(old / "sources.db", {"aaa": {"redate.asked": {"p3": 1.0}}}, json_store=True)
        self.key = os.urandom(32)
        (old / "secret.key").write_bytes(self.key)
        sealed = {name: base64.b64encode(_vault.encrypt(value, self.key, context=f"aaa\0{name}".encode())).decode()
                  for name, value in (("cookies", json.dumps([{"name": "sid", "value": "v", "domain": "aaa.example"}])),
                                      ("origin", "Chrome"))}
        old_store(old / "secrets.db", {"secrets": {f"aaa:{name}": value for name, value in sealed.items()}})
        self.before = hashes(old)

    def test_an_old_home_is_copied_and_its_files_never_change(self):
        db = self.open()
        self.assertEqual(hashes(self.home / "metadata"), self.before)
        targets = db.targets()
        self.assertEqual(sorted(targets), ["aaa:cats", "aaa:example_user", "aaa:other"])
        user = targets["aaa:example_user"]
        self.assertEqual((user.follow, user.only, user.complete, user.files, user.bytes),
                         (True, ("posts",), {"posts": "2026-01-01"}, 2, 7))
        self.assertEqual((targets["aaa:other"].follow, targets["aaa:other"].cursors), (False, {"posts": "c3"}))
        self.assertEqual(targets["aaa:cats"].seen, ["aaa:example_user"])
        files = {r.key: r for r in db.records()}
        self.assertEqual((files["aaa:1"].target, files["aaa:1"].kind, files["aaa:2"].kind),
                         ("aaa:example_user", "image", "video"))
        self.assertEqual((files["aaa:3"].target, files["aaa:3"].estimated), ("aaa:gone_user", True))
        self.assertEqual(db.files_of_post("aaa:1"), 2)
        self.assertEqual(db._store.one("SELECT files FROM posts WHERE post_id = ?", ("p1",)), (2,))
        self.assertEqual(db.state("aaa")["redate.asked"], {"p3": 1.0})
        pending = db.pending("aaa:example_user")
        self.assertEqual([row.key for row in pending], ["aaa:9"])
        self.assertNotIn("secret", repr(db._store.connection().execute("SELECT * FROM failures").fetchall()))

    def test_the_vault_moves_with_its_key_and_still_opens(self):
        self.assertEqual([c["name"] for c in _home.load(self.home, "aaa")], ["sid"])
        self.assertEqual(_home.origin(self.home, "aaa"), "Chrome")
        key = self.home / "secret.key"
        self.assertEqual(key.read_bytes(), self.key)
        if os.name != "nt":
            self.assertEqual(stat.S_IMODE(key.stat().st_mode), 0o600)
        self.assertEqual(hashes(self.home / "metadata"), self.before)

    def test_a_second_open_does_not_migrate_again(self):
        self.open().close()
        with _home.open(self.home) as db:
            sql = db._store.connection()
            counts = [sql.execute(f"SELECT count(*) FROM {t}").fetchone()[0] for t in ("files", "failures", "targets")]
            migrated = sql.execute("SELECT value FROM meta WHERE key = 'migrated'").fetchone()
        with _home.open(self.home) as db:
            sql = db._store.connection()
            self.assertEqual(
                [sql.execute(f"SELECT count(*) FROM {t}").fetchone()[0] for t in ("files", "failures", "targets")],
                counts)
            self.assertEqual(sql.execute("SELECT value FROM meta WHERE key = 'migrated'").fetchone(), migrated)
        self.assertEqual(counts, [3, 1, 3])

    def test_two_runs_opening_at_once_migrate_once(self):
        errors = []

        def opening():
            try:
                _home.open(self.home).close()
            except Exception as error:  # noqa: BLE001 - the test reports it
                errors.append(error)

        threads = [threading.Thread(target=opening) for _ in range(4)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(errors, [])
        with _home.open(self.home) as db:
            self.assertEqual(len(db.records()), 3)
            self.assertEqual(len(db.pending("aaa:example_user")), 1)

    def test_rows_an_old_run_left_in_its_log_are_read_without_writing_the_store(self):
        # An old run that stopped without a checkpoint leaves rows in `history.db-wal`. Copying the
        # files while a connection holds them leaves exactly that.
        scratch = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, scratch)
        old_store(scratch / "history.db", {"downloads": {}})
        live = sqlite3.connect(scratch / "history.db", isolation_level=None)
        live.execute("PRAGMA wal_autocheckpoint = 0")
        live.execute("INSERT INTO t_downloads VALUES ('aaa:7', ?)",
                     (json.dumps({"path": ".flnd/media/aaa/example_user/7.jpg", "size": 1, "id": "p7"}),))
        for name in ("history.db", "history.db-wal"):
            shutil.copy(scratch / name, self.home / "metadata" / name)
        live.close()
        store = hashlib.sha256((self.home / "metadata" / "history.db").read_bytes()).hexdigest()
        with _home.open(self.home) as db:
            self.assertEqual([r.key for r in db.records()], ["aaa:7"])
        self.assertEqual(hashlib.sha256((self.home / "metadata" / "history.db").read_bytes()).hexdigest(), store)


# ---------------------------------------------------------------------------------------------
# Through the engine
# ---------------------------------------------------------------------------------------------

class GoneTest(EngineCase):
    def setUp(self):
        super().setUp()
        self.routes[f"{CDN}/a.jpg"] = _net.reply(200, content=noise("JPEG"))
        self.routes[f"{CDN}/b.jpg"] = _net.reply(404)
        self.source = make_source("aaa", files=[File(f"{CDN}/a.jpg", name="a.jpg"),
                                                File(f"{CDN}/b.jpg", name="b.jpg")])

    def asked(self):
        return sum(str(request.url) == f"{CDN}/b.jpg" for request in self.requests)

    def test_a_404_is_gone_and_asked_again_only_when_told(self):
        engine = Engine(home=self.home, sources={"aaa": self.source})
        engine.download(["aaa:example_user"], categories="all")
        self.assertEqual(self.asked(), 1)
        with _home.open(self.home) as db:
            self.assertEqual(db.held("aaa:b.jpg")[1], "gone")
            self.assertEqual(db._store.connection().execute("SELECT key, class FROM failures").fetchall(),
                             [("aaa:b.jpg", "NotFound")])
        engine.download(["aaa:example_user"], categories="all")
        self.assertEqual(self.asked(), 1)
        engine.download(["aaa:example_user"], categories="all", again=True)
        self.assertEqual(self.asked(), 2)

    def test_ls_sizes_come_from_the_database_never_a_walk(self):
        engine = Engine(home=self.home, sources={"aaa": self.source})
        engine.download(["aaa:example_user"], categories="all", sync=True)
        with mock.patch.object(Path, "rglob", side_effect=AssertionError("a walk")), \
                mock.patch.object(os, "walk", side_effect=AssertionError("a walk")):
            [info] = engine.target_ls()
        self.assertEqual((info.files, info.size), (1, len(noise("JPEG"))))


if __name__ == "__main__":
    unittest.main()
