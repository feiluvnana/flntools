"""Shrink saved photos, videos and audio for `--shrink`, never letting media's errors end a run.

`--shrink` compresses each saved photo, video and audio file at media's `LEVEL`, the balance of
quality and size, in its own format: a JPEG stays a JPEG, an MP4 stays an MP4 in its own codec, so
every file still opens where it opened before (plan/phase-7/shrink.md). There is no format or level
to choose. Videos wait for the run's one-at-a-time lock (`ONE_AT_A_TIME`), since one x264 encode
already uses every core.

`compress_file` shrinks one file through `shared.media` (`media.open(path).compress(LEVEL)`, saved
over the original only when smaller), and turns the result into the file's history fields. It
catches exactly what `media` raises for a file it can't compress (`ValueError`, `FileExistsError`,
`RuntimeError`, `TimeoutError`, `ImportError`), warns once per kind of error per run, and leaves
the original in place. `Compressor` is one run's shrinking on top of it: the encode lock, and the
kinds this machine turned out unable to compress. `apply` shrinks a target's files already saved,
when `flnd download --shrink` turns it on, `AT_ONCE` photos at a time. The job shrinks each new
download.

    fields = Compressor().compress(Path("a.jpg"), "image")
    if fields is not None:
        history[key] = {**row, **fields}
"""

from __future__ import annotations

import threading
from contextlib import nullcontext
from dataclasses import dataclass, field
from functools import partial
from pathlib import Path
from typing import TYPE_CHECKING, Any, Iterable, MutableMapping

from flnd.common import files
from shared import log, worker

if TYPE_CHECKING:
    from shared import media

# media's level for `--shrink`: the balance of quality and size (Phase 5's "balanced"). A history
# row's `compressed` records it.
LEVEL = "mid"
# The kinds of saved file `--shrink` compresses; any other file, such as an archive, is kept as sent.
KINDS = (files.IMAGE, files.VIDEO, files.AUDIO)
# The kinds whose encodes wait for the run's lock, one at a time, on a pool of their own.
ONE_AT_A_TIME = frozenset({files.VIDEO})
# Saved photos and audio `apply` shrinks at once: Pillow releases the GIL while it encodes, and
# ffmpeg runs in a process of its own.
AT_ONCE = 8
# What `LEVEL` gives a video, in bits per pixel per frame (x264 at CRF 23, medium, on phone and
# CDN clips); one already at or below it can't come out smaller, so it isn't encoded.
BITS_PER_PIXEL = 0.1
# `LEVEL`'s caps in `shared.media`: the shorter side in pixels, and the frame rate.
LEVEL_SHORT_SIDE, LEVEL_FPS = 1080, 60
# Seconds of a video encoded first; the whole file is encoded only when the slice came out at
# least `SLICE_GAIN` smaller. A video under two slices long is encoded whole at once.
SLICE = 4.0
SLICE_GAIN = 0.10
# A video's codec as ffmpeg names it, by the name `media.compress` takes: passed so an MP4 keeps its
# own codec, since without one media writes the container's default (H.264 in an MP4).
OWN_CODECS = {"h264": "h264", "hevc": "h265", "vp9": "vp9"}

# Downloads compress photos on several threads at once, so "already warned?" and "now warned" are one step.
_warning = threading.Lock()


