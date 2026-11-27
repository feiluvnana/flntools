"""`flnd migrate`: bring a home up to the newest version, one step at a time.

SPEC.md#migrating-a-home explains it.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable
from pathlib import Path
from typing import Any

from flnd.engine import store
from flnd.errors import DiskError, UsageError
from flnkit import db

__all__ = ["STEPS", "run"]

OLD, NEW = "loi:post/", "loi:"


def run(home: Path, *, dry: bool = False) -> list[str]:
    """Migrate the home's `flnd.db` to the newest version.

    The database is backed up first, to `flnd.db.v<old>.bak` (`.bak.1`, ... when one is there).
    Each step runs in its own transaction. With `dry` the steps run on a copy in memory, and
    nothing is written.

    Returns:
        One line per step, `v1 → v2: <what changed>`; `<home> is already v2` when none ran.

    Raises:
        UsageError: the home is older than the steps reach, or from a newer flnd.
        DiskError: `flnd.db` can't be read or written.
    """
    path, newest = home / store.DB, int(store.VERSION)
    current = f"{home} is already v{newest}"
    if not path.is_file():
        return [current]
    try:
        source = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)
    except sqlite3.Error as error:
        raise DiskError(f"{path}: cannot open: {error}") from error
    try:
        found = store.stored(source)
        old = store.number(home, found) if found is not None else newest
        if old == newest:
            return [current]
        if old not in STEPS:
            raise UsageError(f"{home} is a v{old} home, older than this flnd migrates",
                             hint="open it with an older flnd first")
        if dry:
            sql = sqlite3.connect(":memory:", isolation_level=None)
            source.backup(sql)
        else:
            _backup(source, path.with_name(f"{path.name}.v{old}.bak"))
            sql = db.connect(path)
    except sqlite3.Error as error:
        raise DiskError(f"{path}: {error}") from error
    finally:
        source.close()
    try:
        return [f"v{n} → v{n + 1}: {_step(sql, home, n, dry=dry)}" for n in range(old, newest)]
    finally:
        sql.close()


def _backup(source: sqlite3.Connection, name: Path) -> None:
    """Copy the database to `name`, or to the first of `name.1`, `name.2`, ... that is free."""
    taken, number = name, 0
    while taken.exists():
        number += 1
        taken = name.with_name(f"{name.name}.{number}")
    copy = sqlite3.connect(taken)
    try:
        source.backup(copy)
    finally:
        copy.close()


def _step(sql: sqlite3.Connection, home: Path, old: int, *, dry: bool) -> str:
    """Run the step from `old` in one transaction, and set the version it ends at."""
    sql.execute("BEGIN IMMEDIATE")
    try:
        said = STEPS[old](sql, home, dry=dry)
        sql.execute("UPDATE meta SET value = ? WHERE key = 'version'", (str(old + 1),))
        sql.execute("COMMIT")
    except sqlite3.Error as error:
        if sql.in_transaction:
            sql.execute("ROLLBACK")
        raise DiskError(f"{home / store.DB}: {error}") from error
    except BaseException:
        if sql.in_transaction:
            sql.execute("ROLLBACK")
        raise
    return said


def _two(sql: sqlite3.Connection, home: Path, *, dry: bool) -> str:
    """Version 1 to 2: `loi:post/<id>` becomes `loi:<id>`, and the targets of the routes since
    removed (`tlg`, the `tik` and `loi` searches) are unfollowed.

    Files are not moved: a saved file stays where it is and is found by its key. The home's
    `config.toml` is not touched: a table of a removed plugin is only warned about.
    """
    renamed, merged = _rename_loi(sql)
    unfollowed = sql.execute(
        "UPDATE targets SET follow = 0 WHERE follow = 1 AND (plugin = 'tlg' OR "
        "(plugin IN ('tik', 'loi') AND kind = 'search'))").rowcount
    return (f"{renamed} loi post targets renamed ({merged} merged), "
            f"{unfollowed} targets of removed routes unfollowed")


def _rename_loi(sql: sqlite3.Connection) -> tuple[int, int]:
    """Rename each `loi:post/<id>` key, in every table that holds a target key.

    Returns:
        How many targets were renamed, and how many of them had a `loi:<id>` twin already.
    """
    old = [key for (key,) in sql.execute(
        "SELECT key FROM targets WHERE substr(key, 1, ?) = ?", (len(OLD), OLD))]
    merged = 0
    for key in old:
        new = NEW + key[len(OLD):]
        row = sql.execute("SELECT * FROM targets WHERE key = ?", (key,)).fetchone()
        twin = sql.execute("SELECT * FROM targets WHERE key = ?", (new,)).fetchone()
        if twin is None:
            sql.execute("UPDATE targets SET key = ?, value = ? WHERE key = ?",
                        (new, new.partition(":")[2], key))
        else:
            merged += 1
            _merge(sql, store.TARGET_COLUMNS, row, twin)
            sql.execute("DELETE FROM targets WHERE key = ?", (key,))
        sql.execute("UPDATE files SET target = ? WHERE target = ?", (new, key))
        sql.execute("DELETE FROM failures WHERE key = ? AND EXISTS "
                    "(SELECT 1 FROM failures WHERE key = ?)", (key, new))
        sql.execute("UPDATE failures SET key = ? WHERE key = ?", (new, key))
        sql.execute("UPDATE failures SET target = ? WHERE target = ?", (new, key))
        sql.execute("INSERT OR IGNORE INTO target_seen (target, item, ord) "
                    "SELECT ?1, item, ord + (SELECT coalesce(max(ord), -1) + 1 FROM target_seen "
                    "WHERE target = ?1) FROM target_seen WHERE target = ?2 ORDER BY ord",
                    (new, key))
        sql.execute("DELETE FROM target_seen WHERE target = ?", (key,))
        if twin is not None:
            sql.execute("UPDATE targets SET (files, bytes) = (SELECT count(*), coalesce("
                        "sum(size), 0) FROM files WHERE target = ?1 AND state = 'saved') "
                        "WHERE key = ?1", (new,))
    _rename_seen(sql)
    _rename_heads(sql)
    return len(old), merged


def _merge(sql: sqlite3.Connection, columns: tuple[str, ...], row: tuple[Any, ...],
           twin: tuple[Any, ...]) -> None:
    """Fold the old row's history into its twin's: followed if either was, the twin's own
    categories, query and folder before the old row's, the newest sync, each category's marks
    of both (the twin's win)."""
    old, new = dict(zip(columns, row, strict=True)), dict(zip(columns, twin, strict=True))
    sql.execute(
        "UPDATE targets SET follow = ?, categories = ?, query = ?, folder = ?, last_sync = ?, "
        "complete = ?, cursors = ?, heads = ? WHERE key = ?",
        (old["follow"] or new["follow"],
         new["categories"] if new["categories"] != "[]" else old["categories"],
         new["query"] or old["query"], new["folder"] or old["folder"],
         max(old["last_sync"], new["last_sync"]),
         *(json.dumps({**json.loads(old[name]), **json.loads(new[name])})
           for name in ("complete", "cursors", "heads")), new["key"]))


def _rename_seen(sql: sqlite3.Connection) -> None:
    """Rename the `loi:post/<id>` items a search or author found, dropping one already there."""
    for target, item in sql.execute("SELECT target, item FROM target_seen "
                                    "WHERE substr(item, 1, ?) = ?", (len(OLD), OLD)).fetchall():
        sql.execute("UPDATE OR REPLACE target_seen SET item = ? WHERE target = ? AND item = ?",
                    (NEW + item[len(OLD):], target, item))


def _rename_heads(sql: sqlite3.Connection) -> None:
    """Rename the keys a search's `heads` hold (the key of its first target)."""
    for key, heads in sql.execute("SELECT key, heads FROM targets WHERE heads LIKE ?",
                                  (f"%{OLD}%",)).fetchall():
        sql.execute("UPDATE targets SET heads = ? WHERE key = ?",
                    (json.dumps(_rekey(json.loads(heads))), key))


def _rekey(value: Any) -> Any:
    """`value` with every string that is a `loi:post/<id>` key made `loi:<id>`."""
    if isinstance(value, str):
        return NEW + value[len(OLD):] if value.startswith(OLD) else value
    if isinstance(value, dict):
        return {name: _rekey(each) for name, each in value.items()}
    if isinstance(value, list):
        return [_rekey(each) for each in value]
    return value


# The step from each version to the next; only the last 3 are kept.
STEPS: dict[int, Callable[..., str]] = {1: _two}
