"""What `Media.trim`, `Media.frames` and `Media.merge` do: timeline operations.

Private: callers use `media`.

`Media` checks the kind and the arguments that need only the probe record, then calls one of these
with the record and the chain's temporary path (`frames` gets the folder to write into instead).
Video goes through ffmpeg, animations and photos through Pillow, both reached only through
`_ffmpeg` and `_pillow`, so there is no `.raw`. Loaded on first use.

    trim(info, part, ".mp4", start=12.5, end=30, exact=True)
    merge([video], part, ".mp4", track, fps=None, duration=None, progress=None)      # DASH: copied
"""

from __future__ import annotations

import math
import os
import secrets
import shutil
import tempfile
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from shared import _ffmpeg, _media, _pillow
from shared._media import Info, Progress

__all__ = ["frames", "merge", "trim"]


def trim(
    info: Info, part: Path, extension: str, *, start: float, end: float | None, exact: bool
) -> None:
    """Write the part of `info`'s file from `start` to `end` seconds to `part` as `extension`.

    `exact` re-encodes the video at the "high" level's quality, so the cut is exactly at `start`;
    otherwise streams are copied and a video starts at the keyframe before it. Metadata is kept.

    Args:
        info: the probe of the file to cut.
        part: the temporary file to write; the caller renames it into place.
        extension: the format to write, with its dot, of the same kind as `info`, or an animation.
        start: seconds from the beginning, 0 or more.
        end: seconds from the beginning, after `start`; None runs to the end.
        exact: True to cut exactly at `start` by re-encoding; False to copy the streams.
    """
    # An animated photo has frames, not a timeline of streams, so Pillow cuts its frame list.
    if info.kind == "image":
        frames, delays = _pillow.animation(_photo(info), start=start, end=end)
        _pillow.save_animation(frames, delays, part, _media.ANIMATIONS[extension], quality=90)
        return
    # `-ss` seeks to keyframe at or before `start`; a re-encode drops frames before it.
    window = ["-ss", f"{start:g}", *(["-t", f"{end - start:g}"] if end is not None else [])]
    encoding = _media.Encoding(
        video=info.video, audio=info.audio, speed="slow",
        codec=_default_codec(extension) if exact and info.video else None,
        created=info.tags.get("creation_time"))
    args, copied = _media.output(extension, encoding)
    command = ["-n", *window, *_ffmpeg.source(info.path), *args, _ffmpeg.target(part)]
    if copied:
        _ffmpeg.run(command, name=info.path.name, timeout=_ffmpeg.COPY_TIMEOUT)
    else:
        dur = (end or info.duration or 0) - start
        _ffmpeg.encode(command, name=info.path.name, duration=dur, progress=None)


def frames(info: Info, stem: str, folder: Path, *, every: float | None, at: Sequence[float] | None,
           count: int | None, format: str, quality: int, limit: int | None) -> list[Path]:
    """Save chosen frames of video or animation into `folder`; see `Media.frames`.

    Args:
        info: the probe of the video or animation to read.
        stem: the file name the frames are named after, without its extension.
        folder: the folder to write into; made when missing.
        every: seconds between frames, from the start; None when `at` or `count` chooses them.
        at: the times to take, in seconds; None when `every` or `count` chooses them.
        count: how many frames, spread evenly through the file; None when `every` or `at` chooses.
        format: the photo format, such as "jpg" or "png", with or without its dot.
        quality: 1-100, for a lossy format.
        limit: the most frames allowed, checked before anything is written; None for no limit.

    Returns:
        The paths written, in time order.

    Raises:
        ValueError: more than one of `every`, `at` and `count`; a non-positive `every` or `count`;
            a time outside the file; an unknown `format`; or more frames than `limit`.
        FileExistsError: a frame's name is taken, checked before anything is written.
    """
    if sum(choice is not None for choice in (every, at, count)) > 1:
        raise ValueError("give at most one of every=, at= and count=")
    if (every is not None and every <= 0) or (count is not None and count < 1):
        raise ValueError("every= must be above 0, and count= at least 1")
    extension = f".{format.lower().lstrip('.')}"
    if extension not in _media.STILLS:
        raise ValueError(f"media can't write frames as {format!r}")
    duration = info.duration or 0.0
    # With `count`, each frame sits in middle of one of `count` equal slices.
    times = sorted(at) if at is not None else (
        [(index + 0.5) * duration / count for index in range(count)] if count else None)
    if times and (times[0] < 0 or times[-1] > duration):
        raise ValueError(f"at= must be within 0 and {duration:.3f} s")
    # A stream with no frame rate is counted at 30 fps, the usual default.
    rate = info.fps or 30.0
    # 1e-9 tolerance keeps float error in duration / every from adding extra frame.
    expected = (
        len(times) if times
        else math.ceil(duration / every - 1e-9) if every
        else round(duration * rate)
    )
    if limit is not None and expected > limit:
        msg = f"{expected} frames exceeds limit={limit}; pass larger limit= or limit=None"
        raise ValueError(msg)

    def name(seconds: float) -> Path:
        return folder / f"{stem}_{seconds:09.3f}{extension}"

    predicted = [name(seconds) for seconds in times] if times else [
        name(index * (every or 1 / rate)) for index in range(expected)]
    if taken := [path for path in predicted if path.exists()]:
        raise FileExistsError(f"{taken[0]} already exists")
    pictures = _pictures(info, times, every, rate)
    return _save_frames(pictures, folder, extension, quality, name)


