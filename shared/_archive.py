"""Pack, list, and unpack zip, tar, 7z, and rar archives with safety limits."""

from __future__ import annotations

import os
import re
import shutil
import tarfile
import zipfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import IO, Any, Callable, Literal, Sequence, Union

import shared
from shared import _fs as fs
from shared import _sh as sh

__all__ = [
    "BrokenError", "Entry", "Error", "Limits", "MissingError", "PasswordError",
    "UnsafeError", "ls", "pack", "unpack",
]

PathLike = Union[str, "os.PathLike[str]"]
Progress = Callable[[int, int], Any]
Conflict = Literal["error", "skip", "overwrite"]
Kind = Literal["file", "dir"]

DRIVE = re.compile(r"^[A-Za-z]:")
TAR_SUFFIXES = (".tar", ".tar.gz", ".tgz", ".tar.bz2", ".tbz2", ".tar.xz", ".txz")
# Entries are copied in pieces of this size, so a 20 GB member never sits in memory.
CHUNK = 1024 * 1024
# `unrar vt` names every kind that is not a plain file or folder with one of these words.
RAR_LINKS = ("link", "junction", "reference")


class Error(shared.Error):
    """The base of every error `shared.archive` raises."""


class UnsafeError(Error, shared.UnsafeError):
    """An entry would land outside `dest`, is a link/special file, or exceeds `Limits`."""


class BrokenError(Error, shared.BrokenError):
    """The archive is truncated, corrupt, or unsupported."""


class PasswordError(Error, shared.RefusedError, ValueError):
    """The archive is encrypted and needs a valid password."""


class MissingError(Error, shared.MissingError):
    """Reading or writing this archive format needs an external tool (`7zz`, `unrar`)."""


@dataclass(frozen=True)
class Entry:
    """One member inside an archive."""

    name: str
    size: int | None
    kind: Kind


@dataclass(frozen=True)
class Limits:
    """Bounds enforced on archive entries before unpacking."""

    size: int = 20_000_000_000
    ratio: float = 200
    files: int = 100_000


def ls(
    path: PathLike, *, password: str | None = None, limits: Limits = Limits(),
) -> list[Entry]:
    """List validated entries in `path`."""
    archive_path = Path(path)
    kind = _detect(archive_path)
    entries = _read_entries(archive_path, kind, password=password)
    _check_limits(entries, archive_path.stat().st_size, limits)
    return entries


def pack(
    sources: PathLike | Sequence[PathLike],
    dest: PathLike,
    *,
    password: str | None = None,
    progress: Progress | None = None,
) -> Path:
    """Pack `sources` into `dest` (`.zip`, `.7z`, or `.tar*`) and return `Path(dest)`."""
    src_list = (
        [Path(sources)] if isinstance(sources, (str, os.PathLike))
        else [Path(s) for s in sources]
    )
    if not src_list:
        raise ValueError("pack needs at least one source")
    out = Path(dest)
    out.parent.mkdir(parents=True, exist_ok=True)
    kind = _write_kind(out)
    files = _collect_files(src_list)
    total = len(files)
    if kind == "zip":
        if password:
            _seven_zip_pack(src_list, out, kind, password=password)
        else:
            with fs.stage(out, keep=False) as part:
                with zipfile.ZipFile(part, "w", compression=zipfile.ZIP_DEFLATED) as zf:
                    for done, (disk_path, arc_name) in enumerate(files, 1):
                        zf.write(disk_path, arc_name)
                        if progress:
                            progress(done, total)
    elif kind == "7z":
        _seven_zip_pack(src_list, out, kind, password=password)
    else:
        mode = {"tar": "w", "gz": "w:gz", "bz2": "w:bz2", "xz": "w:xz"}[kind]
        with fs.stage(out, keep=False) as part:
            with tarfile.open(part, mode) as tf:  # type: ignore[call-overload]
                for done, (disk_path, arc_name) in enumerate(files, 1):
                    tf.add(disk_path, arcname=arc_name)
                    if progress:
                        progress(done, total)
    return out


