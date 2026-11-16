"""Benchmark group `db`: the history store over 100k rows, and what a commit costs per row.

Part one is `flnd._db`, which flnd keeps its history in today: inserts, point reads,
membership, a prefix scan and a filter, over 100,000 rows shaped like history entries.

Part two is raw SQLite in WAL mode, the way the new `flnd.db` will run it: 50,000 rows of a
`files` table committed one per row, one per post (2 files), and one per 500 rows. 50,000 rows
make the WAL pass its automatic checkpoint (1,000 pages) many times, so the cost of checkpoints
is inside the numbers rather than after them. `synchronous=FULL` is timed once per row, to show
what durability on every commit would cost.

Every number is the best of 3 runs, each run on a fresh database in a scratch folder.
"""

from __future__ import annotations

import os
import random
import sqlite3
import tempfile
from pathlib import Path

import kit
from flnd import _db as store

ROWS = 100_000
# 10,000 random keys read back, seeded so every run reads the same ones.
PROBES = 10_000
COMMIT_ROWS = 50_000
FILES = ("CREATE TABLE files (key TEXT PRIMARY KEY, target TEXT, post_id TEXT, path TEXT,"
         " size INT, mtime REAL, hash BLOB)")
INSERT = "INSERT INTO files VALUES (?,?,?,?,?,?,?)"


def history(report: kit.Report, scratch: Path) -> None:
    """Time `flnd._db` over 100k history-shaped rows.

    Args:
        report: where the numbers go.
        scratch: a folder for the database files.
    """
    rows = {f"ins:{n:08d}": {"path": f"/m/ins/example_user/{n}.jpg", "size": n,
                             "date": "2026-01-02", "id": str(n)} for n in range(ROWS)}
    probes = random.Random(1).sample(list(rows), PROBES)
    made = 0

    def fill() -> None:
        nonlocal made
        made += 1
        with store.open(scratch / f"fill{made}.db") as db, db.batch():
            table = db["history"]
            for key, value in rows.items():
                table[key] = value

    report.add("insert", kit.best(fill) / ROWS * 1e6, "us")
    with store.open(scratch / "history.db") as db:
        table = db["history"]
        with db.batch():
            for key, value in rows.items():
                table[key] = value
        report.add("point_read", kit.best(lambda: [table[k] for k in probes]) / PROBES * 1e6, "us")
        report.add("contains", kit.best(lambda: [k in table for k in probes]) / PROBES * 1e6, "us")
        # Keys "ins:00000000" to "ins:00009999": 10,000 rows.
        report.add("prefix_scan_10k",
                   kit.best(lambda: table.rows.prefix("ins:0000").get()) * 1e3, "ms")
        # `size` lives inside the JSON value, so this filter reads every row's JSON.
        report.add("filter_json", kit.best(lambda: table.rows.filter(size=5).get()) * 1e3, "ms")


def commits(report: kit.Report, scratch: Path) -> None:
    """Time raw SQLite commits, per row, per post and per 500 rows, in WAL mode.

    Args:
        report: where the numbers go.
        scratch: a folder for the database files.
    """
    hash_ = os.urandom(32)
    rows = [(f"ins:example_user/{n}", "ins:example_user", str(n // 2), f"/m/{n}.jpg", 102400,
             1.0, hash_) for n in range(COMMIT_ROWS)]
    # The connection of the current run: `fresh` replaces it before each run, untimed.
    current: list[sqlite3.Connection] = []

    def fresh(sync: str) -> None:
        for old in current:
            old.close()
        current[:] = [sqlite3.connect(scratch / f"commit{os.urandom(4).hex()}.db",
                                      isolation_level=None)]
        db = current[0]
        db.execute("PRAGMA journal_mode=WAL")
        db.execute(f"PRAGMA synchronous={sync}")
        db.execute(FILES)
        db.execute("CREATE INDEX by_post ON files(target, post_id)")

    def work(batch: int) -> None:
        db = current[0]
        for start in range(0, COMMIT_ROWS, batch):
            db.execute("BEGIN")
            db.executemany(INSERT, rows[start:start + batch])
            db.execute("COMMIT")

    def timed(sync: str, batch: int) -> float:
        took = kit.best(lambda: work(batch), before=lambda: fresh(sync))
        current.pop().close()
        return took / COMMIT_ROWS * 1e6

    report.add("commit_per_file", timed("NORMAL", 1), "us")
    report.add("commit_per_post", timed("NORMAL", 2), "us")
    report.add("commit_per_500", timed("NORMAL", 500), "us")
    report.add("commit_per_file_full", timed("FULL", 1), "us")


def main() -> None:
    """Run both parts and emit the report."""
    report = kit.Report("db")
    with tempfile.TemporaryDirectory(prefix="flnd-bench-") as scratch:
        history(report, Path(scratch))
        commits(report, Path(scratch))
    report.emit()


if __name__ == "__main__":
    main()
