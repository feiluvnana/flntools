"""What happens to a file once it is downloaded: the postprocessors, run by the job in a fixed order.

How it works: the job downloads a file, then calls each postprocessor in `ORDER` with the file's
path and its `Fields`, and each returns the path the file has afterwards (a compressed photo may
be a `.webp` now). A postprocessor records what it did on `fields.row`, the file's history row,
which the job writes only after the last one. The order is fixed and there is no user-built chain:
flnd's options decide what each one does.

1. `compress`: `--shrink`, through `Compressor` (flnd/common/compress.py),
   whose rules are unchanged: replaced only when smaller, videos one at a time across the run,
   and a machine that can't compress a kind stops trying it.
2. `mtime`: the file's modified time becomes its post's day at noon UTC, so sorting by date in
   any file manager sorts by when it was posted (plan/phase-6/dates.md).

    for step in post.ORDER:
        path = step(path, fields)
    history[key] = fields.row
"""

from __future__ import annotations

import datetime
import email.utils
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable, Mapping

from shared import log

if TYPE_CHECKING:
    from flnd.common.compress import Compressor

__all__ = ["ORDER", "Fields", "Postprocessor", "compress", "mtime"]

# The hour of the day a dated file's modified time is set to, in UTC: noon, so the day reads the
# same in every time zone from UTC-11 to UTC+11.
NOON = 12


@dataclass
class Fields:
    """What a postprocessor knows about one downloaded file, and the history row it adds to.

        fields = Fields(day=date(2026, 1, 2), kind="image", row={"path": "…", "size": 1024})
    """

    # The post's day, from its `date`; None for an undated post.
    day: datetime.date | None = None
    # "image", "video" or "audio" by the saved file's extension; None for any other file.
    kind: str | None = None
    # Whether the run shrinks the file (`--shrink`); False leaves it as it came.
    shrink: bool = False
    # The run's one `Compressor`, shared by every source; None compresses nothing.
    compressor: Compressor | None = None
    # The download's `Last-Modified` header, when the server sent one.
    last_modified: str | None = None
    # The file's history row so far: `path`, `size` and `at`; postprocessors add their fields.
    row: dict[str, Any] = field(default_factory=dict)
    # The file's format fields (`id`, `date`, `category`, `num`, … and the post's own).
    values: Mapping[str, Any] = field(default_factory=dict)


# One step: `(path, fields) -> path`, the path the file has once the step is done.
Postprocessor = Callable[[Path, Fields], Path]


def compress(path: Path, fields: Fields) -> Path:
    """Shrink the file when the run asks for it (`--shrink`), and return where it is now.

        path = compress(Path("posts/a.jpg"), fields)      # posts/a.jpg, smaller, with --shrink

    Args:
        path: the downloaded file; replaced by its compressed copy only when that is smaller.
        fields: its kind, whether the run shrinks, and its compressor; the history fields `Compressor`
            returns (`path`, `size`, `compressed`, `original_size`) are merged into `row`.

    Returns:
        The file's path, which is the compressed copy's when media gave it another extension.

    It is slow: a photo takes a moment, a video minutes, on the caller's thread. A video waits for
    the run's one encode at a time. A file this machine can't compress is left as it is, with no
    `compressed` field, so a later run, once ffmpeg or the extra is installed, compresses it.
    """
    if not fields.shrink or fields.kind is None or fields.compressor is None:
        return path
    found = fields.compressor.compress(path, fields.kind)
    if found is None:
        return path
    fields.row.update(found)
    return Path(found.get("path", path))


def mtime(path: Path, fields: Fields) -> Path:
    """Set the file's modified time to its post's day, else to the server's `Last-Modified`, at noon UTC.

        mtime(Path("posts/2026-01-02_C1abc.jpg"), fields)

    Args:
        path: the file, which isn't moved.
        fields: its post's `day`, or the download's `last_modified` when the post has none.

    Returns:
        `path`, unchanged.

    Without either, the file keeps the time it was downloaded, which is the download day, the
    last fallback dates.md gives. A file that can't be touched (gone, or read-only) is logged at
    debug level and kept: its date is a convenience, never a reason to fail the download.
    """
    day = fields.day or _served_day(fields.last_modified)
    if day is None:
        return path
    noon = datetime.datetime.combine(day, datetime.time(NOON), tzinfo=datetime.timezone.utc).timestamp()
    try:
        os.utime(path, (noon, noon))
    # The file is the run's own and was just written, so only a disk or permission problem lands here.
    except OSError as error:
        log.debug("{name}: modified time not set: {error}", name=path.name, error=error)
    return path


# The postprocessors, in the order every file goes through them: `mtime` comes last, so the file
# `compress` kept gets the post's date.
ORDER: tuple[Postprocessor, ...] = (compress, mtime)


def _served_day(value: str | None) -> datetime.date | None:
    """The UTC day of an HTTP date such as `Last-Modified`, or None when there's none or it doesn't parse."""
    if not value:
        return None
    try:
        when = email.utils.parsedate_to_datetime(value)
    # A server's malformed date says nothing about the file; the download day stands.
    except (TypeError, ValueError):
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=datetime.timezone.utc)
    return when.astimezone(datetime.timezone.utc).date()