def unpack(
    path: PathLike,
    dest: PathLike,
    *,
    password: str | None = None,
    conflict: Conflict = "error",
    limits: Limits = Limits(),
    progress: Progress | None = None,
) -> list[Path]:
    """Validate and unpack `path` into `dest`, returning written file paths."""
    if conflict not in ("error", "skip", "overwrite"):
        raise ValueError(f"unknown conflict mode {conflict!r}")
    archive_path = Path(path)
    out_dir = Path(dest)
    kind = _detect(archive_path)
    entries = _read_entries(archive_path, kind, password=password)
    _check_limits(entries, archive_path.stat().st_size, limits)
    targets = [_safe_dest(out_dir, e.name) for e in entries]
    if conflict == "error":
        for e, target in zip(entries, targets):
            if e.kind == "file" and target.exists():
                raise UnsafeError(f"{target} already exists")
    out_dir.mkdir(parents=True, exist_ok=True)
    if kind == "zip":
        return _unpack_zip(
            archive_path, out_dir, password=password, conflict=conflict, progress=progress,
        )
    if kind == "tar":
        return _unpack_tar(archive_path, out_dir, conflict=conflict, progress=progress)
    if kind == "7z":
        return _unpack_7z(
            archive_path, out_dir, entries,
            password=password, conflict=conflict, progress=progress,
        )
    return _unpack_unrar(
        archive_path, out_dir, entries,
        password=password, conflict=conflict, progress=progress,
    )


def _detect(path: Path) -> str:
    if not path.is_file():
        raise BrokenError(f"{path}: archive not found")
    with path.open("rb") as handle:
        head = handle.read(8)
    if head.startswith((b"PK\x03\x04", b"PK\x05\x06", b"PK\x07\x08")):
        return "zip"
    if head.startswith(b"7z\xbc\xaf\x27\x1c"):
        return "7z"
    if head.startswith(b"Rar!\x1a\x07"):
        return "rar"
    if tarfile.is_tarfile(path):
        return "tar"
    raise BrokenError(f"{path.name}: unsupported or corrupt archive")


def _write_kind(path: Path) -> str:
    lower = path.name.lower()
    if lower.endswith(".zip"):
        return "zip"
    if lower.endswith(".7z"):
        return "7z"
    if lower.endswith((".tar.gz", ".tgz")):
        return "gz"
    if lower.endswith((".tar.bz2", ".tbz2")):
        return "bz2"
    if lower.endswith((".tar.xz", ".txz")):
        return "xz"
    if lower.endswith(".tar"):
        return "tar"
    raise ValueError(f"unsupported archive extension for {path.name!r}")


def _collect_files(sources: Sequence[Path]) -> list[tuple[Path, str]]:
    out: list[tuple[Path, str]] = []
    for src in sources:
        if src.is_dir():
            root_name = src.name
            for item in sorted(src.rglob("*")):
                if item.is_file():
                    rel = item.relative_to(src).as_posix()
                    out.append((item, f"{root_name}/{rel}"))
        elif src.is_file():
            out.append((src, src.name))
        else:
            raise Error(f"{src}: source does not exist")
    return out


def _safe_dest(dest: Path, member: str) -> Path:
    norm = member.replace("\\", "/")
    if not norm or norm.startswith("/") or DRIVE.match(norm):
        raise UnsafeError(f"unsafe archive path {member!r}")
    parts = PurePosixPath(norm).parts
    if any(p in ("..", "") for p in parts):
        raise UnsafeError(f"unsafe archive path {member!r}")
    resolved_root = dest.resolve()
    target = (dest / Path(*parts)).resolve()
    if target != resolved_root and resolved_root not in target.parents:
        raise UnsafeError(f"archive path escapes destination: {member!r}")
    return dest / Path(*parts)


def _check_limits(entries: Sequence[Entry], archive_size: int, limits: Limits) -> None:
    if len(entries) > limits.files:
        raise UnsafeError(f"too many entries ({len(entries)} > {limits.files})")
    total = sum(e.size or 0 for e in entries)
    if total > limits.size:
        raise UnsafeError(f"unpacked size exceeds limit ({total} > {limits.size})")
    if archive_size > 0 and total / archive_size > limits.ratio:
        raise UnsafeError(f"compression ratio exceeds limit ({limits.ratio})")


