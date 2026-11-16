"""Safe archive extraction: an untrusted zip, 7z, RAR or tar unpacked beside itself, or not at all.

A library module: it imports only the stdlib, the tool runner (`flnd._tools`, for 7-Zip) and
flnd's error classes. It offers:
- `is_archive(path)`: whether a file's name marks it as an archive (`extract` then reads its
  first bytes, since a name can lie);
- `folder(path)`: the folder an archive extracts into, its name without the archive suffix;
- `extract(path, password=…, limits=…)`: unpack it there, refusing any entry that would escape
  the folder or any archive past `Limits`, and return the folder.

An archive is extracted into a folder of its own name beside it: `loi-5930.7z` fills
`loi-5930/`. Who calls it in flnd: the unpack step. The step chain (`flnd._steps`) calls
`extract` on a downloaded `.zip`, `.7z`, `.rar` or `.tar*` (or any file its source gave a
password) once the archive's history row is written; `--compress` also removes the archive
after a verified extract (docs/media).

How `extract` goes:
1. Read the first bytes to tell the format; a name can lie (loi serves 7z under `.zip`).
2. List every entry without writing anything, and refuse the archive when an entry would land
   outside the folder (`../`, `/abs`, `C:`), is a link of any kind, or when the entries pass
   `Limits` (a zip bomb: too big, too many, or too compressed).
3. Extract into a hidden staging folder beside the archive, `.<name>.unpack`. zip and tar are
   read in Python, entry by entry in 1 MiB pieces; 7z, RAR and encrypted zips go to 7-Zip.
4. Verify: every file entry exists in the stage, at the size the listing gave.
5. Move the stage into place: renamed when the folder is new, else file by file over it.

Any failure removes the stage, so a wrong password or a damaged archive never leaves half a
folder, and raises: the archive stays where it is and its row records `failed`.

Rules a change must keep:
- nothing is written before the listing has passed every entry;
- every 7-Zip call puts `--` before the archive's path and passes the password as one argument,
  so neither is ever read as a switch.
"""

from __future__ import annotations

import gzip
import lzma
import os
import re
import shutil
import tarfile
import zipfile
import zlib
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import IO, Literal

from flnd import _tools
from flnd.errors import SiteError

__all__ = ["Limits", "extract", "folder", "is_archive"]

Kind = Literal["file", "dir"]

# The names that mark a file as an archive. A file with a password from its source is tried
# whatever its name.
SUFFIXES = (".zip", ".7z", ".rar", ".tar", ".tar.gz", ".tgz", ".tar.bz2", ".tbz2", ".tar.xz",
            ".txz")
DRIVE = re.compile(r"^[A-Za-z]:")
# Entries are copied in pieces of this size, so a 20 GB member never sits in memory.
CHUNK = 1024 * 1024
# The staging folder's suffix: `.<folder>.unpack`, hidden, beside the archive.
STAGE = ".unpack"
# A zip entry's general-purpose flag for "encrypted".
ENCRYPTED = 0x1
# What a damaged zip or tar raises inside Python's readers, besides their own error classes.
DAMAGED = (EOFError, zlib.error, gzip.BadGzipFile, lzma.LZMAError)


@dataclass(frozen=True)
class Entry:
    """One member of an archive, as its listing names it.

    Args:
        name: its path inside the archive, with `/` or `\\` between folders.
        size: its unpacked size in bytes; None when the listing doesn't say.
        kind: "file" or "dir".
    """

    name: str
    size: int | None
    kind: Kind


@dataclass(frozen=True)
class Limits:
    """How big an archive may unpack, so a zip bomb fails before it fills the disk.

    Args:
        size: the most bytes all entries may hold together (20 GB).
        ratio: the most unpacked bytes per archive byte (200: real media compresses far less).
        files: the most entries (100,000).
    """

    size: int = 20_000_000_000
    ratio: float = 200
    files: int = 100_000


def is_archive(path: Path) -> bool:
    """Whether `path`'s name marks it as an archive the unpack step takes.

    Args:
        path: a saved file.

    Returns:
        True for `.zip`, `.7z`, `.rar` and the `.tar` family, whatever the case.
    """
    return path.name.lower().endswith(SUFFIXES)


