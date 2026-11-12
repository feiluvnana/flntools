"""Read, convert, compress and check photos, videos and audio.

    from shared import media
    info = media.probe("IMG_0042.heic")               # kind, format, size, date taken, location, tags
    media.convert("clip.webm", dest="clip.mp4")       # keeps the kind: still, moving picture or audio
    media.compress("clip.mp4", preset="low")          # in place: smaller, stripped, same format
    media.video.trim("clip.mp4", start=12.5, end=30, dest="part.mp4")

Photos go through Pillow (with pi-heif for HEIC), in process. Videos and audio go through the
ffmpeg program, out of process, found on first use: imageio-ffmpeg's, else the one on PATH.
`media.image`, `media.video` and `media.audio` hold what works on one kind only, and load on first use.
"""

from __future__ import annotations

import importlib
import os
import re
from collections.abc import Callable, Iterator
from contextlib import nullcontext
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, NamedTuple, Union

import shared
from shared import _atomic, _ffmpeg, _pillow
from shared._log import log

if TYPE_CHECKING:
    from shared import _media_audio as audio
    from shared import _media_image as image
    from shared import _media_video as video

__all__ = ["BrokenError", "Container", "Error", "Info", "Preset", "UnsafeError", "audio", "check", "compress",
           "convert", "image", "kind_of", "probe", "sniff", "video"]

PathLike = Union[str, "os.PathLike[str]"]
Progress = Callable[..., Any]


class Error(shared.Error):
    """The base of every error `shared.media` raises."""


class BrokenError(Error, shared.BrokenError):
    """The file is truncated, corrupt, not media, or lacks a stream the call needs."""


class UnsafeError(Error, shared.UnsafeError):
    """The file is a format media refuses to open, or a decompression bomb."""


@dataclass(frozen=True)
class Info:
    """What `probe` read: the kind, format and size, as displayed, and the file's metadata."""

    path: Path
    kind: str
    animated: bool
    format: str
    size: int
    width: int | None = None
    height: int | None = None
    duration: float | None = None
    frames: int | None = None
    fps: float | None = None
    bitrate: int | None = None
    video: str | None = None
    audio: str | None = None
    sample_rate: int | None = None
    channels: int | None = None
    created: datetime | None = None
    location: tuple[float, float] | None = None
    tags: dict[str, str] = field(default_factory=dict)


class Preset(NamedTuple):
    quality: int
    video_resolution: int | None
    photo_resolution: int | None
    fps: float | None
    speed: str
    audio_bitrate: int


class Container(NamedTuple):
    muxer: str
    video: str | None
    audio: str
    video_holds: str
    audio_holds: str


# The four levels, from no visible loss to smallest: quality, the video and photo caps on the
# shorter or longer side (None keeps the full resolution), the fps cap, x264's speed and the audio
# bitrate. flnd's --image, --video and --audio levels are these names. Every one strips metadata.
PRESETS = {"best": Preset(95, None, None, None, "slow", 192_000),
           "high": Preset(90, None, None, None, "slow", 160_000),
           "mid": Preset(80, 1080, 2304, 60, "medium", 128_000),
           "low": Preset(70, 720, 1536, 30, "medium", 64_000)}
STILLS = {".jpg": "JPEG", ".jpeg": "JPEG", ".png": "PNG", ".webp": "WEBP", ".avif": "AVIF", ".gif": "GIF",
          ".tif": "TIFF", ".tiff": "TIFF", ".bmp": "BMP", ".ico": "ICO", ".tga": "TGA", ".ppm": "PPM",
          ".jp2": "JPEG2000", ".qoi": "QOI"}
