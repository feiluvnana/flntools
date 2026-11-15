"""Pack files into zip, 7z or tar, and list or unpack every common archive without trusting it.

`pack` writes one archive, its format chosen by the destination's extension, atomically and
optionally split into parts. `unpack` and `ls` sniff the format from the bytes, stream
split archives without copying them, and check every entry before anything is written.

    archive.pack("photos", "backup.7z", password="pw")
    for entry in archive.ls("backup.7z", password="pw"):
        print(entry.name, entry.size)
    archive.unpack("backup.7z", "restored/", password="pw")
"""

from __future__ import annotations

import bisect
import bz2
import functools
import gzip
import importlib
import io
import itertools
import lzma
import os
import re
import shutil
import stat
import subprocess
import sys
import tarfile
import tempfile
import types
import zipfile
from contextlib import AbstractContextManager, contextmanager, suppress
from dataclasses import dataclass
from pathlib import Path
from typing import (
    IO, TYPE_CHECKING, Any, BinaryIO, Callable, Iterator, Literal, NamedTuple, Sequence, Union,
    cast,
)

import shared
from shared import _fs as fs
from shared import _text as text

__all__ = [
    "BrokenError", "Entry", "Error", "Limits", "MissingError", "PasswordError",
    "UnsafeError", "ls", "pack", "unpack",
]

if TYPE_CHECKING:
    Extract = Callable[[list["_Entry"], Callable[["_Entry"], "_Output"]], None]
    Opened = Iterator[tuple[list["_Entry"], Extract]]

PathLike = Union[str, "os.PathLike[str]"]
Progress = Callable[[int, int], Any]
Conflict = Literal["error", "skip", "overwrite"]
Kind = Literal["file", "dir", "link", "special"]

CONFLICTS = ("error", "skip", "overwrite")
WRITE_FORMATS = {
    ".zip": "zip", ".7z": "7z", ".tar": "tar", ".tar.gz": "gz", ".tgz": "gz", ".tar.bz2": "bz2",
    ".tbz2": "bz2", ".tar.xz": "xz", ".txz": "xz", ".tar.zst": "zst",
}
ZIP_MAGIC = (b"PK\x03\x04", b"PK\x05\x06", b"PK\x07\x08")
SPANNED_ZIP_MAGIC = b"PK\x07\x08"
SEVEN_ZIP_MAGIC = b"7z\xbc\xaf\x27\x1c"
RAR_MAGIC = b"Rar!\x1a\x07"
COMPRESSION_MAGIC = {
    b"\x1f\x8b": "gz", b"BZh": "bz2", b"\xfd7zXZ\x00": "xz", b"\x28\xb5\x2f\xfd": "zst"
}
RAR_KINDS: dict[str, Kind] = {"File": "file", "Directory": "dir"}
DRIVE = re.compile(r"[A-Za-z]:")
RESERVED_NAMES = frozenset({"CON", "PRN", "AUX", "NUL", *(f"COM{n}" for n in range(1, 10)),
                            *(f"LPT{n}" for n in range(1, 10))})
UNRAR_BAD_PASSWORD = 11
CHUNK_SIZE = 1024 * 1024
NO_ZSTD = "zstd needs Python 3.14 or later"
NO_UNRAR = ("reading RAR needs the unrar program: brew install unrar, apt install unrar, "
            "or https://www.rarlab.com/download.htm")


class Error(shared.Error):
    """The base of every error `shared.archive` raises."""


class UnsafeError(Error, shared.UnsafeError):
    """An entry would land outside dest, collides, exceeds limits, or is a link/device."""


class BrokenError(Error, shared.BrokenError):
    """The file is no archive `unpack` reads, or its index is damaged, or `unrar` failed on it."""


class PasswordError(Error, shared.RefusedError, ValueError):
    """The archive needs a password, or the one given is wrong."""


class MissingError(Error, shared.MissingError):
    """Reading or writing needs what this machine lacks: `unrar`, an extra, or .zst < 3.14."""


class Entry(NamedTuple):
    """One file or folder an archive holds, as `ls` lists it.

        [entry.name for entry in archive.ls("backup.zip") if entry.kind == "file"]
    """

    name: str
    size: int | None
    kind: Literal["file", "dir"]


