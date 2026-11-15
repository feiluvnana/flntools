"""What `Media.extract_audio` does: write a file's first audio stream on its own, through ffmpeg.

Private: callers use `media`.

`Media.extract_audio` checks kind, bitrate and format, and passes the probe record and its
temporary path here. Loaded on first use.

    extract(info, part, ".m4a", bitrate=None)      # AAC is copied as it is
"""

from __future__ import annotations

from pathlib import Path

from shared import _ffmpeg, _media

__all__ = ["extract"]


def extract(
    info: _media.Info, part: Path, extension: str, *, bitrate: int | None,
    progress: _media.Progress | None = None,
) -> None:
    """Write `info`'s first audio stream to `part` as `extension`, copied when codec matches.

    Otherwise it is encoded at `bitrate`, or the format's default (MP3 192 kb/s, AAC 160, Opus 128).
    Tags are kept.
    """
    encoding = _media._Output(
        video=None, audio=info.audio, audio_bitrate=bitrate,
        created=info.tags.get("creation_time"),
    )
    out, copied = _media.output(extension, encoding)
    command = ["-n", *_ffmpeg.source(info.path), *out, _ffmpeg.target(part)]
    if copied:
        _ffmpeg.run(command, name=info.path.name, timeout=_ffmpeg.COPY_TIMEOUT)
    else:
        _ffmpeg.encode(command, name=info.path.name, duration=info.duration, progress=progress)