ANIMATIONS = {".gif": "GIF", ".webp": "WEBP", ".png": "PNG"}
MP4 = ("h264 hevc av1 vp9 mpeg4", "aac opus mp3 alac")
CONTAINERS = {
    ".mp4": Container("mp4", "libx264", "aac", *MP4), ".m4v": Container("mp4", "libx264", "aac", *MP4),
    ".mov": Container("mov", "libx264", "aac", *MP4), ".mkv": Container("matroska", "libx264", "aac", "*", "*"),
    ".webm": Container("webm", "libvpx-vp9", "libopus", "vp8 vp9 av1", "opus vorbis"),
    ".avi": Container("avi", "libx264", "libmp3lame", "h264 mpeg4 mjpeg", "mp3 aac pcm_s16le"),
    ".ts": Container("mpegts", "libx264", "aac", "h264 hevc mpeg2video", "aac mp3"),
    ".3gp": Container("3gp", "libx264", "aac", "h264 mpeg4 h263", "aac amr_nb"),
    ".mp3": Container("mp3", None, "libmp3lame", "", "mp3"), ".m4a": Container("ipod", None, "aac", "", "aac alac"),
    ".aac": Container("adts", None, "aac", "", "aac"), ".opus": Container("opus", None, "libopus", "", "opus"),
    ".ogg": Container("ogg", None, "libvorbis", "", "vorbis"), ".flac": Container("flac", None, "flac", "", "flac"),
    ".wav": Container("wav", None, "pcm_s16le", "", "pcm_s16le"),
    ".aiff": Container("aiff", None, "pcm_s16be", "", "pcm_s16be"),
}
CODECS = {"h264": "libx264", "h265": "libx265", "vp9": "libvpx-vp9"}
CODECS_IN = {".webm": ("vp9",), ".avi": ("h264",), ".mkv": ("h264", "h265", "vp9")}
AUDIO_BITRATES = {"libmp3lame": 192_000, "aac": 160_000, "libopus": 128_000}
LOSSLESS_AUDIO = ("flac", "wav", "aiff")
PHOTO_EXTENSIONS = (".jpg", ".png", ".gif", ".webp", ".tif", ".bmp", ".ico", ".avif", ".heic")
# Extensions media reads but never writes, so `kind_of` knows them though STILLS and CONTAINERS
# don't: HEIC and HEIF through pi-heif, `.oga` (Ogg audio), and video formats `compress` turns into MP4.
READ_ONLY = {".heic": "image", ".heif": "image", ".oga": "audio", ".flv": "video", ".ogv": "video",
             ".wmv": "video"}
REFUSED = (b"%!PS", b"\xc5\xd0\xd3\xc6", b"%PDF", b"\xd7\xcd\xc6\x9a")  # EPS (Ghostscript), PDF, WMF
GIF_FPS = 15


def __getattr__(name: str) -> Any:
    if name not in ("image", "video", "audio"):
        raise AttributeError(f"module 'shared.media' has no attribute {name!r}")
    module = importlib.import_module(f"shared._media_{name}")
    globals()[name] = module
    return module


def probe(path: PathLike) -> Info:
    """Read what `path` holds and its metadata, without decoding pictures or frames.

        info = media.probe("clip.mp4")
        info.kind, info.width, info.height, info.duration, info.created, info.tags.get("title")

    A photo's header and EXIF are read by Pillow; a video or audio file is one short ffmpeg run.
    A file that isn't media raises `BrokenError`; a refused format or a bomb raises `UnsafeError`.
    """
    path = Path(path)
    extension = sniff(path)
    with path.open("rb") as file:
        if file.read(4) in REFUSED:
            raise UnsafeError(f"{path.name}: PostScript, PDF and WMF files are refused")
    if extension in (".html", ".json"):
        raise BrokenError(f"{path.name} is a web page or JSON, not media")
    if extension is None or extension in PHOTO_EXTENSIONS:
        try:
            if (photo := _pillow.open(path, memory=False)) is not None:
                with photo:
                    return _pillow.probe(path, photo)
        except ImportError:
            if extension is not None:
                raise
    try:
        return _ffmpeg.probe(path, format=extension[1:] if extension else None)
    except UnsafeError:
        # A refused reader matters for a playlist or a list, which name other files; for binary
        # junk it only means ffmpeg guessed a format it may not use, so the file isn't media.
        with path.open("rb") as file:
            if file.read(64).isascii():
                raise
        raise BrokenError(f"{path.name}: not media") from None


