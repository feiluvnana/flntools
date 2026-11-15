"""Read, check, convert, compress and edit photos, videos and audio, as one `Media` object per file.

    from shared import media
    clip = media.open("in.mov")
    out = clip.trim(start=5, end=65).compress("mid", format="mp4")
    if out.size < clip.size:
        out.save("clip.mp4")

`media.open(path)` probes the file once and returns a `Media` (`Info` fields). Each transform writes
a temporary `.<name>.<random>.part` file beside the origin and returns a new `Media`; `save(dest)`
atomically renames it into place (`save()` overwrites the origin), and `close()` or `with` drops
temporary files. Photos use Pillow (with pi-heif); videos and audio use ffmpeg. `Media` wraps files,
so it has no `.raw`. Different `Media` chains may run on different threads at once.
"""

from __future__ import annotations

import dataclasses
import errno
import os
import re
import secrets
import shutil
import weakref
from collections.abc import Callable, Iterator, Sequence
from contextlib import nullcontext, suppress
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, NamedTuple, Union

import shared
from shared import _atomic, _ffmpeg, _pillow
from shared._log import log

if TYPE_CHECKING:
    from types import TracebackType

    from typing_extensions import Self

__all__ = [
    "BrokenError", "ClosedError", "Encoding", "Error", "Info", "KindError", "LEVELS",
    "Media", "MissingError", "NotMediaError", "StalledError", "UnsafeError",
    "kind", "open", "sniff", "verify",
]

PathLike = Union[str, "os.PathLike[str]"]
Progress = Callable[..., Any]


class Error(shared.Error):
    """The base of every error `shared.media` raises."""


class BrokenError(Error, shared.BrokenError):
    """The file is truncated, corrupt, not media, or lacks a stream the call needs."""


class UnsafeError(Error, shared.UnsafeError):
    """The file is a format media refuses to open, or a decompression bomb."""


class MissingError(Error, shared.MissingError):
    """No usable ffmpeg, or one built without the encoder a call needs."""


class StalledError(Error, TimeoutError):
    """ffmpeg ran past its time limit, or made no progress for a minute, and was killed."""


class KindError(Error, ValueError):
    """A transform that doesn't apply to the file's kind, such as trimming a photo."""


class ClosedError(Error, ValueError):
    """A transform, save, or unprobed read on a closed media chain."""


class NotMediaError(BrokenError):
    """A web page or JSON where media was expected."""


@dataclass(frozen=True)
class Info:
    """What a probe read: kind, format, display dimensions, and metadata (`Media.info`)."""

    # The file probed: the opened file, or a chain's temporary file.
    path: Path
    # "image" (a photo or an animation), "video" or "audio".
    kind: str
    # Whether a photo has several frames (GIF, WebP, APNG); False for video and audio.
    animated: bool
    # Content format: Pillow's name for a photo ("jpeg", "heif"), extension without dot for
    # video/audio ("mp4", "m4a"), or ffmpeg's reader name when it has no magic.
    format: str
    # Bytes on disk.
    size: int
    # Pixels as displayed, after EXIF orientation or a video's rotation; None for audio.
    width: int | None = None
    height: int | None = None
    # Seconds; None for a still photo, and for a stream that doesn't say.
    duration: float | None = None
    # How many frames a photo has; None for video and audio.
    frames: int | None = None
    # Frames per second of a video or an animation; None otherwise.
    fps: float | None = None
    # Bits per second over the whole file, when ffmpeg reports it.
    bitrate: int | None = None
    # The first video and audio stream's codecs, as ffmpeg names them ("h264", "aac"), or None.
    video: str | None = None
    audio: str | None = None
    # The first audio stream's sample rate in Hz and its channel count.
    sample_rate: int | None = None
    channels: int | None = None
    # When it was taken or made, timezone-aware when the file says the zone; None when unknown.
    created: datetime | None = None
    # (latitude, longitude) in degrees; None when absent or 0, 0.
    location: tuple[float, float] | None = None
    # EXIF names for a photo; ffmpeg's metadata keys for video and audio.
    tags: dict[str, str] = field(default_factory=dict)

    @property
    def codec(self) -> str | None:
        """Video codec of a video or audio codec of audio ("h264", "aac"); None for a photo."""
        return {"video": self.video, "audio": self.audio}.get(self.kind)


@dataclass(frozen=True)
class Encoding:
    """Compression settings for `Media.compress`, or a level in `LEVELS` adjusted via `replace`."""

    quality: int = 80
    resolution: int | None = None
    fps: float | None = None
    audio_bitrate: int | None = None
    codec: str | None = None


class _Container(NamedTuple):
    # The ffmpeg muxer name, as `-f` takes it.
    muxer: str
    # The encoder for video, or None for an audio-only container.
    video: str | None
    # The encoder for audio, used when the stream can't be copied.
    audio: str
    # The codecs this container can hold as they are, space-separated, or "*" for any; "" for none.
    video_holds: str
    # The audio codecs this container can hold as they are, space-separated, or "*" for any.
    audio_holds: str


# The four levels, from no visible loss to smallest.
LEVELS: dict[str, Encoding] = {
    "best": Encoding(quality=95),
    "high": Encoding(quality=90),
    "mid": Encoding(quality=80, resolution=1080, fps=60, audio_bitrate=128_000),
    "low": Encoding(quality=70, resolution=720, fps=30, audio_bitrate=64_000),
}
PHOTO_CAPS = {"mid": 2304, "low": 1536}
STILLS = {
    ".jpg": "JPEG", ".jpeg": "JPEG", ".png": "PNG", ".webp": "WEBP", ".avif": "AVIF",
    ".gif": "GIF", ".tif": "TIFF", ".tiff": "TIFF", ".bmp": "BMP", ".ico": "ICO",
    ".tga": "TGA", ".ppm": "PPM", ".jp2": "JPEG2000", ".qoi": "QOI",
}
ANIMATIONS = {".gif": "GIF", ".webp": "WEBP", ".png": "PNG"}
MP4 = ("h264 hevc av1 vp9 mpeg4", "aac opus mp3 alac")
CONTAINERS = {
    ".mp4": _Container("mp4", "libx264", "aac", *MP4),
    ".m4v": _Container("mp4", "libx264", "aac", *MP4),
    ".mov": _Container("mov", "libx264", "aac", *MP4),
    ".mkv": _Container("matroska", "libx264", "aac", "*", "*"),
    ".webm": _Container("webm", "libvpx-vp9", "libopus", "vp8 vp9 av1", "opus vorbis"),
    ".avi": _Container("avi", "libx264", "libmp3lame", "h264 mpeg4 mjpeg", "mp3 aac pcm_s16le"),
    ".ts": _Container("mpegts", "libx264", "aac", "h264 hevc mpeg2video", "aac mp3"),
    ".3gp": _Container("3gp", "libx264", "aac", "h264 mpeg4 h263", "aac amr_nb"),
    ".mp3": _Container("mp3", None, "libmp3lame", "", "mp3"),
    ".m4a": _Container("ipod", None, "aac", "", "aac alac"),
    ".aac": _Container("adts", None, "aac", "", "aac"),
    ".opus": _Container("opus", None, "libopus", "", "opus"),
    ".ogg": _Container("ogg", None, "libvorbis", "", "vorbis"),
    ".flac": _Container("flac", None, "flac", "", "flac"),
    ".wav": _Container("wav", None, "pcm_s16le", "", "pcm_s16le"),
    ".aiff": _Container("aiff", None, "pcm_s16be", "", "pcm_s16be"),
}
CODECS = {"h264": "libx264", "h265": "libx265", "vp9": "libvpx-vp9"}
CODECS_IN = {".webm": ("vp9",), ".avi": ("h264",), ".mkv": ("h264", "h265", "vp9")}
AUDIO_BITRATES = {"libmp3lame": 192_000, "aac": 160_000, "libopus": 128_000}
# Default audio container per codec for `Media.audio`, so the stream is copied unchanged.
AUDIO_HOMES = {
    "aac": ".m4a", "alac": ".m4a", "mp3": ".mp3", "opus": ".opus", "vorbis": ".ogg",
    "flac": ".flac", "pcm_s16le": ".wav",
}
LOSSLESS_AUDIO = ("flac", "wav", "aiff")
PHOTO_EXTENSIONS = (".jpg", ".png", ".gif", ".webp", ".tif", ".bmp", ".ico", ".avif", ".heic")
# Extensions media reads but never writes, so `kind` knows them though STILLS and CONTAINERS
# don't: HEIC/HEIF through pi-heif, `.oga` (Ogg audio), and video formats `compress` turns to MP4.
READ_ONLY = {".heic": "image", ".heif": "image", ".oga": "audio", ".flv": "video", ".ogv": "video",
             ".wmv": "video"}