def folder(path: Path) -> Path:
    """The folder an archive extracts into: beside it, named after it without its suffix.

    Args:
        path: the archive.

    Returns:
        `a/b.tar.gz` gives `a/b`; a name with no archive suffix gets `_files` added.
    """
    lower = path.name.lower()
    for suffix in sorted(SUFFIXES, key=len, reverse=True):
        if lower.endswith(suffix) and len(lower) > len(suffix):
            return path.with_name(path.name[: -len(suffix)])
    return path.with_name(f"{path.name}_files")


def extract(path: Path, *, password: str | None = None, limits: Limits = Limits()) -> Path:
    """Extract `path` beside itself, refusing unsafe entries, and verify what was written.

    The steps are the module's: detect, list and check, stage, verify, place.

    Args:
        path: the archive.
        password: its password, from the source; None tries without one.
        limits: the bounds a zip bomb would pass.

    Returns:
        The folder now holding the archive's files.

    Raises:
        SiteError: the archive is damaged, unsupported, encrypted with a wrong or missing
            password, or unsafe (an escaping path, a link, past `limits`).
        MissingError: it needs 7-Zip, and `7zz` is not on PATH.
        DiskError: the disk refused a write.
    """
    kind = _detect(path)
    entries = _entries(path, kind, password=password)
    _check_limits(entries, path.stat().st_size, limits)
    dest = folder(path)
    stage = dest.with_name(f".{dest.name}{STAGE}")
    for entry in entries:
        _safe_dest(stage, entry.name)
    # A stage a crash left behind is half an extract: start again.
    shutil.rmtree(stage, ignore_errors=True)
    stage.mkdir(parents=True)
    try:
        if kind == "zip" and not _sevenzip_zip(path, password):
            _unpack_zip(path, stage, password=password)
        elif kind == "tar":
            _unpack_tar(path, stage)
        else:
            _unpack_7z(path, stage, password=password)
        _verify(stage, entries, path.name)
        _place(stage, dest)
    finally:
        shutil.rmtree(stage, ignore_errors=True)
    return dest


# ---------------------------------------------------------------------------------------------
# Detecting and listing


def _detect(path: Path) -> str:
    """The archive's format from its first bytes: "zip", "7z", "rar" or "tar".

    Args:
        path: the archive.

    Returns:
        Its format.

    Raises:
        SiteError: the file is missing, or no format this step knows.
    """
    if not path.is_file():
        raise SiteError(f"{path}: archive not found")
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
    raise SiteError(f"{path.name}: unsupported or corrupt archive")


def _entries(path: Path, kind: str, *, password: str | None) -> list[Entry]:
    """Every entry of the archive, refusing links (the first two parts of `extract`).

    Args:
        path: the archive.
        kind: its format, from `_detect`.
        password: its password, for a 7-Zip listing; None for none.

    Returns:
        Its entries, in the archive's order.

    Raises:
        SiteError: the archive can't be read, has a link, or needs a password it wasn't given.
    """
    if kind == "zip":
        try:
            with zipfile.ZipFile(path) as zf:
                return [Entry(i.filename, i.file_size, "dir" if i.is_dir() else "file")
                        for i in zf.infolist()]
        except zipfile.BadZipFile as error:
            raise SiteError(f"{path.name}: {error}") from error
    if kind == "tar":
        try:
            with tarfile.open(path) as tf:
                out: list[Entry] = []
                for m in tf.getmembers():
                    if m.issym() or m.islnk() or not (m.isfile() or m.isdir()):
                        raise SiteError(f"{path.name}: unsafe tar entry {m.name!r}")
                    out.append(Entry(m.name, m.size, "dir" if m.isdir() else "file"))
                return out
        except (tarfile.TarError, *DAMAGED) as error:
            raise SiteError(f"{path.name}: {error}") from error
    return _ls_7z(path, password=password)


def _safe_dest(dest: Path, member: str) -> Path:
    """Where `member` lands under `dest`, refusing any name that would land outside it.

    Args:
        dest: the folder entries land in.
        member: an entry's name as the archive spells it.

    Returns:
        The path it lands at.

    Raises:
        SiteError: an absolute path, a drive letter, an empty or `..` part, or a name that
            resolves outside `dest`.
    """
    norm = member.replace("\\", "/").rstrip("/")
    if not norm or norm.startswith("/") or DRIVE.match(norm):
        raise SiteError(f"unsafe archive path {member!r}")
    parts = PurePosixPath(norm).parts
    if any(p in ("..", "") for p in parts):
        raise SiteError(f"unsafe archive path {member!r}")
    target = dest / Path(*parts)
    if not os.path.abspath(target).startswith(os.path.abspath(dest) + os.sep):
        raise SiteError(f"archive path escapes its folder: {member!r}")
    return target


