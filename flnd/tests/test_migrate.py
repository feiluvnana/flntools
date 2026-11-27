"""`flnd migrate`: a version 1 home (tests/data/home_v1.sql) brought to version 2, offline."""

import sqlite3
import unittest
from pathlib import Path
from unittest import mock

from test_cli import FlndCase

from flnd.engine import migrate, store
from flnd.errors import UsageError

V1 = (Path(__file__).parent / "data" / "home_v1.sql").read_text(encoding="utf-8")


class MigrateCase(FlndCase):
    def setUp(self):
        super().setUp()
        self.home = Path(self.tmp.name) / "home"
        self.db = self.home / store.DB
        self.db.parent.mkdir(parents=True)
        with sqlite3.connect(self.db) as sql:
            sql.executescript(V1)
        sql.close()

    def sql(self, query, *args):
        with sqlite3.connect(self.db) as sql:
            found = sql.execute(query, args).fetchall()
        sql.close()
        return found

    def migrate(self, *flags):
        return self.run_app("--home", str(self.home), "migrate", *flags)

    def snapshot(self):
        return {table: self.sql(f"SELECT * FROM {table} ORDER BY 1, 2")
                for table in ("meta", "targets", "files", "failures", "target_seen")}


class DryTest(MigrateCase):
    def test_dry_says_what_changes_and_writes_nothing(self):
        before = self.snapshot()
        code, err, _ = self.migrate("--dry")
        self.assertEqual(code, 0)
        self.assertEqual(err.strip(), "v1 → v2: 2 loi post targets renamed (1 merged), "
                                      "3 targets of removed routes unfollowed")
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(list(self.db.parent.glob("*.bak*")), [])
        self.assertFalse((self.home / ".lock").exists())