def convert(src: PathLike, *, dest: PathLike, quality: int = 90, fps: float | None = None,
            codec: str | None = None) -> Path:
    """Write `src` as the format `dest`'s extension names, keeping its kind, and return `dest`.

        media.convert("IMG_0042.heic", dest="IMG_0042.png")
        media.convert("clip.webm", dest="clip.mp4", fps=30)

    Streams are copied when `dest`'s container holds them; otherwise they are encoded, at
    `quality` (1-100), with `codec` ("h264", "h265" or "vp9") when given. Metadata is kept.
    `fps` caps a moving picture's frame rate. A video written as a GIF is at most 15 fps unless
    `fps` says otherwise, and a GIF of a long video is huge. A conversion that changes the kind
    raises `ValueError` naming the function to use; an existing `dest` raises `FileExistsError`.
    """
    src, dest = Path(src), Path(dest)
    extension = writable(dest)
    check_range("quality", quality, 1, 100)
    _codec(extension, codec)
    if dest.exists():
        raise FileExistsError(f"{dest} already exists")
    info = probe(src)
    same_kind(info, extension, "convert")
    if fps is not None and not (info.animated or info.kind == "video"):
        raise ValueError(f"fps= is for moving pictures, and {src.name} is a {_noun(info)}")
    with _atomic.writing(dest) as part:
        transcode(info, part, extension, quality=quality, fps=fps, codec=codec)
    return dest


def compress(src: PathLike, *, dest: PathLike | None = None, format: str | None = None, preset: str = "mid",
             quality: int | None = None, resolution: int | None = None, fps: float | None = None,
             codec: str | None = None, audio_bitrate: int | None = None, strip: bool = True,
             progress: Progress | None = None) -> Path | None:
    """Make `src` smaller, in the same format, and return the path written, or None when nothing changed.

        media.compress("clip.mp4", preset="low")              # replaces clip.mp4 when smaller
        media.compress("a.heic", format="jpg")               # writes a.jpg and deletes a.heic

    `preset` is "best", "high", "mid" or "low" (`PRESETS`); `quality`, `resolution` (the shorter side's cap),
    `fps`, `audio_bitrate` and `codec` given directly replace the preset's value. The result is kept
    only when smaller. With `strip` (the default) EXIF, GPS, XMP, tags, chapters and extra streams
    are dropped; the picture stays upright. HEIC and lossless audio are left alone without
    `format=`. FLV, WMV, MPEG-PS and OGV become MP4. Videos are written as 8-bit yuv420p, so HDR is
    lost. `progress(done, total)` gets seconds of media; without it, an encode shows a log bar.
    """
    src = Path(src)
    settings = _settings(preset, quality, resolution, fps, audio_bitrate)
    info = probe(src)
    extension = _compress_target(info, src, dest, format)
    if extension is None:
        log.debug("{name}: media can't shrink this format without format=", name=src.name)
        return None
    _codec(extension, codec)
    out = Path(dest) if dest is not None else src.with_suffix(extension)
    if out != src and out.exists():
        raise FileExistsError(f"{out} already exists")
    # Without a `progress` callback, a video or audio encode shows its own bar; photos report nothing.
    shown = log.progress(src.name, total=info.duration) if progress is None and info.kind != "image" else nullcontext()
    with _atomic.writing(out, replace=out == src) as part, shown as bar:
        transcode(info, part, extension, quality=settings.quality, codec=codec, fps=settings.fps,
                  resolution=settings.photo_resolution if info.kind == "image" else settings.video_resolution,
                  speed=settings.speed, audio_bitrate=settings.audio_bitrate, strip=strip, copy=False,
                  progress=progress or (_reporter(bar) if bar else None))
        if part.stat().st_size >= info.size:
            part.unlink()
            log.info("{name}: compressing saved nothing; kept the original", name=src.name)
            return None
    if dest is None and out != src:
        src.unlink()
    return out


def _reporter(bar: Any) -> Progress:
    """A `progress(done, total)` callback that moves a `log.progress` row: the total first, then the count."""
    def report(done: float, total: float | None = None) -> None:
        if total is not None:
            bar.total = total
        bar.count = done
    return report


def check(path: PathLike) -> None:
    """Raise `BrokenError` when `path` is truncated, corrupt, or not media.

        media.check("clip.mp4")

    A photo is fully decoded. A video or audio file has every packet read without decoding,
    which catches what a cut-off download leaves behind, but not damage inside frames.
    """
    path = Path(path)
    if probe(path).kind == "image":
        _pillow.check(path)
    else:
        _ffmpeg.run(["-xerror", *_ffmpeg.source(path), "-map", "0", "-c", "copy", "-f", "null", "-"],
                    name=path.name, timeout=_ffmpeg.COPY_TIMEOUT)