def _check_limits(entries: list[Entry], archive_size: int, limits: Limits) -> None:
    """Refuse an archive whose entries pass `limits`.

    Args:
        entries: its entries.
        archive_size: its own size in bytes, for the ratio.
        limits: the bounds.

    Raises:
        SiteError: too many entries, too many bytes, or too high a compression ratio.
    """
    if len(entries) > limits.files:
        raise SiteError(f"too many entries ({len(entries)} > {limits.files})")
    total = sum(e.size or 0 for e in entries)
    if total > limits.size:
        raise SiteError(f"unpacked size exceeds limit ({total} > {limits.size})")
    if archive_size > 0 and total / archive_size > limits.ratio:
        raise SiteError(f"compression ratio exceeds limit ({limits.ratio})")


# ---------------------------------------------------------------------------------------------
# Extracting (the third part)


def _sevenzip_zip(path: Path, password: str | None) -> bool:
    """Whether a zip goes to 7-Zip: when it is encrypted and 7-Zip is installed.

    Args:
        path: the zip.
        password: its password; a zip with none never needs 7-Zip.

    Returns:
        True to extract it with 7-Zip.

    Python's zipfile reads only the old ZipCrypto, not AES, so an encrypted zip goes to 7-Zip
    when there is one, and to zipfile (which may then refuse an AES entry) when there isn't.
    """
    if password is None:
        return False
    with zipfile.ZipFile(path) as zf:
        if not any(info.flag_bits & ENCRYPTED for info in zf.infolist()):
            return False
    try:
        _tools.sevenzip()
    except _tools.MissingError:
        return False
    return True


def _unpack_zip(path: Path, dest: Path, *, password: str | None) -> None:
    """Extract a zip into `dest` with Python's zipfile, entry by entry.

    Args:
        path: the zip.
        dest: the staging folder.
        password: its password, for ZipCrypto entries; None for none.

    Raises:
        SiteError: a wrong or missing password, an AES entry, or a damaged entry.
    """
    pwd = password.encode("utf-8") if password is not None else None
    try:
        with zipfile.ZipFile(path) as zf:
            for info in zf.infolist():
                target = _safe_dest(dest, info.filename)
                if info.is_dir():
                    target.mkdir(parents=True, exist_ok=True)
                    continue
                target.parent.mkdir(parents=True, exist_ok=True)
                with zf.open(info, pwd=pwd) as src:
                    _save(src, target)
    except RuntimeError as error:
        if "password" in str(error).lower() or "encrypted" in str(error).lower():
            raise SiteError(f"{path.name}: wrong or missing password") from error
        raise SiteError(f"{path.name}: {error}") from error
    except NotImplementedError as error:
        raise SiteError(f"{path.name}: {error}",
                        hint="install 7-Zip (7zz) to extract AES-encrypted zips") from error
    except (zipfile.BadZipFile, *DAMAGED) as error:
        raise SiteError(f"{path.name}: {error}") from error


def _unpack_tar(path: Path, dest: Path) -> None:
    """Extract a tar (plain or compressed) into `dest`, file by file.

    Args:
        path: the tar.
        dest: the staging folder.

    Raises:
        SiteError: the tar is damaged.
    """
    try:
        with tarfile.open(path) as tf:
            for m in tf:
                target = _safe_dest(dest, m.name)
                if m.isdir():
                    target.mkdir(parents=True, exist_ok=True)
                    continue
                target.parent.mkdir(parents=True, exist_ok=True)
                stream = tf.extractfile(m)
                if stream is not None:
                    _save(stream, target)
    except (tarfile.TarError, *DAMAGED) as error:
        raise SiteError(f"{path.name}: {error}") from error


def _save(stream: IO[bytes], target: Path) -> None:
    """Copy `stream` to the new file `target`, piece by piece.

    The stage is this run's own folder, so the file is written in place: a failure removes
    the whole stage.

    Args:
        stream: the entry's bytes.
        target: where they go; it must not exist yet.
    """
    with target.open("xb") as out:
        shutil.copyfileobj(stream, out, CHUNK)


