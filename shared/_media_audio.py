"""What `Media.audio` does: write a file's first audio stream on its own, through ffmpeg. Private: callers use `media`.

`Media.audio` checks the kind, the bitrate and the format, and passes the probe record and its
temporary path here. Loaded on first use.

    extract(info, part, ".m4a", bitrate=None)      # AAC is copied as it is
"""

from __future__ import annotations

from pathlib import Path

from shared import _ffmpeg, _media

__all__ = ["extract"]


def extract(info: _media.Info, part: Path, extension: str, *, bitrate: int | None) -> None:
    """Write `info`'s first audio stream to `part` as `extension`, copied when the format holds its codec.

    Otherwise it is encoded at `bitrate`, or the format's default (MP3 192 kb/s, AAC 160, Opus 128).
    Tags are kept.
    """
    out, copied = _media.output(extension, video=None, audio=info.audio, quality=90, audio_bitrate=bitrate,
                                created=info.tags.get("creation_time"))
    command = ["-n", *_ffmpeg.source(info.path), *out, _ffmpeg.target(part)]
    if copied:
        _ffmpeg.run(command, name=info.path.name, timeout=_ffmpeg.COPY_TIMEOUT)
    else:
        _ffmpeg.encode(command, name=info.path.name, duration=info.duration, progress=None)
