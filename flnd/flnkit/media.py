"""Media: one ffmpeg join of a video stream and its audio stream into one file.

SPEC.md#for-developers says where it sits in a transfer.
"""

from __future__ import annotations

import asyncio
import logging

from flnkit import files

_log = logging.getLogger(__name__)

__all__ = ["join"]


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
