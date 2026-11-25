"""Local state tracking via `flnkit.db` (`SPEC.md#local-state`)."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from flnkit.db import Db

SCHEMA = """
CREATE TABLE IF NOT EXISTS hidden (
    id          TEXT PRIMARY KEY,
    orig_path   TEXT UNIQUE NOT NULL,
    kind        TEXT NOT NULL,
    level       TEXT NOT NULL,
    hidden_path TEXT NOT NULL,
    meta_json   TEXT NOT NULL,
    size_bytes  INTEGER NOT NULL,
    hidden_at   REAL NOT NULL
);
"""


def home() -> Path:
    """Return the flnh state directory (`$FLNH_HOME`, `%APPDATA%\\flnh`, or `~/.config/flnh`)."""
    env = os.environ.get("FLNH_HOME")
    if env:
        return Path(env)
    if os.name == "nt":
        base = os.environ.get("APPDATA")
        return (Path(base) if base else Path.home() / "AppData" / "Roaming") / "flnh"
    xdg = os.environ.get("XDG_CONFIG_HOME")
    return (Path(xdg) if xdg else Path.home() / ".config") / "flnh"


def backups_dir() -> Path:
    """Return the folder storing Level 4 partition backup files."""
    return home() / "backups"


def norm_path(path: Path | str) -> str:
    """Normalize a path without resolving missing targets."""
    abs_p = os.path.abspath(os.path.normpath(str(path)))
    if os.name == "nt" and len(abs_p) >= 2 and abs_p[1] == ":":
        abs_p = abs_p[0].upper() + abs_p[1:]
    return abs_p


@dataclass(frozen=True)
class Entry:
    """One tracked hidden folder or file."""

    id: str
    orig_path: str
    kind: str
    level: str
    hidden_path: str
    meta: dict[str, Any]
    size_bytes: int
    hidden_at: float


class Store:
    """Persistent WAL SQLite tracker of hidden items."""

    def __init__(self, path: Path | None = None) -> None:
        self._db = Db(path or (home() / "state.db"), schema=SCHEMA, secure=True)

    def __enter__(self) -> Store:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def close(self) -> None:
        """Close the underlying database connection."""
        self._db.close()

    def add(self, entry: Entry) -> None:
        """Insert or replace a tracked hidden entry."""
        key = norm_path(entry.orig_path)
        self._db.write(
            "INSERT OR REPLACE INTO hidden "
            "(id, orig_path, kind, level, hidden_path, meta_json, size_bytes, hidden_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                entry.id,
                key,
                entry.kind,
                entry.level,
                entry.hidden_path,
                json.dumps(entry.meta, separators=(",", ":")),
                entry.size_bytes,
                entry.hidden_at,
            ),
        )

    def find(self, orig_path: Path | str) -> Entry | None:
        """Look up a tracked entry by its original path."""
        key = norm_path(orig_path)
        row = self._db.one(
            "SELECT id, orig_path, kind, level, hidden_path, meta_json, size_bytes, hidden_at "
            "FROM hidden WHERE orig_path = ?",
            (key,),
        )
        return _row_to_entry(row) if row is not None else None

    def all(self) -> list[Entry]:
        """Return all tracked entries ordered by most recently hidden."""
        rows = self._db.all(
            "SELECT id, orig_path, kind, level, hidden_path, meta_json, size_bytes, hidden_at "
            "FROM hidden ORDER BY hidden_at DESC"
        )
        return [_row_to_entry(row) for row in rows]

    def remove(self, orig_path: Path | str) -> None:
        """Remove a tracked entry if present."""
        if not self._db.path.is_file():
            return
        self._db.write("DELETE FROM hidden WHERE orig_path = ?", (norm_path(orig_path),))


def _row_to_entry(row: tuple[Any, ...]) -> Entry:
    return Entry(
        id=row[0],
        orig_path=row[1],
        kind=row[2],
        level=row[3],
        hidden_path=row[4],
        meta=json.loads(row[5]),
        size_bytes=row[6],
        hidden_at=row[7],
    )
