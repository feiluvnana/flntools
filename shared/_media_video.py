"""What `Media.merge` does: timeline operations.

Private: callers use `media`.

    merge([video], part, ".mp4", track, duration=None, progress=None)      # DASH: copied
"""

from __future__ import annotations

import tempfile
from pathlib import Path
from typing import Any

from shared import _ffmpeg, _media, _pillow
from shared._media import Info, Progress

__all__ = ["merge"]


def merge(infos: list[Info], part: Path, extension: str, track: Info | None, *,
          duration: float | None, progress: Progress | None) -> None:
    """Combine photos or clips into moving picture at `part`, as `extension`, with sound.

    `Media.merge` has checked the kinds and the arguments. Photos are letterboxed to the first
    one's size and shown `duration` seconds each (default 1.0). Clips sharing
    codecs, size and frame rate are copied; others are encoded to the first clip's size. One clip
    with a track is DASH: both streams copied as they are. The inputs are never changed.

    Args:
        infos: the probes of the photos or the clips, in play order; the first one sets the size.
        part: the temporary file to write; the caller renames it into place.
        extension: the format to write: a video container, or an animated ".gif", ".webp" or ".png".
        track: the probe of a soundtrack to add, or None for no added sound.
        duration: seconds each photo shows; None for 1.0 seconds.
        progress: `progress(done, total)` in seconds, called while an encode runs; None for none.
    """
    animation = extension in _media.STILLS
    kinds = {info.kind for info in infos}
    if kinds == {"image"}:
        _slideshow(infos, part, extension, 1 / (duration or 1.0), track, progress)
    elif animation:
        # The clips are joined into one file first, beside `part`, then turned into the animation.
        with tempfile.TemporaryDirectory(dir=part.parent) as folder:
            joined = Path(folder) / "joined.mkv"
            _join(infos, joined, ".mkv", None, progress)
            _media._transcode(_media._probe(joined), part, extension, quality=90)
    elif len(infos) == 1 and track is not None:
        _dash(infos[0], track, part, extension)
    else:
        _join(infos, part, extension, track, progress)


def _slideshow(infos: list[Info], part: Path, extension: str, rate: float, track: Info | None,
               progress: Progress | None) -> None:
    """Letterbox each photo onto first size and write as animation or video at `rate`."""
    from PIL import ImageOps

    width, height = _pillow.upright(_photo(infos[0]), keep=False).size
    # H.264 needs even sides, so size is trimmed to even for video; animation takes any size.
    if extension not in _media.ANIMATIONS:
        width, height = width - width % 2, height - height % 2
    pictures = (
        ImageOps.pad(_pillow.upright(_photo(info), keep=False).convert("RGB"), (width, height),
                     color="black") for info in infos
    )
    if extension in _media.ANIMATIONS:
        _pillow.save_animation(list(pictures), [round(1000 / rate)] * len(infos), part,
                               _media.ANIMATIONS[extension], quality=90)
        return
    encoding = _media._Output(video="rawvideo", audio=track.audio if track else None,
                              codec=_default_codec(extension), maps=("0:v:0", "1:a:0"))
    args, _ = _media._output(extension, encoding)
    track_in = _ffmpeg.source(track.path) if track else []
    inputs = [*_media._raw_input(width, height, rate), *track_in]
    _ffmpeg.encode(["-n", *inputs, *args, *(["-shortest"] if track else []), _ffmpeg.target(part)],
                   name=infos[0].path.name, duration=len(infos) / rate, progress=progress,
                   feed=(picture.tobytes() for picture in pictures))


def _dash(video: Info, track: Info, part: Path, extension: str) -> None:
    """Join video and separate audio into container, copying both streams as they are."""
    # Streams copied as they are; ffmpeg reads every packet, so truncated part fails here.
    cmd = [
        "-n", "-xerror", *_ffmpeg.source(video.path), *_ffmpeg.source(track.path),
        "-map", "0:v:0", "-map", "1:a:0", "-c", "copy",
        *_media._muxer(extension), _ffmpeg.target(part),
    ]
    _ffmpeg.run(cmd, name=video.path.name, timeout=_ffmpeg.COPY_TIMEOUT)


