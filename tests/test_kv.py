"""`shared.kv`: the mapping protocol, JSON values, expiry, prefixes, tables, threads and old files.

The clock is faked by patching `shared._kv._now`; every database is a real sqlite file in a
temporary folder.
"""

import collections.abc
import json
import sqlite3
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from shared import kv

START = 1_000_000.0


class StoreCase(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.file = Path(self.folder.name) / "nested" / "state.db"
        self.db = kv.open(self.file)

    def tearDown(self):
        self.db.close()
        self.folder.cleanup()


class MappingTest(StoreCase):
    def test_is_a_mutable_mapping(self):
        self.assertIsInstance(self.db, collections.abc.MutableMapping)
        self.assertTrue(issubclass(kv.Store, collections.abc.MutableMapping))

    def test_mapping_protocol(self):
        self.db["ins:1"] = {"path": "ins/a.jpg"}
        self.db["ins:2"] = 2
        self.assertEqual(self.db["ins:1"], {"path": "ins/a.jpg"})
        self.assertIn("ins:1", self.db)
        self.assertNotIn("ins:9", self.db)
        self.assertNotIn(1, self.db)
        self.assertEqual(len(self.db), 2)
        self.assertEqual(list(self.db), ["ins:1", "ins:2"])
        self.assertEqual(list(self.db.keys()), ["ins:1", "ins:2"])
        self.assertEqual(list(self.db.values()), [{"path": "ins/a.jpg"}, 2])
        self.assertEqual(dict(self.db.items()), {"ins:1": {"path": "ins/a.jpg"}, "ins:2": 2})
        self.assertEqual(self.db.get("ins:9"), None)
        self.assertEqual(self.db.get("ins:9", default="none"), "none")
        del self.db["ins:1"]
        self.assertEqual(len(self.db), 1)
        with self.assertRaises(KeyError):
            del self.db["ins:1"]
        with self.assertRaises(KeyError):
            self.db["ins:1"]

    def test_mixin_methods(self):
        self.db.update({"a": 1, "b": 2})
        self.assertEqual(self.db.setdefault("a", 9), 1)
        self.assertEqual(self.db.pop("a"), 1)
        self.assertEqual(self.db.popitem(), ("b", 2))
        self.db["c"] = 3
        self.db.clear()
        self.assertEqual(len(self.db), 0)
        self.assertEqual(self.db, {})

    def test_keys_behaves_as_a_mappings(self):
        """`keys()` overrides Mapping's, so `dict(store)`, `update` and set operations must still work."""
        self.db.update({"b": 2, "a": 1})
        keys = self.db.keys()
        self.assertIsInstance(keys, collections.abc.KeysView)
        self.assertEqual(dict(self.db), {"a": 1, "b": 2})
        self.assertEqual(keys, {"a", "b"})
        self.assertEqual(keys & {"a", "z"}, {"a"})
        self.assertEqual(len(keys), 2)
        self.assertIn("a", keys)
        self.assertNotIn("z", keys)
        self.db["c"] = 3
        self.assertEqual(list(keys), ["a", "b", "c"])  # live, as a dict's view
        copy = {}
        copy.update(self.db)
        self.assertEqual(copy, {"a": 1, "b": 2, "c": 3})

    def test_keys_must_be_str(self):
        with self.assertRaises(kv.UnstorableError):
            self.db[1] = "x"  # pyright: ignore[reportArgumentType]


class ValueTest(StoreCase):
    def test_json_round_trip(self):
        value = {"list": [1, 2.5, None, True], "text": "é", "nested": {"a": []}}
        self.db["k"] = value
        self.assertEqual(self.db["k"], value)
        self.assertEqual(self.db.raw.execute("SELECT value FROM kv").fetchone()[0].count("é"), 1)

    def test_value_that_is_not_json(self):
        for value in (object(), {1, 2}, b"bytes"):
            with self.subTest(value=value), self.assertRaises(kv.UnstorableError):
                self.db["k"] = value
        self.assertNotIn("k", self.db)


class ExpiryTest(StoreCase):
    def test_ttl_expires_with_the_clock(self):
        with mock.patch("shared._kv._now", return_value=START):
            self.db.set("loi:quota", 3, ttl=1)
            self.db.set("forever", 1)
            self.assertEqual(self.db["loi:quota"], 3)
        with mock.patch("shared._kv._now", return_value=START + 0.5):
            self.assertIn("loi:quota", self.db)
        with mock.patch("shared._kv._now", return_value=START + 1):
            self.assertEqual(len(self.db), 1)
            self.assertEqual(list(self.db.keys()), ["forever"])
            self.assertNotIn("loi:quota", self.db)
            with self.assertRaises(KeyError):
                self.db["loi:quota"]
        self.assertEqual(self._stored_keys(), ["forever"])

    def test_get_returns_default_once_expired(self):
        with mock.patch("shared._kv._now", return_value=START):
            self.db.set("k", "v", ttl=10)
        with mock.patch("shared._kv._now", return_value=START + 11):
            self.assertEqual(self.db.get("k", default="gone"), "gone")
            with self.assertRaises(KeyError):
                del self.db["k"]

    def test_setting_again_clears_the_ttl(self):
        with mock.patch("shared._kv._now", return_value=START):
            self.db.set("k", 1, ttl=1)
            self.db["k"] = 2
        with mock.patch("shared._kv._now", return_value=START + 100):
            self.assertEqual(self.db["k"], 2)

    def test_open_sweeps_expired_rows(self):
        with mock.patch("shared._kv._now", return_value=START):
            self.db.set("old", 1, ttl=1)
            self.db.set("new", 1, ttl=100)
        self.db.close()
        with mock.patch("shared._kv._now", return_value=START + 50):
            self.db = kv.open(self.file)
        self.assertEqual(self._stored_keys(), ["new"])

    def _stored_keys(self):
        return [key for (key,) in self.db.raw.execute("SELECT key FROM kv ORDER BY key")]


class PrefixTest(StoreCase):
    def test_results(self):
        for key in ("ins:1", "ins:2", "insx", "tik:1", "in"):
            self.db[key] = key.upper()
        self.assertEqual(list(self.db.keys(prefix="ins:")), ["ins:1", "ins:2"])
        self.assertEqual(list(self.db.keys(prefix="tik")), ["tik:1"])
        self.assertEqual(list(self.db.keys(prefix="zzz")), [])
        self.assertEqual(list(self.db.keys()), ["in", "ins:1", "ins:2", "insx", "tik:1"])

    def test_prefix_ending_in_the_last_code_point(self):
        self.db["a\U0010ffffz"] = 1
        self.db["b"] = 2
        self.assertEqual(list(self.db.keys(prefix="a\U0010ffff")), ["a\U0010ffffz"])

    def test_a_prefix_view(self):
        for key in ("ins:1", "ins:2", "tik:1"):
            self.db[key] = 1
        view = self.db.keys(prefix="ins:")
        self.assertEqual(len(view), 2)
        self.assertIn("ins:1", view)
        self.assertNotIn("tik:1", view)
        self.assertNotIn(1, view)
        self.assertEqual(view, {"ins:1", "ins:2"})
        self.assertEqual(repr(view), "_Keys(['ins:1', 'ins:2'])")

    def test_deleting_while_looping_over_keys(self):
        for index in range(5):
            self.db[f"k{index}"] = index
        for key in self.db.keys(prefix="k"):
            del self.db[key]
        self.assertEqual(len(self.db), 0)

    def test_query_plan_uses_the_index(self):
        self.db["ins:1"] = 1
        with mock.patch.object(self.db, "_execute", wraps=self.db._execute) as execute:
            list(self.db.keys(prefix="ins:"))
        statement, parameters = execute.call_args.args
        plan = self.db.raw.execute("EXPLAIN QUERY PLAN " + statement, parameters).fetchall()
        self.assertIn("PRIMARY KEY", " ".join(str(row) for row in plan))
        self.assertNotIn("SCAN", " ".join(str(row) for row in plan))


class TableTest(StoreCase):
    def test_tables_in_one_file_are_separate(self):
        downloads = kv.open(self.file, table="downloads")
        try:
            self.db["k"] = "default"
            downloads["k"] = "downloads"
            downloads["only"] = 1
            self.assertEqual(self.db["k"], "default")
            self.assertEqual(downloads["k"], "downloads")
            self.assertEqual(list(self.db), ["k"])
            self.assertEqual(len(downloads), 2)
        finally:
            downloads.close()


    def test_table_shares_the_connection(self):
        downloads = self.db.table("downloads")
        self.db["k"] = "default"
        downloads["k"] = "downloads"
        self.assertIs(downloads.raw, self.db.raw)
        self.assertEqual(self.db["k"], "default")
        self.assertEqual(downloads["k"], "downloads")
        self.assertEqual(downloads.table("default")["k"], "default")

    def test_table_sweeps_its_expired_keys(self):
        with mock.patch("shared._kv._now", return_value=START):
            self.db.table("other").set("old", 1, ttl=1)
        with mock.patch("shared._kv._now", return_value=START + 2):
            self.db.table("other")
        self.assertEqual(self.db.raw.execute("SELECT COUNT(*) FROM kv").fetchone()[0], 0)

    def test_closing_a_table_leaves_the_rest_open(self):
        with self.db.table("downloads") as downloads:
            downloads["k"] = 1
        with self.assertRaises(kv.ClosedError):
            downloads["k"]
        self.db["k"] = 2
        self.assertEqual(self.db.table("downloads")["k"], 1)

    def test_closing_the_opened_store_closes_its_tables(self):
        downloads = self.db.table("downloads")
        nested = downloads.table("nested")
        self.db.close()
        for store in (downloads, nested):
            with self.subTest(store._table), self.assertRaises(kv.ClosedError):
                store["k"] = 1
        with self.assertRaises(sqlite3.ProgrammingError):
            downloads.raw.execute("SELECT 1")


class ThreadTest(StoreCase):
    def test_many_threads_write_without_losing_rows(self):
        errors = []

        def write_many(worker_number):
            try:
                for index in range(500):
                    self.db[f"{worker_number}:{index}"] = index
            except Exception as error:
                errors.append(error)

        threads = [threading.Thread(target=write_many, args=(number,)) for number in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(errors, [])
        self.assertEqual(len(self.db), 8 * 500)
        self.assertEqual(self.db["7:499"], 499)


    def test_threads_set_and_get_on_one_store_and_its_table(self):
        errors = []
        other = self.db.table("other")

        def use(worker_number):
            store = other if worker_number % 2 else self.db
            try:
                for index in range(200):
                    store.set(f"{worker_number}:{index}", index)
                    if store[f"{worker_number}:{index}"] != index:
                        errors.append((worker_number, index))
                    list(store.keys(prefix=f"{worker_number}:"))
            except Exception as error:
                errors.append(error)

        _run_threads(use, 8)
        self.assertEqual(errors, [])
        self.assertEqual(len(self.db) + len(other), 8 * 200)

    def test_two_stores_of_one_file_wait_instead_of_failing(self):
        """Two connections to one file, as two flnd commands at once: before the busy timeout, "database is locked"."""
        errors = []
        stores = [self.db, kv.open(self.file)]
        try:
            def use(worker_number):
                store = stores[worker_number % 2]
                try:
                    for index in range(200):
                        store[f"{worker_number}:{index}"] = index
                        store.get(f"{worker_number}:{index}")
                except Exception as error:
                    errors.append(error)

            _run_threads(use, 8)
        finally:
            stores[1].close()
        self.assertEqual(errors, [])
        self.assertEqual(len(self.db), 8 * 200)

    def test_a_write_waits_for_another_connections_lock(self):
        # The timer's thread commits, so the connection must allow another thread.
        holder = sqlite3.connect(self.file, isolation_level=None, check_same_thread=False)
        holder.execute("BEGIN IMMEDIATE")  # holds the write lock, as another process's write would
        released = threading.Timer(0.3, holder.execute, ("COMMIT",))
        released.start()
        try:
            self.db["k"] = 1
        finally:
            released.join()
            holder.close()
        self.assertEqual(self.db["k"], 1)

    def test_the_busy_timeout_ends_the_wait(self):
        holder = sqlite3.connect(self.file, isolation_level=None)
        holder.execute("BEGIN IMMEDIATE")
        try:
            # `open` already writes, sweeping expired keys, so it is the call that gives up.
            with self.assertRaisesRegex(sqlite3.OperationalError, "locked"):
                kv.open(self.file, timeout=0.05).close()
        finally:
            holder.close()


class FormatTest(unittest.TestCase):
    def test_reads_a_file_written_before_the_redesign(self):
        """Every `.flnd/*.db` was written in this schema since Phase 4, and must keep reading."""
        with tempfile.TemporaryDirectory() as folder:
            file = Path(folder) / "history.db"
            old = sqlite3.connect(file)
            old.execute("CREATE TABLE kv (tbl TEXT NOT NULL, key TEXT NOT NULL, value TEXT NOT NULL, "
                        "expires REAL, PRIMARY KEY (tbl, key)) WITHOUT ROWID")
            old.executemany("INSERT INTO kv VALUES (?, ?, ?, ?)", [
                ("downloads", "ins:1", json.dumps({"path": "ins/example/a.jpg"}), None),
                ("downloads", "ins:2", json.dumps([1, "é"], ensure_ascii=False), 4_000_000_000.0),
                ("targets", "ins:example", json.dumps({"image": "webp:mid"}), None),
            ])
            old.commit()
            old.close()
            with kv.open(file, table="downloads") as history:
                self.assertEqual(dict(history), {"ins:1": {"path": "ins/example/a.jpg"}, "ins:2": [1, "é"]})
                self.assertEqual(history.table("targets")["ins:example"], {"image": "webp:mid"})
                history["ins:3"] = 3
            raw = sqlite3.connect(file)
            try:
                self.assertEqual(raw.execute("SELECT value, expires FROM kv WHERE key = 'ins:3'").fetchone(), ("3", None))
            finally:
                raw.close()


class LifecycleTest(unittest.TestCase):
    def test_reopening_keeps_the_data(self):
        with tempfile.TemporaryDirectory() as folder:
            file = Path(folder) / "a" / "b" / "state.db"
            with kv.open(file) as db:
                db["k"] = [1, 2]
            self.assertTrue(file.exists())
            with kv.open(file) as db:
                self.assertEqual(db["k"], [1, 2])
                self.assertIsInstance(db.raw, sqlite3.Connection)
                self.assertEqual(db.raw.execute("PRAGMA journal_mode").fetchone()[0], "wal")

    def test_use_after_close_raises(self):
        with tempfile.TemporaryDirectory() as folder:
            db = kv.open(Path(folder) / "state.db")
            db.close()
            db.close()
            uses = {
                "get": lambda: db["k"],
                "set": lambda: db.set("k", 1),
                "len": lambda: len(db),
                "keys": lambda: list(db.keys()),
                "table": lambda: db.table("other"),
            }
            for name, use in uses.items():
                with self.subTest(name), self.assertRaises(kv.ClosedError):
                    use()


def _run_threads(target, count):
    threads = [threading.Thread(target=target, args=(number,)) for number in range(count)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()


if __name__ == "__main__":
    unittest.main()