# EPS (Ghostscript), PDF, WMF.
REFUSED = (b"%!PS", b"\xc5\xd0\xd3\xc6", b"%PDF", b"\xd7\xcd\xc6\x9a")
GIF_FPS = 15


def open(path: PathLike) -> Media:
    """Probe a photo, video or audio file once, and return it as a `Media` to read and transform.

        clip = media.open("clip.mp4")
        clip.kind, clip.width, clip.duration, clip.created, clip.tags.get("title")

    Raises:
        BrokenError, UnsafeError: Not valid media, a refused format, or a decompression bomb.
        FileNotFoundError, ImportError, MissingError, StalledError: Missing file/backend or stall.
    """
    return Media._opened(Path(path))


class _Chain:
    """What one `media.open` started: the origin file and temporary files held by its transforms."""

    def __init__(self, origin: Path) -> None:
        self.origin = origin
        # Each Media's finalizer removes its own path; `close` removes them all.
        self.temporaries: set[Path] = set()
        self.closed = False

    def temporary(self) -> Path:
        """A new temporary path beside the origin on the same disk, so `save` is a rename."""
        # Named like `atomic`'s parts so flnd's repair (`.*.part`) removes leftovers.
        return self.origin.with_name(f".{self.origin.name}.{secrets.token_hex(4)}.part")


def _drop(path: Path, temporaries: set[Path]) -> None:
    """Delete one temporary file and forget it; run by `close` and a collected `Media` finalizer."""
    temporaries.discard(path)
    # A finalizer can run at interpreter exit or while another program holds the file open on
    # Windows; a leftover `.part` is removed by the next repair rather than raising here.
    with suppress(OSError):
        path.unlink(missing_ok=True)


