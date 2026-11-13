"""Find and run the ffmpeg program for shared.media, safely. Private: callers use `media`.

It wraps the ffmpeg program, found through imageio-ffmpeg when that is installed and else on PATH,
and runs it as a subprocess, so there is no library object and no `.raw`. Every command is built
here or in media's files from fixed options and the caller's paths. Inputs are `file:` + an
absolute path, behind a protocol and format whitelist, so a hostile download can't make ffmpeg
read another file, a playlist or a URL. Each run is one of three shapes: `run` waits for the end
and returns stderr, `encode` reports progress, and `frames_out` yields raw frames from stdout.

    run(["-xerror", *source(path), "-map", "0", "-c", "copy", "-f", "null", "-"], name=path.name, timeout=COPY_TIMEOUT)
"""

from __future__ import annotations

import functools
import os
import re
import shutil
import subprocess
import tempfile
import threading
from collections.abc import Callable, Iterable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import IO, Any

from shared import _media

# Private: what `media`'s files call.
__all__ = ["encode", "encoders", "exe", "failure", "ffmetadata", "found", "frames_out", "need", "parse_version",
           "passthrough", "probe", "run", "source", "summary", "target"]

# Readers that never open another file or URL. hls, dash, concat, image2 and the *_pipe readers are left out.
READERS = "mov,matroska,avi,mpegts,mpeg,flv,asf,ogg,mp3,aac,flac,wav,aiff,caf,amr,gif"
# The oldest build the options here work on; `found` refuses an older one by its printed version.
MIN_VERSION = (4, 4)
# Reading a header or listing encoders is normally quick, so 30 s only catches a hung ffmpeg.
PROBE_TIMEOUT = 30.0
# A stream copy reads the whole file, so it gets ten minutes, which `media.check` documents.
COPY_TIMEOUT = 600.0
# An encode writes progress about twice a second; a minute with none means ffmpeg has stopped.
STALL = 60.0
NO_FFMPEG = ("shared.media needs ffmpeg for videos and audio: pip install 'flntools[media]', "
             "or install ffmpeg on your PATH")
CHANNELS = {"mono": 1, "stereo": 2, "2.1": 3, "quad": 4, "5.0": 5, "5.1": 6, "6.1": 7, "7.1": 8}


def exe() -> str:
    """The ffmpeg to run: imageio-ffmpeg's (which honors IMAGEIO_FFMPEG_EXE), else the one on PATH."""
    return found()[0]


@functools.cache
def found() -> tuple[str, tuple[int, int] | None]:
    """The ffmpeg path and its version, looked up once; a failed lookup is tried again on the next call."""
    path = None
    # imageio-ffmpeg ships its own binary; it raises RuntimeError when there's none for this platform,
    # and then the ffmpeg on PATH is the fallback, so that error is not an error here.
    try:
        import imageio_ffmpeg

        path = imageio_ffmpeg.get_ffmpeg_exe()
    except (ImportError, RuntimeError):
        pass
    path = path or shutil.which("ffmpeg")
    if not path:
        raise _media.MissingError(NO_FFMPEG)
    text = subprocess.run([path, "-hide_banner", "-version"], capture_output=True, text=True, timeout=PROBE_TIMEOUT,
                          env=_env()).stdout
    # A git or other build that prints no version number is accepted: its `None` skips the check.
    if (version := parse_version(text)) and version < MIN_VERSION:
        raise _media.MissingError(f"ffmpeg {'.'.join(map(str, version))} is too old; "
                                  "shared.media needs 4.4 or later")
    return path, version


def parse_version(text: str) -> tuple[int, int] | None:
    """(major, minor) from `ffmpeg -version`, or None for a build that doesn't say, such as a git one."""
    match = re.search(r"ffmpeg version n?(\d+)\.(\d+)", text)
    return (int(match[1]), int(match[2])) if match else None


