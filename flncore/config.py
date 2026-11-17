"""A TOML file into a typed struct, with errors that name the key."""

from __future__ import annotations

import tomllib
from pathlib import Path
from typing import Any, TypeVar

from flncore.errors import UsageError

__all__ = ["load", "read"]

T = TypeVar("T")


def read(path: Path) -> dict[str, Any]:
    """Read a TOML file; return {} if missing, raise UsageError if invalid."""
    if not path.is_file():
        return {}
    try:
        return tomllib.loads(path.read_text(encoding="utf-8"))
    except tomllib.TOMLDecodeError as e:
        raise UsageError(
            f"{path}: invalid TOML: {e}", hint="check the syntax of the config file"
        ) from e


def load(path: Path, cls: type[T]) -> T:
    """Load a TOML file into a typed dataclass or msgspec struct."""
    data = read(path)
    try:
        return cls(**data)  # type: ignore[call-arg]
    except Exception as e:
        raise UsageError(f"{path}: invalid configuration: {e}") from e
