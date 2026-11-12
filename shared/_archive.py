"""Pack files into zip, 7z or tar, and list or unpack every common archive without trusting it.

`zip` writes one archive, its format chosen by the destination's extension, atomically and
optionally split into parts. `unzip` and `ls` sniff the format from the bytes, never the name, join
the parts of a split archive, and read its index. Every entry is checked before anything is
written: a name that would land outside the destination or that Windows would misread, a link or
device file, and an archive that would expand past its `Limits` are all refused. `unzip` then
stages the files in a hidden folder and moves them into place only when every one is written.

Plain zip and tar use only the stdlib. Password zips and 7z need the `archive` extra
(`pyzipper`, `py7zr`), and RAR is read through the `unrar` program. Each library is wrapped
for one call, so there is no `.raw`.

    archive.zip("photos", "backup.7z", password="pw")
    for entry in archive.ls("backup.7z", password="pw"):
        print(entry.name, entry.size)
    archive.unzip("backup.7z", "restored/", password="pw")
"""

from __future__ import annotations

import bz2
import functools
import gzip
import importlib
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
from typing import IO, TYPE_CHECKING, Any, Callable, Iterator, Literal, Sequence, Union, cast

import shared
from shared import _atomic
from shared import _fmt as fmt

__all__ = ["BombError", "BrokenError", "ConflictError", "Entry", "Error", "Limits", "PasswordError", "UnsafeError",
           "ls", "unzip", "zip"]

if TYPE_CHECKING:
    Extract = Callable[[list["_Entry"], Callable[["_Entry"], "_Output"]], None]
    Opened = Iterator[tuple[list["_Entry"], Extract]]

PathLike = Union[str, "os.PathLike[str]"]
Conflict = Literal["error", "skip", "overwrite", "rename"]
Kind = Literal["file", "dir", "link", "special"]

CONFLICTS = ("error", "skip", "overwrite", "rename")
WRITE_FORMATS = {
    ".zip": "zip", ".7z": "7z", ".tar": "tar", ".tar.gz": "gz", ".tgz": "gz", ".tar.bz2": "bz2",
    ".tbz2": "bz2", ".tar.xz": "xz", ".txz": "xz", ".tar.zst": "zst",
}
ZIP_MAGIC = (b"PK\x03\x04", b"PK\x05\x06", b"PK\x07\x08")
SPANNED_ZIP_MAGIC = b"PK\x07\x08"
SEVEN_ZIP_MAGIC = b"7z\xbc\xaf\x27\x1c"
RAR_MAGIC = b"Rar!\x1a\x07"
COMPRESSION_MAGIC = {b"\x1f\x8b": "gz", b"BZh": "bz2", b"\xfd7zXZ\x00": "xz", b"\x28\xb5\x2f\xfd": "zst"}
RAR_KINDS: dict[str, Kind] = {"File": "file", "Directory": "dir"}
DRIVE = re.compile(r"[A-Za-z]:")
# Windows' device names: a file named CON, NUL, COM1… opens the device instead, with any extension
# ("con.txt") and whatever its case. COM0, LPT0 and COM10 are ordinary names. Windows also drops
# trailing spaces before the extension ("con .txt"), so those are stripped before comparing.
RESERVED_NAMES = frozenset({"CON", "PRN", "AUX", "NUL", *(f"COM{n}" for n in range(1, 10)),
                            *(f"LPT{n}" for n in range(1, 10))})
UNRAR_BAD_PASSWORD = 11
CHUNK_SIZE = 1024 * 1024
NO_ZSTD = "zstd needs Python 3.14 or later"
NO_UNRAR = ("reading RAR needs the unrar program: brew install unrar, apt install unrar, "
            "or https://www.rarlab.com/download.htm")


class Error(shared.Error):
    """The base of every error `shared.archive` raises."""


# Each error is still a ValueError, as before the error tree: the unsafe ones through shared.UnsafeError.
class UnsafeError(Error, shared.UnsafeError):
    """An entry would land outside dest, or is a link, device file, pipe or socket."""


