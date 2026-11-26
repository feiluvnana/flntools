"""Linux Levels 1 & 2: leading `.` + `.hidden` entry, `chmod 000` (`SPEC.md#levels-1-and-2`)."""

from __future__ import annotations

import json
import os
import stat
from pathlib import Path
from typing import Any

from flnh import errors, pack
from flnkit import files


def add_dot_hidden(parent: Path, name: str) -> None:
    """Add `name` to `<parent>/.hidden` without duplicating existing entries."""
    hidden_file = parent / ".hidden"
    entries: list[str] = []
    if hidden_file.is_file():
        entries = [line for line in hidden_file.read_text(encoding="utf-8").splitlines() if line]
    if name not in entries:
        entries.append(name)
        files.write(hidden_file, "\n".join(entries) + "\n")


def remove_dot_hidden(parent: Path, name: str) -> None:
    """Remove `name` from `<parent>/.hidden`, deleting `.hidden` if empty."""
    hidden_file = parent / ".hidden"
    if not hidden_file.is_file():
        return
    entries = [
        line
        for line in hidden_file.read_text(encoding="utf-8").splitlines()
        if line and line != name
    ]
    if entries:
        files.write(hidden_file, "\n".join(entries) + "\n")
    else:
        hidden_file.unlink()


def _dot_name(orig_path: Path) -> Path:
    name = orig_path.name
    return orig_path if name.startswith(".") else orig_path.parent / f".{name}"


def _sidecar_path(orig_path: Path) -> Path:
    return orig_path.parent / f".flnh.{pack.item_id(orig_path.name)}.meta"


def _write_sidecar(orig_path: Path, data: dict[str, Any]) -> Path:
    sc = _sidecar_path(orig_path)
    files.write(sc, json.dumps(data, separators=(",", ":")))
    os.chmod(sc, 0o600)
    add_dot_hidden(orig_path.parent, sc.name)
    return sc


def _read_sidecar(orig_path: Path) -> dict[str, Any] | None:
    sc = _sidecar_path(orig_path)
    if not sc.is_file():
        return None
    return json.loads(sc.read_text(encoding="utf-8"))


def _remove_sidecar(orig_path: Path) -> None:
    sc = _sidecar_path(orig_path)
    if sc.exists():
        sc.unlink()
    remove_dot_hidden(orig_path.parent, sc.name)


def detect(orig_path: Path, entry: Any = None) -> dict[str, Any] | None:
    """Detect whether `orig_path` is hidden on Linux."""
    target = Path(entry.hidden_path) if entry is not None else _dot_name(orig_path)
    if not target.exists():
        return None
    hidden_file = orig_path.parent / ".hidden"
    in_dot_hidden = (
        hidden_file.is_file()
        and target.name in hidden_file.read_text(encoding="utf-8").splitlines()
    )
    # A `.name` alone is not proof: `~/.ssh` exists without flnh. Only flnh's own marks count.
    if entry is not None or in_dot_hidden or _sidecar_path(orig_path).is_file():
        return {"hidden_path": target, "entry": entry}
    return None


def hide(path: Path, *, level: str) -> dict[str, Any]:
    """Hide `path` on Linux using leading `.` + `.hidden`, plus `chmod 000` for `lock`."""
    dest = _dot_name(path)
    if dest != path and dest.exists():
        raise errors.UsageError(
            f"already hidden: {path}",
            hint=f"run flnh show {path} to restore it",
        )
    hidden_file = path.parent / ".hidden"
    if hidden_file.is_file() and dest.name in hidden_file.read_text(encoding="utf-8").splitlines():
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
        "orig_name": path.name,
        "renamed": dest != path,
        "orig_mode": orig_mode,
    }
    if dest != path:
        os.replace(path, dest)
    add_dot_hidden(path.parent, dest.name)
    if level == "lock":
        _write_sidecar(path, meta)
        os.chmod(dest, 0o000)

    return {
        "kind": kind,
        "hidden_path": str(dest),
        "size_bytes": size_bytes,
        "meta": meta,
    }


def show(orig_path: Path, *, entry: Any = None) -> tuple[Path, str]:
    """Restore a Linux Level 1 or Level 2 hidden path."""
    found = detect(orig_path, entry=entry)
    if found is None:
        raise errors.UsageError(
            f"no hidden folder or file found for: {orig_path}",
            hint="run flnh show --list to see tracked items",
        )
    target: Path = found["hidden_path"]
    if target != orig_path and orig_path.exists():
        raise errors.UsageError(f"destination already exists: {orig_path}")

    sc = _read_sidecar(orig_path)
    meta: dict[str, Any] = dict(entry.meta) if entry is not None else (sc or {})
    mode = stat.S_IMODE(target.lstat().st_mode)
    is_locked = (
        (entry is not None and entry.level == "lock")
        or mode == 0
        or meta.get("level") == "lock"
    )
    if is_locked:
        default_mode = 0o644 if target.is_file() else 0o755
        os.chmod(target, int(meta.get("orig_mode", default_mode)))
    remove_dot_hidden(orig_path.parent, target.name)
    _remove_sidecar(orig_path)
    if target != orig_path:
        os.replace(target, orig_path)
    level = str(meta.get("level") or ("lock" if is_locked else "flag"))
    return orig_path, level