@functools.cache
def encoders() -> frozenset[str]:
    """The names of every encoder this ffmpeg build has, looked up once; `need` checks one of them."""
    text = subprocess.run([exe(), "-hide_banner", "-encoders"], capture_output=True, text=True,
                          timeout=PROBE_TIMEOUT, env=_env()).stdout
    # Each encoder line starts with a six-character flag column (type V, A or S, then frame, slice,
    # experimental, band and direct-rendering flags) and then its name; matching the column keeps
    # a description line from passing as an encoder.
    return frozenset(re.findall(r"^ [VAS][F.][S.][X.][B.][D.] (\S+)", text, re.MULTILINE))


def need(encoder: str) -> str:
    """`encoder`, or a `media.MissingError` when this ffmpeg was built without it."""
    if encoder not in encoders():
        raise _media.MissingError(f"this ffmpeg has no {encoder} encoder; install a full ffmpeg build, "
                           "or pip install 'flntools[media]' for one")
    return encoder


def passthrough() -> list[str]:
    """Keep frames as they come, with no duplicates: `-vsync` was renamed `-fps_mode` in ffmpeg 5.1."""
    version = found()[1]
    return ["-vsync", "0"] if version and version < (5, 1) else ["-fps_mode", "passthrough"]


def source(path: Path) -> list[str]:
    """The input options for a file from outside: whitelisted readers, local files only."""
    return ["-protocol_whitelist", "file", "-format_whitelist", READERS, "-i", target(path)]


def target(path: Path) -> str:
    """The ffmpeg input or output name for `path`: `file:` plus its absolute path, so nothing else is named.

        target(Path("clip.mp4"))    # "file:/abs/path/clip.mp4"
    """
    # file: + an absolute path can't start with "-" or name another protocol.
    return f"file:{Path(path).resolve()}"


def run(args: list[str], *, name: str, timeout: float, level: str = "error") -> str:
    """Run ffmpeg to the end and return its stderr; a failure raises the matching media error.

    Args:
        args: ffmpeg's arguments after the fixed `-hide_banner -nostdin -v LEVEL`; `source` and
            `target` name the files.
        name: the file's name, for messages.
        timeout: seconds for the whole run, the limit for a stream copy.
        level: ffmpeg's `-v` log level; "error" keeps stderr to what went wrong.

    Returns:
        ffmpeg's stderr, decoded with replacement characters.

    Raises:
        TimeoutError: the run took longer than `timeout`; ffmpeg is killed by `subprocess`.
        UnsafeError, RuntimeError, BrokenError: ffmpeg failed, as `failure` sorts it.
    """
    command = [exe(), "-hide_banner", "-nostdin", "-v", level, *args]
    try:
        done = subprocess.run(command, capture_output=True, timeout=timeout, env=_env())
    except subprocess.TimeoutExpired:
        raise _media.StalledError(f"{name}: ffmpeg took longer than {timeout:.0f} s") from None
    stderr = done.stderr.decode("utf-8", "replace")
    if done.returncode:
        raise failure(name, stderr)
    return stderr


def encode(args: list[str], *, name: str, duration: float | None, progress: Callable[..., Any] | None,
           feed: Iterable[bytes] | None = None) -> None:
    """Run an encode, reporting seconds done to `progress(done, total)`, and kill it after STALL quiet seconds.

    Args:
        args: ffmpeg's arguments; the last one is the output file, and `-progress` goes before it.
        name: the file's name, for messages.
        duration: the media's length in seconds, the total `progress` gets; None when unknown.
        progress: `progress(done, total)`, called with seconds encoded so far, or None for no report.
        feed: raw frames written to ffmpeg's stdin, for an input that isn't a file; None for none.

    Raises:
        StalledError: ffmpeg made no progress for `STALL` seconds, and was killed.
        UnsafeError, RuntimeError, BrokenError: ffmpeg failed, as `failure` sorts it.

    Blocks the calling thread until the encode ends; `progress` runs on that thread.
    """
    # `-progress pipe:1` sends key=value lines to stdout; the output is then the last argument.
    with _started([*args[:-1], "-progress", "pipe:1", "-nostats", args[-1]], name=name, feed=feed) as (out, kick):
        for line in out:
            kick()
            # out_time_us is in microseconds of output written, so /1e6 gives seconds.
            if line.startswith(b"out_time_us=") and line[12:].strip().isdigit() and progress:
                done = int(line[12:]) / 1e6
                progress(min(done, duration) if duration else done, duration)