@dataclass(frozen=True)
class Limits:
    """How far `unpack` lets an archive expand before refusing it as a bomb.

        archive.unpack("backup.zip", "out/", limits=archive.Limits(size=100_000_000_000))
    """

    size: int = 20_000_000_000
    ratio: float = 200
    files: int = 100_000


class MultiPartFile(io.RawIOBase):
    """Seekable read-only binary stream over split archive parts without copying."""

    def __init__(self, parts: Sequence[Path]) -> None:
        self._parts = list(parts)
        self._sizes = [part.stat().st_size for part in self._parts]
        self._starts = list(itertools.accumulate(self._sizes, initial=0))
        self._total = self._starts[-1]
        self._pos = 0
        self._index = -1
        self._current: BinaryIO | None = None

    def readable(self) -> bool:
        """Whether the stream supports reading."""
        return True

    def seekable(self) -> bool:
        """Whether the stream supports seeking."""
        return True

    def tell(self) -> int:
        """Current byte offset across the joined parts."""
        return self._pos

    def seek(self, offset: int, whence: int = io.SEEK_SET) -> int:
        """Move the stream position to `offset` relative to `whence`."""
        if whence == io.SEEK_SET:
            target = offset
        elif whence == io.SEEK_CUR:
            target = self._pos + offset
        elif whence == io.SEEK_END:
            target = self._total + offset
        else:
            raise ValueError(f"invalid whence: {whence}")
        if target < 0:
            raise ValueError(f"negative seek position: {target}")
        self._pos = target
        return self._pos

    def readinto(self, buffer: Any) -> int:
        """Read bytes from the current part into `buffer` and return the count read."""
        if self._pos >= self._total or not len(buffer):
            return 0
        index = bisect.bisect_right(self._starts, self._pos) - 1
        if index >= len(self._parts):
            return 0
        if self._index != index:
            if self._current is not None:
                self._current.close()
            self._current = self._parts[index].open("rb")
            self._index = index
        assert self._current is not None
        self._current.seek(self._pos - self._starts[index])
        remaining = self._starts[index + 1] - self._pos
        chunk = self._current.read(min(len(buffer), remaining))
        n = len(chunk)
        memoryview(buffer)[:n] = chunk
        self._pos += n
        return n

    def close(self) -> None:
        """Close the currently open part handle."""
        if self._current is not None:
            self._current.close()
            self._current = None
        super().close()


def pack(sources: PathLike | Sequence[PathLike], dest: PathLike, *, password: str | None = None,
         split: int | None = None, progress: Progress | None = None) -> list[Path]:
    """Pack files and folders into one archive, whose format comes from `dest`'s extension.

        archive.pack(["photos", "notes.txt"], "out/backup.7z", password="pw")
    """
    dest = Path(dest)
    kind = _write_format(dest, password)
    if split is not None and split < 1:
        raise Error(f"split must be a positive number of bytes, not {split}")
    files = _collect(sources)
    dest.parent.mkdir(parents=True, exist_ok=True)
    pieces: list[Path] = []
    try:
        with fs.write(dest, replace=True) as whole:
            if kind == "zip":
                _write_zip(whole, files, password, progress)
            elif kind == "7z":
                _write_7z(whole, files, password, progress)
            else:
                _write_tar(whole, files, kind, progress)
            if split is not None:
                pieces = _split(whole, split)
                whole.unlink()
        if split is None:
            return [dest]
        finals = [
            dest.with_name(f"{dest.name}.{number:03}") for number in range(1, len(pieces) + 1)
        ]
        for index, piece in enumerate(pieces):
            piece.replace(finals[index])
        return finals
    finally:
        for leftover in pieces:
            leftover.unlink(missing_ok=True)


def ls(parts: PathLike | Sequence[PathLike], *, password: str | None = None,
       limits: Limits = Limits()) -> list[Entry]:
    """The index of a zip, 7z, RAR or tar archive, or of a compressed file, without unpacking it.

        archive.ls("in/photos.7z.001", password="pw")
    """
    part_paths = _resolve_parts(parts)
    with _checked(part_paths, password, limits) as (found, _extract, _size):
        return [
            Entry(e.name, 0, "dir") if e.kind == "dir" else Entry(e.name, e.size, "file")
            for e in found
        ]