class Media:
    """A photo, video or audio file, probed once, whose transforms each return a new `Media`.

        with media.open("in.mov") as clip:
            out = clip.trim(end=10).compress("low")
            if out.size < clip.size:
                out.save()

    Made by `media.open`. Properties read `Info`; each transform writes a temporary file and
    returns a new `Media` in the same chain; `save` puts it in place, and `close` or `with`
    drops temporary files. One chain belongs to one thread at a time.
    """

    _path: Path
    _info: Info | None
    _chain: _Chain
    _extension: str
    _finalizer: weakref.finalize | None

    def __init__(self) -> None:
        """Refused: `media.open(path)` is the one way to make a `Media`."""
        raise TypeError("make a Media with media.open(path)")

    @classmethod
    def _opened(cls, path: Path) -> Media:
        """A `Media` for a file opened by `media.open`: probed now, holding no temporary file."""
        info = _probe(path)
        made = cls.__new__(cls)
        made._path, made._info, made._chain = path, info, _Chain(path)
        made._extension, made._finalizer = _extension_of(path, info), None
        return made

    @classmethod
    def _temporary(cls, chain: _Chain, path: Path, extension: str) -> Media:
        """A `Media` for a temporary file a transform wrote, probed lazily on first read."""
        made = cls.__new__(cls)
        made._path, made._info, made._chain, made._extension = path, None, chain, extension
        chain.temporaries.add(path)
        made._finalizer = weakref.finalize(made, _drop, path, chain.temporaries)
        return made

    def __repr__(self) -> str:
        return f"<Media {self._chain.origin.name!r} as {self._extension}>"

    def __enter__(self) -> Self:
        return self

    def __exit__(self, kind: type[BaseException] | None, error: BaseException | None,
                 traceback: TracebackType | None) -> None:
        self.close()

    # The probe's fields, as properties.

    @property
    def info(self) -> Info:
        """The probe record; a transform's result is probed on first use and may raise as `open`."""
        if self._info is None:
            self._usable()
            self._info = _probe(self._path)
        return self._info

    @property
    def size(self) -> int:
        """Bytes on disk; read via `stat` on unprobed transform results without running a probe."""
        if self._info is not None:
            return self._info.size
        self._usable()
        return self._path.stat().st_size

    # Reads and checks.

    def check(self) -> Media:
        """Decode the file to find damage, and return this same `Media`.

            media.open("clip.mp4").check()

        Raises:
            BrokenError: the file is truncated or corrupt.
            MissingError: a video or audio file, and no usable ffmpeg.
            StalledError: ffmpeg took longer than 10 minutes.

        A photo is fully decoded. A video or audio file has every packet read without decoding,
        which catches what a cut-off download leaves behind, but not damage inside frames.
        """
        self._usable()
        if self.info.kind == "image":
            _pillow.check(self._path)
        else:
            _ffmpeg.run(
                [
                    "-xerror", *_ffmpeg.source(self._path), "-map", "0",
                    "-c", "copy", "-f", "null", "-",
                ],
                name=self._name, timeout=_ffmpeg.COPY_TIMEOUT,
            )
        return self

    def frames(self, dest: PathLike, *, every: float | None = None,
               at: Sequence[float] | None = None, count: int | None = None, format: str = "jpg",
               quality: int = 90, limit: int | None = 1000) -> list[Path]:
        """Save frames of a video or animation as photos in `dest`, returning paths in time order.

            media.open("clip.mp4").frames("frames/", every=1.0)
            media.open("clip.mp4").frames("thumbs/", count=9)

        Args:
            dest: Output folder (created if missing); files are `<stem>_<seconds>.<ext>`.
            every: Seconds between frames from the start.
            at: Exact timestamps in seconds, in any order.
            count: Number of frames spread evenly across the duration.
            format: Photo format, such as "jpg" or "png".
            quality: 1-100 for lossy formats.
            limit: Max frames to write (None for no limit).

        Raises:
            KindError, ValueError, FileExistsError: Still/audio, invalid args/limit, or collision.
        """
        self._usable()
        info = self.info
        self._expect("frames", "videos and animations", info.kind == "video" or info.animated)
        from shared import _media_video

        return _media_video.frames(
            info, Path(self._chain.origin.name).stem, Path(dest), every=every, at=at,
            count=count, format=format, quality=quality, limit=limit,
        )

    # Transforms: each writes a temporary file and returns a new Media.

    def compress(self, level: str | Encoding = "mid", *, format: str | None = None,
                 strip: bool = True, progress: Progress | None = None) -> Media:
        """Encode a smaller copy in the same format (or `format`), returning a new `Media`.

            out = clip.compress("low")
            if out.size < clip.size:
                out.save()

        Args:
            level: "best", "high", "mid", "low", or an `Encoding` record.
            format: Target format of the same kind (or "gif" for video); None keeps current.
            strip: Drop metadata, chapters and extra streams while keeping display orientation.
            progress: `progress(done, total)` in seconds for AV; None shows a `log.progress` row.

        Returns:
            Compressed `Media` (or `self` when HEIC/lossless audio is given without `format`).

        Raises:
            KindError, ValueError, BrokenError, MissingError, StalledError: Bad kind/arg or failure.
        """
        settings = _settings(level)
        info = self.info
        extension = _compress_target(info, self._name, self._extension, format)
        if extension is None:
            log.debug("{name}: media can't shrink this format without format=", name=self._name)
            return self
        _codec(extension, settings.codec)
        codec = settings.codec or (
            _source_codec(info.video, extension) if info.kind == "video" else None
        )
        resolution = (
            PHOTO_CAPS.get(level) if isinstance(level, str) and info.kind == "image"
            else settings.resolution
        )
        speed = "slow" if settings.quality >= 90 else "medium"
        shown = (log.progress(self._name, total=info.duration)
                 if progress is None and info.kind != "image" else nullcontext())
        with shown as bar:
            return self._derive(extension, lambda part: transcode(
                info, part, extension, quality=settings.quality, codec=codec, fps=settings.fps,
                resolution=resolution, speed=speed, audio_bitrate=settings.audio_bitrate,
                strip=strip, copy=False, progress=progress or (_reporter(bar) if bar else None)))

    def convert(self, format: str, *, quality: int = 90, fps: float | None = None,
                codec: str | None = None, progress: Progress | None = None) -> Media:
        """A copy in another format of the same kind, keeping its metadata.

            media.open("IMG_0042.heic").convert("png").save("IMG_0042.png")
            media.open("clip.webm").convert("mp4", fps=30).save("clip.mp4")

        Args:
            format: the format to write, such as "png", "mp4" or "mp3". A video may become an
                animated "gif", "webp" or "png", and an animation a video.
            quality: 1-100, for whatever has to be encoded.
            fps: a cap on a moving picture's frame rate. A video written as a GIF is at most 15 fps
                unless this says otherwise, and a GIF of a long video is huge.
            codec: "h264", "h265" or "vp9", for a video.
            progress: `progress(done, total)` in seconds for AV; None for no callback.

        Raises:
            KindError: `format` is of another kind (a video to "jpg": use `frames`; a photo to
                "mp4": use `merge`; a video to "mp3": use `extract_audio`), or `fps` for a photo.
            ValueError: an unknown format or codec, or `quality` out of range.

        Streams are copied when the new container holds them, and encoded otherwise.
        """
        extension = _writable(format)
        _check_range("quality", quality, 1, 100)
        _codec(extension, codec)
        info = self.info
        _same_kind(info, self._name, extension, "convert")
        if fps is not None and not (info.animated or info.kind == "video"):
            raise KindError(f"fps= is for moving pictures, and {self._name} is a {_noun(info)}")
        return self._derive(
            extension,
            lambda part: transcode(
                info, part, extension, quality=quality, fps=fps, codec=codec, progress=progress
            ),
        )

    def resize(self, *, width: int | None = None, height: int | None = None) -> Media:
        """A still photo scaled to fit `width` and/or `height`, keeping aspect ratio and metadata.

            media.open("a.jpg").resize(width=1200).save("a.small.jpg")
            media.open("a.jpg").resize(width=800, height=800)       # fits inside 800x800

        Args:
            width: the most pixels across, as displayed; it scales up when larger than the photo.
            height: the most pixels down, as displayed.

        Raises:
            KindError: not a still photo (`convert` keeps an animation; `compress` caps a video).
            ValueError: neither `width` nor `height`, or a format media can't write, such as HEIC
                (`convert` it first).
        """
        if width is None and height is None:
            raise ValueError("give width= and/or height=")
        from shared import _media_image

        return self._edit("resize", lambda photo: _media_image.fitted(photo, width, height))

    def crop(self, box: tuple[int, int, int, int]) -> Media:
        """The part of a still photo inside `box` `(left, top, right, bottom)` as displayed.

            media.open("a.jpg").crop((0, 0, 1080, 1080)).save("a.square.jpg")

        Args:
            box: the corners, as the photo is displayed (after EXIF orientation).

        Raises:
            KindError: not a still photo.
            ValueError: an empty or out-of-bounds box, or a format media can't write.
        """
        from shared import _media_image

        return self._edit("crop", lambda photo: _media_image.cut(photo, box))

    def trim(self, *, start: float = 0.0, end: float | None = None, exact: bool = True,
             progress: Progress | None = None) -> Media:
        """The part of a video, audio file or animation from `start` to `end` seconds.

            media.open("clip.mp4").trim(start=12.5, end=30).save("part.mp4")

        Args:
            start: seconds from the beginning, 0 or more.
            end: seconds from the beginning, after `start`; None runs to the end.
            exact: cut at exactly those times, re-encoding the video at the "high" level's quality
                (the default). False copies the streams, as fast as a file copy, but a video then
                starts at the keyframe at or before `start`, up to a few seconds early. Audio cuts
                exactly either way.
            progress: `progress(done, total)` in seconds for AV; None for no callback.

        Raises:
            KindError: a still photo.
            ValueError: `start` below 0, past the end, or not before `end`; or an unwritable format.
        """
        info = self.info
        self._expect("trim", "videos, audio and animations", info.kind != "image" or info.animated)
        extension = _writable(self._extension)
        if info.kind == "image" and extension not in ANIMATIONS:
            raise ValueError(f"{self._name} has no timeline to trim as {extension}")
        if start < 0 or (end is not None and end <= start):
            raise ValueError(
                f"can't trim from {start} to {end}: start must be 0 or more, and before end"
            )
        if info.duration is not None and start >= info.duration:
            raise ValueError(
                f"start {start} is past the end of {self._name} ({info.duration:.3f} s)"
            )
        from shared import _media_video

        return self._derive(
            extension,
            lambda part: _media_video.trim(
                info, part, extension, start=start, end=end, exact=exact, progress=progress
            ),
        )

    def merge(self, *others: Media | PathLike, audio: Media | PathLike | None = None,
              format: str | None = None, duration: float | None = None,
              progress: Progress | None = None) -> Media:
        """Join this file and `others` end to end into one moving picture, or add `audio` to video.

            media.open("v.mp4").merge(audio="a.m4a").save("joined.mp4")
            media.open("part1.mp4").merge("part2.mp4").save("whole.mp4")
            media.open("a.jpg").merge("b.jpg", duration=2.0).save("slides.mp4")

        Args:
            others: more photos, or more videos, as paths or `Media`; never changed.
            audio: a soundtrack, a file with an audio stream; it ends with the pictures.
            format: what to write: a video container ("mp4", "mkv") or an animated "gif", "webp" or
                "png", which can't hold `audio`; None keeps this video's container, or "mp4".
            duration: seconds each photo shows, 1.0 by default.
            progress: `progress(done, total)` in seconds, for an encode.

        Raises:
            KindError: audio files, or photos mixed with videos.
            BrokenError: `audio` has no audio stream, or a part is cut off.
            ValueError: an audio/still `format`, or `audio` with animation.

        Photos are letterboxed to the first one's size. Clips sharing codecs, size and frame rate
        are copied; others are encoded to the first clip's size.
        """
        infos = [
            self.info,
            *(other.info if isinstance(other, Media) else _probe(Path(other)) for other in others),
        ]
        kinds = {info.kind for info in infos}
        if len(kinds) > 1 or "audio" in kinds:
            raise KindError("merge joins photos or videos, not a mix of them, and not audio files")
        if format is not None:
            extension = _writable(format)
        elif kinds == {"video"} and self._extension in CONTAINERS:
            extension = self._extension
        else:
            extension = ".mp4"
        animation = extension in STILLS
        if (animation and extension not in ANIMATIONS) or kind(extension) == "audio":
            raise ValueError(
                f"merge writes a video or an animated .gif, .webp or .png, not {extension}"
            )
        if animation and audio is not None:
            raise ValueError("an animated image can't hold audio=")
        track = (None if audio is None
                 else audio.info if isinstance(audio, Media) else _probe(Path(audio)))
        if track is not None and not track.audio:
            raise BrokenError(f"{track.path.name} has no audio stream")
        if format is None and len(infos) == 1 and track is not None and extension in CONTAINERS:
            box = CONTAINERS[extension]
            if not (holds(box.video_holds, infos[0].video) and holds(box.audio_holds, track.audio)):
                extension = ".mkv"
        from shared import _media_video

        return self._derive(extension, lambda part: _media_video.merge(
            infos, part, extension, track, duration=duration, progress=progress))

    def extract_audio(self, *, bitrate: int | None = None, format: str | None = None,
                      progress: Progress | None = None) -> Media:
        """The first audio stream, as an audio file of its own, keeping the tags.

            media.open("clip.mp4").extract_audio().save("clip.m4a")

        Args:
            bitrate: bits per second, 32,000-320,000, which re-encodes the stream; None copies it
                when the format holds its codec, and otherwise encodes at the format's default:
                MP3 192 kb/s, AAC 160, Opus 128.
            format: the audio format to write, such as "mp3" or "m4a"; None picks the one that
                holds the stream's codec as it is (AAC in M4A, Opus in .opus).
            progress: `progress(done, total)` in seconds for AV; None for no callback.

        Raises:
            KindError: a photo, or a `format` that isn't audio.
            BrokenError: the file has no audio stream.
            ValueError: `bitrate` out of range, or an unknown format.
        """
        info = self.info
        self._expect("extract_audio", "videos and audio", info.kind != "image")
        _check_range("bitrate", bitrate, 32_000, 320_000)
        if not info.audio:
            raise BrokenError(f"{self._name} has no audio stream")
        extension = _writable(format) if format is not None else AUDIO_HOMES.get(info.audio, ".m4a")
        if kind(extension) != "audio":
            raise KindError(f"extract_audio writes audio, not {extension}")
        from shared import _media_audio

        return self._derive(
            extension,
            lambda part: _media_audio.extract(info, part, extension, bitrate=bitrate,
                                              progress=progress),
        )

    # Ending a chain.

    def save(self, dest: PathLike | None = None) -> Path:
        """Put this file in place, atomically, and return where it went.

            out.save("clip.mp4")
            out.save()                  # over the file the chain started from

        Args:
            dest: the file to write, whose extension must name the format this `Media` is in
                (".jpeg" for "jpg" is fine; ".png" for a JPEG isn't: `convert` first). None writes
                over the file the chain started from; when the chain changed the format, it is
                written under its new extension instead and the original is deleted, so
                `a.heic` compressed to JPEG becomes `a.jpg`.

        Returns:
            The path written. This `Media` then holds that file, not a temporary one. The `Media`
            `open` returned still holds what it read before, even when `save()` wrote over its file.

        Raises:
            FileExistsError: `dest`, or the new name, is a file the chain didn't start from.
            ValueError: `dest`'s extension names another format, or the chain is closed.

        A transform's file is renamed into place, which is one step on the same disk; to another
        disk it is copied through a hidden `.part` file and renamed, so `dest` is never partial.
        """
        self._usable()
        origin = self._chain.origin
        if dest is None:
            target = (origin if _same_format(origin.suffix, self._extension)
                      else origin.with_suffix(self._extension))
        else:
            target = Path(dest)
            if not _same_format(target.suffix, self._extension):
                raise ValueError(
                    f"{target.name}: this is {self._extension}, "
                    f"not {target.suffix or 'no format'}; convert it first"
                )
        if _same_path(self._path, target):
            return target
        over_origin = _same_path(target, origin)
        if target.exists() and not over_origin:
            raise FileExistsError(f"{target} already exists")
        if self._finalizer is not None and self._finalizer.alive:
            try:
                _atomic._replace(self._path, target)
            except OSError as error:
                if error.errno != errno.EXDEV:
                    raise
                # Another disk: rename can't cross it, so copy through a part beside `target`.
                with _atomic.writing(target, replace=True) as part:
                    shutil.copyfile(self._path, part)
                self._path.unlink(missing_ok=True)
            self._finalizer.detach()
            self._chain.temporaries.discard(self._path)
        else:
            # The opened file itself, or one saved before: keep it and copy to `target`.
            with _atomic.writing(target, replace=over_origin) as part:
                shutil.copyfile(self._path, part)
        if dest is None and target != origin:
            origin.unlink(missing_ok=True)
        self._path = target
        if self._info is not None:
            self._info = dataclasses.replace(self._info, path=target)
        return target

    def close(self) -> None:
        """Delete every remaining temporary file of this chain, and refuse further transforms.

            clip = media.open("a.mov")
            try:
                clip.trim(end=10).save("a.mp4")
            finally:
                clip.close()

        Files `save` wrote stay. The chain's `Media` objects keep the properties already read, but a
        transform, a `save`, or a transform's result read for the first time then raises
        `ValueError`. Closing twice does nothing.
        """
        self._chain.closed = True
        for path in list(self._chain.temporaries):
            _drop(path, self._chain.temporaries)

    # Helpers.

    @property
    def _name(self) -> str:
        """The name messages give: the file the chain started from, not a temporary one."""
        return self._chain.origin.name

    def _usable(self) -> None:
        if self._chain.closed:
            raise ClosedError(f"{self._name}: this media chain is closed")

    def _expect(self, verb: str, applies: str, fits: bool) -> None:
        """Raise `KindError` naming what `verb` is for when the file doesn't fit."""
        if not fits:
            raise KindError(f"{verb} is for {applies}; {self._name} is {_article(self.info)}")

    def _edit(self, verb: str, change: Callable[[Any], Any]) -> Media:
        """Apply `change` to a still photo, upright, and write it in its own format at q=90."""
        info = self.info
        self._expect(verb, "still photos", info.kind == "image" and not info.animated)
        extension = _writable(self._extension)
        if extension not in STILLS:
            raise ValueError(f"{self._name}: media can't write a photo as {extension}")

        def write(part: Path) -> None:
            photo = _pillow.open(info.path) or _not_photo(info)
            _pillow.save(
                change(_pillow.upright(photo, keep=True)), part, STILLS[extension], quality=90
            )

        return self._derive(extension, write)

    def _derive(self, extension: str, write: Callable[[Path], None]) -> Media:
        """Run `write` into a new temporary file of the chain, and return the `Media` for it.

        Whatever `write` raises, Ctrl-C included, the temporary file is deleted first.
        """
        self._usable()
        part = self._chain.temporary()
        try:
            write(part)
            if not part.exists():
                raise BrokenError(f"{self._name}: nothing was written")
        except BaseException:
            part.unlink(missing_ok=True)
            raise
        return Media._temporary(self._chain, part, extension)