class StepTest(MigrateCase):
    def test_it_backs_up_then_renames_merges_and_unfollows(self):
        code, err, _ = self.migrate()
        self.assertEqual(code, 0)
        self.assertIn("v1 → v2: 2 loi post targets renamed (1 merged)", err)
        self.assertEqual(self.sql("SELECT value FROM meta WHERE key = 'version'"), [("2",)])
        [backup] = self.db.parent.glob("flnd.db.v1.bak")
        self.assertEqual(sqlite3.connect(backup).execute(
            "SELECT value FROM meta").fetchone(), ("5",))
        self.assertEqual(self.sql("SELECT key FROM targets WHERE key LIKE 'loi:post/%'"), [])
        # The twin keeps both histories: followed, its own categories, both files, the newest sync.
        self.assertEqual(self.sql(
            "SELECT follow, categories, last_sync, files, bytes, heads FROM targets"
            " WHERE key = 'loi:101'"),
            [(1, '["info"]', "2026-11-05T00:00:00+00:00", 3, 25, '{"info": "loi:101"}')])
        self.assertEqual(self.sql("SELECT key, value, files FROM targets WHERE key = 'loi:102'"),
                         [("loi:102", "102", 1)])
        self.assertEqual(self.sql("SELECT target FROM files WHERE key LIKE 'loi:%' ORDER BY key"),
                         [("loi:101",), ("loi:101",), ("loi:101",), ("loi:102",)])
        self.assertEqual(self.sql("SELECT key, target FROM failures ORDER BY key"),
                         [("loi:102", "loi:102"), ("loi:f_0", "loi:101")])
        self.assertEqual(self.sql("SELECT target, item FROM target_seen ORDER BY target, item"),
                         [("loi:101", "x"), ("loi:101", "y"), ("loi:author/9", "loi:101"),
                          ("loi:author/9", "loi:102")])

    def test_removed_routes_are_unfollowed_and_nothing_else(self):
        self.migrate()
        self.assertEqual(self.sql("SELECT key, follow FROM targets ORDER BY key"),
                         [("loi:101", 1), ("loi:102", 0), ("loi:author/9", 1),
                          ("loi:example words", 0), ("tik:example words", 0),
                          ("tlg:examplechannel", 0), ("twt:example_user", 1)])
        # Their files stay known, and no file moves.
        self.assertEqual(self.sql("SELECT count(*) FROM files WHERE state = 'saved'"), [(6,)])
        self.assertEqual(self.sql("SELECT path FROM files WHERE key = 'tlg:m_0'"),
                         [("media/tlg/examplechannel/m.jpg",)])

    def test_the_twt_target_is_untouched(self):
        before = self.sql("SELECT * FROM targets WHERE key = 'twt:example_user'")
        self.migrate()
        self.assertEqual(self.sql("SELECT * FROM targets WHERE key = 'twt:example_user'"), before)

    def test_a_backup_is_never_overwritten(self):
        taken = self.db.with_name("flnd.db.v1.bak")
        taken.write_bytes(b"older")
        self.migrate()
        self.assertEqual(taken.read_bytes(), b"older")
        self.assertTrue(self.db.with_name("flnd.db.v1.bak.1").is_file())

    def test_a_home_at_version_2_is_already_so(self):
        self.migrate()
        before = self.snapshot()
        code, err, _ = self.migrate()
        self.assertEqual((code, err.strip()), (0, f"{self.home} is already v2"))
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(len(list(self.db.parent.glob("flnd.db.v*.bak*"))), 1)

    def test_a_home_with_no_database_is_already_v2(self):
        code, err, _ = self.run_app("--home", str(Path(self.tmp.name) / "none"), "migrate")
        self.assertEqual((code, err.strip()), (0, f"{Path(self.tmp.name) / 'none'} is already v2"))

    def test_a_step_that_raises_rolls_back_and_leaves_version_1(self):
        before = self.snapshot()

        def broken(sql, home, *, dry):
            sql.execute("DELETE FROM targets")
            raise RuntimeError("an example failure")

        with mock.patch.dict(migrate.STEPS, {1: broken}), self.assertRaises(RuntimeError):
            migrate.run(self.home)
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(self.sql("SELECT value FROM meta"), [("5",)])

    def test_an_older_home_than_the_steps_reach_is_refused(self):
        with mock.patch.dict(migrate.STEPS, clear=True), self.assertRaisesRegex(
                UsageError, "older than this flnd migrates"):
            migrate.run(self.home)


class RefusalTest(MigrateCase):
    def test_every_other_command_refuses_a_v1_home(self):
        before = self.snapshot()
        for argv in (("status",), ("target", "list"), ("get", "twt:example_user", "--dry"),
                     ("sync", "--dry"), ("target", "unfollow", "twt:example_user")):
            with self.subTest(argv=argv):
                code, _, err = self.run_app("--home", str(self.home), *argv)
                self.assertEqual(code, 2)
                self.assertEqual(err.splitlines(), [
                    f"x {self.home} is a v1 home; run flnd migrate",
                    "  hint  flnd migrate --dry shows what changes"])
        self.assertEqual(self.snapshot(), before)

    def test_other_old_and_newer_values_keep_their_refusals(self):
        for value, text in [("1", "is from an old flnd; start a new home"),
                            ("3", "is from an old flnd; start a new home"),
                            ("4", "is from an old flnd; start a new home"),
                            ("6", "needs a newer flnd")]:
            with self.subTest(value=value):
                self.sql("UPDATE meta SET value = ?", value)
                code, _, err = self.migrate()
                self.assertEqual(code, 2)
                self.assertIn(text, err)


class NewHomeTest(MigrateCase):
    def test_a_new_home_is_version_2(self):
        self.db.unlink()
        with store.open(self.home) as opened:
            opened.ready()
        self.assertEqual(self.sql("SELECT value FROM meta WHERE key = 'version'"), [("2",)])
        self.assertEqual(store.VERSION, "2")


if __name__ == "__main__":
    unittest.main()