class BombError(Error, shared.UnsafeError):
    """An archive would expand past its `Limits`."""


class BrokenError(Error, shared.BrokenError):
    """The file is no archive `unzip` reads, or its index is damaged, or `unrar` failed on it."""


class ConflictError(Error, ValueError):
    """Files from the archive already exist in dest, and `conflict="error"`."""


class PasswordError(Error, shared.RefusedError, ValueError):
    """The archive needs a password, or the one given is wrong."""


@dataclass(frozen=True)
class Entry:
    """One file or folder an archive holds, as `ls` lists it.

        [entry.name for entry in archive.ls("backup.zip") if entry.kind == "file"]
    """

    # The path `unzip` writes it to under dest, its parts joined by "/": "album/photos/1.jpg".
    name: str
    # Its size unpacked, in bytes, as the archive declares it: 0 for a folder, and None for the one
    # file of a .gz, .bz2, .xz or .zst, whose size isn't declared (a gzip's trailer is only mod 4 GB).
    size: int | None
    # "file" or "dir"; links and device files are refused, so `ls` never lists one.
    kind: Literal["file", "dir"]


@dataclass(frozen=True)
class Limits:
    """How far `unzip` lets an archive expand before refusing it as a bomb.

        archive.unzip("backup.zip", "out/", limits=archive.Limits(size=100_000_000_000))
    """

    size: int = 20_000_000_000
    ratio: float = 200
    files: int = 100_000


def zip(sources: PathLike | Sequence[PathLike], dest: PathLike, *, password: str | None = None,
        split: int | None = None) -> list[Path]:
    """Pack files and folders into one archive, whose format comes from dest's extension.

        archive.zip(["photos", "notes.txt"], "out/backup.7z", password="pw")

    A folder is added with everything inside it, named relative to its parent. `split`
    writes parts of that many bytes, `backup.7z.001`, `.002`, …, returned in order.
    A failed `zip` leaves no file behind.
    """
    dest = Path(dest)
    kind = _write_format(dest, password)
    if split is not None and split < 1:
        raise ValueError(f"split must be a positive number of bytes, not {split}")
    files = _collect(sources)
    dest.parent.mkdir(parents=True, exist_ok=True)
    pieces: list[Path] = []
    try:
        # The archive is written whole to a part file first; unsplit, it becomes dest when done.
        with _atomic.writing(dest, replace=True) as whole:
            if kind == "zip":
                _write_zip(whole, files, password)
            elif kind == "7z":
                _write_7z(whole, files, password)
            else:
                _write_tar(whole, files, kind)
            if split is not None:
                # Split, only the pieces are kept: deleting the whole leaves dest untouched.
                pieces = _split(whole, split)
                whole.unlink()
        if split is None:
            return [dest]
        finals = [dest.with_name(f"{dest.name}.{number:03}") for number in range(1, len(pieces) + 1)]
        for index, piece in enumerate(pieces):
            piece.replace(finals[index])
        return finals
    finally:
        for leftover in pieces:
            leftover.unlink(missing_ok=True)


def ls(path: PathLike, *, password: str | None = None, limits: Limits = Limits()) -> list[Entry]:
    """The index of a zip, 7z, RAR or tar archive, or of a compressed file, without unpacking it.

        archive.ls("in/photos.7z.001", password="pw")   # [Entry(name='photos', size=0, kind='dir'), …]

    It reads what `unzip` reads, under the same checks, so an archive it lists is one `unzip`
    would unpack, and in the same order. A first part (`.001`, `.z01`) finds its siblings.

    Args:
        path: the archive, or the first part of a split one; its format is sniffed from the bytes.
        password: the password of an encrypted zip, 7z or RAR; None for none.
        limits: how far the archive may expand by what its index declares, as for `unzip`.

    Raises:
        UnsafeError: an entry's name would land outside dest, holds a `:` or a Windows device name
            such as `NUL`, or the entry is a link, device file, pipe or socket.
        BombError: the index declares more than `limits` allow. A size the index doesn't declare,
            or declares falsely, is only caught by `unzip`, as it writes.
        PasswordError: the archive is encrypted and no password is given, or a 7z whose names are
            encrypted gets a wrong one. A zip's index isn't encrypted, so a wrong password for a
            zip shows only when `unzip` reads its contents.
        BrokenError: the file is no archive this reads, or its index is damaged.
        RuntimeError: a .zst file before Python 3.14, or a RAR without the `unrar` program.

    Reads the whole archive for tar, whose index is spread through it, and copies a split
    archive's parts into one temporary file.
    """
    with _checked(_find_parts(Path(path)), password, limits) as (entries, _extract, _size):
        # `_check` refused links and specials, so every entry left is a file or a folder.
        return [Entry(entry.name, 0, "dir") if entry.kind == "dir" else Entry(entry.name, entry.size, "file")
                for entry in entries]