def _read_entries(path: Path, kind: str, *, password: str | None) -> list[Entry]:
    if kind == "zip":
        try:
            with zipfile.ZipFile(path) as zf:
                return [
                    Entry(i.filename, i.file_size, "dir" if i.is_dir() else "file")
                    for i in zf.infolist()
                ]
        except zipfile.BadZipFile as error:
            raise BrokenError(f"{path.name}: {error}") from error
    if kind == "tar":
        try:
            with tarfile.open(path) as tf:
                out: list[Entry] = []
                for m in tf.getmembers():
                    if m.issym() or m.islnk() or not (m.isfile() or m.isdir()):
                        raise UnsafeError(f"unsafe tar entry {m.name!r}")
                    out.append(Entry(m.name, m.size, "dir" if m.isdir() else "file"))
                return out
        except tarfile.TarError as error:
            raise BrokenError(f"{path.name}: {error}") from error
    if kind == "7z":
        return _ls_7z(path, password=password)
    return _ls_unrar(path, password=password)


def _unpack_zip(
    path: Path, dest: Path, *, password: str | None, conflict: Conflict, progress: Progress | None,
) -> list[Path]:
    pwd = password.encode("utf-8") if password is not None else None
    written: list[Path] = []
    try:
        with zipfile.ZipFile(path) as zf:
            infos = [i for i in zf.infolist() if not i.is_dir()]
            for i in zf.infolist():
                if i.is_dir():
                    _safe_dest(dest, i.filename).mkdir(parents=True, exist_ok=True)
            for idx, info in enumerate(infos, 1):
                target = _safe_dest(dest, info.filename)
                if target.exists() and conflict == "skip":
                    continue
                target.parent.mkdir(parents=True, exist_ok=True)
                with zf.open(info, pwd=pwd) as src:
                    _save(src, target, replace=(conflict == "overwrite"))
                written.append(target)
                if progress:
                    progress(idx, len(infos))
    except RuntimeError as error:
        if "password" in str(error).lower():
            raise PasswordError(f"{path.name}: wrong or missing password") from error
        raise BrokenError(f"{path.name}: {error}") from error
    except zipfile.BadZipFile as error:
        raise BrokenError(f"{path.name}: {error}") from error
    return written


def _unpack_tar(
    path: Path, dest: Path, *, conflict: Conflict, progress: Progress | None,
) -> list[Path]:
    written: list[Path] = []
    try:
        with tarfile.open(path) as tf:
            members = [m for m in tf.getmembers() if m.isfile()]
            for m in tf.getmembers():
                if m.isdir():
                    _safe_dest(dest, m.name).mkdir(parents=True, exist_ok=True)
            for idx, m in enumerate(members, 1):
                target = _safe_dest(dest, m.name)
                if target.exists() and conflict == "skip":
                    continue
                target.parent.mkdir(parents=True, exist_ok=True)
                stream = tf.extractfile(m)
                if stream is not None:
                    _save(stream, target, replace=(conflict == "overwrite"))
                    written.append(target)
                if progress:
                    progress(idx, len(members))
    except tarfile.TarError as error:
        raise BrokenError(f"{path.name}: {error}") from error
    return written


def _save(stream: IO[bytes], target: Path, *, replace: bool) -> None:
    """Copy `stream` onto `target` atomically, piece by piece."""
    if not replace and target.exists():
        raise UnsafeError(f"{target} already exists")
    with fs.stage(target, keep=False) as part, part.open("wb") as out:
        shutil.copyfileobj(stream, out, CHUNK)


def _7z_tool() -> sh.Tool:
    for name in ("7zz", "7z"):
        try:
            return sh.find(name)
        except sh.MissingError:
            continue
    raise MissingError("7z archives need `7zz` or `7z` on PATH")


def _seven_zip_pack(
    sources: Sequence[Path], out: Path, kind: str, *, password: str | None,
) -> None:
    tool = _7z_tool()
    # `-t` names the format: the stage's name ends in `.stage`, not in `.7z` or `.zip`.
    args = ["a", "-y", f"-t{kind}"]
    if password:
        args.append(f"-p{password}")
    # `--` ends the switches, so a path starting with "-" (`-sdel`) is never read as one.
    with fs.stage(out, keep=False) as part:
        # 7zz adds to an archive already there, such as a stage a crash left behind.
        part.unlink(missing_ok=True)
        try:
            tool.run(*args, "--", part, *sources)
        except sh.RunError as error:
            raise Error(str(error)) from error