@dataclass
class Compressor:
    """One run's compressing: the encode lock, the kinds this machine can't compress, and the errors warned of.

        compressor = Compressor()
        fields = compressor.compress(Path("clip.mp4"), "video")

    Each kind is on its own (fixes #8): a photo this machine can't compress never stops videos.
    """

    # The kinds of error already warned of, so a stalled ffmpeg on every file warns once.
    warned: set[type[Exception]] = field(default_factory=set)
    # The kinds this machine turned out unable to compress (no ffmpeg, encoder or extra); not tried again.
    off: set[str] = field(default_factory=set)
    # One encode of a `one_at_a_time` kind in the whole run, across every source's threads.
    turns: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def compress(self, path: Path, kind: str) -> dict[str, Any] | None:
        """Shrink one saved file, waiting for its turn when its kind takes turns, and return its history fields.

            fields = compressor.compress(Path("a.jpg"), "image")

        Args:
            path: the saved file; replaced by its compressed copy only when that is smaller.
            kind: one of `KINDS`.

        Returns:
            What `compress_file` returns; None also once the kind is off for this run, and then the
            first file that turned it off has warned "<kind> compression stops for this run".

        It is slow: a photo takes a moment, a video minutes, on the caller's thread.
        """
        with self.turns if kind in ONE_AT_A_TIME else nullcontext():
            if kind in self.off:
                return None
            fields = compress_file(path, kind, self.warned)
            if fields is None:
                # No ffmpeg, encoder or extra: every other file of the kind would fail the same way.
                self.off.add(kind)
                log.warn("{kind} compression stops for this run", kind=kind)
            return fields


def compress_file(path: Path, kind: str, warned: set[type[Exception]]) -> dict[str, Any] | None:
    """Shrink one saved photo, video or audio file at `LEVEL` in its own format, and return its history fields.

        fields = compress_file(Path("a.jpg"), "image", warned)

    Args:
        path: the saved file; replaced by its compressed copy only when that is smaller.
        kind: one of `KINDS`; a video keeps its container and, when media can write it, its codec.
        warned: the kinds of error already warned of in this run, one set for the whole run; the
            first error of each kind is a warning, the ones after it only a debug line.

    Returns:
        The history fields `path`, `size`, `compressed` (`LEVEL`, or "none") and `original_size`.
        "none" means this file can't be made smaller here: it didn't shrink, can't be read or
        written (an `OSError`, such as a `PermissionError` or a full disk), or ffmpeg stalled on it
        (`TimeoutError`). None means this machine can't compress it at all (no ffmpeg or encoder, `RuntimeError`; a missing
        extra, `ImportError`): nothing is recorded, so a later run, once that is installed,
        compresses it. A file that is gone before compression starts gives only
        `{"compressed": "none"}`, since it has no size to record; the caller's row keeps its own
        `path` and `size`.

    Raises:
        Nothing of media's; anything else, such as Ctrl-C or a mistake in flnd, isn't caught.

    It is slow, and a video encode uses every core: callers run one video at a time (`Compressor`).
    """
    from shared import media

    try:
        size = path.stat().st_size
    except OSError as error:
        # This file only, like the errors below: it vanished (or can't be read) between its save
        # or the listing and now. With no size there are no size fields to record, so only
        # "compressed" is returned: every caller merges the fields onto the file's history row,
        # which keeps its own path and size. Not None, which would mean this machine can't compress.
        _warn(warned, error, path)
        return {"compressed": "none"}
    try:
        # `with` drops the compressed copy's temporary file whenever it isn't saved, an error included.
        with media.open(path) as original:
            codec = OWN_CODECS.get(original.info.video or "") if kind == files.VIDEO else None
            saved: Path | None = None
            if kind == files.VIDEO and not _can_win(original, codec):
                log.info("{name}: an encode wouldn't save enough; kept as it is", name=path.name)
            else:
                out = original.compress(LEVEL, codec=codec)
                if out.size < original.size:
                    saved = out.save()
                elif out is not original:
                    log.info("{name}: compressing saved nothing; kept the original", name=path.name)
    except (ValueError, OSError) as error:
        # This file only: unreadable or refused (ValueError; BrokenError, UnsafeError and KindError among them),
        # or an OSError: its new name taken (FileExistsError), ffmpeg stalled or took too long on it
        # (TimeoutError), or the file vanished, can't be opened (PermissionError) or the disk is full.
        # Each is about this file or this moment, so it fails this file and the run goes on.
        # A transform writes to a temporary file and only `save` replaces the original, so it is untouched.
        _warn(warned, error, path)
        saved = None
    except (RuntimeError, ImportError) as error:
        # This machine: no ffmpeg, an ffmpeg too old or without the encoder, or no Pillow. Every
        # other file of the kind would fail the same way, and installing it fixes them all.
        _warn(warned, error, path)
        return None
    if saved is None:
        return {"path": str(path), "size": size, "compressed": "none", "original_size": size}
    return {"path": str(saved), "size": saved.stat().st_size, "compressed": LEVEL, "original_size": size}


