import collections.abc
import sqlite3
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

# `shared/__init__.py` exposes `kv` once flnd moves off `store`; until then it is imported directly.
from shared import kv

START = 1_000_000.0


class KVCase(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.file = Path(self.folder.name) / "nested" / "state.db"
        self.db = kv.open(self.file)

    def tearDown(self):
        self.db.close()
        self.folder.cleanup()


class MappingTest(KVCase):
    def test_is_a_mutable_mapping(self):
        self.assertIsInstance(self.db, collections.abc.MutableMapping)
        self.assertTrue(issubclass(kv.KV, collections.abc.MutableMapping))

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

    def test_keys_must_be_str(self):
        with self.assertRaises(TypeError):
            self.db[1] = "x"  # pyright: ignore[reportArgumentType]


class ValueTest(KVCase):
    def test_json_round_trip(self):
        value = {"list": [1, 2.5, None, True], "text": "é", "nested": {"a": []}}
        self.db["k"] = value
        self.assertEqual(self.db["k"], value)
        self.assertEqual(self.db.raw.execute("SELECT value FROM kv").fetchone()[0].count("é"), 1)

    def test_value_that_is_not_json(self):
        for value in (object(), {1, 2}, b"bytes"):
            with self.subTest(value=value), self.assertRaises(TypeError):
                self.db["k"] = value
        self.assertNotIn("k", self.db)


class ExpiryTest(KVCase):
    def test_ttl_expires_with_the_clock(self):
        with mock.patch("shared._kv._now", return_value=START):
            self.db.set("loi:quota", 3, ttl=1)
            self.db.set("forever", 1)
            self.assertEqual(self.db["loi:quota"], 3)
        with mock.patch("shared._kv._now", return_value=START + 0.5):
            self.assertIn("loi:quota", self.db)
        with mock.patch("shared._kv._now", return_value=START + 1):
            self.assertEqual(len(self.db), 1)
            self.assertEqual(self.db.get_keys(), ["forever"])
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


class PrefixTest(KVCase):
    def test_results(self):
        for key in ("ins:1", "ins:2", "insx", "tik:1", "in"):
            self.db[key] = key.upper()
        self.assertEqual(self.db.get_keys(prefix="ins:"), ["ins:1", "ins:2"])
        self.assertEqual(self.db.get_keys(prefix="tik"), ["tik:1"])
        self.assertEqual(self.db.get_keys(prefix="zzz"), [])
        self.assertEqual(self.db.get_keys(), ["in", "ins:1", "ins:2", "insx", "tik:1"])

    def test_prefix_ending_in_the_last_code_point(self):
        self.db["a\U0010ffffz"] = 1
        self.db["b"] = 2
        self.assertEqual(self.db.get_keys(prefix="a\U0010ffff"), ["a\U0010ffffz"])

    def test_query_plan_uses_the_index(self):
        self.db["ins:1"] = 1
        with mock.patch.object(self.db, "_execute", wraps=self.db._execute) as execute:
            self.db.get_keys(prefix="ins:")
        statement, parameters = execute.call_args.args
        plan = self.db.raw.execute("EXPLAIN QUERY PLAN " + statement, parameters).fetchall()
        self.assertIn("PRIMARY KEY", " ".join(str(row) for row in plan))
        self.assertNotIn("SCAN", " ".join(str(row) for row in plan))


class TableTest(KVCase):
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


class ThreadTest(KVCase):
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
                "keys": lambda: db.get_keys(),
            }
            for name, use in uses.items():
                with self.subTest(name), self.assertRaises(RuntimeError):
                    use()


if __name__ == "__main__":
    unittest.main()