def frames_out(args: list[str], *, name: str, size: int, feed: Iterable[bytes] | None = None) -> Iterator[bytes]:
    """Run ffmpeg writing raw frames of `size` bytes to its stdout, and yield each frame.

    Args:
        args: ffmpeg's arguments, ending with a raw output to `pipe:1`.
        name: the file's name, for messages.
        size: the bytes in one frame, such as width x height x 3 for RGB.
        feed: raw frames written to ffmpeg's stdin, for an input that isn't a file; None for none.

    Yields:
        Each whole frame, as `size` bytes, in order.

    Raises:
        StalledError: ffmpeg made no progress for `STALL` seconds. A failed run raises after the
            last frame, once the generator is exhausted.
    """
    with _started(args, name=name, feed=feed) as (out, kick):
        while frame := out.read(size):
            kick()
            # A short read can only be the end of the stream, not a whole frame, so it is dropped.
            if len(frame) == size:
                yield frame


@contextmanager
def _started(args: list[str], *, name: str, feed: Iterable[bytes] | None) -> Iterator[tuple[IO[bytes], Callable]]:
    """Start ffmpeg with `args`, yield its stdout and a `kick` to call on each line, and check its exit when done.

    A watcher thread kills ffmpeg after STALL quiet seconds, and a feeder thread writes `feed` to its
    stdin. On exit, whatever happened, ffmpeg is killed if still running; a run that failed then
    raises its stderr as the media error.
    """
    # stderr goes to a file, so a chatty ffmpeg can't fill a pipe and deadlock.
    with tempfile.TemporaryFile() as errors, subprocess.Popen(
            [exe(), "-hide_banner", "-nostdin", "-v", "error", *args], env=_env(), stdout=subprocess.PIPE,
            stdin=subprocess.PIPE if feed is not None else subprocess.DEVNULL, stderr=errors) as process:
        stalled = threading.Event()
        quiet = threading.Event()
        threading.Thread(target=_watch, args=(process, quiet, stalled), daemon=True).start()
        if feed is not None:
            threading.Thread(target=_feed, args=(process, feed), daemon=True).start()
        try:
            assert process.stdout is not None
            yield process.stdout, quiet.set
            process.wait()
        finally:
            # Any exception, Ctrl-C included, kills ffmpeg before it is passed on.
            if process.poll() is None:
                process.kill()
                process.wait()
            quiet.set()
        if stalled.is_set():
            raise _media.StalledError(f"{name}: ffmpeg made no progress for {STALL:.0f} s")
        if process.returncode:
            errors.seek(0)
            raise failure(name, errors.read().decode("utf-8", "replace"))


def _watch(process: subprocess.Popen, quiet: threading.Event, stalled: threading.Event) -> None:
    """Kill `process` when `quiet` isn't set for STALL seconds, recording that in `stalled`; runs on its own thread."""
    # Each progress line or frame sets `quiet`; STALL seconds without one kill ffmpeg.
    while process.poll() is None:
        if not quiet.wait(STALL):
            # Checked again: ffmpeg may have exited in the same moment the wait timed out.
            if process.poll() is None:
                stalled.set()
                process.kill()
            return
        quiet.clear()