def unpack(parts: PathLike | Sequence[PathLike], dest: PathLike, *, password: str | None = None,
           conflict: Conflict = "error", limits: Limits = Limits(),
           progress: Progress | None = None) -> list[Path]:
    """Unpack a zip, 7z, RAR or tar archive, or all the parts of one, directly into `dest`.

        archive.unpack("in/photos.7z.001", "out/", password="pw", conflict="skip")
    """
    if conflict not in CONFLICTS:
        raise Error(f"conflict must be one of {', '.join(CONFLICTS)}, not {conflict!r}")
    dest = Path(dest)
    part_paths = _resolve_parts(parts)
    with _checked(part_paths, password, limits) as (found, extract, archive_size):
        plan = _plan(found, dest, conflict)
        undeclared_cap = min(limits.size, int(limits.ratio * max(archive_size, 1)))
        return _extract(extract, plan, dest, undeclared_cap, progress)


def _resolve_parts(path: PathLike | Sequence[PathLike]) -> list[Path]:
    if isinstance(path, (str, os.PathLike)):
        return _find_parts(Path(path))
    parts = [Path(part) for part in path]
    if not parts:
        raise BrokenError("no archive parts given")
    return parts


@contextmanager
def _checked(parts: list[Path], password: str | None,
             limits: Limits) -> Iterator[tuple[list[_Entry], Extract, int]]:
    """Open the parts as one virtual stream, list and validate every entry before writing."""
    try:
        raw = MultiPartFile(parts)
    except FileNotFoundError as error:
        raise Error(str(error)) from error
    archive_size = sum(raw._sizes)
    with io.BufferedReader(raw) as stream:
        with _open(stream, parts, password, limits) as (found, extract):
            found = [entry for entry in found if entry.name]
            _check(found, limits, archive_size)
            yield found, extract, archive_size


def _write_format(dest: Path, password: str | None) -> str:
    lower = dest.name.lower()
    kind = next((kind for suffix, kind in WRITE_FORMATS.items() if lower.endswith(suffix)), None)
    if kind is None:
        raise Error(
            f"unknown archive type {dest.name!r}: use one of {', '.join(WRITE_FORMATS)}"
        )
    if kind == "zst" and sys.version_info < (3, 14):
        raise MissingError(f"cannot write {dest.name}: {NO_ZSTD}")
    if password is not None and kind not in ("zip", "7z"):
        raise Error(f"tar has no password: use .zip or .7z for {dest.name}")
    return kind


def _collect(sources: PathLike | Sequence[PathLike]) -> list[tuple[Path, str]]:
    roots = [sources] if isinstance(sources, (str, os.PathLike)) else list(sources)
    found: list[tuple[Path, str]] = []
    for root in roots:
        source = Path(os.path.abspath(root))
        if not source.exists():
            raise Error(f"nothing to pack at {root}")
        inside = sorted(source.rglob("*")) if source.is_dir() else []
        found += [(path, path.relative_to(source.parent).as_posix()) for path in [source, *inside]]
    return found


def _write_zip(path: Path, files: list[tuple[Path, str]], password: str | None,
               progress: Progress | None = None) -> None:
    if password is None:
        packed = zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED)
    else:
        pyzipper = _require("pyzipper")
        packed = pyzipper.AESZipFile(
            path, "w", compression=pyzipper.ZIP_DEFLATED, encryption=pyzipper.WZ_AES
        )
        packed.setpassword(password.encode())
    total = len(files)
    with packed:
        for index, (file, name) in enumerate(files, 1):
            packed.write(file, name)
            if progress:
                progress(index, total)


def _write_7z(path: Path, files: list[tuple[Path, str]], password: str | None,
              progress: Progress | None = None) -> None:
    py7zr = _require("py7zr")
    total = len(files)
    with py7zr.SevenZipFile(path, "w", password=password, header_encryption=password is not None,
                            dereference=True) as packed:
        for index, (file, name) in enumerate(files, 1):
            packed.write(file, name)
            if progress:
                progress(index, total)


def _write_tar(path: Path, files: list[tuple[Path, str]], compression: str,
               progress: Progress | None = None) -> None:
    mode = "w" if compression == "tar" else f"w:{compression}"
    total = len(files)
    with tarfile.open(path, mode, dereference=True) as packed:  # type: ignore[call-overload]
        for index, (file, name) in enumerate(files, 1):
            packed.add(file, name, recursive=False)
            if progress:
                progress(index, total)