def _can_win(original: media.Media, codec: str | None) -> bool:
    """Whether a video encoded at `LEVEL` can come out much smaller, by its bitrate and a slice."""
    info = original.info
    target = _target_bitrate(info)
    if info.bitrate is None or target is None:
        return True
    if info.bitrate <= target:
        return False
    if info.duration is None or info.duration <= 2 * SLICE:
        return True
    # From the middle, past any intro; cut at keyframes, so the slice is copied, not encoded.
    start = info.duration / 2 - SLICE / 2
    piece = original.trim(start=start, end=start + SLICE, exact=False)
    return piece.compress(LEVEL, codec=codec).size <= piece.size * (1 - SLICE_GAIN)


def _target_bitrate(info: media.Info) -> float | None:
    """The bitrate `LEVEL` gives a video of this size and frame rate, after its caps, if known."""
    if not (info.width and info.height and info.fps):
        return None
    short, long = sorted((info.width, info.height))
    scale = min(1.0, LEVEL_SHORT_SIDE / short)
    return BITS_PER_PIXEL * (short * scale) * (long * scale) * min(info.fps, LEVEL_FPS)


def apply(history: MutableMapping[str, Any], rows: Iterable[tuple[str, dict]], compressor: Compressor, *,
          label: str) -> int:
    """Shrink a target's saved photos, videos and audio when `flnd download --shrink` turns it on, recording each.

        apply(history, rows, Compressor(), label="ins:example_user")

    Args:
        history: the download history, written with each file's new fields.
        rows: the target's (key, row) history rows, as `repair.index` gives them.
        compressor: the shrinking for this command, shared by every target it changes.
        label: the target, for the progress row.

    Returns:
        How many files were shrunk or tried; a file already shrunk (`compressed` is `LEVEL`), of
        another kind, or gone from the disk, isn't.

    Raises:
        OSError: the history can't be written.

    A file is replaced only when the result is smaller. It is slow and can't be undone.
    """
    todo = [(key, row) for key, row in rows if files.kind_of(row["path"]) in KINDS
            and row.get("compressed") != LEVEL and Path(row["path"]).is_file()]
    if not todo:
        return 0
    # Videos last: they take turns on the run's lock, so a pool thread holding one only waits.
    todo.sort(key=lambda item: files.kind_of(item[1]["path"]) in ONE_AT_A_TIME)
    with log.progress(f"--shrink {label}", total=len(todo)) as bar:
        for result in worker.as_completed(partial(_shrink, history, compressor), todo,
                                          concurrency=AT_ONCE):
            bar.advance()
            if result.error is not None:
                raise result.error
    return len(todo)


def _shrink(history: MutableMapping[str, Any], compressor: Compressor,
            item: tuple[str, dict]) -> None:
    """Shrink one saved file and write its history row, on a pool thread."""
    key, row = item
    kind = files.kind_of(row["path"])
    fields = compressor.compress(Path(row["path"]), kind) if kind is not None else None
    if fields is None:
        return
    path = Path(fields.get("path", row["path"]))
    # Written here, not by the caller: after a Ctrl-C the pool lets running files finish, and a
    # file shrunk without its row would be shrunk a second time, losing quality again. Stamped
    # as it now is, so `sync` trusts the smaller file.
    history[key] = {**row, **fields, **(files.stamp(path) if path.is_file() else {})}


def _warn(warned: set[type[Exception]], error: Exception, path: Path) -> None:
    """Warn of `error` the first time its kind is seen in this run, and log it at debug after that."""
    with _warning:
        first = type(error) not in warned
        warned.add(type(error))
    (log.warn if first else log.debug)("{name}: not compressed: {error}", name=path.name, error=error)