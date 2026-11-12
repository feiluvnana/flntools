"""Compress one saved photo or video for `--compress-*` and `flnd sync`, never letting media's errors end a run.

`compress_file` picks the preset and format from the run's `Options`, calls `shared.media.compress`,
and turns its result into the file's history fields. It catches exactly what `media` raises for
a file it can't compress (`ValueError`, `FileExistsError`, `RuntimeError`, `TimeoutError`,
`ImportError`), warns once per kind of error per run, and leaves the original in place. What to
do next (stop compressing videos, say) is left to the caller: `session` for new downloads, `sync`
for files saved earlier.

    warned = set()
    fields = compress_file(Path("clip.mp4"), VIDEO, Options(compress_video="small"), warned)
    if fields is not None:
        history[key] = {**row, **fields}
"""

from __future__ import annotations

import threading
from pathlib import Path
from typing import Any

from flnd.session import CODECS_IN, PHOTO, Options, video_format
from shared import log

# Downloads compress photos on several threads at once, so "already warned?" and "now warned" are one step.
_warning = threading.Lock()


def compress_file(path: Path, kind: str, options: Options, warned: set[type[Exception]]) -> dict[str, Any] | None:
    """Compress one saved photo or video with the options' preset and format, and return its history fields.

        fields = compress_file(Path("a.jpg"), PHOTO, Options(compress_image="small"), warned)

    Args:
        path: the saved file; replaced by its compressed copy only when that is smaller.
        kind: `session.PHOTO` or `session.VIDEO`, which picks `compress_image` or `compress_video`.
        options: the run's options, for the preset and `format_image` or `format_video`.
        warned: the kinds of error already warned of in this run, one set for the whole run; the
            first error of each kind is a warning, the ones after it only a debug line.

    Returns:
        The history fields `path`, `size`, `compressed` (the preset, or "none") and `original_size`.
        "none" means this file can't be made smaller here: it didn't shrink, can't be read or
        written (an `OSError`, such as a `PermissionError` or a full disk), or ffmpeg stalled on it
        (`TimeoutError`), so it isn't tried again. None means this machine
        can't compress it at all (no ffmpeg or encoder, `RuntimeError`; a missing extra,
        `ImportError`): nothing is recorded, so a later run, once that is installed, compresses it.
        A file that is gone before compression starts gives only `{"compressed": "none"}`, since
        it has no size to record; the caller's row keeps its own `path` and `size`.

    Raises:
        Nothing of media's; anything else, such as Ctrl-C or a mistake in flnd, isn't caught.

    It is slow, and a video encode uses every core: callers run one video at a time.
    """
    from shared import media

    if kind == PHOTO:
        preset, (format, codec) = options.compress_image, (options.format_image, None)
    else:
        preset, (format, codec) = options.compress_video, video_format(options.format_video)
        if codec and not format and codec not in CODECS_IN.get(path.suffix.lower()[1:], ("h264", "h265")):
            # A codec alone keeps the file's container when it can hold it.
            format = "webm" if codec == "vp9" else "mp4"
    try:
        size = path.stat().st_size
    except OSError as error:
        # This file only, like the errors below: it vanished (or can't be read) between its save
        # or the sync's listing and now. With no size there are no size fields to record, so only
        # "compressed" is returned: every caller merges the fields onto the file's history row,
        # which keeps its own path and size, and a sync's tally skips a row without
        # "original_size". Not None, which would mean this machine can't compress and stop videos.
        _warn(warned, error, path)
        return {"compressed": "none"}
    try:
        out = media.compress(path, preset=preset or "balanced", format=format, codec=codec)
    except (ValueError, OSError) as error:
        # This file only: unreadable or refused (ValueError, BrokenError and UnsafeError among them),
        # or an OSError: its new name taken (FileExistsError), ffmpeg stalled or took too long on it
        # (TimeoutError), or the file vanished, can't be opened (PermissionError) or the disk is full.
        # Each is about this file or this moment, so it fails this file and the run goes on.
        # `media.compress` writes to a temporary part first, so the original is untouched.
        _warn(warned, error, path)
        out = None
    except (RuntimeError, ImportError) as error:
        # This machine: no ffmpeg, an ffmpeg too old or without the encoder, or no Pillow. Every
        # other file of the kind would fail the same way, and installing it fixes them all.
        _warn(warned, error, path)
        return None
    if out is None:
        return {"path": str(path), "size": size, "compressed": "none", "original_size": size}
    return {"path": str(out), "size": out.stat().st_size, "compressed": preset, "original_size": size}


def _warn(warned: set[type[Exception]], error: Exception, path: Path) -> None:
    """Warn of `error` the first time its kind is seen in this run, and log it at debug after that."""
    with _warning:
        first = type(error) not in warned
        warned.add(type(error))
    (log.warn if first else log.debug)("{name}: not compressed: {error}", name=path.name, error=error)
