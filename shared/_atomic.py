"""Write a file atomically so a reader sees either the old or whole new file.

Every write goes to a hidden `.<name>.<random>.part` in the destination folder,
and `os.replace` moves it onto the destination in one step. `writing` yields the
part path for external tools (ffmpeg, Pillow, zip); `write` writes bytes from memory.
The part file is deleted on any error, so failed runs leave only `.*.part`.

    from shared import atomic

    with atomic.writing(Path("out/clip.mp4")) as part:
        run_ffmpeg(output=part)
    atomic.write(Path("cookies.txt"), text.encode("utf-8"), mode=0o600)

Calls from multiple threads or processes each get a unique part path, so writers
never interfere; the last rename wins.
"""

from __future__ import annotations

import os
import secrets
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Union

import shared
from shared import _clock as clock

__all__ = ["Error", "write", "writing"]

PathLike = Union[str, "os.PathLike[str]"]

# Windows opens files in text mode unless told otherwise, which would turn "\n" into "\r\n";
# other systems have no such flag and no such translation, hence the 0.
BINARY = getattr(os, "O_BINARY", 0)
# On Windows an antivirus or a thumbnail scanner may hold `dest` open for a moment, and the rename
# fails with PermissionError meanwhile: it is tried again, every 0.05 s, for about a second.
WINDOWS = os.name == "nt"
REPLACE_TRIES, REPLACE_PAUSE = 20, 0.05


class Error(shared.Error):
    """The base of every error `shared.atomic` raises.

    It raises none of its own today: a file that can't be written is the `OSError` the system
    gives, and an existing `dest` a `FileExistsError`, which callers already catch as such.
    """


@contextmanager
def writing(dest: PathLike, *, replace: bool = False) -> Iterator[Path]:
    """Yield a fresh part path next to `dest`, and move it onto `dest` when the block ends cleanly.

        with atomic.writing(Path("a.jpg")) as part:
            image.save(part)
        with atomic.writing(Path("a.jpg"), replace=True) as part:
            image.save(part)

    Args:
        dest: the file to write; its folder must exist.
        replace: True writes over an existing `dest`; False (the default) refuses one.

    Returns:
        The part path, `.<dest name>.<8 hex digits>.part` in `dest`'s folder.

    Raises:
        FileExistsError: `dest` exists and `replace` is False, checked before the block runs.
    """
    dest = Path(dest)
    if dest.exists() and not replace:
        raise FileExistsError(f"{dest} already exists")
    # The random part keeps two writers of the same file, in two threads or processes, apart.
    # The name must keep matching flnd's PART_GLOB (".*.part"), which sync's repair deletes.
    part = dest.with_name(f".{dest.name}.{secrets.token_hex(4)}.part")
    try:
        yield part
        if part.exists():
            _replace(part, dest)
    finally:
        part.unlink(missing_ok=True)


def _replace(part: Path, dest: Path) -> None:
    """`os.replace(part, dest)`, tried again for about a second while Windows says it's in use."""
    for attempt in range(1, REPLACE_TRIES + 1):
        try:
            os.replace(part, dest)
            return
        except PermissionError:
            if not WINDOWS or attempt == REPLACE_TRIES:
                raise
            clock.sleep(REPLACE_PAUSE)


def write(dest: PathLike, data: bytes, *, mode: int = 0o666) -> None:
    """Replace `dest` with `data` in one step, creating it with permission bits `mode`.

        atomic.write(Path(".flnd/cookies/ins.txt"), text.encode("utf-8"), mode=0o600)

    Args:
        dest: the file to write; its folder must exist. An existing file is replaced.
        data: the whole new content, written byte for byte (no newline translation on Windows).
        mode: the new file's permission bits, less the umask; 0o666 gives an ordinary file. The
            part file is created with them, so the content is never readable with wider ones,
            even for a moment.

    Raises:
        OSError: the folder or file can't be written. No part is left, and an old `dest` is kept.
    """
    with writing(dest, replace=True) as part:
        # O_EXCL: the part name is new, so an existing file there would be someone else's.
        handle = os.open(part, os.O_WRONLY | os.O_CREAT | os.O_EXCL | BINARY, mode)
        try:
            file = os.fdopen(handle, "wb")
        # Until fdopen owns the handle, close it: open handle prevents Windows from deleting part.
        except BaseException:
            os.close(handle)
            raise
        with file:
            file.write(data)