def _join(
    infos: list[Info], part: Path, extension: str, track: Info | None, progress: Progress | None
) -> None:
    """Concatenate clips into `part`, copying when matching, else encoding to first size."""
    box = _media.CONTAINERS[extension]
    first = infos[0]
    # Clips can be copied only when codecs, size and rate match, so concat joins packets.
    props = {(info.video, info.width, info.height, info.fps, info.audio) for info in infos}
    alike = len(props) == 1
    holds_v = _media._holds(box.video_holds, first.video)
    holds_a = _media._holds(box.audio_holds, first.audio)
    if alike and holds_v and holds_a:
        _concat_copy(infos, part, extension, track)
        return
    # Even sides, as H.264 needs; the first clip's size is the one every other clip is fitted to.
    width, height = (first.width or 2) // 2 * 2, (first.height or 2) // 2 * 2
    rate = first.fps or 30.0
    sound = track is None and any(info.audio for info in infos)
    graph = []
    for index, info in enumerate(infos):
        # Fitted inside frame and padded with black, at one shared frame rate.
        graph.append(
            f"[{index}:v:0]scale={width}:{height}:force_original_aspect_ratio=decrease,"
            f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2,setsar=1,fps={rate:g},"
            f"format=yuv420p[v{index}]"
        )
        if sound:  # clip without audio gets silence, so sound stays in sync
            # Clip sound resampled to 48 kHz stereo, so concat joins streams of one format.
            if info.audio:
                graph.append(
                    f"[{index}:a:0]aresample=48000,aformat=channel_layouts=stereo[a{index}]"
                )
            else:
                dur = info.duration or 0
                graph.append(f"anullsrc=r=48000:cl=stereo,atrim=duration={dur:g}[a{index}]")
    pads = "".join(f"[v{index}]" + (f"[a{index}]" if sound else "") for index in range(len(infos)))
    graph.append(f"{pads}concat=n={len(infos)}:v=1:a={int(sound)}[v]" + ("[a]" if sound else ""))
    encoding = _media._Output(
        video="filtered", audio="filtered" if sound or track else None,
        codec=_default_codec(extension), copy=False, simple=False,
        maps=("[v]", "[a]" if sound else f"{len(infos)}:a:0"))
    args, _ = _media._output(extension, encoding)
    all_infos = [*infos, *([track] if track else [])]
    inputs = [arg for info in all_infos for arg in _ffmpeg.source(info.path)]
    shortest = ["-shortest"] if track else []
    cmd = [
        "-n", *inputs, "-filter_complex", ";".join(graph), *args,
        *shortest, _ffmpeg.target(part),
    ]
    tot_dur = sum(info.duration or 0 for info in infos)
    _ffmpeg.encode(cmd, name=infos[0].path.name, duration=tot_dur, progress=progress)


def _concat_copy(infos: list[Info], part: Path, extension: str, track: Info | None) -> None:
    """Join the clips by copying their packets, through a concat list written here."""
    # The concat reader is refused for anything from outside, because a list can name any file.
    # This list is written here, naming only clips probe has accepted.
    with tempfile.TemporaryDirectory() as folder:
        listing = Path(folder) / "list.ffconcat"
        quoted = ("'" + _ffmpeg.target(info.path).replace("'", "'\\''") + "'" for info in infos)
        content = "ffconcat version 1.0\n" + "\n".join(f"file {line}" for line in quoted) + "\n"
        listing.write_text(content, encoding="utf-8")
        track_in = _ffmpeg.source(track.path) if track else []
        inputs = [
            "-protocol_whitelist", "file", "-format_whitelist", f"concat,{_ffmpeg.READERS}",
            "-f", "concat", "-safe", "0", "-i", _ffmpeg.target(listing), *track_in,
        ]
        maps = ["-map", "0:v:0", *(["-map", "1:a:0"] if track else ["-map", "0:a:0?"])]
        shortest = ["-shortest"] if track else []
        cmd = [
            "-n", *inputs, *maps, "-c", "copy", *shortest,
            *_media._muxer(extension), _ffmpeg.target(part),
        ]
        _ffmpeg.run(cmd, name=infos[0].path.name, timeout=_ffmpeg.COPY_TIMEOUT)


def _default_codec(extension: str) -> str:
    return "vp9" if _media.CONTAINERS[extension].video == "libvpx-vp9" else "h264"


def _photo(info: Info) -> Any:
    """The opened Pillow image of `info`'s file; BrokenError when not a photo."""
    photo = _pillow.open(info.path)
    if photo is None:
        raise _media.BrokenError(f"{info.path.name}: not a photo")
    return photo