def _split(whole: Path, part_size: int) -> list[Path]:
    count = max(1, -(-whole.stat().st_size // part_size))
    pieces = [whole.with_name(f"{whole.name}.{number:03}") for number in range(1, count + 1)]
    with whole.open("rb") as source:
        for piece in pieces:
            with piece.open("wb") as out:
                left = part_size
                while left and (chunk := source.read(min(left, CHUNK_SIZE))):
                    out.write(chunk)
                    left -= len(chunk)
    return pieces


def _require(module: str) -> Any:
    try:
        return importlib.import_module(module)
    except ImportError:
        raise MissingError(
            f"shared.archive needs {module}: pip install 'flntools[archive]'"
        ) from None


def _find_parts(first: Path) -> list[Path]:
    stem, _, suffix = first.name.rpartition(".")
    suffix = suffix.lower()
    if suffix == "001":
        template, last = f"{stem}.{{:03}}", []
    elif suffix == "z01" or (suffix == "zip" and first.with_name(f"{stem}.z01").exists()):
        template, last = f"{stem}.z{{:02}}", [first.with_name(f"{stem}.zip")]
    else:
        return [first]
    found: list[Path] = []
    while (part := first.with_name(template.format(len(found) + 1))).exists():
        found.append(part)
    return found + last


@dataclass(eq=False)
class _Entry:
    name: str
    kind: Kind
    size: int | None
    packed: int | None = None
    member: Any = None


def _open(stream: BinaryIO, parts: list[Path], password: str | None,
          limits: Limits) -> AbstractContextManager[tuple[list[_Entry], Extract]]:
    """Sniff the format from the first bytes and open it with the matching reader."""
    head = stream.read(8)
    stream.seek(0)
    name = parts[0].name
    if head.startswith(ZIP_MAGIC):
        spanned = head.startswith(SPANNED_ZIP_MAGIC) and len(parts) > 1
        sizes = [part.stat().st_size for part in parts] if spanned else None
        return _read_zip(stream, name, password, sizes)
    if head.startswith(SEVEN_ZIP_MAGIC):
        return _read_7z(stream, name, password)
    if head.startswith(RAR_MAGIC):
        return _read_rar(parts[0], password)
    compression = next(
        (kind for magic, kind in COMPRESSION_MAGIC.items() if head.startswith(magic)), None
    )
    if compression == "zst" and sys.version_info < (3, 14):
        raise MissingError(f"cannot read {name}: {NO_ZSTD}")
    if _is_tar(stream, compression):
        stream.seek(0)
        return _read_tar(stream, name, limits.files)
    if compression is not None:
        stream.seek(0)
        unpacked_name = Path(re.sub(r"\.\d{3}$", "", name)).stem
        return _read_single(stream, compression, unpacked_name)
    raise BrokenError(
        f"{name} is not a zip, 7z, RAR or tar archive, nor a compressed file"
    )


def _is_tar(stream: BinaryIO, compression: str | None) -> bool:
    if compression is None:
        return stream.read(262)[257:] == b"ustar"
    with _decompress(stream, compression) as decompressed:
        return decompressed.read(262)[257:] == b"ustar"


@contextmanager
def _read_zip(stream: BinaryIO, name: str, password: str | None,
              part_sizes: list[int] | None) -> Opened:
    """Open a zip stream for reading and yield its entries with an extractor."""
    with _broken_errors(name, zipfile.BadZipFile):
        if password is None:
            packed = zipfile.ZipFile(stream)
        else:
            pyzipper = _require("pyzipper")
            packed = pyzipper.AESZipFile(stream)
            packed.setpassword(password.encode())
    with packed:
        infos = packed.infolist()
        if password is None and any(info.flag_bits & 1 for info in infos):
            raise PasswordError(f"{name} needs a password")
        if part_sizes is not None:
            _fix_spanned_offsets(packed, part_sizes)
        yield [_zip_entry(info) for info in infos], functools.partial(_extract_zip, packed)


def _fix_spanned_offsets(packed: zipfile.ZipFile, part_sizes: list[int]) -> None:
    starts = list(itertools.accumulate(part_sizes, initial=0))
    directory_start = max(start for start in starts if start <= packed.start_dir)
    for info in packed.infolist():
        info.header_offset += starts[info.volume] - directory_start
        if hasattr(info, "_end_offset"):
            setattr(info, "_end_offset", None)


def _zip_entry(info: zipfile.ZipInfo) -> _Entry:
    file_type = stat.S_IFMT(info.external_attr >> 16)
    if file_type == stat.S_IFLNK:
        kind: Kind = "link"
    elif file_type not in (0, stat.S_IFREG, stat.S_IFDIR):
        kind = "special"
    else:
        kind = "dir" if info.is_dir() or file_type == stat.S_IFDIR else "file"
    return _Entry(_safe_name(info.filename), kind, info.file_size, info.compress_size, info)


def _extract_zip(packed: Any, selected: list[_Entry],
                 open_output: Callable[[_Entry], _Output]) -> None:
    for entry in selected:
        with _password_errors(entry.name, encrypted=bool(entry.member.flag_bits & 1)):
            with packed.open(entry.member) as source, open_output(entry) as out:
                shutil.copyfileobj(source, out, CHUNK_SIZE)


@contextmanager
def _broken_errors(name: str, *errors: type[Exception]) -> Iterator[None]:
    try:
        yield
    except errors as error:
        raise BrokenError(f"{name} is damaged: {error}") from error


@contextmanager
def _password_errors(name: str, *, encrypted: bool,
                     missing: tuple[type[Exception], ...] = ()) -> Iterator[None]:
    try:
        yield
    except missing as error:
        raise PasswordError(f"{name} needs a password") from error
    except (UnsafeError, OSError):
        raise
    except Exception as error:
        if not encrypted:
            raise
        raise PasswordError(f"wrong password for {name}, or it is damaged") from error


@contextmanager
def _read_tar(stream: BinaryIO, name: str, max_entries: int) -> Opened:
    with tarfile.open(fileobj=stream, mode="r:*") as packed:
        with _broken_errors(name, tarfile.ReadError, EOFError):
            members = list(itertools.islice(packed, max_entries + 1))

        def extract(selected: list[_Entry], open_output: Callable[[_Entry], _Output]) -> None:
            for entry in selected:
                with cast(IO[bytes], packed.extractfile(entry.member)) as source, \
                        open_output(entry) as out:
                    shutil.copyfileobj(source, out, CHUNK_SIZE)

        yield [_Entry(_safe_name(member.name), _tar_kind(member), member.size, None, member)
               for member in members], extract


def _tar_kind(member: tarfile.TarInfo) -> Kind:
    if member.issym() or member.islnk():
        return "link"
    if member.isdir():
        return "dir"
    return "file" if member.isfile() else "special"


@contextmanager
def _read_7z(stream: BinaryIO, name: str, password: str | None) -> Opened:
    """Open a 7z stream with py7zr and yield its entries with an extractor."""
    py7zr = _require("py7zr")
    password_errors = functools.partial(_password_errors, name, encrypted=password is not None,
                                        missing=(py7zr.exceptions.PasswordRequired,))
    with _broken_errors(name, py7zr.exceptions.Bad7zFile), password_errors(), \
            py7zr.SevenZipFile(stream, password=password) as packed:
        found = [_sevenzip_entry(file) for file in packed.files]

        def extract(selected: list[_Entry], open_output: Callable[[_Entry], _Output]) -> None:
            by_name = {entry.name: entry for entry in selected}

            def create(filename: str) -> Any:
                entry = by_name.get(_safe_name(filename))
                return open_output(entry) if entry is not None else py7zr.io.NullIO()

            with password_errors():
                factory = types.SimpleNamespace(create=create)
                packed.extract(targets=[entry.member for entry in selected], factory=factory)

        yield found, extract


def _sevenzip_entry(file: Any) -> _Entry:
    if file.is_symlink or file.is_junction:
        kind: Kind = "link"
    elif file.is_socket or (file.st_fmt or 0) not in (0, stat.S_IFREG, stat.S_IFDIR):
        kind = "special"
    else:
        kind = "dir" if file.is_directory else "file"
    return _Entry(
        _safe_name(file.filename), kind, file.uncompressed, file.compressed, file.filename
    )


@contextmanager
def _read_rar(path: Path, password: str | None) -> Opened:
    """List a RAR via `unrar lt` and yield its entries with a single-pass `unrar x` extractor."""
    unrar = shutil.which("unrar")
    if unrar is None:
        raise MissingError(NO_UNRAR)
    # -p- disables password prompting when none is given; passwords go via stdin, never argv.
    pw_args = ["-p-"] if password is None else []
    pw_input = None if password is None else f"{password}\n".encode()
    listing = subprocess.run(
        [unrar, "lt", "-c-", *pw_args, "--", str(path)], input=pw_input, capture_output=True
    )
    _check_unrar(listing.returncode, listing.stderr)

    def extract(selected: list[_Entry], open_output: Callable[[_Entry], _Output]) -> None:
        if not selected:
            return
        with tempfile.TemporaryDirectory() as rar_dir:
            cmd = [
                unrar, "x", "-y", "-c-", "-inul", *pw_args, "--",
                str(path), *(entry.member for entry in selected), rar_dir + os.sep,
            ]
            result = subprocess.run(cmd, input=pw_input, capture_output=True)
            _check_unrar(result.returncode, result.stderr)
            for entry in selected:
                extracted = Path(rar_dir) / entry.name
                with extracted.open("rb") as source, open_output(entry) as out:
                    shutil.copyfileobj(source, out, CHUNK_SIZE)

    yield _rar_entries(listing.stdout.decode("utf-8", "replace")), extract


def _check_unrar(returncode: int, stderr: bytes = b"") -> None:
    if returncode == UNRAR_BAD_PASSWORD:
        raise PasswordError("the RAR needs a password, or the one given is wrong")
    if returncode:
        detail = stderr.decode("utf-8", "replace").strip()
        raise BrokenError(f"unrar failed with code {returncode} {detail}".strip())


def _rar_entries(listing: str) -> list[_Entry]:
    found: list[_Entry] = []
    for block in re.split(r"\n\s*\n", listing):
        fields = dict(line.strip().split(": ", 1) for line in block.splitlines() if ": " in line)
        if "Name" in fields:
            kind = RAR_KINDS.get(fields.get("Type", ""), "link")
            packed_size = int(fields["Packed size"]) if "Packed size" in fields else None
            size = int(fields.get("Size", 0))
            found.append(
                _Entry(_safe_name(fields["Name"]), kind, size, packed_size, fields["Name"])
            )
    return found


@contextmanager
def _read_single(stream: BinaryIO, compression: str, name: str) -> Opened:
    def extract(selected: list[_Entry], open_output: Callable[[_Entry], _Output]) -> None:
        for entry in selected:
            stream.seek(0)
            with _decompress(stream, compression) as source, open_output(entry) as out:
                shutil.copyfileobj(source, out, CHUNK_SIZE)

    yield [_Entry(name, "file", None)], extract


def _decompress(stream: BinaryIO, compression: str) -> IO[bytes]:
    if compression == "zst":
        return cast(IO[bytes], importlib.import_module("compression.zstd").ZstdFile(stream, "rb"))
    openers: dict[str, Any] = {
        "gz": gzip.GzipFile, "bz2": bz2.BZ2File, "xz": lzma.LZMAFile,
    }
    if compression == "gz":
        return cast(IO[bytes], openers[compression](fileobj=stream, mode="rb"))
    return cast(IO[bytes], openers[compression](stream, "rb"))


def _safe_name(raw: str) -> str:
    name = raw.replace("\\", "/")
    parts = [part for part in name.split("/") if part not in ("", ".")]
    if name.startswith("/") or DRIVE.match(name) or ".." in parts:
        raise UnsafeError(f"refusing {raw!r}: it would land outside dest")
    if ":" in name:
        raise UnsafeError(f"refusing {raw!r}: a ':' names an NTFS stream or a drive on Windows")
    if any(part.split(".")[0].rstrip(" ").upper() in RESERVED_NAMES for part in parts):
        raise UnsafeError(f"refusing {raw!r}: Windows reserves that name for a device")
    return "/".join(parts)


def _check(found: list[_Entry], limits: Limits, archive_size: int) -> None:
    """Refuse links, devices, internal collisions and bombs before writing."""
    if len(found) > limits.files:
        raise UnsafeError(f"the archive holds more than {limits.files:,} entries")
    folders = _folder_keys(found)
    seen_files: dict[str, str] = {}
    for entry in found:
        if entry.kind == "link":
            raise UnsafeError(f"refusing {entry.name!r}: archives may not hold links")
        if entry.kind == "special":
            raise UnsafeError(
                f"refusing {entry.name!r}: device files, pipes and sockets are not allowed"
            )
        if entry.kind == "file":
            key = entry.name.casefold()
            if key in folders:
                raise UnsafeError(
                    f"refusing {entry.name!r}: collides with a folder of the same name"
                )
            if key in seen_files:
                raise UnsafeError(
                    f"refusing {entry.name!r}: collides with {seen_files[key]!r} inside the archive"
                )
            seen_files[key] = entry.name
        if entry.size and entry.packed is not None:
            if entry.size > limits.ratio * max(entry.packed, 1):
                raise UnsafeError(f"{entry.name!r} expands more than {limits.ratio:g} times")
    total = sum(entry.size or 0 for entry in found)
    if total > limits.size:
        raise UnsafeError(
            f"the archive expands to {text.fmt.size(total)}, over the {text.fmt.size(limits.size)} limit"
        )
    if total > limits.ratio * max(archive_size, 1):
        raise UnsafeError(f"the archive expands more than {limits.ratio:g} times")


def _folder_keys(found: list[_Entry]) -> set[str]:
    folders: set[str] = set()
    for entry in found:
        if entry.kind == "dir":
            folders.add(entry.name.casefold())
        parent = entry.name.rsplit("/", 1)[0] if "/" in entry.name else ""
        while parent:
            folders.add(parent.casefold())
            parent = parent.rsplit("/", 1)[0] if "/" in parent else ""
    return folders


def _plan(found: list[_Entry], dest: Path, conflict: str) -> list[tuple[_Entry, Path]]:
    """Pair each entry with its target under `dest`, resolving existing files by `conflict`."""
    planned: dict[str, tuple[_Entry, Path]] = {}
    clashes: list[str] = []
    for entry in found:
        target = dest / entry.name
        key = str(target).casefold()
        if entry.kind == "dir" or (key not in planned and not target.exists()):
            planned.setdefault(key, (entry, target))
            continue
        clashes.append(entry.name)
        if conflict == "overwrite":
            planned[key] = (entry, target)
    if clashes and conflict == "error":
        extra = f" and {len(clashes) - 10} more" if len(clashes) > 10 else ""
        shown = ", ".join(clashes[:10]) + extra
        raise Error(f"already in {dest}: {shown}")
    return list(planned.values())


def _extract(extract: Extract, plan: list[tuple[_Entry, Path]], dest: Path,
             undeclared_cap: int, progress: Progress | None = None) -> list[Path]:
    """Stage the planned files in a hidden folder, move them into place, and return them."""
    created = not dest.exists()
    dest.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=".unpack-", dir=dest))
    files = {entry: (staging / str(index), target) for index, (entry, target) in enumerate(plan)
             if entry.kind == "file"}
    outputs: list[_Output] = []
    total = len(files)
    done = 0

    def on_done() -> None:
        nonlocal done
        done += 1
        if progress:
            progress(done, total)

    def open_output(entry: _Entry) -> _Output:
        cap = undeclared_cap if entry.size is None else entry.size
        outputs.append(_Output(files[entry][0], cap, entry.name, on_done if progress else None))
        return outputs[-1]

    try:
        try:
            extract(list(files), open_output)
        finally:
            for output in outputs:
                output.close()
        for entry, target in plan:
            if entry.kind == "dir":
                target.mkdir(parents=True, exist_ok=True)
        for staged, target in files.values():
            target.parent.mkdir(parents=True, exist_ok=True)
            os.replace(staged, target)
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        if created:
            with suppress(OSError):
                dest.rmdir()
        raise
    staging.rmdir()
    return [target for _, target in files.values()]


class _Output:
    """A staged file that stops its entry writing more than it declared."""

    def __init__(self, path: Path, cap: int, name: str,
                 on_done: Callable[[], None] | None = None) -> None:
        self.file = path.open("wb")
        self.cap, self.name, self.written = cap, name, 0
        self._on_done = on_done

    def write(self, chunk: bytes) -> int:
        self.written += len(chunk)
        if self.written > self.cap:
            raise UnsafeError(f"{self.name!r} expands past {text.fmt.size(self.cap)}, "
                              "more than it declared or the limits allow")
        return self.file.write(chunk)

    def seekable(self) -> bool:
        return False

    def flush(self) -> None:
        self.file.flush()

    def size(self) -> int:
        return self.written

    def close(self) -> None:
        if not self.file.closed:
            self.file.close()
            if self._on_done is not None:
                cb, self._on_done = self._on_done, None
                cb()

    def __enter__(self) -> _Output:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()
