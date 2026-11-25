"""Benchmark group `db`: a home's `flnd.db` over 100k files, and what a commit costs per file.

Part one is `flnd.engine.store` over 100,000 file rows of 10 targets: inserting them, the point read
every listed file makes (`Store.known`: its path and state by key), one target's 10,000 rows, and
a filter on a column (`size`), which SQL runs.

Part two is what commits cost: 50,000 files saved through `Store.save`, one transaction per post
of 2 files (`commit_per_post`, the run's real pattern), and raw SQLite in WAL mode committing
one per row, one per 500 rows, and one per row with `synchronous=FULL`, for comparison. 50,000
rows make the WAL pass its automatic checkpoint (1,000 pages) many times, so the cost of
checkpoints is inside the numbers rather than after them.

Every number is the best of 3 runs, each run on a fresh database in a scratch folder.
"""

from __future__ import annotations

import os
import random
import sqlite3
import tempfile
from pathlib import Path

import kit

from flnd.engine import store as _store

ROWS = kit.size(100_000, 1000)
# 10,000 random keys read back, seeded so every run reads the same ones.
PROBES = kit.size(10_000, 100)
COMMIT_ROWS = kit.size(50_000, 1000)
FILES = ("CREATE TABLE files (key TEXT PRIMARY KEY, target TEXT, post_id TEXT, path TEXT,"
         " size INT, mtime REAL)")
INSERT = "INSERT INTO files VALUES (?,?,?,?,?,?)"


def files(n: int, *, post_files: int = 1) -> _store.FileRow:
    """File `n` of the benchmark's homes: target `n % 10`, `post_files` files per post."""
    user = f"ins:example_user{n % 10}"
    return _store.FileRow(f"ins:{n:08d}", user, f"/m/ins/example_user{n % 10}/{n}.jpg", n,
                         post_id=str(n // post_files), category="posts", date="2026-01-02",
                         kind="image", at="2026-01-02T00:00:00+00:00")


def history(report: kit.Report, scratch: Path) -> None:
    """Time `flnd.engine.store` over 100k file rows.

    Args:
        report: where the numbers go.
        scratch: a folder for the homes.
    """
    rows = [files(n) for n in range(ROWS)]
    probes = random.Random(1).sample([row.key for row in rows], PROBES)
    made = 0

    def fill() -> None:
        nonlocal made
        made += 1
        with _store.open(scratch / f"fill{made}") as db:
            db.save(rows)

    report.add("insert", kit.best(fill) / ROWS * 1e6, "us")
    with _store.open(scratch / "home") as db:
        db.save(rows)
        report.add("point_read", kit.best(lambda: [db.known(k) for k in probes]) / PROBES * 1e6,
                   "us")
        report.add("contains", kit.best(
            lambda: [db.known(k) is not None for k in probes]) / PROBES * 1e6, "us")
        # One target's rows: every tenth file, 10,000 rows.
        report.add("prefix_scan_10k",
                   kit.best(lambda: db.records(target="ins:example_user0")) * 1e3, "ms")
        # `size` is a column now, so SQL filters it; the name stays for the comparison.
        report.add("filter_json", kit.best(lambda: db._db.all(
            "SELECT key FROM files WHERE size = ?", (5,))) * 1e3, "ms")


def commits(report: kit.Report, scratch: Path) -> None:
    """Time raw SQLite commits, per row, per post and per 500 rows, in WAL mode.

    Args:
        report: where the numbers go.
        scratch: a folder for the database files.
    """
    rows = [(f"ins:example_user/{n}", "ins:example_user", str(n // 2), f"/m/{n}.jpg", 102400,
             1.0) for n in range(COMMIT_ROWS)]
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
    # The run's real pattern: `Store.save` per post of 2 files, through flnd's own code.
    posts = [[files(n, post_files=2), files(n + 1, post_files=2)]
             for n in range(0, COMMIT_ROWS, 2)]
    homes = iter(range(10))

    def saved() -> None:
        with _store.open(scratch / f"posts{next(homes)}") as db:
            for post in posts:
                db.save(post)

    report.add("commit_per_post", kit.best(saved) / COMMIT_ROWS * 1e6, "us")
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