def _probe(path: Path) -> Info:
    """Read what `path` holds and its metadata without decoding frames; see `open` for errors."""
    extension = sniff(path)
    with path.open("rb") as file:
        head = file.read(64)
    if (
        head[:4] in REFUSED
        or head.startswith(b"ffconcat")
        or (path.suffix.lower() in (".m3u", ".m3u8") and head.lstrip().startswith(b"#EXTM3U"))
        or b"<MPD" in head
    ):
        raise UnsafeError(f"{path.name}: PostScript, PDF, WMF and playlists are refused")
    if extension in (".html", ".json"):
        raise BrokenError(f"{path.name} is a web page or JSON, not media")
    if extension is None or extension in PHOTO_EXTENSIONS:
        try:
            if (photo := _pillow.open(path, memory=False)) is not None:
                with photo:
                    return _pillow.probe(path, photo)
        except MissingError:
            if extension is not None:
                raise
    return _ffmpeg.probe(path, format=extension[1:] if extension else None)


def _extension_of(path: Path, info: Info) -> str:
    """The extension for an opened file's content: its suffix if it matches kind, else sniffed."""
    suffix = path.suffix.lower()
    if kind(suffix) == info.kind:
        return suffix
    return sniff(path) or f".{info.format}"


def _reporter(bar: Any) -> Progress:
    """A `progress(done, total)` callback that updates a `log.progress` row."""
    def report(done: float, total: float | None = None) -> None:
        if total is not None:
            bar.total = total
        bar.count = done
    return report


