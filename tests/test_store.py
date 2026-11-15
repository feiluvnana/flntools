"""Tests for `shared._store`: the atomic JSON key-value store."""

import collections.abc
import json
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

import shared
from shared import _store as store


class StoreCase(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.file = Path(self.folder.name) / "nested" / "state.json"
        self.db = store.open(self.file)

    def tearDown(self):
        self.db.close()
        self.folder.cleanup()


class MappingTest(StoreCase):
    def test_mutable_mapping_protocol(self):
        self.assertIsInstance(self.db, collections.abc.MutableMapping)
        self.db["a"] = {"path": "x.jpg"}
        self.db["b"] = 2
        self.assertEqual(self.db["a"], {"path": "x.jpg"})
        self.assertIn("a", self.db)
        self.assertNotIn("z", self.db)
        self.assertNotIn(1, self.db)
        self.assertEqual(len(self.db), 2)
        self.assertEqual(list(self.db), ["a", "b"])
        self.assertEqual(list(self.db.keys()), ["a", "b"])
        self.assertEqual(list(self.db.values()), [{"path": "x.jpg"}, 2])
        self.assertEqual(dict(self.db.items()), {"a": {"path": "x.jpg"}, "b": 2})
        self.assertEqual(self.db.get("z", default="none"), "none")
        del self.db["a"]
        self.assertEqual(len(self.db), 1)
        with self.assertRaises(KeyError):
            del self.db["a"]
        with self.assertRaises(KeyError):
            _ = self.db["a"]

    def test_update_and_clear(self):
        self.db.update({"a": 1, "b": 2}, c=3)
        self.assertEqual(dict(self.db), {"a": 1, "b": 2, "c": 3})
        self.db.clear()
        self.assertEqual(len(self.db), 0)

    def test_update_bad_value_rolls_back_without_writing(self):
        self.db["keep"] = 1
        with self.assertRaises(store.Error):
            self.db.update([("ok", 2), ("bad", object())])
        self.assertEqual(dict(self.db), {"keep": 1})
        on_disk = json.loads(self.file.read_text(encoding="utf-8"))
        self.assertEqual(on_disk, {"kv": {"keep": 1}})


class PersistenceTest(StoreCase):
    def test_file_is_readable_json_and_reloads(self):
        self.db["a"] = {"name": "example"}
        self.assertEqual(
            json.loads(self.file.read_text(encoding="utf-8")),
            {"kv": {"a": {"name": "example"}}},
        )
        self.db.close()
        again = store.open(self.file)
        self.addCleanup(again.close)
        self.assertEqual(again["a"], {"name": "example"})

    def test_tables_share_one_file(self):
        sub = self.db.table("sub")
        sub["x"] = 1
        self.db["y"] = 2
        self.assertEqual(
            json.loads(self.file.read_text(encoding="utf-8")),
            {"kv": {"y": 2}, "sub": {"x": 1}},
        )
        self.assertEqual(len(self.db), 1)
        self.assertEqual(len(sub), 1)

    def test_keys_filter_by_prefix(self):
        self.db.update({"ab": 1, "ac": 2, "bd": 3})
        self.assertEqual(list(self.db.keys("a")), ["ab", "ac"])
        self.assertEqual(list(self.db.keys()), ["ab", "ac", "bd"])


class ValuesTest(StoreCase):
    def test_keys_must_be_strings(self):
        with self.assertRaises(store.Error):
            self.db[1] = "x"  # pyright: ignore[reportArgumentType]

    def test_unserializable_value_is_a_store_error(self):
        self.db["ok"] = 1
        with self.assertRaises(store.Error) as ctx:
            self.db["bad"] = object()
        self.assertIsInstance(ctx.exception, shared.Error)
        self.assertEqual(self.db["ok"], 1)
        self.assertNotIn("bad", self.db)


class ErrorsAndThreadsTest(StoreCase):
    def test_closed_store_raises_closed_error(self):
        self.db["y"] = 1
        sub = self.db.table("sub")
        sub.close()
        with self.assertRaises(store.ClosedError) as ctx:
            _ = sub["x"]
        self.assertIsInstance(ctx.exception, store.Error)
        self.assertIsInstance(ctx.exception, RuntimeError)
        self.assertEqual(self.db["y"], 1)

        self.db.close()
        with self.assertRaises(store.ClosedError):
            self.db["x"] = 1

    def test_bad_file_is_wrapped_in_store_error(self):
        self.file.parent.mkdir(parents=True, exist_ok=True)
        self.file.write_text("not json", encoding="utf-8")
        with self.assertRaises(store.Error) as ctx:
            store.open(self.file)
        self.assertIsInstance(ctx.exception, shared.Error)
        self.assertIsInstance(ctx.exception.__cause__, ValueError)

    def test_os_errors_are_wrapped_in_store_error(self):
        with mock.patch.object(Path, "mkdir", side_effect=OSError("no perms")):
            with self.assertRaises(store.Error) as ctx:
                self.db["x"] = 1
        self.assertIsInstance(ctx.exception.__cause__, OSError)

    def test_concurrent_threads_read_and_write(self):
        errors: list[BaseException] = []

        def worker(idx: int) -> None:
            try:
                for step in range(20):
                    key = f"t{idx}:{step}"
                    self.db[key] = step
                    _ = self.db.get(key)
            except BaseException as err:
                errors.append(err)

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(6)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(errors, [])
        self.assertEqual(len(self.db), 120)


if __name__ == "__main__":
    unittest.main()
