"""macOS Levels 1 & 2: `UF_HIDDEN`, `chmod 000`, and `UF_IMMUTABLE` (`SPEC.md#levels-1-and-2`)."""

from __future__ import annotations

import json
import os
import stat
from pathlib import Path
from typing import Any

from flnh import errors, pack
from flnkit import files

UF_HIDDEN = getattr(stat, "UF_HIDDEN", 0x8000)
UF_IMMUTABLE = getattr(stat, "UF_IMMUTABLE", 0x0002)


def _chflags(path: Path, flags: int) -> None:
    fn = getattr(os, "chflags", None)
    if fn is None:
        raise errors.UnsupportedError("os.chflags is only supported on macOS/BSD")
    fn(path, flags)


def _get_flags(path: Path) -> int:
    st = path.lstat()
    return int(getattr(st, "st_flags", 0))


def set_hidden_flag(path: Path, hidden: bool) -> None:
    """Set or clear `UF_HIDDEN` on `path`."""
    cur = _get_flags(path)
    new = (cur | UF_HIDDEN) if hidden else (cur & ~UF_HIDDEN)
    _chflags(path, new)


def _sidecar_path(orig_path: Path) -> Path:
    return orig_path.parent / f".flnh.{pack.item_id(orig_path.name)}.meta"


def _write_sidecar(orig_path: Path, data: dict[str, Any]) -> Path:
    sc = _sidecar_path(orig_path)
    files.write(sc, json.dumps(data, separators=(",", ":")))
    os.chmod(sc, 0o600)
    try:
        set_hidden_flag(sc, True)
    except errors.UnsupportedError:
        pass
    return sc


def _read_sidecar(orig_path: Path) -> dict[str, Any] | None:
    sc = _sidecar_path(orig_path)
    if not sc.is_file():
        return None
    return json.loads(sc.read_text(encoding="utf-8"))


def _remove_sidecar(orig_path: Path) -> None:
    sc = _sidecar_path(orig_path)
    if sc.exists():
        try:
            set_hidden_flag(sc, False)
        except errors.UnsupportedError:
            pass
        sc.unlink()


def detect(orig_path: Path, entry: Any = None) -> dict[str, Any] | None:
    """Detect whether `orig_path` is hidden on macOS."""
    if not orig_path.exists():
        return None
    flags = _get_flags(orig_path)
    if (flags & UF_HIDDEN) or _sidecar_path(orig_path).is_file() or entry is not None:
        return {"hidden_path": orig_path, "entry": entry}
    return None


def hide(path: Path, *, level: str) -> dict[str, Any]:
    """Hide `path` on macOS using `UF_HIDDEN`, plus `chmod 000` + `UF_IMMUTABLE` for `lock`."""
    flags = _get_flags(path)
    if flags & UF_HIDDEN:
        raise errors.UsageError(
            f"already hidden: {path}",
            hint=f"run flnh show {path} to restore it",
        )
    st = path.lstat()
    kind = "file" if path.is_file() and not path.is_symlink() else "dir"
    size_bytes = pack.tree_size(path)
    orig_mode = stat.S_IMODE(st.st_mode)
    meta: dict[str, Any] = {
        "kind": kind,
        "level": level,
        "orig_mode": orig_mode,
        "orig_flags": flags,
    }
    set_hidden_flag(path, True)
    if level == "lock":
        _write_sidecar(path, meta)
        os.chmod(path, 0o000)
        _chflags(path, flags | UF_HIDDEN | UF_IMMUTABLE)

    return {
        "kind": kind,
        "hidden_path": str(path),
        "size_bytes": size_bytes,
        "meta": meta,
    }


def show(orig_path: Path, *, entry: Any = None) -> tuple[Path, str]:
    """Restore a macOS Level 1 or Level 2 hidden path."""
    if not orig_path.exists():
        raise errors.UsageError(f"path not found: {orig_path}")
    flags = _get_flags(orig_path)
    sc = _read_sidecar(orig_path)
    meta: dict[str, Any] = dict(entry.meta) if entry is not None else (sc or {})
    is_locked = (
        (entry is not None and entry.level == "lock")
        or bool(flags & UF_IMMUTABLE)
        or meta.get("level") == "lock"
    )
    if not (flags & UF_HIDDEN) and not is_locked and entry is None and sc is None:
        raise errors.UsageError(f"{orig_path} is not hidden")

    orig_flags = int(meta.get("orig_flags", flags & ~(UF_HIDDEN | UF_IMMUTABLE)))
    if is_locked:
        _chflags(orig_path, flags & ~UF_IMMUTABLE)
        default_mode = 0o644 if orig_path.is_file() else 0o755
        os.chmod(orig_path, int(meta.get("orig_mode", default_mode)))
    _chflags(orig_path, orig_flags & ~UF_HIDDEN)
    _remove_sidecar(orig_path)
    level = str(meta.get("level") or ("lock" if is_locked else "flag"))
    return orig_path, level