def sniff(path: PathLike) -> str | None:
    """The extension the first bytes of `path` call for (".webp", ".mp4", ".html"), or None.

        media.sniff("download.part")     # needs neither Pillow nor ffmpeg

    Args:
        path: the file; only its first 192 bytes are read.

    Returns:
        The extension, with its dot, or None for content media doesn't know. `.html` and `.json`
        mean a server sent a page instead of media.

    Raises:
        OSError: the file can't be read, such as FileNotFoundError.

    Safe to call from several threads at once.
    """
    # Every check below reads within these 192 bytes; ftyp brands and WebM sit in the first 64.
    with Path(path).open("rb") as file:
        head = file.read(192)
    # Signatures are the first bytes each format starts with: magic numbers from the format specs,
    # checked in this order so a shared prefix (RIFF, OggS) is resolved by the rest of the header.
    for prefix, extension in (
        (b"\xff\xd8\xff", ".jpg"), (b"\x89PNG\r\n\x1a\n", ".png"), (b"GIF87a", ".gif"),
        (b"GIF89a", ".gif"), (b"II*\0", ".tif"), (b"MM\0*", ".tif"), (b"BM", ".bmp"),
        (b"\0\0\1\0", ".ico"), (b"FLV", ".flv"), (b"fLaC", ".flac"), (b"ID3", ".mp3"),
    ):
        if head.startswith(prefix):
            return extension
    if head[:4] == b"RIFF":
        return {b"WEBP": ".webp", b"WAVE": ".wav", b"AVI ": ".avi"}.get(head[8:12])
    if head[4:8] == b"ftyp":
        return _brand(head)
    if head[:4] == b"\x1a\x45\xdf\xa3":
        return ".webm" if b"webm" in head[:64] else ".mkv"
    # MPEG-TS: a sync byte "G" every 188 bytes.
    if head[:1] == b"G" and head[188:189] == b"G":
        return ".ts"
    if head[:4] == b"OggS":
        return next(
            (ext for mark, ext in (
                (b"OpusHead", ".opus"), (b"\x01vorbis", ".ogg"), (b"\x80theora", ".ogv")
            ) if mark in head[:64]),
            None,
        )
    if head[:4] == b"FORM" and head[8:12] in (b"AIFF", b"AIFC"):
        return ".aiff"
    # ADTS AAC sync (0xFFF with layer == 00) before MPEG audio frame sync.
    if len(head) > 1 and head[0] == 0xFF and (head[1] & 0xF6) == 0xF0:
        return ".aac"
    if len(head) > 1 and head[0] == 0xFF and head[1] >= 0xE0:
        return ".mp3"
    text = head.removeprefix(b"\xef\xbb\xbf").lstrip()
    return {b"<": ".html", b"{": ".json", b"[": ".json"}.get(text[:1])


def verify(path: PathLike, *, kind: str | None = None, decode: bool = True) -> str:
    """Verify `path` is valid media (not a web page or corrupt) and return its content extension."""
    target = Path(path)
    extension = sniff(target)
    if extension in (".html", ".json"):
        raise NotMediaError(f"{target.name}: got a web page or JSON instead of {kind or 'media'}")
    if decode:
        try:
            opened = open(target)
            opened.check()
            extension = extension or opened._extension
        except (MissingError, StalledError) as error:
            log.debug("{name}: not checked: {error}", name=target.name, error=error)
    result = extension or ""
    if kind is not None and result:
        actual = globals()["kind"](result)
        if actual is not None and actual != kind:
            raise KindError(f"{target.name} is {actual}, expected {kind}")
    return result