def unzip(parts: PathLike | Sequence[PathLike], dest: PathLike, *, password: str | None = None,
          conflict: Conflict = "error", limits: Limits = Limits()) -> list[Path]:
    """Unpack a zip, 7z, RAR or tar archive, or all the parts of one, directly into dest.

        archive.unzip("in/photos.7z.001", "out/", password="pw", conflict="rename")

    The format is sniffed from the bytes, never the name, and a first part (`.001`, `.z01`)
    finds its siblings. Returns the files written. Every entry is checked before anything
    is written: traversal, links and devices raise `UnsafeError`, archives past `limits`
    raise `BombError`, and files that already exist follow `conflict`.

    Raises:
        BrokenError: the file is no archive this reads, or its index is damaged.
        (The other errors are `ls`'s, plus `ConflictError` and a `PasswordError` for a wrong
        password found while reading the contents.)
    """
    if conflict not in CONFLICTS:
        raise ValueError(f"conflict must be one of {', '.join(CONFLICTS)}, not {conflict!r}")
    dest = Path(dest)
    single = isinstance(parts, (str, os.PathLike))
    part_paths = _find_parts(Path(parts)) if single else [Path(part) for part in parts]
    with _checked(part_paths, password, limits) as (entries, extract, archive_size):
        plan = _plan(entries, dest, conflict)
        # A stream that doesn't declare its size may still grow only as far as the limits allow.
        undeclared_cap = min(limits.size, int(limits.ratio * max(archive_size, 1)))
        return _extract(extract, plan, dest, undeclared_cap)


@contextmanager
def _checked(parts: list[Path], password: str | None, limits: Limits) -> Iterator[tuple[list[_Entry], Extract, int]]:
    """The parts joined and opened: their checked entries, what extracts them, and the joined size.

    What `ls` and `unzip` share, so the two can't drift apart in what they refuse.
    """
    with tempfile.TemporaryDirectory() as workdir:
        joined = _join(parts, Path(workdir))
        archive_size = joined.stat().st_size
        with _open(joined, parts, password, limits) as (entries, extract):
            # An entry named "." or "/" names nothing to write, so it is dropped.
            entries = [entry for entry in entries if entry.name]
            _check(entries, limits, archive_size)
            yield entries, extract, archive_size


def _write_format(dest: Path, password: str | None) -> str:
    lower = dest.name.lower()
    kind = next((kind for suffix, kind in WRITE_FORMATS.items() if lower.endswith(suffix)), None)
    if kind is None:
        raise ValueError(f"unknown archive type {dest.name!r}: use one of {', '.join(WRITE_FORMATS)}")
    if kind == "zst" and sys.version_info < (3, 14):
        raise RuntimeError(f"cannot write {dest.name}: {NO_ZSTD}")
    if password is not None and kind not in ("zip", "7z"):
        raise ValueError(f"tar has no password: use .zip or .7z for {dest.name}")
    return kind


def _collect(sources: PathLike | Sequence[PathLike]) -> list[tuple[Path, str]]:
    roots = [sources] if isinstance(sources, (str, os.PathLike)) else list(sources)
    found: list[tuple[Path, str]] = []
    for root in roots:
        source = Path(os.path.abspath(root))
        if not source.exists():
            raise FileNotFoundError(f"nothing to pack at {root}")
        inside = sorted(source.rglob("*")) if source.is_dir() else []
        found += [(path, path.relative_to(source.parent).as_posix()) for path in [source, *inside]]
    return found


