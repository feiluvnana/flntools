"""flnd/_db.py, the SQLite store library: lazy files, transactions, threads, read-only opens."""

import tempfile
import threading
import unittest
from pathlib import Path

from flnd import _db
from flnd.errors import DiskError

SCHEMA = "CREATE TABLE IF NOT EXISTS t (k TEXT PRIMARY KEY, v INTEGER)"


class DbTest(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.path = Path(folder.name) / "a" / "x.db"
        self.db = _db.Db(self.path, schema=SCHEMA)
        self.addCleanup(self.db.close)

    def test_reads_of_a_missing_file_create_nothing(self):
        self.assertEqual((self.db.one("SELECT v FROM t", ()), self.db.all("SELECT v FROM t")),
                         (None, []))
        self.assertFalse(self.path.parent.exists())

    def test_a_transaction_commits_once_and_rolls_back_on_an_error(self):
        with self.db.transaction() as sql:
            sql.execute("INSERT INTO t VALUES ('a', 1)")
            # A nested block joins the outer transaction.
            self.db.write("INSERT INTO t VALUES ('b', 2)")
        with self.assertRaises(ValueError), self.db.transaction() as sql:
            sql.execute("INSERT INTO t VALUES ('c', 3)")
            raise ValueError
        with self.assertRaises(DiskError):
            self.db.write("INSERT INTO t VALUES ('a', 9)")
        self.assertEqual(self.db.all("SELECT k, v FROM t ORDER BY k"), [("a", 1), ("b", 2)])

    def test_threads_share_one_connection(self):
        def write(start):
            for n in range(start, start + 200):
                self.db.write("INSERT INTO t VALUES (?, ?)", (str(n), n))

        threads = [threading.Thread(target=write, args=(n * 1000,)) for n in range(4)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(self.db.one("SELECT count(*) FROM t", ()), (800,))

    def test_a_closed_db_refuses_and_setup_runs_once(self):
        calls = []
        db = _db.Db(self.path, schema=SCHEMA, setup=calls.append)
        db.write("INSERT INTO t VALUES ('a', 1)")
        db.one("SELECT v FROM t", ())
        db.close()
        self.assertEqual(len(calls), 1)
        with self.assertRaises(RuntimeError):
            db.write("INSERT INTO t VALUES ('b', 2)")


if __name__ == "__main__":
    unittest.main()