def _brand(head: bytes) -> str:
    """The extension an ISO BMFF file's `ftyp` brands call for: AVIF, HEIC, MOV, M4A, 3GP or MP4."""
    brand = head[8:12]
    compatible = {
        head[index:index + 4] for index in range(16, min(int.from_bytes(head[:4], "big"), 64), 4)
    }
    if brand in (b"avif", b"avis") or (brand == b"mif1" and b"avif" in compatible):
        return ".avif"
    if brand in (b"heic", b"heix", b"heim", b"heis", b"hevc", b"hevx", b"mif1", b"msf1"):
        return ".heic"
    if brand == b"qt  ":
        return ".mov"
    if brand == b"M4A ":
        return ".m4a"
    return ".3gp" if brand in (b"3gp4", b"3gp5", b"3gp6") else ".mp4"


def kind(name: str) -> str | None:
    """The kind of file a name or extension says ("image", "video", "audio"), or None if unknown.

        media.kind("posts/a.heic")     # "image"
        media.kind("M4A")              # "audio"
        media.kind(".zip")             # None

    Args:
        name: a file name or path ("a.jpg"), or an extension with or without its dot, in any case
            (".jpg", "MP4").

    Returns:
        "image" for a photo or an animation (GIF, animated WebP), "video" for a container that holds
        video, "audio" for one that holds sound only, or None for anything else. It reads only the
        name; the content may say otherwise, which `sniff` reads.

    Needs neither Pillow nor ffmpeg; safe to call from several threads at once.
    """
    # A bare extension (".jpg", "MP4") has no suffix of its own, so the whole text is the extension.
    extension = "." + (Path(name).suffix or name).lower().lstrip(".")
    if extension in STILLS:
        return "image"
    if extension in CONTAINERS:
        return "video" if CONTAINERS[extension].video else "audio"
    return READ_ONLY.get(extension)


def transcode(info: Info, part: Path, extension: str, *, quality: int, codec: str | None = None,
              fps: float | None = None, resolution: int | None = None, speed: str = "medium",
              audio_bitrate: int | None = None, strip: bool = False, copy: bool = True,
              progress: Progress | None = None) -> None:
    """Write `info`'s file to `part` as `extension`, re-encoding what the format can't hold as-is.

        transcode(info, part, ".mp4", quality=80, resolution=1080)

    Args:
        info: the probe of the file to read; its path is the input.
        part: the temporary file to write; the caller renames it into place.
        extension: the format to write, such as ".jpg" or ".mp4". It is of the same kind as `info`,
            except that a video may become an animation (".gif", ".webp", ".png").
        quality: 1-100, for whatever is encoded.
        codec: "h264", "h265" or "vp9", for a video; None takes the format's own.
        fps: a frame-rate cap, or None to keep the source's rate.
        resolution: a cap in pixels on the shorter side of a video, or the longer side of a photo;
            None keeps the full size.
        speed: x264's speed preset: "medium" for the mid and low levels, "slow" for best and high.
        audio_bitrate: bits per second for a re-encoded audio stream; None uses the codec default.
        strip: drop metadata, chapters and extra streams (EXIF, GPS, XMP and ffmpeg tags).
        copy: copy streams when the new container holds them as-is; False always re-encodes.
        progress: `progress(done, total)` in seconds, called only while a video or audio is encoded;
            a copy or a photo reports nothing.

    Raises:
        BrokenError: a photo can't be decoded, or ffmpeg fails on the file.
        MissingError: ffmpeg or the encoder isn't available.
        StalledError: a stream copy runs past 10 minutes, or an encode stalls for 60 s.
    """
    # Decode still photos in process; `keep` leaves EXIF in when the caller didn't strip it.
    if info.kind == "image" and not info.animated:
        photo = _pillow.upright(_pillow.open(info.path) or _not_photo(info), keep=not strip)
        # `optimize` is on only when re-encoding (`copy` False): slower, shrinks the file.
        _pillow.save(
            _pillow.shrink(photo, resolution), part, STILLS[extension],
            quality=quality, optimize=not copy,
        )
    # An animated photo stays animated: Pillow decodes each frame and writes them with their delays.
    elif info.kind == "image" and extension in ANIMATIONS:
        frames, delays = _pillow.animation(
            _pillow.open(info.path) or _not_photo(info), resolution=resolution, fps=fps
        )
        _pillow.save_animation(frames, delays, part, ANIMATIONS[extension], quality=quality)
    # A video written as an animated picture goes through ffmpeg's GIF or frame path.
    elif extension in ANIMATIONS:
        _video_to_animation(
            info, part, extension, quality=quality, fps=fps, resolution=resolution
        )
    else:
        inputs, feed = _inputs(info)
        # An animation reaches ffmpeg as GIF or raw frames, never copied as-is into a container.
        encoding = _Output(
            video=info.video or ("rawvideo" if info.kind == "image" else None),
            audio=info.audio, quality=quality, codec=codec, fps=fps, source_fps=info.fps,
            resolution=resolution, speed=speed, audio_bitrate=audio_bitrate, strip=strip,
            copy=copy and info.kind != "image", created=info.tags.get("creation_time"))
        out, copied = output(extension, encoding)
        command = ["-n", *inputs, *out, _ffmpeg.target(part)]
        if copied:
            _ffmpeg.run(command, name=info.path.name, timeout=_ffmpeg.COPY_TIMEOUT)
        else:
            _ffmpeg.encode(
                command, name=info.path.name, duration=info.duration, progress=progress, feed=feed
            )


@dataclass(frozen=True)
class _Output:
    """Stream, quality, filter and metadata options for `output`."""

    video: str | None
    audio: str | None
    quality: int = 90
    codec: str | None = None
    fps: float | None = None
    source_fps: float | None = None
    resolution: int | None = None
    speed: str = "medium"
    audio_bitrate: int | None = None
    strip: bool = False
    copy: bool = True
    created: str | None = None
    maps: tuple[str, str] = ("0:v:0", "0:a:0?")
    simple: bool = True