def sniff(path: PathLike) -> str | None:
    """The extension the first bytes of `path` call for (".webp", ".mp4", ".html"), or None.

        media.sniff("download.part")     # needs neither Pillow nor ffmpeg

    `.html` and `.json` mean the server sent a page instead of media.
    """
    with Path(path).open("rb") as file:
        head = file.read(192)
    for prefix, extension in ((b"\xff\xd8\xff", ".jpg"), (b"\x89PNG\r\n\x1a\n", ".png"), (b"GIF87a", ".gif"),
                              (b"GIF89a", ".gif"), (b"II*\0", ".tif"), (b"MM\0*", ".tif"), (b"BM", ".bmp"),
                              (b"\0\0\1\0", ".ico"), (b"FLV", ".flv"), (b"fLaC", ".flac"), (b"ID3", ".mp3")):
        if head.startswith(prefix):
            return extension
    if head[:4] == b"RIFF":
        return {b"WEBP": ".webp", b"WAVE": ".wav", b"AVI ": ".avi"}.get(head[8:12])
    if head[4:8] == b"ftyp":
        return _brand(head)
    if head[:4] == b"\x1a\x45\xdf\xa3":
        return ".webm" if b"webm" in head[:64] else ".mkv"
    if head[:1] == b"G" and head[188:189] == b"G":
        return ".ts"
    if head[:4] == b"OggS":
        return next((ext for mark, ext in ((b"OpusHead", ".opus"), (b"\x01vorbis", ".ogg"), (b"\x80theora", ".ogv"))
                     if mark in head[:64]), None)
    if head[:4] == b"FORM" and head[8:12] in (b"AIFF", b"AIFC"):
        return ".aiff"
    if len(head) > 1 and head[0] == 0xFF and head[1] >= 0xE0:
        return ".mp3"
    text = head.removeprefix(b"\xef\xbb\xbf").lstrip()
    return {b"<": ".html", b"{": ".json", b"[": ".json"}.get(text[:1])


def _brand(head: bytes) -> str:
    brand = head[8:12]
    compatible = {head[index:index + 4] for index in range(16, min(int.from_bytes(head[:4], "big"), 64), 4)}
    if brand in (b"avif", b"avis") or (brand == b"mif1" and b"avif" in compatible):
        return ".avif"
    if brand in (b"heic", b"heix", b"heim", b"heis", b"hevc", b"hevx", b"mif1", b"msf1"):
        return ".heic"
    if brand == b"qt  ":
        return ".mov"
    if brand == b"M4A ":
        return ".m4a"
    return ".3gp" if brand in (b"3gp4", b"3gp5", b"3gp6") else ".mp4"


def transcode(info: Info, part: Path, extension: str, *, quality: int, codec: str | None = None,
              fps: float | None = None, resolution: int | None = None, speed: str = "medium",
              audio_bitrate: int | None = None, strip: bool = False, copy: bool = True,
              progress: Progress | None = None) -> None:
    """Write `info`'s file to `part` as `extension`, of the same kind (a video may become an animation)."""
    if info.kind == "image" and not info.animated:
        photo = _pillow.upright(_pillow.open(info.path) or _not_photo(info), keep=not strip)
        _pillow.save(_pillow.shrink(photo, resolution), part, STILLS[extension], quality=quality, optimize=not copy)
    elif info.kind == "image" and extension in ANIMATIONS:
        frames, delays = _pillow.animation(_pillow.open(info.path) or _not_photo(info), resolution=resolution, fps=fps)
        _pillow.save_animation(frames, delays, part, ANIMATIONS[extension], quality=quality)
    elif extension in ANIMATIONS:
        _video_to_animation(info, part, extension, quality=quality, fps=fps, resolution=resolution)
    else:
        inputs, feed = _inputs(info)
        # An animation reaches ffmpeg as GIF or raw frames, which no container should hold as they are.
        out, copied = output(extension, video=info.video or ("rawvideo" if info.kind == "image" else None),
                             audio=info.audio, quality=quality, codec=codec, fps=fps, source_fps=info.fps,
                             resolution=resolution, speed=speed, audio_bitrate=audio_bitrate, strip=strip,
                             copy=copy and info.kind != "image", created=info.tags.get("creation_time"))
        command = ["-n", *inputs, *out, _ffmpeg.target(part)]
        if copied:
            _ffmpeg.run(command, name=info.path.name, timeout=_ffmpeg.COPY_TIMEOUT)
        else:
            _ffmpeg.encode(command, name=info.path.name, duration=info.duration, progress=progress, feed=feed)


