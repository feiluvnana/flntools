"""media.audio: sound on its own.

    media.audio.extract("clip.mp4", dest="clip.mp3")
"""

from __future__ import annotations

from pathlib import Path

from shared import _atomic, _ffmpeg, _media
from shared._media import PathLike


def extract(src: PathLike, *, dest: PathLike, bitrate: int | None = None) -> Path:
    """Save the first audio stream of `src` as the audio format `dest`'s extension names, and return `dest`.

        media.audio.extract("clip.mp4", dest="clip.m4a")      # AAC is copied as it is

    The stream is copied when the format holds its codec, and otherwise encoded at `bitrate`
    (bits per second) or the format's default: MP3 192 kb/s, AAC 160, Opus 128. Tags are kept.
    A file without audio raises `BrokenError`.
    """
    src, dest = Path(src), Path(dest)
    extension = _media.writable(dest)
    if _media.kind_of(extension) != "audio":
        raise ValueError(f"{dest.name}: media.audio.extract writes audio, not {extension}")
    _media.check_range("bitrate", bitrate, 32_000, 320_000)
    if dest.exists():
        raise FileExistsError(f"{dest} already exists")
    info = _media.probe(src)
    if not info.audio:
        raise _media.BrokenError(f"{src.name} has no audio stream")
    out, copied = _media.output(extension, video=None, audio=info.audio, quality=90, audio_bitrate=bitrate,
                                created=info.tags.get("creation_time"))
    with _atomic.writing(dest) as part:
        command = ["-n", *_ffmpeg.source(src), *out, _ffmpeg.target(part)]
        if copied:
            _ffmpeg.run(command, name=src.name, timeout=_ffmpeg.COPY_TIMEOUT)
        else:
            _ffmpeg.encode(command, name=src.name, duration=info.duration, progress=None)
    return dest
