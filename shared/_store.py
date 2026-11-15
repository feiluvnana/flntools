"""Simple atomic JSON key-value store, partitioned by table name."""

from __future__ import annotations

import json
import os
import threading
from collections.abc import Iterator, KeysView, ItemsView, MutableMapping, ValuesView
from pathlib import Path
from typing import Any, Union

import shared
from shared import _fs as fs

__all__ = ["ClosedError", "Error", "Store", "open"]

PathLike = Union[str, "os.PathLike[str]"]


class Error(shared.Error):
    """The base of every error `shared.store` raises."""


class ClosedError(Error, RuntimeError):
    """Raised when operating on a closed `Store`."""


class _Backend:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.lock = threading.RLock()
        self.data = self._load()

    def _load(self) -> dict[str, dict[str, Any]]:
        if not self.path.exists():
            return {}
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as error:
            raise Error(f"{self.path}: cannot read store: {error}") from error
        if not isinstance(raw, dict):
            raise Error(f"{self.path}: expected JSON object")
        return {
            str(k): dict(v) for k, v in raw.items() if isinstance(v, dict)
        }

    def save(self) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            payload = json.dumps(self.data, indent=2, ensure_ascii=False, sort_keys=True)
            fs.write(self.path, data=payload)
        except (OSError, TypeError, ValueError, UnicodeError) as error:
            raise Error(f"{self.path}: cannot write store: {error}") from error


class Store(MutableMapping[str, Any]):
    """A dict-like JSON-backed key-value store for one table."""

    def __init__(self, backend: _Backend, table: str) -> None:
        self._backend = backend
        self._table = table
        self._closed = False

    def __enter__(self) -> Store:
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    def _bucket(self, *, create: bool = False) -> dict[str, Any]:
        if self._closed:
            raise ClosedError(f"{self._backend.path}: store is closed")
        bucket = self._backend.data.get(self._table)
        if bucket is None:
            bucket = {}
            if create:
                self._backend.data[self._table] = bucket
        return bucket

    def __getitem__(self, key: str) -> Any:
        with self._backend.lock:
            return self._bucket()[key]

    def __setitem__(self, key: str, value: Any) -> None:
        self.set(key, value)

    def __delitem__(self, key: str) -> None:
        with self._backend.lock:
            bucket = self._bucket()
            if key not in bucket:
                raise KeyError(key)
            del bucket[key]
            self._backend.save()

    def __iter__(self) -> Iterator[str]:
        with self._backend.lock:
            return iter(list(self._bucket()))

    def __len__(self) -> int:
        with self._backend.lock:
            return len(self._bucket())

    def get(self, key: str, default: Any = None) -> Any:
        """Return the value for `key` if present, else `default`."""
        with self._backend.lock:
            return self._bucket().get(key, default)

    def set(self, key: str, value: Any) -> None:
        """Store `value` under `key` and flush atomically."""
        self.update({key: value})

    def keys(self, prefix: str = "") -> KeysView[str]:
        """Return keys in this table, optionally filtered by `prefix`."""
        with self._backend.lock:
            matched = {k: v for k, v in self._bucket().items() if k.startswith(prefix)}
            return matched.keys()

    def items(self) -> ItemsView[str, Any]:
        """Return `(key, value)` pairs in this table."""
        with self._backend.lock:
            return dict(self._bucket()).items()

    def values(self) -> ValuesView[Any]:
        """Return values in this table."""
        with self._backend.lock:
            return dict(self._bucket()).values()

    def update(self, other: Any = (), /, **kwargs: Any) -> None:
        """Update multiple keys in one atomic write."""
        changes = dict(other, **kwargs)
        if any(not isinstance(key, str) for key in changes):
            raise Error(f"{self._backend.path}: store keys must be strings")
        with self._backend.lock:
            bucket = self._bucket(create=True)
            previous = {key: bucket.get(key, _MISSING) for key in changes}
            bucket.update(changes)
            try:
                self._backend.save()
            except Error:
                for key, value in previous.items():
                    if value is _MISSING:
                        bucket.pop(key, None)
                    else:
                        bucket[key] = value
                raise

    def clear(self) -> None:
        """Remove every key from this table."""
        with self._backend.lock:
            if self._table in self._backend.data:
                previous = self._backend.data.pop(self._table)
                try:
                    self._backend.save()
                except Error:
                    self._backend.data[self._table] = previous
                    raise

    def table(self, name: str) -> Store:
        """Return another table sharing the same file."""
        return Store(self._backend, name)

    def close(self) -> None:
        """Mark this store closed."""
        with self._backend.lock:
            self._closed = True


_MISSING = object()


def open(path: PathLike, *, table: str = "kv") -> Store:
    """Open an atomic JSON `Store` at `path` for `table`."""
    return Store(_Backend(Path(path)), table)
