"""Media: one ffmpeg join of a video and its audio, or one video made of timed frames.

SPEC.md#for-developers says where it sits in a transfer.
"""

from __future__ import annotations

import asyncio
import logging
import re
from collections.abc import Sequence
from pathlib import Path

from flnkit import files
from flnkit.errors import Changed

_log = logging.getLogger(__name__)

__all__ = ["FRAME", "animate", "check_frames", "ffconcat", "join"]

# The only frame names taken: six digits and an image extension, so no path and no option.
FRAME = re.compile(r"\d{6}\.(jpe?g|png|gif)")


async def join(video: files.Part, audio: files.Part, out: files.Part,
               ffmpeg: files.Tool) -> None:
    """Join `video` and `audio` into `out`'s part, copying both streams (no re-encode).

    The container follows `out.dest`'s suffix: `.webm` is WebM, anything else MP4 with
    `+faststart`. When ffmpeg rejects only the audio stream, the video is saved alone.

    Args:
        video: the part holding the video stream, already downloaded.
        audio: the part holding the audio stream, already downloaded.
        out: the part to write the joined file to; its `dest` gives the name and container.
        ffmpeg: the ffmpeg tool, looked up by the caller before any download is kept or dropped.

    Raises:
        RunError: a stream is empty (0 bytes), or ffmpeg failed on the video.
    """
    fmt = "webm" if out.dest.suffix.lower() == ".webm" else "mp4"
    flags = ["-movflags", "+faststart"] if fmt == "mp4" else []
    if video.size == 0:
        raise files.RunError(f"video stream is empty (0 bytes): {video.path}")
    if audio.size == 0:
        raise files.RunError(f"audio stream is empty (0 bytes): {audio.path}")
    both = ["-y", "-i", str(video.path), "-i", str(audio.path)]
    try:
        await asyncio.to_thread(ffmpeg.run, *both, "-c", "copy", *flags, "-f", fmt,
                                str(out.path))
    except files.RunError as error:
        if not _audio_only(str(error), video, audio):
            raise
        _log.debug(
            "audio stream invalid for {dest}; saving video only",
            extra={"fields": {
                "dest": out.dest.name,
            }},
        )
        await asyncio.to_thread(ffmpeg.run, "-y", "-i", str(video.path), "-c", "copy", *flags,
                                "-f", fmt, str(out.path))


def _audio_only(said: str, video: files.Part, audio: files.Part) -> bool:
    """Whether ffmpeg's error `said` blames the audio input (`in#1`) and not the video."""
    if "in#0" in said or str(video.path) in said:
        return False
    return "in#1" in said or str(audio.path) in said or "Invalid data found" in said


def check_frames(names: Sequence[str], delays: Sequence[tuple[str, int]]) -> None:
    """Check that `names` (a zip's entries) are frame names and are exactly `delays`' names.

    Raises:
        Changed: a name isn't `FRAME`, or the two sets of names differ.
    """
    for name in (*names, *(one for one, _ in delays)):
        if not FRAME.fullmatch(name):
            raise Changed(f"the frames zip holds {name!r}, not a numbered frame")
    if len(set(names)) != len(names) or set(names) != {name for name, _ in delays}:
        raise Changed("the frames zip doesn't hold the frames its delay list names")


def ffconcat(delays: Sequence[tuple[str, int]]) -> str:
    """The ffconcat list for `delays`, durations in seconds.

    The last frame is listed once more, without a duration, or ffmpeg drops its duration.

    Raises:
        Changed: a name isn't `FRAME`.
    """
    lines = ["ffconcat version 1.0"]
    for name, milliseconds in delays:
        if not FRAME.fullmatch(name):
            raise Changed(f"the frames zip holds {name!r}, not a numbered frame")
        lines += [f"file {name}", f"duration {milliseconds / 1000:.3f}"]
    lines.append(f"file {delays[-1][0]}")
    return "\n".join(lines) + "\n"


async def animate(frames_dir: Path, delays: Sequence[tuple[str, int]], out: files.Part,
                  ffmpeg: files.Tool) -> None:
    """Make an H.264 MP4 from the frames in `frames_dir`, each shown for its delay.

    Args:
        frames_dir: a folder holding the frames `delays` names.
        delays: (frame file name, milliseconds) in play order.
        out: the part to write the MP4 to.
        ffmpeg: the ffmpeg tool, looked up by the caller.

    Raises:
        Changed: a frame name isn't `FRAME`.
        RunError: ffmpeg failed.
    """
    listing = frames_dir / "frames.ffconcat"
    await asyncio.to_thread(listing.write_text, ffconcat(delays), encoding="utf-8")
    await asyncio.to_thread(
        ffmpeg.run, "-y", "-f", "concat", "-safe", "0", "-i", str(listing),
        "-c:v", "libx264", "-crf", "18", "-pix_fmt", "yuv420p",
        "-vf", "pad=ceil(iw/2)*2:ceil(ih/2)*2", "-movflags", "+faststart", "-f", "mp4",
        str(out.path))