def _write_zip(path: Path, files: list[tuple[Path, str]], password: str | None) -> None:
    if password is None:
        packed = zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED)
    else:
        pyzipper = _require("pyzipper")
        packed = pyzipper.AESZipFile(path, "w", compression=pyzipper.ZIP_DEFLATED, encryption=pyzipper.WZ_AES)
        packed.setpassword(password.encode())
    with packed:
        for file, name in files:
            packed.write(file, name)


def _write_7z(path: Path, files: list[tuple[Path, str]], password: str | None) -> None:
    py7zr = _require("py7zr")
    # Encrypting the header too hides the file names, not just their contents.
    with py7zr.SevenZipFile(path, "w", password=password, header_encryption=password is not None,
                            dereference=True) as packed:
        for file, name in files:
            packed.write(file, name)


def _write_tar(path: Path, files: list[tuple[Path, str]], compression: str) -> None:
    mode = "w" if compression == "tar" else f"w:{compression}"
    # Links are stored as the files they point to, because unzip refuses links.
    with tarfile.open(path, mode, dereference=True) as packed:  # pyright: ignore[reportCallIssue, reportArgumentType]
        for file, name in files:
            packed.add(file, name, recursive=False)


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
        raise ImportError(f"shared.archive needs {module}: pip install 'flntools[archive]'") from None


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


def _join(parts: list[Path], workdir: Path) -> Path:
    if len(parts) == 1:
        return parts[0]
    joined = workdir / "joined"
    with joined.open("wb") as out:
        for part in parts:
            with part.open("rb") as source:
                shutil.copyfileobj(source, out, CHUNK_SIZE)
    return joined


@dataclass(eq=False)
class _Entry:
    """One archive entry. `size` is None when the format doesn't declare it."""

    name: str
    kind: Kind
    size: int | None
    packed: int | None = None
    member: Any = None


def _open(path: Path, parts: list[Path], password: str | None,
          limits: Limits) -> AbstractContextManager[tuple[list[_Entry], Extract]]:
    with path.open("rb") as file:
        head = file.read(8)
    if head.startswith(ZIP_MAGIC):
        spanned = head.startswith(SPANNED_ZIP_MAGIC) and len(parts) > 1
        return _read_zip(path, password, [part.stat().st_size for part in parts] if spanned else None)
    if head.startswith(SEVEN_ZIP_MAGIC):
        return _read_7z(path, password)
    if head.startswith(RAR_MAGIC):
        return _read_rar(path, password)
    compression = next((name for magic, name in COMPRESSION_MAGIC.items() if head.startswith(magic)), None)
    if compression == "zst" and sys.version_info < (3, 14):
        raise RuntimeError(f"cannot read {parts[0].name}: {NO_ZSTD}")
    if _is_tar(path, compression):
        return _read_tar(path, limits.files)
    if compression is not None:
        unpacked_name = Path(re.sub(r"\.\d{3}$", "", parts[0].name)).stem
        return _read_single(path, compression, unpacked_name)
    raise BrokenError(f"{parts[0].name} is not a zip, 7z, RAR or tar archive, nor a compressed file")


def _is_tar(path: Path, compression: str | None) -> bool:
    # tarfile.is_tarfile would take any file that starts with 512 zero bytes for an empty tar.
    with path.open("rb") if compression is None else _decompress(path, compression) as stream:
        return stream.read(262)[257:] == b"ustar"