def _ls_7z(path: Path, *, password: str | None) -> list[Entry]:
    """List a 7z, RAR or encrypted zip with `7zz l -slt`, refusing links.

    Args:
        path: the archive.
        password: its password; None lists without one.

    Returns:
        Its entries.

    Raises:
        SiteError: a wrong or missing password, a damaged archive, or a link.
        MissingError: 7-Zip is not installed.
    """
    tool = _tools.sevenzip()
    # `-p` with an empty value means "no password": 7-Zip fails instead of asking for one.
    try:
        res = tool.run("l", "-slt", f"-p{password or ''}", "--", str(path))
    except _tools.RunError as error:
        raise _failed(path, error) from error
    entries: list[Entry] = []
    cur: dict[str, str] = {}
    # `-slt` prints a block of `Key = value` lines per entry, blocks split by a blank line.
    for line in [*res.out.splitlines(), ""]:
        if not line.strip():
            if "Path" in cur:
                entries.append(_7z_entry(cur))
            cur = {}
        elif " = " in line:
            k, _, v = line.partition(" = ")
            cur[k.strip()] = v.strip()
    # The first block describes the archive itself, under its own path.
    return [entry for entry in entries if Path(entry.name) != path]


def _7z_entry(cur: dict[str, str]) -> Entry:
    """One `7zz l -slt` block as an `Entry`, refusing a link.

    Args:
        cur: the block's `Key = value` pairs.

    Returns:
        The entry.

    Raises:
        SiteError: the entry is a symbolic link, a hard link or a reparse point.
    """
    # `Attributes` holds Windows flags, then a Unix mode when there is one: `A_ lrwxrwxrwx`.
    flags, _, mode = cur.get("Attributes", "").partition(" ")
    # `L` is a Windows reparse point (a link or junction), `l` a Unix symbolic link.
    if "L" in flags or mode.startswith("l") or cur.get("Symbolic Link") or cur.get("Hard Link"):
        raise SiteError(f"unsafe archive entry {cur['Path']!r}: a link")
    is_dir = cur.get("Folder") == "+" or flags.startswith("D")
    size_raw = cur.get("Size", "")
    size = int(size_raw) if size_raw.isdigit() else None
    return Entry(cur["Path"], size, "dir" if is_dir else "file")


def _unpack_7z(path: Path, dest: Path, *, password: str | None) -> None:
    """Extract with `7zz x` into `dest` (a 7z, a RAR, or an encrypted zip).

    Args:
        path: the archive.
        dest: the staging folder.
        password: its password; None extracts without one.

    Raises:
        SiteError: a wrong or missing password, or a damaged archive.
        MissingError: 7-Zip is not installed.
    """
    tool = _tools.sevenzip()
    try:
        tool.run("x", "-y", f"-p{password or ''}", f"-o{dest}", "--", str(path))
    except _tools.RunError as error:
        raise _failed(path, error) from error


def _failed(path: Path, error: _tools.RunError) -> SiteError:
    """The `SiteError` for a failed 7-Zip run: a password problem, or a damaged archive."""
    said = f"{error.err}\n{error.out}".lower()
    if "password" in said:
        return SiteError(f"{path.name}: wrong or missing password")
    return SiteError(f"{path.name}: {error}")


# ---------------------------------------------------------------------------------------------
# Verifying and placing (the last two parts)


def _verify(stage: Path, entries: list[Entry], name: str) -> None:
    """Check every file entry is in the stage at its listed size.

    Args:
        stage: the staging folder.
        entries: what the listing said the archive holds.
        name: the archive's name, for the error.

    Raises:
        SiteError: a file is missing or the wrong size: the extract can't be trusted.
    """
    for entry in entries:
        if entry.kind != "file":
            continue
        written = _safe_dest(stage, entry.name)
        try:
            size = written.stat().st_size
        except OSError as error:
            raise SiteError(f"{name}: {entry.name!r} was not extracted") from error
        if entry.size is not None and size != entry.size:
            raise SiteError(f"{name}: {entry.name!r} came out {size} bytes, not {entry.size}")


def _place(stage: Path, dest: Path) -> None:
    """Move the verified stage to `dest`: one rename when `dest` is new, else file by file.

    A file already at `dest` from an earlier extract of the same archive is replaced.

    Args:
        stage: the verified staging folder.
        dest: the folder named after the archive.
    """
    if not dest.exists():
        os.replace(stage, dest)
        return
    for root, _, names in os.walk(stage):
        here = dest / Path(root).relative_to(stage)
        here.mkdir(parents=True, exist_ok=True)
        for name in names:
            os.replace(Path(root, name), here / name)
