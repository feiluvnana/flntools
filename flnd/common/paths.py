"""flnd's files on disk: layout of `.flnd/`, target directories, and media validation.

`.flnd/` holds three folders: `media/` with every target's files, `metadata/` with stores
and secrets, and `log/` with one log per run. Every module takes its path from the table
below, joined onto flnd's home folder, and never hardcodes paths.

`net` streams downloads into temporary parts matching `PART_GLOB`, which `shared.fs.sweep`
removes; `shared.fs.write` writes small files atomically.

`checked` provides media validation used by `net` and the job.

    extension = paths.checked(folder / "a.jpg", "image")
    paths.target_folder(Path(".flnd"), target)
    Path(".flnd") / paths.HISTORY
"""

from __future__ import annotations

import mimetypes
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import TYPE_CHECKING, Any

from shared import store, text

if TYPE_CHECKING:
    from flnd.source import Target

# `.flnd/`'s three folders: the media, the stores, and a log of each run.
MEDIA, METADATA, LOGS = Path("media"), Path("metadata"), Path("log")
# Each store in `metadata/`: saved files by key, target settings, complete listings,
# source state (table per source), and the vault with the key beside the store it opens.
HISTORY = METADATA / "history.db"
TARGETS = METADATA / "targets.db"
SOURCE_STATE = METADATA / "sources.db"
SECRETS = METADATA / "secrets.db"
KEY = METADATA / "secret.key"

# Temporary files match (`shared.fs`'s part names) and `shared.fs.sweep` removes them.
PART_GLOB = ".*.part"
# What `sniff` names a server's web page or JSON answer: a login wall or an error page.
PAGES = (".html", ".json")
# The kinds of saved file; each gets its extension from its content.
IMAGE, VIDEO, AUDIO = "image", "video", "audio"
# The kind `net.download` takes for a file whose content decides what it is: a post's file named
# without an extension, because neither its name nor its URL said.
SNIFF = "sniff"
# How an error names each kind.
NOUNS = {IMAGE: "a photo", VIDEO: "a video", AUDIO: "an audio file"}
# The history fields of a finished file's size and modified time (ns), which `flnd sync` trusts
# while the file still has them.
SIZE, MTIME = "size", "mtime"


@contextmanager
def table(path: Path, name: str) -> Iterator[store.Table[Any]]:
    """One table of the store at `path`, open for the block."""
    with store.open(path) as db:
        yield db[name]


def stamp(path: Path) -> dict[str, int]:
    """A finished file's size and modified time (ns), as its history row keeps them."""
    found = path.stat()
    return {SIZE: found.st_size, MTIME: found.st_mtime_ns}


class NotMediaError(ValueError):
    """A web page or JSON where media was expected (e.g. login wall or error page)."""


# Common media extensions mapped to `IMAGE`, `VIDEO`, or `AUDIO` without probing disk.
MEDIA_EXTENSIONS = {
    ".jpg": IMAGE, ".jpeg": IMAGE, ".png": IMAGE, ".gif": IMAGE, ".webp": IMAGE,
    ".heic": IMAGE, ".heif": IMAGE, ".avif": IMAGE, ".bmp": IMAGE, ".tif": IMAGE, ".tiff": IMAGE,
    ".mp4": VIDEO, ".m4v": VIDEO, ".mov": VIDEO, ".webm": VIDEO, ".mkv": VIDEO, ".avi": VIDEO,
    ".mp3": AUDIO, ".m4a": AUDIO, ".aac": AUDIO, ".ogg": AUDIO, ".opus": AUDIO,
    ".wav": AUDIO, ".flac": AUDIO,
}


def kind_of(name: str) -> str | None:
    """What a saved file is by its name: `IMAGE`, `VIDEO` or `AUDIO`, or None."""
    suffix = Path(name).suffix.lower()
    if suffix in MEDIA_EXTENSIONS:
        return MEDIA_EXTENSIONS[suffix]
    mime, _ = mimetypes.guess_type(f"x{suffix}")
    if mime:
        head = mime.split("/", 1)[0]
        if head in (IMAGE, VIDEO, AUDIO):
            return head
    return None


def target_folder(home: Path, target: Target) -> Path:
    """A target's folder: `<home>/media/<code>/<value>`, with value made safe."""
    return home / MEDIA / target.source / text.filename(target.value)


def sniff(path: Path) -> str | None:
    """Sniff a common media, archive, or web-page extension from `path`'s leading bytes."""
    try:
        with path.open("rb") as handle:
            head = handle.read(64)
    except OSError:
        return None
    if head.startswith(b"\xff\xd8\xff"):
        return ".jpg"
    if head.startswith(b"\x89PNG\r\n\x1a\n"):
        return ".png"
    if head.startswith((b"GIF87a", b"GIF89a")):
        return ".gif"
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return ".webp"
    if head[4:8] == b"ftyp":
        brand = head[8:12]
        if brand in (b"avif", b"avis"):
            return ".avif"
        if brand in (b"heic", b"heix", b"hevc", b"mif1"):
            return ".heic"
        if brand == b"M4A ":
            return ".m4a"
        return ".mp4"
    if head.startswith(b"\x1aE\xdf\xa3"):
        return ".webm"
    if head.startswith((b"ID3", b"\xff\xfb", b"\xff\xf3", b"\xff\xf2")):
        return ".mp3"
    if head.startswith(b"OggS"):
        return ".ogg"
    if head.startswith(b"fLaC"):
        return ".flac"
    if head.startswith((b"PK\x03\x04", b"PK\x05\x06")):
        return ".zip"
    if head.startswith(b"7z\xbc\xaf\x27\x1c"):
        return ".7z"
    if head.startswith(b"Rar!\x1a\x07"):
        return ".rar"
    stripped = head.lstrip()
    if stripped.startswith((b"<", b"<!DOCTYPE", b"<!doctype")):
        return ".html"
    if stripped.startswith((b"{", b"[")):
        return ".json"
    return None


def checked(path: Path, kind: str, *, decode: bool = True) -> str | None:
    """The extension a media file calls for, once validated not a web page."""
    del decode
    extension = sniff(path)
    if extension in PAGES:
        raise NotMediaError(f"got a web page instead of {NOUNS.get(kind, kind)}")
    return extension