def _pictures(info: Info, times: list[float] | None, every: float | None, rate: float) -> Any:
    """(seconds, Pillow image) pairs for the frames chosen, in time order.

    Args:
        info: the probe of the video or animation.
        times: the times to take, in seconds, or None for a step of `every` (or every frame).
        every: seconds between frames, or None.
        rate: the frames per second to pick from, the source's or 30.

    Yields:
        One `(seconds, RGB image)` pair per frame chosen.
    """
    if info.kind == "image":
        photo = _photo(info)
        starts, clock = [], 0.0
        for index, delay in _pillow.durations(photo):
            starts.append((clock, index))
            clock += delay / 1000
        wanted = times if times is not None else _steps(clock, every) if every else None
        # Each time picks the frame on screen then; with no selector, every frame at its own start.
        picks = starts if wanted is None else [
            (seconds, max(index for start, index in starts if start <= seconds + 1e-6))
            for seconds in wanted
        ]
        for seconds, index in picks:
            photo.seek(index)
            yield seconds, photo.convert("RGB")
        return
    width, height = info.width or 0, info.height or 0
    if times is None:
        step = every or 1 / rate
        graph = f"fps=1/{every:g}" if every else f"fps={rate:g}"
        for index, picture in enumerate(_media.decoded(info, graph, width, height)):
            yield index * step, picture
        return
    # Each time picks frame shown then; frames decoded once with ffmpeg's select filter.
    # The 1e-6 nudge lands a time that float error leaves just under a frame boundary on that frame.
    wanted = sorted({math.floor(t * rate + 1e-6) for t in times})
    picked = dict(zip(wanted, _media.decoded(
        info, f"fps={rate:g},select='{'+'.join(f'eq(n,{n})' for n in wanted)}'", width, height)))
    for seconds in times:
        if (picture := picked.get(math.floor(seconds * rate + 1e-6))) is not None:
            yield seconds, picture


def _steps(duration: float, every: float) -> list[float]:
    """The start times of each `every`-second step through `duration`, the first at 0."""
    return [index * every for index in range(math.ceil(duration / every - 1e-9))]


def _save_frames(
    pictures: Any, folder: Path, extension: str, quality: int, name: Any
) -> list[Path]:
    """Write pictures into `folder` under `name`, through a hidden folder for safety.

    Args:
        pictures: `(seconds, image)` pairs, in time order.
        folder: the folder to finish in; made when missing.
        extension: the photo extension, with its dot.
        quality: 1-100, for a lossy format.
        name: maps a time to the final path of its frame.

    Returns:
        The final paths, in order.

    Raises:
        FileExistsError: a final name is taken by the time the frames are moved in; the frames
            already moved are left, the rest are removed with the hidden folder.
    """
    folder.mkdir(parents=True, exist_ok=True)
    # Frames go to a hidden folder next to `dest` first, so a failure leaves no half set behind.
    work = folder.parent / f".{folder.name}.{secrets.token_hex(4)}.part"
    work.mkdir()
    try:
        saved: list[tuple[Path, Path]] = []
        for seconds, picture in pictures:
            final = name(seconds)
            _pillow.save(picture, work / final.name, _media.STILLS[extension], quality=quality)
            saved.append((work / final.name, final))
        # Checked just before moves: a file may have appeared while frames were decoded.
        for part, final in saved:
            if final.exists():
                raise FileExistsError(f"{final} already exists")
            os.replace(part, final)
        return [final for _, final in saved]
    finally:
        shutil.rmtree(work, ignore_errors=True)


