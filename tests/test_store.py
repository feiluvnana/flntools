"""Tests for `shared._store`: WAL SQLite + `msgspec.json` TTL KV store."""

import collections.abc
import sqlite3
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

import msgspec

import shared
from shared import _clock as clock
from shared import _store as store


class Item(msgspec.Struct):
    name: str
    count: int


def at(seconds: float):
    fake = clock.Fake()
    fake.advance(seconds)
    return clock.use(fake)


class StoreCase(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.file = Path(self.folder.name) / "nested" / "state.db"
        self.db = store.open(self.file)

    def tearDown(self):
        self.db.close()
        self.folder.cleanup()

    def stored_keys(self) -> list[str]:
        rows = self.db.raw.execute("SELECT key FROM kv ORDER BY key").fetchall()
        return [key for (key,) in rows]


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
        with self.assertRaises(store.UnstorableError):
            self.db.update([("ok", 2), ("bad", object())])
        self.assertEqual(dict(self.db), {"keep": 1})


class SchemaAndValuesTest(StoreCase):
    def test_wal_and_synchronous_normal(self):
        mode = self.db.raw.execute("PRAGMA journal_mode").fetchone()[0]
        sync = self.db.raw.execute("PRAGMA synchronous").fetchone()[0]
        self.assertEqual(mode.lower(), "wal")
        self.assertEqual(sync, 1)  # 1 == NORMAL

    def test_schema_typed_round_trip(self):
        with store.open(self.file, table="items", schema=Item) as typed:
            typed["one"] = Item(name="alpha", count=7)
            got = typed["one"]
            self.assertIsInstance(got, Item)
            self.assertEqual(got, Item(name="alpha", count=7))

    def test_surrogate_safe_encoding_and_bad_keys(self):
        self.db["ok"] = {"name": "bad\udc80name"}
        self.assertEqual(self.db["ok"], {"name": "bad\udc80name"})
        with self.assertRaises(store.UnstorableError):
            self.db["bad\udc80key"] = 1
        with self.assertRaises(store.UnstorableError):
            self.db[1] = "x"  # pyright: ignore[reportArgumentType]
        with self.assertRaises(store.UnstorableError):
            self.db["bad"] = object()


class ExpiryAndReadsTest(StoreCase):
    def test_reads_never_take_write_locks_and_prune_removes_expired(self):
        with at(0):
            self.db.set("old", 1, ttl=1)
            self.db.set("new", 2, ttl=100)
        with at(5):
            # Reads return expired as missing without deleting rows.
            self.assertNotIn("old", self.db)
            self.assertIsNone(self.db.get("old"))
            self.assertEqual(list(self.db.items()), [("new", 2)])
            self.assertEqual(list(self.db.values()), [2])
            self.assertEqual(self.stored_keys(), ["new", "old"])

            removed = self.db.prune()
            self.assertEqual(removed, 1)
            self.assertEqual(self.stored_keys(), ["new"])

    def test_prefix_near_surrogates(self):
        self.db["\ud7ffa"] = 1
        self.db["\ue000a"] = 2
        self.assertEqual(list(self.db.keys("\ud7ff")), ["\ud7ffa"])


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

    def test_sqlite_and_os_errors_wrapped_in_store_error(self):
        err = sqlite3.OperationalError("bad")
        with mock.patch.object(store.sqlite3, "connect", side_effect=err):
            with self.assertRaises(store.Error) as ctx:
                store.open(self.file)
        self.assertIsInstance(ctx.exception, shared.Error)
        self.assertIsInstance(ctx.exception.__cause__, sqlite3.OperationalError)

        with mock.patch("pathlib.Path.mkdir", side_effect=OSError("no perms")):
            with self.assertRaises(store.Error) as ctx:
                store.open(self.file)
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
