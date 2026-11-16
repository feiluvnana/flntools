"""Media probes and ffmpeg encodes: what a photo, video or song holds, and progress as it encodes.

A library module: it imports only the stdlib, Pillow (lazily), the tool runner (`flnd._tools`)
and flnd's error classes. It offers:
- `probe(path)`: a `Probe` with the file's dimensions, frame count, duration and codecs,
  cached per path, size and modified time;
- `encoders()`: the encoders this ffmpeg build has, asked once per process;
- `encode(args, duration=…, progress=…, stop=…)`: one ffmpeg run that reports the fraction
  done to a callback and can be stopped from another thread.

Who calls it in flnd: the compress step (`flnd._shrink`) asks `probe` what a file holds before
it plans an encode, and again after, to check the result kept its dimensions, frames and
duration; it runs ffmpeg through `encode`.

How a probe goes:
1. The cache answers when it has the file at the same path, size and modified time, so a
   file is probed once per process however many steps ask.
2. A photo (by its suffix) is opened by Pillow, which reads only its header: no process.
3. Anything else costs one `ffprobe -show_streams -show_format -of json` (about 16 ms).

`encode` runs ffmpeg with `-progress pipe:1`, which prints `out_time_us=` lines as it goes; the
fraction of the input's duration done so far goes to the caller's `progress` callback. A
`stop` event set meanwhile kills ffmpeg at its next line.

Rules a change must keep:
- Pillow is imported inside the functions that need it, so `flnd --help` never loads it;
- the cache is shared by every thread, so it is read and written under `_lock`;
- every ffmpeg call takes an argument list, with `-nostdin` so it never waits on a terminal.
"""

from __future__ import annotations

import json
import threading
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from flnd import _tools
from flnd.errors import ToolError

__all__ = ["IMAGES", "Probe", "encode", "encoders", "probe"]

# The suffixes Pillow probes in-process. Every other file is ffprobe's.
IMAGES = frozenset({".jpg", ".jpeg", ".png", ".gif", ".webp", ".avif", ".bmp", ".tif", ".tiff"})
# How many probes the cache keeps; a run touches each file a few times, close together.
CACHE_SIZE = 4096
# `ffmpeg -progress` reports the time encoded so far in microseconds.
PROGRESS_KEY = "out_time_us="


@dataclass(frozen=True)
class Probe:
    """What a media file holds.

    Args:
        width: the picture's width in pixels; 0 for sound alone.
        height: its height in pixels; 0 for sound alone.
        frames: how many frames: 1 for a still photo, more for an animation; 0 when unknown
            (ffprobe counts a video's frames only when its container says).
        duration: its length in seconds; 0.0 for a still photo.
        video: the video or image codec (`h264`, `JPEG`), or "" for none.
        audio: the audio codec (`aac`, `opus`), or "" for none.
        bits: the video's bit depth per channel (8 or 10); 8 when unknown.
    """

    width: int = 0
    height: int = 0
    frames: int = 0
    duration: float = 0.0
    video: str = ""
    audio: str = ""
    bits: int = 8


_cache: dict[tuple[str, int, int], Probe] = {}
_lock = threading.Lock()
_encoders: set[str] | None = None


def probe(path: Path) -> Probe:
    """What `path` holds, from the cache, Pillow or ffprobe (the module's steps).

    Args:
        path: a saved file.

    Returns:
        Its dimensions, frames, duration and codecs.

    Raises:
        ToolError: the file can't be read as media (ffprobe or Pillow refused it), or a video
            needs ffprobe and it is not installed (`MissingError`).
    """
    found = path.stat()
    key = (str(path), found.st_size, found.st_mtime_ns)
    with _lock:
        known = _cache.get(key)
    if known is not None:
        return known
    made = _image(path) if path.suffix.lower() in IMAGES else _ffprobe(path)
    with _lock:
        if len(_cache) >= CACHE_SIZE:
            _cache.clear()
        _cache[key] = made
    return made


