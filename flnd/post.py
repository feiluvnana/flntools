"""What happens to a file once it is downloaded: the postprocessors, run by the job in a fixed order.

How it works: the job downloads a file, then calls each postprocessor in `ORDER` with the file's
path and its `Fields`, and each returns the path the file has afterwards (a compressed photo may
be a `.webp` now). A postprocessor records what it did on `fields.row`, the file's history row,
which the job writes only after the last one. The order is fixed and there is no user-built chain:
flnd's options decide what each one does.

1. `compress`: `--image`, `--video` and `--audio`, through `Compressor` (flnd/common/compress.py),
   whose rules are unchanged: replaced only when smaller, videos one at a time across the run,
   and a machine that can't compress a kind stops trying it.
2. `classify`: `--classify`, through the run's `Labeller` (flnd/common/labels.py): the file's labels
   go in its history row, and it is linked into `labels/<label>/` beside its own folder. It never
   renames or moves a file, and reads it after compressing, so it labels what is kept.
3. `mtime`: the file's modified time becomes its post's day at noon UTC, so sorting by date in
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
    from flnd.common.compress import Choice, Compressor
    from flnd.common.labels import Labeller

__all__ = ["ORDER", "Fields", "Postprocessor", "classify", "compress", "mtime"]

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
    # The run's compress choice for `kind`; None leaves the file as it came.
    choice: Choice | None = None
    # The run's one `Compressor`, shared by every source; None compresses nothing.
    compressor: Compressor | None = None
    # The download's `Last-Modified` header, when the server sent one.
    last_modified: str | None = None
    # The file's history row so far: `path`, `size` and `at`; postprocessors add their fields.
    row: dict[str, Any] = field(default_factory=dict)
    # The file's format fields (`id`, `date`, `category`, `num`, … and the post's own).
    values: Mapping[str, Any] = field(default_factory=dict)
    # The run's one `Labeller` when `--classify` is on; None classifies nothing.
    labeller: Labeller | None = None
    # The target's folder, which holds `labels/`; None classifies nothing.
    folder: Path | None = None


# One step: `(path, fields) -> path`, the path the file has once the step is done.
Postprocessor = Callable[[Path, Fields], Path]


def compress(path: Path, fields: Fields) -> Path:
    """Compress the file with the run's choice for its kind, and return where it is now.

        path = compress(Path("posts/a.jpg"), fields)      # posts/a.webp with --image webp,mid

    Args:
        path: the downloaded file; replaced by its compressed copy only when that is smaller.
        fields: its kind, the run's choice and compressor; the history fields `Compressor`
            returns (`path`, `size`, `compressed`, `original_size`) are merged into `row`.

    Returns:
        The file's path, which is the compressed copy's when its format changed.

    It is slow: a photo takes a moment, a video minutes, on the caller's thread. A video waits for
    the run's one encode at a time. A file this machine can't compress is left as it is, with no
    `compressed` field, so a later run, once ffmpeg or the extra is installed, compresses it.
    """
    if fields.choice is None or fields.kind is None or fields.compressor is None:
        return path
    found = fields.compressor.compress(path, fields.kind, fields.choice)
    if found is None:
        return path
    fields.row.update(found)
    return Path(found.get("path", path))


def classify(path: Path, fields: Fields) -> Path:
    """Label a photo or video for `--classify`, record its labels, and link it into `labels/<label>/`.

        classify(Path("posts/a.jpg"), fields)       # row["labels"] = ["food"]; labels/food/a.jpg

    Args:
        path: the file, after compressing; it is never moved or renamed.
        fields: its kind, the run's `labeller` and the target's `folder`; the label names go in
            `row` as `labels`.

    Returns:
        `path`, unchanged.

    The first file of a process loads the model, which may ask to download it, then each photo
    takes about 20 ms on the caller's thread. A file that can't be labelled, or a run whose
    classifying stopped (no extra, no model), keeps no `labels` field, so a later
    `download --classify` labels it.
    """
    if fields.labeller is None or fields.folder is None or fields.kind not in ("image", "video"):
        return path
    names = fields.labeller.labels(path)
    if names is None:
        return path
    fields.row["labels"] = names
    fields.labeller.link(path, fields.folder, names)
    return path


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


# The postprocessors, in the order every file goes through them: `classify` reads what `compress`
# kept, and `mtime` comes last, so a hard link (the same file) gets the post's date too.
ORDER: tuple[Postprocessor, ...] = (compress, classify, mtime)


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
