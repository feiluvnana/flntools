"""Levels 1 & 2 (`flag` and `lock`) cross-OS dispatch (`SPEC.md#levels-1-and-2`)."""

from __future__ import annotations

import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

from flnh import errors


def _backend() -> Any:
    if sys.platform == "win32":
        from flnh.os_hide import win32

        return win32
    if sys.platform == "darwin":
        from flnh.os_hide import darwin

        return darwin
    from flnh.os_hide import linux

    return linux


def hide(
    path: Path,
    *,
    level: str,
    password: str | None = None,
    track: bool = True,
) -> dict[str, Any]:
    """Apply Level 1 (`flag`) or Level 2 (`lock`) to `path`."""
    if not path.exists() and not path.is_symlink():
        raise errors.UsageError(f"path not found: {path}")
    if path.parent == path:
        raise errors.SafetyError(f"refusing to hide root path: {path}")
    return _backend().hide(path, level=level, password=password, track=track)


def show(
    orig_path: Path,
    *,
    entry: Any = None,
    get_password: Callable[[], str] | None = None,
) -> tuple[Path, str]:
    """Restore a Level 1 or Level 2 hidden path, returning `(restored_path, level)`."""
    return _backend().show(orig_path, entry=entry, get_password=get_password)


def detect(orig_path: Path, entry: Any = None) -> dict[str, Any] | None:
    """Detect whether `orig_path` is hidden at Level 1 or 2."""
    return _backend().detect(orig_path, entry=entry)