@contextmanager
def _read_zip(path: Path, password: str | None, part_sizes: list[int] | None) -> Opened:
    # Opening reads only the central directory, so BadZipFile here means the index is damaged.
    with _broken_errors(path, zipfile.BadZipFile):
        if password is None:
            packed = zipfile.ZipFile(path)
        else:
            # pyzipper reads both ZipCrypto and AES, and zipfile only ZipCrypto.
            pyzipper = _require("pyzipper")
            packed = pyzipper.AESZipFile(path)
            packed.setpassword(password.encode())
    with packed:
        infos = packed.infolist()
        if password is None and any(info.flag_bits & 1 for info in infos):
            raise PasswordError(f"{path.name} needs a password")
        if part_sizes is not None:
            _fix_spanned_offsets(packed, part_sizes)
        yield [_zip_entry(info) for info in infos], functools.partial(_extract_zip, packed)


def _fix_spanned_offsets(packed: zipfile.ZipFile, part_sizes: list[int]) -> None:
    # A spanned zip counts each offset from the start of its own part, while zipfile reads
    # the joined parts as one stream that starts where the central directory's part starts.
    starts = list(itertools.accumulate(part_sizes, initial=0))
    directory_start = max(start for start in starts if start <= packed.start_dir)
    for info in packed.infolist():
        info.header_offset += starts[info.volume] - directory_start
        # zipfile's overlap check assumes the old order of offsets; the bomb limits still apply.
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


def _extract_zip(packed: Any, selected: list[_Entry], open_output: Callable[[_Entry], _Output]) -> None:
    for entry in selected:
        # A wrong ZipCrypto password passes its one-byte check once in 256 tries and then
        # fails as bad data, so any read error on an encrypted entry is put down to the password.
        with _password_errors(entry.name, encrypted=bool(entry.member.flag_bits & 1)):
            with packed.open(entry.member) as source, open_output(entry) as out:
                shutil.copyfileobj(source, out, CHUNK_SIZE)


@contextmanager
def _broken_errors(path: Path, *errors: type[Exception]) -> Iterator[None]:
    """Raise `BrokenError` for `errors`, the ones a library raises for a damaged index."""
    try:
        yield
    except errors as error:
        raise BrokenError(f"{path.name} is damaged: {error}") from error


@contextmanager
def _password_errors(name: str, *, encrypted: bool, missing: tuple[type[Exception], ...] = ()) -> Iterator[None]:
    try:
        yield
    except missing as error:
        raise PasswordError(f"{name} needs a password") from error
    except (BombError, UnsafeError, OSError):
        raise
    except Exception as error:
        if not encrypted:
            raise
        raise PasswordError(f"wrong password for {name}, or it is damaged") from error


@contextmanager
def _read_tar(path: Path, max_entries: int) -> Opened:
    with tarfile.open(path, "r:*") as packed:
        # Listing stops one past the limit, so a bomb of tiny entries isn't read to the end. A tar
        # cut off partway raises ReadError, or EOFError from its decompressor, while it is listed.
        with _broken_errors(path, tarfile.ReadError, EOFError):
            members = list(itertools.islice(packed, max_entries + 1))

        def extract(selected: list[_Entry], open_output: Callable[[_Entry], _Output]) -> None:
            for entry in selected:
                with cast(IO[bytes], packed.extractfile(entry.member)) as source, open_output(entry) as out:
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
def _read_7z(path: Path, password: str | None) -> Opened:
    py7zr = _require("py7zr")
    password_errors = functools.partial(_password_errors, path.name, encrypted=password is not None,
                                        missing=(py7zr.exceptions.PasswordRequired,))
    with _broken_errors(path, py7zr.exceptions.Bad7zFile), password_errors(), \
            py7zr.SevenZipFile(path, password=password) as packed:
        entries = [_sevenzip_entry(file) for file in packed.files]

        def extract(selected: list[_Entry], open_output: Callable[[_Entry], _Output]) -> None:
            by_name = {entry.name: entry for entry in selected}

            # py7zr pushes each file's bytes into a writer it asks for by name.
            def create(filename: str) -> Any:
                entry = by_name.get(_safe_name(filename))
                return open_output(entry) if entry is not None else py7zr.io.NullIO()

            with password_errors():
                factory = types.SimpleNamespace(create=create)
                packed.extract(targets=[entry.member for entry in selected], factory=factory)

        yield entries, extract


