"""Write a file so that a reader sees either the old file or the whole new one, never half.

How it works: every write goes to a hidden `.<name>.<random>.part` in the destination's own
folder, and `os.replace` then moves it onto the destination in one step, which every system does
atomically within one disk. `writing` hands the part path to the caller's block, for anything that
writes a file by path (ffmpeg, Pillow, an archive library); `write` is the same for bytes already
in memory. The part is deleted on any error, Ctrl-C included, so the only leftovers a killed
process can leave match `.*.part`, which flnd's `sync` repair deletes. It wraps nothing but the
stdlib, so there is no `.raw`. Every write in `shared` goes through it (`cookies`, `archive`,
`media`), and a test checks that no other module opens a file for writing.

    from shared import atomic

    with atomic.writing(Path("out/clip.mp4")) as part:
        run_ffmpeg(output=part)                 # out/clip.mp4 appears only if ffmpeg finished
    atomic.write(Path("cookies.txt"), text.encode("utf-8"), mode=0o600)

Both functions may be called from several threads and processes at once, even for one file:
each call gets its own part, so two writers never write into each other's file, and the last
rename wins, whole.
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
        with atomic.writing(Path("a.jpg"), replace=True) as part:     # over an existing a.jpg
            image.save(part)

    Args:
        dest: the file to write; its folder must exist.
        replace: True writes over an existing `dest`; False (the default) refuses one, so a
            caller that must never lose a file, such as `Media.save` to a new name, says so.

    Returns:
        The part path, `.<dest name>.<8 hex digits>.part` in `dest`'s folder. It doesn't exist
        yet: the block creates it, in whatever way it writes files.

    Raises:
        FileExistsError: `dest` exists and `replace` is False, checked before the block runs.
            Another writer can still create `dest` while the block runs; the rename then
            replaces it, so the check guards against mistakes, not against races.

    A block that leaves no file at the part path (it deleted it, moved it elsewhere, or never
    wrote one) leaves `dest` as it was. The part is deleted however the block ends.
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
        OSError: the folder or the file can't be written. No part is left, and an old `dest` is kept.
    """
    with writing(dest, replace=True) as part:
        # O_EXCL: the part name is new, so an existing file there would be someone else's.
        handle = os.open(part, os.O_WRONLY | os.O_CREAT | os.O_EXCL | BINARY, mode)
        try:
            file = os.fdopen(handle, "wb")
        # Until fdopen owns the handle, it's ours to close: an open handle keeps Windows from deleting the part.
        except BaseException:
            os.close(handle)
            raise
        with file:
            file.write(data)