def _feed(process: subprocess.Popen, chunks: Iterable[bytes]) -> None:
    """Write `chunks` to `process`'s stdin, then close it; runs on its own thread."""
    assert process.stdin is not None
    try:
        for chunk in chunks:
            process.stdin.write(chunk)
        process.stdin.close()
    except (BrokenPipeError, OSError, ValueError):
        # ffmpeg stopped reading (a broken pipe) or was killed; its exit status is the error to report, not this.
        pass


def failure(name: str, stderr: str) -> Exception:
    """The error for a failed run, named by ffmpeg's first message.

    Args:
        name: the file's name, for the message.
        stderr: ffmpeg's stderr from the failed run.

    Returns:
        `UnsafeError` when ffmpeg refused an input by the whitelist; `RuntimeError` when the
        container can't hold a codec or header write failed, which is a setting rather than a
        bad file; else `BrokenError` with ffmpeg's first message.
    """
    lines = [re.sub(r"^\[[^]]+ @ 0x[0-9a-f]+\] ", "", line).strip() for line in stderr.splitlines()]
    lines = [line for line in lines if line and not line.startswith("Last message repeated")]
    first = lines[0] if lines else "ffmpeg failed"
    # ffmpeg's wording for a refused input, from `-protocol_whitelist` or `-format_whitelist`.
    if "not on whitelist" in stderr:
        return _media.UnsafeError(f"{name}: refused by the input rules ({first})")
    # These messages come from the muxer, and the code shows its last line rather than the first.
    if "supported in container" in stderr or "Could not write header" in stderr:
        return RuntimeError(f"{name}: {lines[-1]}")
    return _media.BrokenError(f"{name}: {first}")


def _env() -> dict[str, str]:
    # FFREPORT makes ffmpeg write a log file into the working folder; colors would break the parser.
    env = {key: value for key, value in os.environ.items() if key != "FFREPORT"}
    env["AV_LOG_FORCE_NOCOLOR"] = "1"
    return env


def probe(path: Path, *, format: str | None) -> _media.Info:
    """Info for a video or audio file from one ffmpeg run: the stream summary and ffmetadata.

    Args:
        path: the file to read, passed to ffmpeg as a whitelisted `file:` input.
        format: the format to report, given without its dot, such as "mp4"; None reports
            ffmpeg's own reader name instead.

    Returns:
        The file's `Info`, with the video or audio fields that ffmpeg's summary named.

    Raises:
        StalledError: ffmpeg took longer than `PROBE_TIMEOUT` to open the file.
        UnsafeError, RuntimeError, BrokenError: ffmpeg refused or failed to read it, or it has no
            video or audio stream.
    """
    # bitexact keeps ffmpeg from adding its own `encoder` tag to the metadata it prints.
    command = [exe(), "-hide_banner", "-nostdin", "-v", "info", *source(path), "-fflags", "+bitexact",
               "-f", "ffmetadata", "pipe:1"]
    try:
        done = subprocess.run(command, capture_output=True, timeout=PROBE_TIMEOUT, env=_env())
    except subprocess.TimeoutExpired:
        raise _media.StalledError(f"{path.name}: ffmpeg took longer than {PROBE_TIMEOUT:.0f} s "
                                  "to open it") from None
    stderr = done.stderr.decode("utf-8", "replace")
    if done.returncode:
        raise failure(path.name, stderr)
    fields = summary(stderr)
    if fields["video"] is None and fields["audio"] is None:
        raise _media.BrokenError(f"{path.name}: no video or audio stream")
    tags = ffmetadata(done.stdout.decode("utf-8", "replace"))
    created = fields.pop("created")
    if created:
        tags["creation_time"] = created
    return _media.Info(
        path=path, kind="video" if fields["video"] else "audio", animated=False,
        format=format or fields.pop("reader"), size=path.stat().st_size, frames=None,
        created=_media.parse_time(created), location=_media.parse_iso6709(
            tags.get("location") or tags.get("com.apple.quicktime.location.ISO6709")),
        tags=tags, **{key: value for key, value in fields.items() if key != "reader"})


