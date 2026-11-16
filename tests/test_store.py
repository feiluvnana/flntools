"""Tests for `shared.store`: tables of JSON values and typed rows in one SQLite file."""

import collections.abc
import json
import sqlite3
import tempfile
import threading
import time
import unittest
from contextlib import closing
from pathlib import Path
from unittest import mock

import shared
from shared import store


class File(store.Row):
    table = "files"
    state: str
    size: int
    mtime: int = 0


class StoreCase(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.file = Path(self.folder.name) / "nested" / "state.db"
        self.db = store.open(self.file)
        self.kv = self.db["kv"]

    def tearDown(self):
        self.db.close()
        self.folder.cleanup()


class MappingTest(StoreCase):
    def test_mutable_mapping_protocol(self):
        self.assertIsInstance(self.kv, collections.abc.MutableMapping)
        self.kv["b"] = 2
        self.kv["a"] = {"path": "x.jpg"}
        self.assertEqual(self.kv["a"], {"path": "x.jpg"})
        self.assertIn("a", self.kv)
        self.assertNotIn("z", self.kv)
        self.assertNotIn(1, self.kv)
        self.assertEqual(len(self.kv), 2)
        self.assertEqual(list(self.kv), ["a", "b"])
        self.assertEqual(list(self.kv.values()), [{"path": "x.jpg"}, 2])
        self.assertEqual(dict(self.kv.items()), {"a": {"path": "x.jpg"}, "b": 2})
        self.assertEqual(self.kv.get("z", "none"), "none")
        del self.kv["a"]
        self.assertNotIn("a", self.kv)
        with self.assertRaises(KeyError):
            del self.kv["a"]
        self.kv.update({"c": 3}, d=4)
        self.kv.clear()
        self.assertEqual(len(self.kv), 0)

    def test_an_unwritten_table_reads_empty(self):
        self.assertEqual(len(self.db["nothing"]), 0)
        self.assertEqual(self.db["nothing"].rows.get(), [])
        self.assertNotIn("x", self.db["nothing"])

    def test_tables_are_separate_and_listed(self):
        self.db["one"]["k"] = 1
        self.db["two"]["k"] = 2
        self.assertEqual((self.db["one"]["k"], self.db["two"]["k"]), (1, 2))
        self.assertEqual(self.db.tables, ["one", "two"])

    def test_values_survive_reopening(self):
        self.kv["a"] = [1, "two", None, True]
        self.db.close()
        with store.open(self.file) as again:
            self.assertEqual(again["kv"]["a"], [1, "two", None, True])

    def test_bad_keys_values_and_names_raise_store_errors(self):
        with self.assertRaises(store.Error):
            self.kv[1] = "x"  # type: ignore[index]
        with self.assertRaises(store.Error):
            self.kv["x"] = object()
        with self.assertRaises(store.Error):
            self.db['bad"name']
        self.assertTrue(issubclass(store.Error, shared.Error))

    def test_a_closed_db_refuses_calls(self):
        self.db.close()
        with self.assertRaises(store.ClosedError):
            self.kv["a"] = 1


class RowTest(StoreCase):
    def test_a_row_type_names_its_table_and_decodes(self):
        files = self.db[File]
        files["ins/a.jpg"] = File("kept", 1000, 17)
        self.assertEqual(files["ins/a.jpg"], File("kept", 1000, 17))
        self.assertEqual(self.db["files"]["ins/a.jpg"], {"state": "kept", "size": 1000, "mtime": 17})
        self.assertIs(self.db[File], files)


class RowsTest(StoreCase):
    def setUp(self):
        super().setUp()
        self.files = self.db[File]
        for key, row in {
            "ins/a.jpg": File("kept", 5), "ins/b.jpg": File("failed", 9),
            "twt/c.mp4": File("failed", 1), "ins/d.png": File("kept", 7),
        }.items():
            self.files[key] = row

    def keys(self, query):
        return [key for key, _ in query.get()]

    def test_prefix_filter_sort_and_limit_run_in_sql(self):
        rows = self.files.rows
        self.assertEqual(self.keys(rows.prefix("ins/")), ["ins/a.jpg", "ins/b.jpg", "ins/d.png"])
        self.assertEqual(self.keys(rows.filter(state="failed")), ["ins/b.jpg", "twt/c.mp4"])
        self.assertEqual(self.keys(rows.prefix("ins/").filter(state="kept").sort("size", desc=True)),
                         ["ins/d.png", "ins/a.jpg"])
        self.assertEqual(self.keys(rows.sort("size").limit(2)), ["twt/c.mp4", "ins/a.jpg"])
        self.assertEqual(rows.prefix("ins/").count, 3)
        self.assertEqual(rows.filter(state="failed").first, ("ins/b.jpg", File("failed", 9)))

    def test_a_predicate_sees_key_and_value(self):
        query = self.files.rows.filter(lambda pair: pair[1].size > 6).sort("key", desc=True)
        self.assertEqual(self.keys(query), ["ins/d.png", "ins/b.jpg"])

    def test_json_tables_filter_by_field(self):
        self.kv["a"] = {"sync": True}
        self.kv["b"] = {"sync": False}
        self.kv["c"] = {}
        self.assertEqual(self.keys(self.kv.rows.filter(sync=True)), ["a"])
        self.assertEqual(self.keys(self.kv.rows.filter(sync=None)), ["c"])


class BatchTest(StoreCase):
    def test_a_batch_commits_once_and_rolls_back_on_error(self):
        with self.db.batch():
            self.kv["a"] = 1
            with self.db.batch():
                self.kv["b"] = 2
        self.assertEqual(len(self.kv), 2)
        with self.assertRaises(RuntimeError):
            with self.db.batch():
                self.kv["c"] = 3
                raise RuntimeError("boom")
        self.assertNotIn("c", self.kv)

    def test_a_begin_that_fails_leaves_later_batches_whole(self):
        with mock.patch("shared._store.BUSY_MS", 50), store.open(self.file.with_name("busy.db")) as db:
            db["kv"]["a"] = 1
            with closing(sqlite3.connect(db.path, isolation_level=None)) as other:
                # Another writer holds the file past the busy timeout, so BEGIN fails.
                other.execute("BEGIN IMMEDIATE")
                with self.assertRaises(store.Error):
                    with db.batch():
                        pass
                other.execute("ROLLBACK")
            with self.assertRaises(RuntimeError):
                with db.batch():
                    db["kv"]["b"] = 2
                    raise RuntimeError("boom")
            self.assertNotIn("b", db["kv"])

    def test_a_table_made_in_a_rolled_back_batch_works_after(self):
        with self.assertRaises(RuntimeError):
            with self.db.batch():
                self.db["new"]["k"] = 1
                raise RuntimeError("boom")
        self.assertNotIn("k", self.db["new"])
        self.db["new"]["k"] = 2
        self.assertEqual(self.db["new"]["k"], 2)

    def test_threads_write_without_losing_rows(self):
        def write(start):
            for n in range(start, start + 200):
                self.kv[str(n)] = n

        threads = [threading.Thread(target=write, args=(n * 200,)) for n in range(4)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(len(self.kv), 800)

    def test_100k_batched_writes_and_reads_are_fast(self):
        start = time.perf_counter()
        with self.db.batch():
            for n in range(100_000):
                self.kv[f"k{n}"] = {"n": n}
        self.assertEqual(len(dict(self.kv.items())), 100_000)
        self.assertEqual(self.kv["k99999"], {"n": 99999})
        self.assertLess(time.perf_counter() - start, 5.0)


class ConvertTest(unittest.TestCase):
    def test_a_json_store_is_converted_in_place(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "old.db"
            path.write_text(json.dumps({"downloads": {"a": {"path": "x"}}, "other": {"b": 2}}))
            with store.open(path) as db:
                self.assertEqual(db["downloads"]["a"], {"path": "x"})
                self.assertEqual(db["other"]["b"], 2)
            with sqlite3.connect(path) as raw:
                raw.execute("SELECT 1")
            self.assertEqual(sorted(p.name for p in Path(folder).iterdir() if not p.name.startswith("old.db")), [])

    def test_a_damaged_json_store_raises(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "bad.db"
            path.write_text("{nope")
            with self.assertRaises(store.Error):
                store.open(path)


if __name__ == "__main__":
    unittest.main()