def output(extension: str, *, video: str | None, audio: str | None, quality: int, codec: str | None = None,
           fps: float | None = None, source_fps: float | None = None, resolution: int | None = None,
           speed: str = "medium", audio_bitrate: int | None = None, strip: bool = False, copy: bool = True,
           created: str | None = None, maps: tuple[str, str] = ("0:v:0", "0:a:0?"),
           simple: bool = True) -> tuple[list[str], bool]:
    """ffmpeg's output options for `extension`, and whether every stream is copied.

    `simple=False` leaves out `-vf`, for video that comes out of a `-filter_complex` graph.
    """
    box = CONTAINERS[extension]
    video = video if box.video else None
    args = [*(["-map", maps[0]] if video else []), *(["-map", maps[1]] if audio else [])]
    copy_video = copy and not (codec or resolution or fps) and holds(box.video_holds, video)
    copy_audio = copy and not audio_bitrate and holds(box.audio_holds, audio)
    if video:
        filters = ["-vf", _filters(resolution, fps, source_fps)] if simple else []
        args += ["-c:v", "copy"] if copy_video else [*filters, *_video_codec(extension, codec, quality, speed)]
    if audio:
        args += ["-c:a", "copy"] if copy_audio else _audio_codec(box.audio, audio_bitrate)
    if strip:
        args += ["-map_metadata", "-1", "-map_chapters", "-1", "-fflags", "+bitexact", "-flags:v", "+bitexact",
                 "-flags:a", "+bitexact", "-metadata:s", "encoder="]
    elif created:
        # ffmpeg drops creation_time when it copies metadata, so it is written back explicitly.
        args += ["-metadata", f"creation_time={created}"]
    return [*args, *muxer(extension)], (copy_video or not video) and (copy_audio or not audio)


def muxer(extension: str) -> list[str]:
    """The output format for `extension`; MP4 and MOV get their index first, so playback starts at once."""
    name = CONTAINERS[extension].muxer
    return [*(["-movflags", "+faststart"] if name in ("mp4", "mov", "ipod", "3gp") else []), "-f", name]


def _filters(resolution: int | None, fps: float | None, source_fps: float | None) -> str:
    # -2 keeps both sides even, which H.264 needs; the shorter side is capped, never scaled up.
    size = ("scale='if(gte(iw,ih),-2,min(iw,{0}))':'if(gte(iw,ih),min(ih,{0}),-2)'".format(resolution)
            if resolution else "scale=trunc(iw/2)*2:trunc(ih/2)*2")
    rate = f",fps={fps:g}" if fps and (source_fps is None or source_fps > fps) else ""
    return f"{size}{rate},setsar=1"