def summary(stderr: str) -> dict[str, Any]:
    """The fields of ffmpeg's `Input #0` summary, matched at their exact indent so a tag can't fake a line."""
    fields: dict[str, Any] = dict(reader=None, duration=None, bitrate=None, video=None, width=None, height=None,
                                  fps=None, audio=None, sample_rate=None, channels=None, created=None)
    # Global tags are printed at 4 spaces before `Duration:`; streams at 2 (ffmpeg 6 and later) or 4
    # (older) after it. A tag's continuation lines start with spaces and ": ", so they never match.
    rotation, in_video, before_duration = 0.0, False, True
    for line in stderr.split("Stream mapping:")[0].splitlines():
        if match := re.match(r"Input #0, (\S+?),? from ", line):
            fields["reader"] = match[1].split(",")[0]
        elif before_duration and (match := re.match(r"    creation_time   : (\S+)$", line)):
            fields["created"] = match[1]
        elif match := re.match(r"  Duration: (?:(\d+):(\d+):(\d+(?:\.\d+)?)|N/A).*?bitrate: (?:(\d+) kb/s|N/A)", line):
            before_duration = False
            if match[1]:
                fields["duration"] = int(match[1]) * 3600 + int(match[2]) * 60 + float(match[3])
            fields["bitrate"] = int(match[4]) * 1000 if match[4] else None
        elif before_duration:
            continue
        elif match := re.match(r"  (?:  )?Stream #0:\d+.*?: (Video|Audio): (\w+)(.*)$", line):
            in_video = match[1] == "Video" and fields["video"] is None
            if in_video:
                _video_fields(fields, match[2], match[3])
            elif match[1] == "Audio" and fields["audio"] is None:
                _audio_fields(fields, match[2], match[3])
        elif re.match(r"  (?:  )?Stream #0:", line):
            in_video = False
        elif in_video and (match := re.match(r"      display ?matrix: rotation of (-?\d+(?:\.\d+)?) degrees", line,
                                             re.IGNORECASE)):
            rotation = float(match[1])
    if round(abs(rotation)) % 180 == 90:
        fields["width"], fields["height"] = fields["height"], fields["width"]
    return fields


def _video_fields(fields: dict[str, Any], codec: str, rest: str) -> None:
    fields["video"] = codec
    if size := re.search(r", (\d+)x(\d+)[ ,]", rest + " "):
        fields["width"], fields["height"] = int(size[1]), int(size[2])
    if rate := re.search(r"(\d+(?:\.\d+)?) fps", rest):
        fields["fps"] = float(rate[1])


def _audio_fields(fields: dict[str, Any], codec: str, rest: str) -> None:
    fields["audio"] = codec
    if match := re.search(r"(\d+) Hz, ([^,]+)", rest):
        fields["sample_rate"] = int(match[1])
        layout = match[2].split("(")[0].strip()
        count = re.match(r"(\d+) channels", layout)
        fields["channels"] = int(count[1]) if count else CHANNELS.get(layout)


def ffmetadata(text: str) -> dict[str, str]:
    """The global tags from ffmpeg's ffmetadata output, where `=`, `;`, `#`, `\\` and newline are escaped."""
    if not text.startswith(";FFMETADATA"):
        return {}
    tags: dict[str, str] = {}
    entries: list[tuple[str, int]] = []
    current, split, escaped = [], -1, False
    for char in text.split("\n", 1)[1] if "\n" in text else "":
        if escaped:
            current.append(char)
            escaped = False
        elif char == "\\":
            escaped = True
        elif char == "\n":
            entries.append(("".join(current), split))
            current, split = [], -1
        else:
            if char == "=" and split < 0:
                split = len(current)
            current.append(char)
    entries.append(("".join(current), split))
    for entry, split in entries:
        if entry.startswith("[") and split < 0:
            break  # a [CHAPTER] or [STREAM] section: the global tags are over
        if split > 0:
            tags[entry[:split]] = entry[split + 1:]
    return tags
