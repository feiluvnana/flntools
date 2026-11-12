"""flnd's files on disk: one way to write a file atomically, and one way to check a photo or video.

Every temporary file flnd writes is named `.<name>.<random>.part` (`part_for`) in the folder of
the file it becomes, and is renamed onto that file only once complete. So a reader never sees half
a file, and whatever a killed run leaves behind matches `PART_GLOB`, which `flnd sync`'s repair
removes. `net` streams downloads into such parts; `write_atomic` writes small files whole.

`checked` is the one media validation, used by `net` for each download and by `sync` for each
file it repairs, so both sniff the same way and treat the same errors as "not checkable here".

    files.write_atomic(folder / "info.md", "# Title\\n")
    extension = files.checked(folder / "a.jpg", "photo")    # ".webp" when it holds a WebP
"""

from __future__ import annotations

import os
import secrets
from pathlib import Path
from typing import BinaryIO

from shared import log

# What every temporary file flnd writes matches, and what `flnd sync`'s repair deletes.
PART_GLOB = ".*.part"
# What `media.sniff` names a server's web page or JSON answer: a login wall or an error page.
PAGES = (".html", ".json")


class NotMediaError(ValueError):
    """A web page or JSON where a photo or video was expected: a login wall or an error page."""


def part_for(dest: Path) -> Path:
    """A new temporary path next to `dest`, named `.<name>.<random>.part`.

        part = files.part_for(Path("posts/a.jpg"))    # posts/.a.jpg.3f9c01ab.part

    Args:
        dest: the file the part becomes once complete.

    Returns:
        A path in `dest`'s folder that matches `PART_GLOB`. Its random part (8 hex digits) keeps two
        runs, or two threads, from ever writing into each other's file; the file isn't created.
    """
    return dest.with_name(f".{dest.name}.{secrets.token_hex(4)}.part")


def open_new(path: Path) -> BinaryIO:
    """Create `path`, which must not exist, and return it open for writing bytes.

        with files.open_new(part) as file:
            file.write(chunk)

    Args:
        path: the file to create, usually from `part_for`.

    Returns:
        A binary file object, mode 0644 before the umask, so the saved file is readable like any download.

    Raises:
        FileExistsError: `path` already exists (`O_EXCL`), so a part is always new.
    """
    # Windows' C runtime opens a file in text mode unless O_BINARY is given, and `fdopen(…, "wb")`
    # then writes every \n byte as \r\n, corrupting every download (fixes #1). Other systems have
    # no O_BINARY and no text mode, hence the 0.
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0)
    return os.fdopen(os.open(path, flags, 0o644), "wb")


def write_atomic(dest: Path, data: bytes | str) -> Path:
    """Write `data` to `dest` through a `.part` file, so `dest` is either the old file or the whole new one.

        files.write_atomic(folder / "info.md", "# Title\\n")

    Args:
        dest: the file to write; its folder is created when missing, and a file already there is replaced.
        data: the bytes to write, or text, written as UTF-8 with its line endings as given on every platform.

    Returns:
        `dest`.

    Raises:
        OSError: the folder or the file can't be written. No part is left behind, and an old `dest` is kept.
    """
    dest.parent.mkdir(parents=True, exist_ok=True)
    part = part_for(dest)
    try:
        with open_new(part) as file:
            file.write(data.encode("utf-8") if isinstance(data, str) else data)
        os.replace(part, dest)
    finally:
        # After the rename the part is gone; on any error, Ctrl-C included, it is removed here.
        part.unlink(missing_ok=True)
    return dest


def checked(path: Path, kind: str, *, decode: bool = True) -> str | None:
    """The extension a photo or video's content calls for, once it is known not to be a web page or broken.

        extension = files.checked(Path("posts/a.jpg"), "photo")    # ".webp" when it holds a WebP

    Args:
        path: the file to check; it is never changed.
        kind: what the file should be, "photo" or "video", named in the error for a web page.
        decode: whether to check the whole file with `media.check` (a photo decoded, a video's packets
            read); False only sniffs its first bytes, for a download whose size was already verified.

    Returns:
        The extension `media.sniff` reads from the first bytes (".jpg", ".mp4"), or None when it knows
        none. A file that can't be checked here counts as fine: see below.

    Raises:
        NotMediaError: the file is a web page or JSON, such as a login wall.
        media.BrokenError: the file is truncated or corrupt.
        media.UnsafeError: media refuses to open it, such as a decompression bomb.

    Without ffmpeg (`RuntimeError`), without the media extra (`ImportError`), or when ffmpeg stalls
    (`TimeoutError`), the file isn't known to be broken, so it is logged at debug level and kept.
    """
    from shared import media

    extension = media.sniff(path)
    if extension in PAGES:
        raise NotMediaError(f"got a web page instead of a {kind}")
    if decode:
        try:
            media.check(path)
        except (RuntimeError, ImportError, TimeoutError) as error:
            # These say the check can't run here, not that the file is bad; BrokenError and UnsafeError
            # are about the file itself, so they reach the caller, which decides whether to retry or delete.
            log.debug("{name}: not checked: {error}", name=path.name, error=error)
    return extension
