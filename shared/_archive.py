"""Pack and unpack zip and tar files with one call. The format comes from the file name."""

from __future__ import annotations

import os
import posixpath
import stat
import sys
import tarfile
import zipfile
from pathlib import Path
from typing import Iterable, Literal, Union

from shared import _path as path

PathLike = Union[str, "os.PathLike[str]"]

# Suffix -> tarfile compression, or "zip". Longer suffixes come first so ".tar.gz" wins over ".gz".
FORMATS = {
    ".tar.gz": "gz", ".tar.bz2": "bz2", ".tar.xz": "xz", ".tgz": "gz", ".tar": "", ".zip": "zip",
}
TAR_WRITE_MODES: dict[str, Literal["w:", "w:gz", "w:bz2", "w:xz"]] = {
    "": "w:", "gz": "w:gz", "bz2": "w:bz2", "xz": "w:xz",
}


class UnsafeError(ValueError):
    """An archive entry would land outside the folder it is unpacked into."""


def pack(sources: PathLike | Iterable[PathLike], dest: PathLike, *, root: PathLike | None = None) -> Path:
    """Write `sources` (files or folders, added recursively) into the archive `dest`.

    Names inside the archive are relative to `root`, which defaults to the parent of a
    single source, or to the current folder for a list.
    """
    dest = Path(dest)
    kind = _format_of(dest)
    single = isinstance(sources, (str, os.PathLike))
    source_paths = [Path(sources)] if single else [Path(source) for source in sources]
    if root is None:
        root = source_paths[0].parent if single else Path.cwd()
    root = Path(os.path.abspath(root))
    entries = [(file, _name_in(file, root)) for source in source_paths for file in _walk(source)]
    entries = [(file, name) for file, name in entries if name != "."]
    with path.atomic(dest) as handle:
        if kind == "zip":
            with zipfile.ZipFile(handle, "w", zipfile.ZIP_DEFLATED) as archive:
                for file, name in entries:
                    archive.write(file, name)
        else:
            with tarfile.open(fileobj=handle, mode=TAR_WRITE_MODES[kind]) as archive:
                for file, name in entries:
                    archive.add(file, name, recursive=False)
    return dest


def unpack(src: PathLike, dest_dir: PathLike) -> list[Path]:
    """Extract every entry into `dest_dir` and return the extracted paths.

    Every entry is checked before anything is written. An absolute name, a `..` part,
    a link that points outside `dest_dir`, or a device file raises `UnsafeError`.
    """
    src, dest_dir = Path(src), Path(dest_dir)
    if _format_of(src) == "zip":
        with zipfile.ZipFile(src) as archive:
            members = archive.infolist()
            for member in members:
                _check_zip_member(archive, member)
            path.mkdir(dest_dir)
            archive.extractall(dest_dir)
            return [dest_dir / member.filename.rstrip("/") for member in members]
    with tarfile.open(src) as archive:
        members = archive.getmembers()
        for member in members:
            _check_tar_member(member)
        path.mkdir(dest_dir)
        if sys.version_info >= (3, 12):
            archive.extractall(dest_dir, filter="data")
        else:
            archive.extractall(dest_dir)
        return [dest_dir / member.name for member in members]


def names(src: PathLike) -> list[str]:
    """The entry names in the archive, without extracting it."""
    src = Path(src)
    if _format_of(src) == "zip":
        with zipfile.ZipFile(src) as archive:
            return archive.namelist()
    with tarfile.open(src) as archive:
        return archive.getnames()


def _format_of(file: Path) -> str:
    lower = file.name.lower()
    for suffix, kind in FORMATS.items():
        if lower.endswith(suffix):
            return kind
    raise ValueError(f"unknown archive type {file.name!r}: use one of {', '.join(FORMATS)}")


def _walk(source: Path) -> list[Path]:
    if not source.exists():
        raise FileNotFoundError(f"nothing to pack at {source}")
    if source.is_dir() and not source.is_symlink():
        return [source, *sorted(source.rglob("*"))]
    return [source]


def _name_in(file: Path, root: Path) -> str:
    absolute = Path(os.path.abspath(file))
    try:
        return absolute.relative_to(root).as_posix()
    except ValueError:
        raise ValueError(f"{file} is not inside the archive root {root}") from None


def _check_name(name: str) -> None:
    parts = name.replace("\\", "/").split("/")
    if name.startswith(("/", "\\")) or (parts and ":" in parts[0]) or ".." in parts:
        raise UnsafeError(f"refusing to unpack {name!r}: it would land outside the folder")


def _check_link(name: str, target: str) -> None:
    resolved = posixpath.normpath(posixpath.join(posixpath.dirname(name), target))
    if target.startswith("/") or resolved == ".." or resolved.startswith("../"):
        raise UnsafeError(f"refusing to unpack link {name!r} -> {target!r}: it points outside the folder")


def _check_zip_member(archive: zipfile.ZipFile, member: zipfile.ZipInfo) -> None:
    _check_name(member.filename)
    if stat.S_ISLNK(member.external_attr >> 16):
        _check_link(member.filename, archive.read(member).decode("utf-8", "replace"))


def _check_tar_member(member: tarfile.TarInfo) -> None:
    _check_name(member.name)
    if member.issym():
        _check_link(member.name, member.linkname)
    elif member.islnk():
        _check_name(member.linkname)
    elif not (member.isfile() or member.isdir()):
        raise UnsafeError(f"refusing to unpack {member.name!r}: device and pipe entries are not allowed")