def _ls_7z(path: Path, *, password: str | None) -> list[Entry]:
    tool = _7z_tool()
    args = ["l", "-slt", f"-p{password or ''}", "--", str(path)]
    try:
        res = tool.run(*args)
    except sh.RunError as error:
        if "password" in error.err.lower() or "wrong password" in error.out.lower():
            raise PasswordError(f"{path.name}: wrong or missing password") from error
        raise BrokenError(str(error)) from error
    entries: list[Entry] = []
    cur: dict[str, str] = {}
    for line in res.out.splitlines():
        if not line.strip():
            if "Path" in cur:
                entries.append(_7z_entry(cur))
            cur = {}
        elif " = " in line:
            k, _, v = line.partition(" = ")
            cur[k.strip()] = v.strip()
    if "Path" in cur:
        entries.append(_7z_entry(cur))
    return [entry for entry in entries if Path(entry.name) != path]


def _7z_entry(cur: dict[str, str]) -> Entry:
    # `Attributes` holds Windows flags, then a Unix mode when there is one: `A_ lrwxrwxrwx`.
    flags, _, mode = cur.get("Attributes", "").partition(" ")
    # `L` is a Windows reparse point (a link or junction), `l` a Unix symbolic link.
    if "L" in flags or mode.startswith("l") or cur.get("Symbolic Link") or cur.get("Hard Link"):
        raise UnsafeError(f"unsafe 7z entry {cur['Path']!r}: a link")
    is_dir = cur.get("Folder") == "+" or flags.startswith("D")
    size_raw = cur.get("Size", "")
    size = int(size_raw) if size_raw.isdigit() else None
    return Entry(cur["Path"], size, "dir" if is_dir else "file")


def _unpack_7z(
    path: Path, dest: Path, entries: Sequence[Entry], *, password: str | None,
    conflict: Conflict, progress: Progress | None,
) -> list[Path]:
    tool = _7z_tool()
    flag = "-aos" if conflict == "skip" else "-aoa"
    args = ["x", "-y", flag, f"-p{password or ''}", f"-o{dest}", "--", str(path)]
    try:
        tool.run(*args)
    except sh.RunError as error:
        if "password" in error.err.lower():
            raise PasswordError(f"{path.name}: wrong or missing password") from error
        raise BrokenError(str(error)) from error
    written = [_safe_dest(dest, e.name) for e in entries if e.kind == "file"]
    if progress:
        progress(len(written), len(written))
    return written


def _unrar_tool() -> sh.Tool:
    try:
        return sh.find("unrar")
    except sh.MissingError as error:
        raise MissingError("RAR archives need `unrar` on PATH") from error


def _ls_unrar(path: Path, *, password: str | None) -> list[Entry]:
    tool = _unrar_tool()
    pwd_flag = f"-p{password}" if password else "-p-"
    try:
        res = tool.run("vt", pwd_flag, "--", str(path))
    except sh.RunError as error:
        if error.code == 11 or "password" in error.err.lower():
            raise PasswordError(f"{path.name}: wrong or missing password") from error
        raise BrokenError(str(error)) from error
    entries: list[Entry] = []
    name: str | None = None
    size: int | None = None
    kind: Kind = "file"
    for line in res.out.splitlines():
        key, _, val = line.partition(":")
        k, v = key.strip(), val.strip()
        if k == "Name":
            if name is not None:
                entries.append(Entry(name, size, kind))
            name, size, kind = v, None, "file"
        elif k == "Size" and v.isdigit():
            size = int(v)
        elif k == "Type" and any(word in v.lower() for word in RAR_LINKS):
            raise UnsafeError(f"unsafe rar entry {name!r}: {v}")
        elif k == "Type":
            kind = "dir" if "dir" in v.lower() else "file"
    if name is not None:
        entries.append(Entry(name, size, kind))
    return entries


def _unpack_unrar(
    path: Path, dest: Path, entries: Sequence[Entry], *, password: str | None,
    conflict: Conflict, progress: Progress | None,
) -> list[Path]:
    tool = _unrar_tool()
    pwd_flag = f"-p{password}" if password else "-p-"
    ov_flag = "-o-" if conflict == "skip" else "-o+"
    try:
        tool.run("x", "-y", ov_flag, pwd_flag, "--", str(path), str(dest) + os.sep)
    except sh.RunError as error:
        if error.code == 11 or "password" in error.err.lower():
            raise PasswordError(f"{path.name}: wrong or missing password") from error
        raise BrokenError(str(error)) from error
    written = [_safe_dest(dest, e.name) for e in entries if e.kind == "file"]
    if progress:
        progress(len(written), len(written))
    return written