def _sevenzip_entry(file: Any) -> _Entry:
    if file.is_symlink or file.is_junction:
        kind: Kind = "link"
    elif file.is_socket or (file.st_fmt or 0) not in (0, stat.S_IFREG, stat.S_IFDIR):
        kind = "special"
    else:
        kind = "dir" if file.is_directory else "file"
    return _Entry(_safe_name(file.filename), kind, file.uncompressed, file.compressed, file.filename)


@contextmanager
def _read_rar(path: Path, password: str | None) -> Opened:
    unrar = shutil.which("unrar")
    if unrar is None:
        raise RuntimeError(NO_UNRAR)
    password_switch = "-p-" if password is None else f"-p{password}"
    listing = subprocess.run([unrar, "lt", "-c-", password_switch, "--", str(path)], capture_output=True)
    _check_unrar(listing.returncode, listing.stderr)

    def extract(selected: list[_Entry], open_output: Callable[[_Entry], _Output]) -> None:
        for entry in selected:
            command = [unrar, "p", "-inul", password_switch, "--", str(path), entry.member]
            with subprocess.Popen(command, stdout=subprocess.PIPE) as process, open_output(entry) as out:
                shutil.copyfileobj(cast(IO[bytes], process.stdout), out, CHUNK_SIZE)
            _check_unrar(process.returncode)

    yield _rar_entries(listing.stdout.decode("utf-8", "replace")), extract


def _check_unrar(returncode: int, stderr: bytes = b"") -> None:
    if returncode == UNRAR_BAD_PASSWORD:
        raise PasswordError("the RAR needs a password, or the one given is wrong")
    if returncode:
        raise BrokenError(f"unrar failed with code {returncode} {stderr.decode('utf-8', 'replace').strip()}".strip())


def _rar_entries(listing: str) -> list[_Entry]:
    entries: list[_Entry] = []
    for block in re.split(r"\n\s*\n", listing):
        fields = dict(line.strip().split(": ", 1) for line in block.splitlines() if ": " in line)
        if "Name" in fields:
            # unrar names every other type as some kind of link: symbolic, hard, junction, file copy.
            kind = RAR_KINDS.get(fields.get("Type", ""), "link")
            packed_size = int(fields["Packed size"]) if "Packed size" in fields else None
            entries.append(_Entry(_safe_name(fields["Name"]), kind, int(fields.get("Size", 0)), packed_size,
                                  fields["Name"]))
    return entries


@contextmanager
def _read_single(path: Path, compression: str, name: str) -> Opened:
    def extract(selected: list[_Entry], open_output: Callable[[_Entry], _Output]) -> None:
        for entry in selected:
            with _decompress(path, compression) as source, open_output(entry) as out:
                shutil.copyfileobj(source, out, CHUNK_SIZE)

    yield [_Entry(name, "file", None)], extract


def _decompress(path: Path, compression: str) -> IO[bytes]:
    if compression == "zst":
        # Imported by name, because the module only exists on Python 3.14 and later.
        return importlib.import_module("compression.zstd").open(path, "rb")
    return cast(IO[bytes], {"gz": gzip.open, "bz2": bz2.open, "xz": lzma.open}[compression](path, "rb"))


def _safe_name(raw: str) -> str:
    """The entry name as a relative path joined by `/`, or `UnsafeError` when it can't be written safely.

    Names Windows would misread are refused on every platform, so an archive unpacks the same
    everywhere: a `:` anywhere (a drive, or an NTFS alternate data stream: `x/a:b` writes stream
    `b` of file `a`) and a device name such as `NUL` or `com1.txt` as any part of the path.
    """
    # Windows treats a backslash as a separator, so it counts as one everywhere.
    name = raw.replace("\\", "/")
    parts = [part for part in name.split("/") if part not in ("", ".")]
    if name.startswith("/") or DRIVE.match(name) or ".." in parts:
        raise UnsafeError(f"refusing {raw!r}: it would land outside dest")
    if ":" in name:
        raise UnsafeError(f"refusing {raw!r}: a ':' names an NTFS stream or a drive on Windows")
    if any(part.split(".")[0].rstrip(" ").upper() in RESERVED_NAMES for part in parts):
        raise UnsafeError(f"refusing {raw!r}: Windows reserves that name for a device")
    return "/".join(parts)


