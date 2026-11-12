"""Write a file so that a reader sees either the old file or the whole new one, never half.

Every write goes to a hidden `.<name>.<random>.part` in the destination's own folder, and
`os.replace` then moves it onto the destination in one step. The part file is deleted on any
error, Ctrl-C included, so the only leftovers a crash can leave are named `.part`. Private:
`cookies`, `archive` and `media` use it, and `shared/__init__.py` doesn't export it.

    with _atomic.writing(Path("out/clip.mp4")) as part:
        run_ffmpeg(output=part)                 # dest appears only if ffmpeg finished
    _atomic.write(Path("cookies.txt"), text.encode(), mode=0o600)
"""

from __future__ import annotations

import os
import secrets
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

# Windows opens files in text mode unless told otherwise, which would turn "\n" into "\r\n";
# other systems have no such flag and no such translation.
BINARY = getattr(os, "O_BINARY", 0)


@contextmanager
def writing(dest: Path, *, replace: bool = False) -> Iterator[Path]:
    """Yield a fresh part path next to `dest`, and move it onto `dest` when the block ends cleanly.

        with writing(Path("a.jpg")) as part:
            image.save(part)

    Args:
        dest: the file to write; its folder must exist.
        replace: True overwrites an existing `dest`; False (the default) refuses one.

    Raises:
        FileExistsError: `dest` exists and `replace` is False, checked before the block runs.

    A block that leaves no file at the part path (it deleted it, or never wrote one) leaves
    `dest` as it was. The part is deleted however the block ends.
    """
    if dest.exists() and not replace:
        raise FileExistsError(f"{dest} already exists")
    # The random part keeps two writers of the same file, in two threads or processes, apart.
    part = dest.with_name(f".{dest.name}.{secrets.token_hex(4)}.part")
    try:
        yield part
        if part.exists():
            os.replace(part, dest)
    finally:
        part.unlink(missing_ok=True)


def write(dest: Path, data: bytes, *, mode: int = 0o666) -> None:
    """Replace `dest` with `data` in one step, creating it with permission bits `mode`.

        write(Path(".flnd/cookies/ins.txt"), text.encode("utf-8"), mode=0o600)

    Args:
        dest: the file to write; its folder must exist. An existing file is replaced.
        data: the whole new content, written byte for byte (no newline translation on Windows).
        mode: the new file's permission bits, less the umask. The part file is created with
            them, so the content is never readable with wider ones, even for a moment.
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