def _video_codec(extension: str, codec: str | None, quality: int, speed: str) -> list[str]:
    encoder = _ffmpeg.need(CODECS[codec] if codec else CONTAINERS[extension].video or "libx264")
    # One quality scale for every encoder: 70, 80, 90 give x264 CRF 28, 23, 19 and VP9 40, 34, 28.
    if encoder == "libvpx-vp9":
        return ["-c:v", encoder, "-crf", str(min(63, (410 - 3 * quality) // 5)), "-b:v", "0", "-row-mt", "1",
                "-deadline", "good", "-cpu-used", "2", "-pix_fmt", "yuv420p"]
    crf = min(51, (1190 - 9 * quality) // 20 + (4 if encoder == "libx265" else 0))
    tag = ["-tag:v", "hvc1"] if encoder == "libx265" and CONTAINERS[extension].muxer in ("mp4", "mov") else []
    return ["-c:v", encoder, "-crf", str(crf), "-preset", speed, "-pix_fmt", "yuv420p", *tag]


def _audio_codec(encoder: str, bitrate: int | None) -> list[str]:
    if encoder == "libvorbis" and encoder not in _ffmpeg.encoders():
        encoder = "libopus"  # Opus in Ogg, when this ffmpeg has no libvorbis
    _ffmpeg.need(encoder)
    if encoder in ("flac", "pcm_s16le", "pcm_s16be"):
        return ["-c:a", encoder]
    if encoder == "libvorbis" and not bitrate:
        return ["-c:a", encoder, "-q:a", "5"]
    return ["-c:a", encoder, "-b:a", str(bitrate or AUDIO_BITRATES.get(encoder, 128_000))]


def holds(codecs: str, codec: str | None) -> bool:
    return codec is None or codecs == "*" or codec in codecs.split()


def _inputs(info: Info) -> tuple[list[str], Any]:
    """ffmpeg's input options for `info`, and the raw frames to feed it for an animation it can't read."""
    if not (info.kind == "image" and info.format != "gif"):
        return _ffmpeg.source(info.path), None
    frames, delays = _pillow.animation(_pillow.open(info.path) or _not_photo(info))
    rate = 1000 * len(delays) / sum(delays)
    first = frames[0]
    return raw_input(first.width, first.height, rate), (frame.convert("RGB").tobytes() for frame in frames)


def raw_input(width: int, height: int, rate: float) -> list[str]:
    """Input options for raw RGB frames that media writes to ffmpeg's stdin itself."""
    return ["-f", "rawvideo", "-pix_fmt", "rgb24", "-s", f"{width}x{height}", "-r", f"{rate:g}",
            "-protocol_whitelist", "pipe", "-i", "pipe:0"]


def _video_to_animation(info: Info, part: Path, extension: str, *, quality: int, fps: float | None,
                        resolution: int | None) -> None:
    rate = min(fps or GIF_FPS, info.fps or GIF_FPS)
    if extension == ".gif":
        size = _filters(resolution, None, None).removesuffix(",setsar=1")
        graph = f"fps={rate:g},{size},split[a][b];[a]palettegen[p];[b][p]paletteuse"
        _ffmpeg.encode(["-n", *_ffmpeg.source(info.path), "-map", "0:v:0", "-vf", graph, "-f", "gif",
                        _ffmpeg.target(part)], name=info.path.name, duration=info.duration, progress=None)
        return
    width, height = info.width or 0, info.height or 0
    images = [_pillow.shrink(image, resolution) for image in decoded(info, f"fps={rate:g}", width, height)]
    _pillow.save_animation(images, [round(1000 / rate)] * len(images), part, ANIMATIONS[extension], quality=quality)


def decoded(info: Info, graph: str, width: int, height: int) -> Iterator[Any]:
    """The frames `graph` picks from a video, decoded by ffmpeg into Pillow images, upright."""
    Image = _pillow.load()
    command = [*_ffmpeg.source(info.path), "-map", "0:v:0", "-vf", f"{graph},scale={width}:{height},setsar=1",
               *_ffmpeg.passthrough(), "-f", "rawvideo", "-pix_fmt", "rgb24", "pipe:1"]
    for frame in _ffmpeg.frames_out(command, name=info.path.name, size=width * height * 3):
        yield Image.frombytes("RGB", (width, height), frame)


def writable(dest: Path) -> str:
    extension = dest.suffix.lower()
    if extension not in STILLS and extension not in CONTAINERS:
        raise ValueError(f"media can't write {extension or 'a file without an extension'} ({dest.name})")
    return extension


def kind_of(extension: str) -> str | None:
    """The kind of file an extension names: "image", "video" or "audio", or None when media doesn't know it.

        media.kind_of(".heic")     # "image"
        media.kind_of("M4A")       # "audio"
        media.kind_of(".zip")      # None

    Args:
        extension: a file extension, with or without its dot, in any case, such as ".jpg" or "MP4".

    Returns:
        "image" for a photo or an animation (GIF, animated WebP), "video" for a container that holds
        video, "audio" for one that holds sound only, or None for anything else. It reads only the
        name; the content may say otherwise, which `sniff` reads.
    """
    extension = "." + extension.lower().lstrip(".")
    if extension in STILLS:
        return "image"
    if extension in CONTAINERS:
        return "video" if CONTAINERS[extension].video else "audio"
    return READ_ONLY.get(extension)


def same_kind(info: Info, extension: str, verb: str) -> None:
    # `extension` passed `writable`, so media knows its kind; "" only keeps the checker sure of it.
    target = kind_of(extension) or ""
    name = info.path.name
    if info.kind == "image" and info.animated and target != "audio":
        if target == "image" and extension not in ANIMATIONS:
            raise ValueError(f"{name} is animated; {extension} can't hold animation; use media.video.frames")
        return
    if info.kind == "video" and extension in ANIMATIONS:
        return
    if info.kind != target:
        instead = {("video", "image"): "use media.video.frames to get photos",
                   ("image", "video"): "use media.video.merge to make a video",
                   ("video", "audio"): "use media.audio.extract"}.get((info.kind, target), f"{verb} keeps the kind")
        raise ValueError(f"{name} is a {_noun(info)}; {instead}")


def _noun(info: Info) -> str:
    return {"image": "photo", "video": "video", "audio": "audio file"}[info.kind]


def _not_photo(info: Info) -> Any:
    raise BrokenError(f"{info.path.name}: not a photo")


def _settings(preset: str, quality: int | None, resolution: int | None, fps: float | None,
              audio_bitrate: int | None) -> Preset:
    if preset not in PRESETS:
        raise ValueError(f"preset must be one of {', '.join(PRESETS)}, not {preset!r}")
    base = PRESETS[preset]
    for name, value, low, high in (("quality", quality, 1, 100), ("resolution", resolution, 16, 1 << 20),
                                   ("fps", fps, 1, 240), ("audio_bitrate", audio_bitrate, 32_000, 320_000)):
        check_range(name, value, low, high)
    return Preset(quality or base.quality, resolution or base.video_resolution, resolution or base.photo_resolution,
                  fps or base.fps, base.speed, audio_bitrate or base.audio_bitrate)


def check_range(name: str, value: float | None, low: float, high: float) -> None:
    if value is not None and not low <= value <= high:
        raise ValueError(f"{name} must be from {low:,} to {high:,}, not {value!r}")


def _codec(extension: str, codec: str | None) -> None:
    if codec is None:
        return
    if codec not in CODECS:
        raise ValueError(f"codec must be one of {', '.join(CODECS)}, not {codec!r}")
    allowed = CODECS_IN.get(extension, ("h264", "h265")) if extension in CONTAINERS else tuple(CODECS)
    if codec not in allowed:
        raise ValueError(f"{extension} can't hold {codec}; use {' or '.join(allowed)}")


def _compress_target(info: Info, src: Path, dest: PathLike | None, format: str | None) -> str | None:
    wanted = {f".{format.lower().lstrip('.')}"} if format else set()
    if dest is not None:
        wanted.add(Path(dest).suffix.lower())
    if len(wanted) > 1:
        raise ValueError(f"format={format!r} and dest={str(dest)!r} name different formats")
    if wanted:
        extension = writable(Path(f"x{wanted.pop()}"))
        same_kind(info, extension, "compress")
        return extension
    suffix = src.suffix.lower()
    if info.kind == "image":
        same = suffix if STILLS.get(suffix, "").lower() == info.format else None
        return same or {"jpeg": ".jpg", "tiff": ".tif", "jpeg2000": ".jp2"}.get(info.format) or (
            f".{info.format}" if f".{info.format}" in STILLS else None)
    if info.kind == "audio" and info.format in LOSSLESS_AUDIO:
        return None
    own = f".{info.format}"
    if own in CONTAINERS and kind_of(own) == info.kind:
        return suffix if suffix in CONTAINERS and CONTAINERS[suffix].muxer == CONTAINERS[own].muxer else own
    return ".mp4" if info.kind == "video" else ".m4a"


def parse_time(text: str | None) -> datetime | None:
    """An ISO 8601 `creation_time`, timezone-aware when it names one, or None."""
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")) if text else None
    except ValueError:
        return None


def parse_iso6709(text: str | None) -> tuple[float, float] | None:
    """(latitude, longitude) from an ISO 6709 string such as `+48.8577+002.2950/`, or None at 0, 0."""
    match = re.match(r"([+-]\d+(?:\.\d+)?)([+-]\d+(?:\.\d+)?)", text or "")
    if not match or float(match[1]) == float(match[2]) == 0:
        return None
    return float(match[1]), float(match[2])