def _check(entries: list[_Entry], limits: Limits, archive_size: int) -> None:
    if len(entries) > limits.files:
        raise BombError(f"the archive holds more than {limits.files:,} entries")
    for entry in entries:
        if entry.kind == "link":
            # Links are refused even when they point inside dest: a chain of them can
            # still escape, as the review reproduced on Python 3.10 and 3.11.
            raise UnsafeError(f"refusing {entry.name!r}: archives may not hold links")
        if entry.kind == "special":
            raise UnsafeError(f"refusing {entry.name!r}: device files, pipes and sockets are not allowed")
        if entry.size and entry.packed is not None and entry.size > limits.ratio * max(entry.packed, 1):
            raise BombError(f"{entry.name!r} expands more than {limits.ratio:g} times")
    total = sum(entry.size or 0 for entry in entries)
    if total > limits.size:
        raise BombError(f"the archive expands to {fmt.size(total)}, over the {fmt.size(limits.size)} limit")
    if total > limits.ratio * max(archive_size, 1):
        raise BombError(f"the archive expands more than {limits.ratio:g} times")


def _plan(entries: list[_Entry], dest: Path, conflict: str) -> list[tuple[_Entry, Path]]:
    # Keys are case-folded, because many disks can't hold both A.jpg and a.jpg.
    planned: dict[str, tuple[_Entry, Path]] = {}
    clashes: list[str] = []
    for entry in entries:
        target = dest / entry.name
        key = str(target).casefold()
        if entry.kind == "dir" or (key not in planned and not target.exists()):
            planned.setdefault(key, (entry, target))
            continue
        clashes.append(entry.name)
        if conflict == "overwrite":
            planned[key] = (entry, target)
        elif conflict == "rename":
            renamed = _free_name(target, planned)
            planned[str(renamed).casefold()] = (entry, renamed)
    if clashes and conflict == "error":
        shown = ", ".join(clashes[:10]) + (f" and {len(clashes) - 10} more" if len(clashes) > 10 else "")
        raise ConflictError(f"already in {dest}: {shown}")
    return list(planned.values())


def _free_name(target: Path, taken: dict[str, tuple[_Entry, Path]]) -> Path:
    candidates = (target.with_name(f"{target.stem} ({number}){target.suffix}") for number in itertools.count(1))
    return next(path for path in candidates if str(path).casefold() not in taken and not path.exists())


def _extract(extract: Extract, plan: list[tuple[_Entry, Path]], dest: Path, undeclared_cap: int) -> list[Path]:
    # Entries are staged in a hidden folder inside dest and moved into place only once all
    # of them are written, so a bomb or a wrong password found midway leaves dest as it was.
    created = not dest.exists()
    dest.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=".unzip-", dir=dest))
    files = {entry: (staging / str(index), target) for index, (entry, target) in enumerate(plan)
             if entry.kind == "file"}
    outputs: list[_Output] = []

    def open_output(entry: _Entry) -> _Output:
        cap = undeclared_cap if entry.size is None else entry.size
        outputs.append(_Output(files[entry][0], cap, entry.name))
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
    """A staged file that stops its entry writing more than it declared.

    It is also the writer py7zr asks for, which is why it has `seekable`, `flush` and `size`.
    """

    def __init__(self, path: Path, cap: int, name: str) -> None:
        self.file = path.open("wb")
        self.cap, self.name, self.written = cap, name, 0

    def write(self, chunk: bytes) -> int:
        self.written += len(chunk)
        if self.written > self.cap:
            raise BombError(f"{self.name!r} expands past {fmt.size(self.cap)}, "
                            "more than it declared or the limits allow")
        return self.file.write(chunk)

    def seekable(self) -> bool:
        return False

    def flush(self) -> None:
        self.file.flush()

    def size(self) -> int:
        return self.written

    def close(self) -> None:
        self.file.close()

    def __enter__(self) -> _Output:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()
