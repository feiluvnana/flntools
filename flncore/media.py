"""Media helpers: merge video and audio streams through ffmpeg."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from flncore.errors import ToolError

__all__ = ["merge"]


async def merge(
    video_path: Path,
    audio_path: Path | None,
    dest: Path,
    *,
    fmt: str = "mp4",
    flags: tuple[str, ...] = ("-movflags", "+faststart"),
    ffmpeg: Any = None,
) -> None:
    """Merge video and optional audio streams into `dest` using ffmpeg."""
    if ffmpeg is None:
        from flncore.files import Tool, which

        ff = which("ffmpeg")
        if not ff:
            raise ToolError(
                "ffmpeg is not installed on PATH",
                hint="install ffmpeg to merge DASH streams",
            )
        ffmpeg = Tool(Path(ff), "ffmpeg")

    if not video_path.exists() or video_path.stat().st_size == 0:
        raise ToolError(f"video stream is empty (0 bytes): {video_path}")

    if audio_path is not None and audio_path.exists() and audio_path.stat().st_size > 0:
        cmd = [
            "-y",
            "-i",
            str(video_path),
            "-i",
            str(audio_path),
            "-c",
            "copy",
            *flags,
            "-f",
            fmt,
            str(dest),
        ]
        try:
            await asyncio.to_thread(ffmpeg.run, *cmd)
            return
        except Exception as error:
            err_text = str(error)
            is_video_err = "in#0" in err_text or str(video_path) in err_text
            is_audio_err = not is_video_err and (
                "in#1" in err_text
                or str(audio_path) in err_text
                or ("Invalid data found" in err_text and "in#0" not in err_text)
            )
            if not is_audio_err:
                raise

    cmd = ["-y", "-i", str(video_path), "-c", "copy", *flags, "-f", fmt, str(dest)]
    await asyncio.to_thread(ffmpeg.run, *cmd)