def output(extension: str, encoding: _Output) -> tuple[list[str], bool]:
    """ffmpeg's output options for `extension`, and whether every stream is copied.

    `encoding.simple=False` leaves out `-vf`, for video from a `-filter_complex` graph.
    """
    box = CONTAINERS[extension]
    video = encoding.video if box.video else None
    args = [*(["-map", encoding.maps[0]] if video else []),
            *(["-map", encoding.maps[1]] if encoding.audio else [])]
    copy_video = (encoding.copy and not (encoding.codec or encoding.resolution or encoding.fps)
                  and holds(box.video_holds, video))
    copy_audio = (encoding.copy and not encoding.audio_bitrate
                  and holds(box.audio_holds, encoding.audio))
    if video:
        filters = (["-vf", _filters(encoding.resolution, encoding.fps, encoding.source_fps)]
                   if encoding.simple else [])
        codec_args = _video_codec(extension, encoding.codec, encoding.quality, encoding.speed)
        args += ["-c:v", "copy"] if copy_video else [*filters, *codec_args]
    if encoding.audio:
        args += (["-c:a", "copy"] if copy_audio
                 else _audio_codec(box.audio, encoding.audio_bitrate))
    if encoding.strip:
        args += ["-map_metadata", "-1", "-map_chapters", "-1", "-fflags", "+bitexact",
                 "-flags:v", "+bitexact", "-flags:a", "+bitexact", "-metadata:s", "encoder="]
    elif encoding.created:
        # ffmpeg drops creation_time when it copies metadata, so it is written back explicitly.
        args += ["-metadata", f"creation_time={encoding.created}"]
    copied = (copy_video or not video) and (copy_audio or not encoding.audio)
    return [*args, *muxer(extension)], copied


def muxer(extension: str) -> list[str]:
    """The output format for `extension`; MP4 and MOV put their index first for fast start."""
    name = CONTAINERS[extension].muxer
    return [
        *(["-movflags", "+faststart"] if name in ("mp4", "mov", "ipod", "3gp") else []), "-f", name
    ]


def _filters(resolution: int | None, fps: float | None, source_fps: float | None) -> str:
    """ffmpeg's `-vf` chain: SAR-corrected scale cap, frame-rate cap, square pixels."""
    # -2 keeps both sides even, which H.264 needs; the shorter side is capped, never scaled up.
    size = (
        "scale='if(gte(iw*sar,ih),-2,min(iw*sar,{0}))':"
        "'if(gte(iw*sar,ih),min(ih,{0}),-2)'".format(resolution)
        if resolution else "scale=trunc(iw*sar/2)*2:trunc(ih/2)*2"
    )
    rate = f",fps={fps:g}" if fps and (source_fps is None or source_fps > fps) else ""
    return f"{size}{rate},setsar=1"


