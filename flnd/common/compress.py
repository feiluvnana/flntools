"""Shrink saved photos, videos and audio for `--shrink`, never letting media errors end a run.

`--shrink` compresses each saved photo, video and audio file at media's `LEVEL`, the balance of
quality and size, in its own format: a JPEG stays a JPEG, an MP4 stays an MP4 in its own codec, so
every file still opens normally. Videos wait for the run's one-at-a-time lock (`ONE_AT_A_TIME`),
since one video encode already uses every core.

`compress_file` shrinks one file through `shared.media`, saved over original only when smaller.
`Compressor` coordinates one run's shrinking across threads. `apply` shrinks already saved files.

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

from flnd.common import paths
from shared import log, worker

if TYPE_CHECKING:
    from shared import media

# media's level for `--shrink`: the balance of quality and size ("balanced"). A history
# row's `compressed` records it.
LEVEL = "mid"
# The kinds of saved file `--shrink` compresses; other files like archives are kept as sent.
KINDS = (paths.IMAGE, paths.VIDEO, paths.AUDIO)
# The kinds whose encodes wait for the run's lock, one at a time, on a pool of their own.
ONE_AT_A_TIME = frozenset({paths.VIDEO})
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

# Downloads compress photos on several threads at once, so warning tracking is locked.
_warning = threading.Lock()


@dataclass
class Compressor:
    """One run's compressing: encode lock, disabled kinds, and warned errors.

        compressor = Compressor()
        fields = compressor.compress(Path("clip.mp4"), "video")
    """

    # The kinds of error already warned of, so a stalled ffmpeg on every file warns once.
    warned: set[type[Exception]] = field(default_factory=set)
    # Kinds this machine cannot compress (no ffmpeg, encoder or extra); not tried again.
    off: set[str] = field(default_factory=set)
    # One encode of a `one_at_a_time` kind in the whole run, across every source's threads.
    turns: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def compress(self, path: Path, kind: str) -> dict[str, Any] | None:
        """Shrink one saved file, waiting for turn when required, and return history fields.

            fields = compressor.compress(Path("a.jpg"), "image")
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
    """Shrink one saved photo, video or audio file at `LEVEL` in its format, and return fields.

        fields = compress_file(Path("a.jpg"), "image", warned)
    """
    from shared import media

    try:
        size = path.stat().st_size
    except OSError as error:
        _warn(warned, error, path)
        return {"compressed": "none"}
    try:
        # Temporary file drops automatically whenever it is not saved, including on error.
        with media.open(path) as original:
            codec = OWN_CODECS.get(original.info.video or "") if kind == paths.VIDEO else None
            saved: Path | None = None
            if kind == paths.VIDEO and not _can_win(original, codec):
                log.info("{name}: an encode wouldn't save enough; kept as it is", name=path.name)
            else:
                out = original.compress(LEVEL, codec=codec)
                if out.size < original.size:
                    saved = out.save()
                elif out is not original:
                    log.info("{name}: compressing saved nothing; kept the original", name=path.name)
    except (ValueError, OSError) as error:
        # File only: unreadable, refused, file exists, timeout, missing, permission, or disk full.
        # Fails this file and the run proceeds; transform writes temporary file so original is safe.
        _warn(warned, error, path)
        saved = None
    except (RuntimeError, ImportError) as error:
        # This machine: no ffmpeg, an ffmpeg too old or without the encoder, or no Pillow. Every
        # other file of the kind would fail the same way, and installing it fixes them all.
        _warn(warned, error, path)
        return None
    if saved is None:
        return {"path": str(path), "size": size, "compressed": "none", "original_size": size}
    return {
        "path": str(saved), "size": saved.stat().st_size, "compressed": LEVEL, "original_size": size
    }


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


def apply(
    history: MutableMapping[str, Any],
    rows: Iterable[tuple[str, dict]],
    compressor: Compressor,
    *,
    label: str,
) -> int:
    """Shrink a target's saved photos, videos and audio when `--shrink` turns it on.

        apply(history, rows, Compressor(), label="ins:example_user")
    """
    todo = [(key, row) for key, row in rows if paths.kind_of(row["path"]) in KINDS
            and row.get("compressed") != LEVEL and Path(row["path"]).is_file()]
    if not todo:
        return 0
    # Videos last: they take turns on the run's lock, so a pool thread holding one only waits.
    todo.sort(key=lambda item: paths.kind_of(item[1]["path"]) in ONE_AT_A_TIME)
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
    kind = paths.kind_of(row["path"])
    fields = compressor.compress(Path(row["path"]), kind) if kind is not None else None
    if fields is None:
        return
    path = Path(fields.get("path", row["path"]))
    # Written here, not by caller: after Ctrl-C pool finishes files, and a file shrunk without
    # its row would be shrunk again, losing quality. Stamped so `sync` trusts smaller file.
    history[key] = {**row, **fields, **(paths.stamp(path) if path.is_file() else {})}


def _warn(warned: set[type[Exception]], error: Exception, path: Path) -> None:
    """Warn of `error` once per run, and log it at debug level after that."""
    with _warning:
        first = type(error) not in warned
        warned.add(type(error))
    log_fn = log.warn if first else log.debug
    log_fn("{name}: not compressed: {error}", name=path.name, error=error)