def merge(infos: list[Info], part: Path, extension: str, track: Info | None, *, fps: float | None,
          duration: float | None, progress: Progress | None) -> None:
    """Combine photos or clips into moving picture at `part`, as `extension`, with sound.

    `Media.merge` has checked the kinds and the arguments. Photos are letterboxed to the first
    one's size and shown `duration` seconds each (default 1.0), or played at `fps`. Clips sharing
    codecs, size and frame rate are copied; others are encoded to the first clip's size. One clip
    with a track is DASH: both streams copied as they are. The inputs are never changed.

    Args:
        infos: the probes of the photos or the clips, in play order; the first one sets the size.
        part: the temporary file to write; the caller renames it into place.
        extension: the format to write: a video container, or an animated ".gif", ".webp" or ".png".
        track: the probe of a soundtrack to add, or None for no added sound.
        fps: the rate photos play at, one per frame; None when `duration` is used instead.
        duration: seconds each photo shows; None for 1.0 seconds, or when `fps` is used.
        progress: `progress(done, total)` in seconds, called while an encode runs; None for none.
    """
    animation = extension in _media.STILLS
    kinds = {info.kind for info in infos}
    if kinds == {"image"}:
        _slideshow(infos, part, extension, fps or 1 / (duration or 1.0), track, progress)
    elif animation:
        # The clips are joined into one file first, beside `part`, then turned into the animation.
        with tempfile.TemporaryDirectory(dir=part.parent) as folder:
            joined = Path(folder) / "joined.mkv"
            _join(infos, joined, ".mkv", None, progress)
            _media.transcode(_media._probe(joined), part, extension, quality=90, fps=fps)
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
    encoding = _media.Encoding(video="rawvideo", audio=track.audio if track else None,
                               codec=_default_codec(extension), maps=("0:v:0", "1:a:0"))
    args, _ = _media.output(extension, encoding)
    track_in = _ffmpeg.source(track.path) if track else []
    inputs = [*_media.raw_input(width, height, rate), *track_in]
    _ffmpeg.encode(["-n", *inputs, *args, *(["-shortest"] if track else []), _ffmpeg.target(part)],
                   name=part.name, duration=len(infos) / rate, progress=progress,
                   feed=(picture.tobytes() for picture in pictures))


def _dash(video: Info, track: Info, part: Path, extension: str) -> None:
    """Join video and separate audio into container, copying both streams as they are."""
    # Streams copied as they are; ffmpeg reads every packet, so truncated part fails here.
    cmd = [
        "-n", "-xerror", *_ffmpeg.source(video.path), *_ffmpeg.source(track.path),
        "-map", "0:v:0", "-map", "1:a:0", "-c", "copy",
        *_media.muxer(extension), _ffmpeg.target(part),
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
    holds_v = _media.holds(box.video_holds, first.video)
    holds_a = _media.holds(box.audio_holds, first.audio)
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
    encoding = _media.Encoding(
        video="filtered", audio="filtered" if sound or track else None,
        codec=_default_codec(extension), copy=False, simple=False,
        maps=("[v]", "[a]" if sound else f"{len(infos)}:a:0"))
    args, _ = _media.output(extension, encoding)
    all_infos = [*infos, *([track] if track else [])]
    inputs = [arg for info in all_infos for arg in _ffmpeg.source(info.path)]
    shortest = ["-shortest"] if track else []
    cmd = [
        "-n", *inputs, "-filter_complex", ";".join(graph), *args,
        *shortest, _ffmpeg.target(part),
    ]
    tot_dur = sum(info.duration or 0 for info in infos)
    _ffmpeg.encode(cmd, name=part.name, duration=tot_dur, progress=progress)


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
            *_media.muxer(extension), _ffmpeg.target(part),
        ]
        _ffmpeg.run(cmd, name=part.name, timeout=_ffmpeg.COPY_TIMEOUT)


def _default_codec(extension: str) -> str:
    return "vp9" if _media.CONTAINERS[extension].video == "libvpx-vp9" else "h264"


def _photo(info: Info) -> Any:
    """The opened Pillow image of `info`'s file; BrokenError when not a photo."""
    photo = _pillow.open(info.path)
    if photo is None:
        raise _media.BrokenError(f"{info.path.name}: not a photo")
    return photo