def _video_codec(extension: str, codec: str | None, quality: int, speed: str) -> list[str]:
    """ffmpeg's video encoder options for `extension`, mapping `quality` onto the encoder scale."""
    encoder = _ffmpeg.need(CODECS[codec] if codec else CONTAINERS[extension].video or "libx264")
    # One quality scale for every encoder: 70, 80, 90 give x264 CRF 28, 23, 19 and VP9 40, 34, 28.
    if encoder == "libvpx-vp9":
        return [
            "-c:v", encoder, "-crf", str(min(63, (410 - 3 * quality) // 5)), "-b:v", "0",
            "-row-mt", "1", "-deadline", "good", "-cpu-used", "2", "-pix_fmt", "yuv420p",
        ]
    crf = min(51, (1190 - 9 * quality) // 20 + (4 if encoder == "libx265" else 0))
    tag = (["-tag:v", "hvc1"]
           if encoder == "libx265" and CONTAINERS[extension].muxer in ("mp4", "mov") else [])
    return ["-c:v", encoder, "-crf", str(crf), "-preset", speed, "-pix_fmt", "yuv420p", *tag]


def _audio_codec(encoder: str, bitrate: int | None) -> list[str]:
    """ffmpeg's audio encoder options: encoder and bitrate or quality, as the codec takes them."""
    if encoder == "libvorbis" and encoder not in _ffmpeg.encoders():
        encoder = "libopus"  # Opus in Ogg, when this ffmpeg has no libvorbis
    _ffmpeg.need(encoder)
    if encoder in ("flac", "pcm_s16le", "pcm_s16be"):
        return ["-c:a", encoder]
    if encoder == "libvorbis" and not bitrate:
        return ["-c:a", encoder, "-q:a", "5"]
    return ["-c:a", encoder, "-b:a", str(bitrate or AUDIO_BITRATES.get(encoder, 128_000))]


def holds(codecs: str, codec: str | None) -> bool:
    """Whether a container can hold `codec` as it is, given its space-separated `codecs` list.

        holds(MP4[0], "h264")      # True
        holds("*", "flac")         # True: MKV holds anything

    Args:
        codecs: the codecs the container holds, space-separated, or "*" for any; "" for none.
        codec: the stream's codec as ffmpeg names it, or None for no stream, which is always held.

    Returns:
        True when the stream can be copied into the container unchanged.
    """
    return codec is None or codecs == "*" or codec in codecs.split()


def _inputs(info: Info) -> tuple[list[str], Any]:
    """ffmpeg's input options for `info`, plus raw frames to feed for an animation it can't read."""
    if not (info.kind == "image" and info.format != "gif"):
        return _ffmpeg.source(info.path), None
    frames, delays = _pillow.animation(_pillow.open(info.path) or _not_photo(info))
    rate = 1000 * len(delays) / sum(delays)
    first = frames[0]
    return (
        raw_input(first.width, first.height, rate),
        (frame.convert("RGB").tobytes() for frame in frames),
    )


def raw_input(width: int, height: int, rate: float) -> list[str]:
    """Input options for raw RGB frames that media writes to ffmpeg's stdin itself.

    Args:
        width: the frame's width in pixels.
        height: the frame's height in pixels.
        rate: the frames per second the frames play at.

    Returns:
        ffmpeg's input arguments, which must be followed by the frames on stdin.
    """
    # `source` whitelists `file` only, so this input names `pipe` itself to read stdin.
    return ["-f", "rawvideo", "-pix_fmt", "rgb24", "-s", f"{width}x{height}", "-r", f"{rate:g}",
            "-protocol_whitelist", "pipe", "-i", "pipe:0"]


def _video_to_animation(info: Info, part: Path, extension: str, *, quality: int, fps: float | None,
                        resolution: int | None) -> None:
    """Write a video as an animated GIF, WebP or PNG at most `fps` frames/s (15 by default)."""
    # GIF's 15 fps by default: a GIF stores every frame, so a faster rate makes a much bigger file.
    rate = min(fps or GIF_FPS, info.fps or GIF_FPS)
    if extension == ".gif":
        size = _filters(resolution, None, None).removesuffix(",setsar=1")
        # GIF holds 256 colours per frame. palettegen builds one palette from the clip and
        # paletteuse applies it, so colours stay close to the video's.
        graph = f"fps={rate:g},{size},split[a][b];[a]palettegen[p];[b][p]paletteuse"
        _ffmpeg.encode(
            ["-n", *_ffmpeg.source(info.path), "-map", "0:v:0", "-vf", graph, "-f", "gif",
             _ffmpeg.target(part)],
            name=info.path.name, duration=info.duration, progress=None,
        )
        return
    width, height = info.width or 0, info.height or 0
    images = [
        _pillow.shrink(image, resolution)
        for image in decoded(info, f"fps={rate:g}", width, height)
    ]
    _pillow.save_animation(
        images, [round(1000 / rate)] * len(images), part, ANIMATIONS[extension], quality=quality
    )


def decoded(info: Info, graph: str, width: int, height: int) -> Iterator[Any]:
    """The frames `graph` picks from a video, decoded by ffmpeg into Pillow images, upright.

    Args:
        info: the probe of the video to read.
        graph: an ffmpeg filter chain that picks frames (e.g. "fps=15"), run before the scale.
        width: the frame width in pixels, which every frame is scaled to.
        height: the frame height in pixels, which every frame is scaled to.

    Yields:
        One Pillow RGB image per frame, in time order.

    Raises:
        BrokenError: ffmpeg fails on the file; raised after the last frame, once iteration ends.
        StalledError: ffmpeg makes no progress for 60 s.
    """
    Image = _pillow.load()
    command = [
        *_ffmpeg.source(info.path), "-map", "0:v:0",
        "-vf", f"{graph},scale={width}:{height},setsar=1",
        *_ffmpeg.passthrough(), "-f", "rawvideo", "-pix_fmt", "rgb24", "pipe:1",
    ]
    for frame in _ffmpeg.frames_out(command, name=info.path.name, size=width * height * 3):
        yield Image.frombytes("RGB", (width, height), frame)


def _writable(format: str) -> str:
    """The extension for a format media writes ("png", ".mp4"), or ValueError if unwritable."""
    extension = "." + format.lower().lstrip(".")
    if extension not in STILLS and extension not in CONTAINERS:
        raise ValueError(f"media can't write {format or 'a file without a format'}")
    return extension


def _family(extension: str) -> str:
    """Format family: Pillow's name for a photo or muxer for a container (.jpeg == .jpg)."""
    extension = extension.lower()
    if extension in STILLS:
        return STILLS[extension]
    if extension in CONTAINERS:
        return CONTAINERS[extension].muxer
    return extension


def _same_format(first: str, second: str) -> bool:
    return _family(first) == _family(second)


def _same_path(first: Path, second: Path) -> bool:
    return os.path.abspath(first) == os.path.abspath(second)


def _same_kind(info: Info, name: str, extension: str, verb: str) -> None:
    """Raise `KindError` if writing `info`'s file as `extension` would change its kind."""
    target = kind(extension) or ""
    if info.kind == "image" and info.animated and target != "audio":
        if target == "image" and extension not in ANIMATIONS:
            raise KindError(f"{name} is animated; {extension} can't hold animation; use .frames()")
        return
    if info.kind == "video" and extension in ANIMATIONS:
        return
    if info.kind != target:
        instead = {("video", "image"): "use .frames() to get photos",
                   ("image", "video"): "use .merge() to make a video",
                   ("video", "audio"): "use .extract_audio()"}.get((info.kind, target),
                                                                   f"{verb} keeps the kind")
        raise KindError(f"{name} is a {_noun(info)}; {instead}")


def _noun(info: Info) -> str:
    return {"image": "photo", "video": "video", "audio": "audio file"}[info.kind]


def _article(info: Info) -> str:
    """The file's kind with its article for `KindError` ("a photo", "an animation", etc.)."""
    if info.kind == "image" and info.animated:
        return "an animation"
    return {"image": "a photo", "video": "a video", "audio": "an audio file"}[info.kind]


def _not_photo(info: Info) -> Any:
    raise BrokenError(f"{info.path.name}: not a photo")


def _settings(level: str | Encoding) -> Encoding:
    """Validate `level` (name or `Encoding` record) and return the resolved `Encoding`."""
    if isinstance(level, str):
        if level not in LEVELS:
            raise ValueError(f"level must be one of {', '.join(LEVELS)}, not {level!r}")
        return LEVELS[level]
    if not isinstance(level, Encoding):
        raise ValueError(f"level must be a preset name or Encoding, not {level!r}")
    for name, value, low, high in (
        ("quality", level.quality, 1, 100), ("resolution", level.resolution, 16, 1 << 20),
        ("fps", level.fps, 1, 240), ("audio_bitrate", level.audio_bitrate, 32_000, 320_000),
    ):
        _check_range(name, value, low, high)
    return level


def _source_codec(video: str | None, extension: str) -> str | None:
    """Keep the source video codec when `extension`'s container can write it."""
    if not video:
        return None
    mapped = {"hevc": "h265"}.get(video, video)
    if mapped not in CODECS:
        return None
    allowed = CODECS_IN.get(extension, ("h264", "h265")) if extension in CONTAINERS else ()
    return mapped if mapped in allowed else None


def _check_range(name: str, value: float | None, low: float, high: float) -> None:
    if value is not None and not low <= value <= high:
        raise ValueError(f"{name} must be from {low:,} to {high:,}, not {value!r}")


def _codec(extension: str, codec: str | None) -> None:
    """Raise ValueError for an unknown codec or one `extension`'s container can't hold."""
    if codec is None:
        return
    if codec not in CODECS:
        raise ValueError(f"codec must be one of {', '.join(CODECS)}, not {codec!r}")
    allowed = (CODECS_IN.get(extension, ("h264", "h265"))
               if extension in CONTAINERS else tuple(CODECS))
    if codec not in allowed:
        raise ValueError(f"{extension} can't hold {codec}; use {' or '.join(allowed)}")


def _compress_target(info: Info, name: str, suffix: str, format: str | None) -> str | None:
    """The extension `compress` writes (`format` or the file's own), or None if it can't shrink.

    `suffix` is kept when it names the same format as the content (".jpeg" stays ".jpeg"), so
    `save()` writes over the same name.
    """
    if format:
        extension = _writable(format)
        _same_kind(info, name, extension, "compress")
        return extension
    if info.kind == "image":
        same = suffix if STILLS.get(suffix, "").lower() == info.format else None
        return same or {"jpeg": ".jpg", "tiff": ".tif", "jpeg2000": ".jp2"}.get(info.format) or (
            f".{info.format}" if f".{info.format}" in STILLS else None)
    if info.kind == "audio" and info.format in LOSSLESS_AUDIO:
        return None
    own = f".{info.format}"
    if own in CONTAINERS and kind(own) == info.kind:
        return (suffix
                if suffix in CONTAINERS and CONTAINERS[suffix].muxer == CONTAINERS[own].muxer
                else own)
    return ".mp4" if info.kind == "video" else ".m4a"


def parse_time(text: str | None) -> datetime | None:
    """An ISO 8601 `creation_time`, timezone-aware when it names one, or None."""
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")) if text else None
    except ValueError:
        return None


def parse_iso6709(text: str | None) -> tuple[float, float] | None:
    """(latitude, longitude) from an ISO 6709 string (e.g. `+48.8577+002.2950/`), or None at 0,0."""
    match = re.match(r"([+-]\d+(?:\.\d+)?)([+-]\d+(?:\.\d+)?)", text or "")
    if not match or float(match[1]) == float(match[2]) == 0:
        return None
    return float(match[1]), float(match[2])