def encoders() -> set[str]:
    """The encoders this ffmpeg has (`libsvtav1`, `libx265`, `libopus`), asked once per process.

    Returns:
        Their names; empty when ffmpeg is not installed.
    """
    global _encoders
    with _lock:
        if _encoders is not None:
            return _encoders
    try:
        listed = _tools.ffmpeg().run("-hide_banner", "-encoders").out
    except ToolError:
        names: set[str] = set()
    else:
        # Each line is `<flags> <name> <description>`, the flags a run of letters and dots.
        names = {parts[1] for line in listed.splitlines()
                 if len(parts := line.split()) > 1 and set(parts[0]) <= set("VASFXBD.")}
    with _lock:
        _encoders = names
    return names


def encode(
    args: list[str], *, duration: float, progress: Callable[[float], None] | None = None,
    stop: threading.Event | None = None,
) -> None:
    """Run ffmpeg with `args` (inputs, options, output), reporting the fraction done.

    Args:
        args: ffmpeg's arguments after its own global ones; paths are passed whole.
        duration: the input's length in seconds, which `out_time_us` is a fraction of; 0
            reports nothing.
        progress: called with 0.0 to 1.0 as ffmpeg goes; None reports nothing.
        stop: when set, ffmpeg is killed at its next progress line and `KeyboardInterrupt`
            raised.

    Raises:
        MissingError: ffmpeg is not installed.
        RunError: ffmpeg failed, quoting its last lines.
        KeyboardInterrupt: `stop` was set.
    """
    lines = _tools.ffmpeg().stream(
        "-nostdin", "-hide_banner", "-loglevel", "error", "-nostats", "-progress", "pipe:1",
        "-y", *args,
    )
    try:
        for line in lines:
            if stop is not None and stop.is_set():
                raise KeyboardInterrupt
            if progress is not None and duration > 0 and line.startswith(PROGRESS_KEY):
                done = line[len(PROGRESS_KEY):]
                if done.isdigit():
                    progress(min(1.0, int(done) / 1e6 / duration))
    finally:
        # Leaving early (a stop, a callback's error) kills ffmpeg rather than leaving it running.
        lines.close()


def _image(path: Path) -> Probe:
    """A photo's probe from its header, through Pillow.

    Args:
        path: the photo.

    Returns:
        Its dimensions, frame count and format name.

    Raises:
        ToolError: Pillow can't read it.
    """
    from PIL import Image, UnidentifiedImageError

    try:
        with Image.open(path) as image:
            return Probe(width=image.width, height=image.height,
                         frames=getattr(image, "n_frames", 1), video=image.format or "")
    except (UnidentifiedImageError, OSError, ValueError) as error:
        raise ToolError(f"{path.name}: not an image Pillow can read ({error})") from error


def _ffprobe(path: Path) -> Probe:
    """A video's or song's probe from one ffprobe run.

    The cover art some songs carry is a video stream marked `attached_pic`; it is skipped, so
    a song probes as sound alone.

    Args:
        path: the video or song.

    Returns:
        Its first real video stream's size, frames and codec, its first audio codec, and the
        container's duration.

    Raises:
        MissingError: ffprobe is not installed.
        ToolError: ffprobe can't read the file.
    """
    out = _tools.ffprobe().run(
        "-v", "error", "-show_streams", "-show_format", "-of", "json", "--", str(path),
    ).out
    try:
        data = json.loads(out)
    except ValueError as error:
        raise ToolError(f"{path.name}: ffprobe gave no JSON") from error
    streams = data.get("streams") or []
    video = next((s for s in streams if s.get("codec_type") == "video"
                  and not (s.get("disposition") or {}).get("attached_pic")), {})
    audio = next((s for s in streams if s.get("codec_type") == "audio"), {})
    return Probe(
        width=int(video.get("width") or 0),
        height=int(video.get("height") or 0),
        frames=int(video.get("nb_frames") or 0) if str(video.get("nb_frames", "")).isdigit()
        else 0,
        duration=_seconds((data.get("format") or {}).get("duration")),
        video=str(video.get("codec_name") or ""),
        audio=str(audio.get("codec_name") or ""),
        bits=int(video.get("bits_per_raw_sample") or 8)
        if str(video.get("bits_per_raw_sample", "")).isdigit() else 8,
    )


def _seconds(value: object) -> float:
    """A duration ffprobe printed (`"12.345000"`), or 0.0 when it printed none."""
    try:
        return float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return 0.0
