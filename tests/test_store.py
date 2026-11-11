import contextlib
import datetime
import os
import sqlite3
import stat
import tempfile
import threading
import unittest
from pathlib import Path

from shared import crypto, store

try:
    import cryptography  # noqa: F401
    HAS_CRYPTOGRAPHY = True
except ImportError:
    HAS_CRYPTOGRAPHY = False


class StoreCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.file = Path(self.tmp.name) / "nested" / "state.db"
        self.db = store.open(self.file)

    def tearDown(self):
        self.db.close()
        self.tmp.cleanup()


class TableTest(StoreCase):
    def test_mapping_basics(self):
        seen = self.db.table("downloads")
        seen["ins:3210"] = {"path": "ins/a.jpg", "size": 12345}
        self.assertIn("ins:3210", seen)
        self.assertNotIn("ins:9", seen)
        self.assertEqual(seen["ins:3210"], {"path": "ins/a.jpg", "size": 12345})
        self.assertEqual(seen.get("ins:9", {}), {})
        self.assertEqual(len(seen), 1)
        self.assertEqual(list(seen), ["ins:3210"])
        del seen["ins:3210"]
        self.assertEqual(len(seen), 0)
        with self.assertRaises(KeyError):
            del seen["ins:3210"]
        with self.assertRaises(KeyError):
            seen["missing"]

    def test_prefix_queries(self):
        table = self.db.table("t")
        for key in ("ins:1", "ins:2", "insx", "tik:1", "in"):
            table[key] = key.upper()
        self.assertEqual(table.keys(prefix="ins:"), ["ins:1", "ins:2"])
        self.assertEqual(table.values(prefix="ins:"), ["INS:1", "INS:2"])
        self.assertEqual(table.items(prefix="tik"), [("tik:1", "TIK:1")])
        self.assertEqual(len(table.keys()), 5)
        plan = self.db.sql.execute(
            "EXPLAIN QUERY PLAN SELECT key, value FROM kv WHERE ns = ? AND key >= ? AND key < ?",
            ("t", "ins:", "ins;")).fetchall()
        self.assertIn("PRIMARY KEY", " ".join(str(row) for row in plan))

    def test_prefix_ending_in_last_code_point(self):
        table = self.db.table("t")
        table["a\U0010ffffz"] = 1
        table["b"] = 2
        self.assertEqual(table.keys(prefix="a\U0010ffff"), ["a\U0010ffffz"])

    def test_json_round_trip(self):
        table = self.db.table("t")
        value = {"list": [1, 2.5, None, True], "text": "é", "nested": {"a": []}}
        table["k"] = value
        self.assertEqual(table["k"], value)

    def test_value_that_is_not_json(self):
        with self.assertRaises(TypeError):
            self.db.table("t")["k"] = object()
        with self.assertRaises(TypeError):
            self.db.table("t")[1] = "x"

    def test_mtime(self):
        table = self.db.table("t")
        self.assertIsNone(table.mtime("k"))
        before = datetime.datetime.now() - datetime.timedelta(seconds=1)
        table["k"] = 1
        self.assertGreaterEqual(table.mtime("k"), before)

    def test_tables_are_separate(self):
        self.db.table("a")["k"] = 1
        self.assertNotIn("k", self.db.table("b"))
        self.assertEqual(len(self.db.table("b")), 0)


class BatchTest(StoreCase):
    def other_count(self) -> int:
        with contextlib.closing(sqlite3.connect(self.file)) as other:
            return other.execute("SELECT COUNT(*) FROM kv").fetchone()[0]

    def test_commits_once_at_the_end(self):
        table = self.db.table("t")
        with self.db.batch():
            for n in range(10):
                table[str(n)] = n
            self.assertTrue(self.db.sql.in_transaction)
            self.assertEqual(self.other_count(), 0)
        self.assertEqual(self.other_count(), 10)

    def test_rolls_back_when_the_block_raises(self):
        table = self.db.table("t")
        table["kept"] = 1
        with self.assertRaises(RuntimeError):
            with self.db.batch():
                table["lost"] = 2
                raise RuntimeError("boom")
        self.assertEqual(table.keys(), ["kept"])
        self.assertFalse(self.db.sql.in_transaction)

    def test_nested_batch_joins_the_outer(self):
        table = self.db.table("t")
        with self.db.batch():
            with self.db.batch():
                table["a"] = 1
            self.assertTrue(self.db.sql.in_transaction)
        self.assertEqual(self.other_count(), 1)


class ThreadsAndFilesTest(StoreCase):
    def test_writes_from_several_threads(self):
        table = self.db.table("t")

        def write(thread: int):
            for n in range(50):
                table[f"{thread}:{n}"] = n

        threads = [threading.Thread(target=write, args=(t,)) for t in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(len(table), 400)

    def test_reopen_keeps_data(self):
        self.db.table("t")["k"] = [1]
        self.db.close()
        with store.open(self.file) as again:
            self.assertEqual(again.table("t")["k"], [1])
        self.db = store.open(self.file)

    def test_sql_custom_table(self):
        self.db.sql.execute("CREATE TABLE notes (id INTEGER PRIMARY KEY, body TEXT)")
        self.db.sql.execute("INSERT INTO notes (body) VALUES (?)", ("hi",))
        self.assertEqual(self.db.sql.execute("SELECT body FROM notes").fetchall(), [("hi",)])
        self.assertIsInstance(self.db.sql, sqlite3.Connection)

    def test_wal_mode(self):
        self.assertEqual(self.db.sql.execute("PRAGMA journal_mode").fetchone()[0], "wal")


@unittest.skipUnless(HAS_CRYPTOGRAPHY, "cryptography is not installed")
class SecretsTest(StoreCase):
    def raw_values(self) -> list[str]:
        return [row[0] for row in self.db.sql.execute("SELECT value FROM kv WHERE ns = 'secret:'")]

    def test_round_trip_and_no_plain_text(self):
        secrets = self.db.secrets()
        secrets["ins:sessionid"] = "abc-plain-text"
        self.assertEqual(secrets["ins:sessionid"], "abc-plain-text")
        self.assertNotIn("abc-plain-text", "".join(self.raw_values()))
        self.assertNotIn(b"abc-plain-text", self.file.read_bytes())
        with self.assertRaises(TypeError):
            secrets["n"] = 1

    def test_key_file_mode(self):
        self.db.secrets()["k"] = "v"
        key_file = self.file.with_name("state.db.key")
        self.assertEqual(len(key_file.read_bytes()), 32)
        if os.name == "posix":
            self.assertEqual(stat.S_IMODE(key_file.stat().st_mode), 0o600)
        self.assertEqual(self.db.secrets()["k"], "v")

    def test_wrong_key(self):
        self.db.secrets(crypto.keygen())["k"] = "v"
        with self.assertRaises(crypto.CryptoError):
            self.db.secrets(crypto.keygen())["k"]

    def test_password_and_stored_salt(self):
        self.db.secrets(password="pw")["k"] = "v"
        salt = self.db.table("meta")["secrets.salt"]
        self.assertEqual(self.db.secrets(password="pw")["k"], "v")
        self.assertEqual(self.db.table("meta")["secrets.salt"], salt)
        with self.assertRaises(crypto.CryptoError):
            self.db.secrets(password="other")["k"]
        with self.assertRaises(ValueError):
            self.db.secrets(crypto.keygen(), password="pw")


if __name__ == "__main__":
    unittest.main()
