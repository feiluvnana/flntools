"""Compress saved photos, videos and audio for `--image`, `--video` and `--audio`, never letting media's errors end a run.

The three kinds of saved file go through one table, `KINDS`: each kind's option, the formats it
takes, and whether its encodes wait for the run's one-at-a-time lock (videos only, since one x264
encode already uses every core). A setting is a `Choice(format, level)`, read from the command
line or `targets.db` by `parse_choice`; its level is one of `media.compress`'s presets.

`compress_file` compresses one file with one choice, calls `shared.media.compress`, and turns its
result into the file's history fields. It catches exactly what `media` raises for a file it can't
compress (`ValueError`, `FileExistsError`, `RuntimeError`, `TimeoutError`, `ImportError`), warns
once per kind of error per run, and leaves the original in place. `Compressor` is one run's
compressing on top of it: the encode lock, and the kinds this machine turned out unable to
compress. `apply` compresses a target's files already saved, when `flnd download` changes a
setting. `session` compresses each new download; `sync.download` calls `apply`.

    choice = parse_choice("image", "webp,mid")              # Choice("webp", "mid")
    fields = Compressor().compress(Path("a.jpg"), "image", choice)
    if fields is not None:
        history[key] = {**row, **fields}
"""

from __future__ import annotations

import threading
from contextlib import nullcontext
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Iterable, MutableMapping

from flnd.common import files
from shared import log

if TYPE_CHECKING:
    from flnd.session import Options

# The levels, best first: `media.compress`'s preset names, so the CLI, the history and media agree.
LEVELS = ("best", "high", "mid", "low")
# A format alone re-encodes anyway, so it takes the level that loses nothing visible at a fair size.
FORMAT_LEVEL = "high"
# Phase 5's preset names, as old `targets.db` and history rows still say them; "high" kept its name.
OLD_LEVELS = {"small": "low", "balanced": "mid"}
# What `media` writes for each kind.
IMAGE_FORMATS = ("jpg", "webp", "avif", "png", "gif", "tif", "bmp")
VIDEO_CONTAINERS = ("mp4", "mov", "mkv", "webm", "avi", "ts", "3gp", "gif")
VIDEO_CODECS = ("h264", "h265", "vp9")
AUDIO_FORMATS = ("mp3", "m4a", "aac", "opus", "ogg", "flac")
# The codecs a container holds; every other container holds H.264 and H.265.
CODECS_IN = {"webm": ("vp9",), "avi": ("h264",), "mkv": VIDEO_CODECS, "gif": ()}

# Downloads compress photos on several threads at once, so "already warned?" and "now warned" are one step.
_warning = threading.Lock()


@dataclass(frozen=True)
class Choice:
    """One kind's compress setting: `Choice("webp", "mid")` is `--image webp,mid`.

    It prints as the command line writes it ("webp,mid", or "mid" without a format), which is
    also how `targets.db` saves it and how a history row's `compressed` records it.
    """

    # The format to write, such as "webp", "h265" or "mkv:h265"; None keeps each file's own.
    format: str | None
    # One of `LEVELS`.
    level: str

    def __str__(self) -> str:
        return f"{self.format},{self.level}" if self.format else self.level


@dataclass(frozen=True)
class Kind:
    """What one kind of saved file is compressed with: its option, its formats, and whether it takes turns."""

    # The command-line option without its dashes, which is also the `Options` field: "image" is --image.
    option: str
    # The formats the option takes; a video's container and codec may also be paired as container:codec.
    formats: tuple[str, ...]
    # Whether its encodes wait for the run's lock, one at a time, on a pool of their own.
    one_at_a_time: bool


KINDS = {files.IMAGE: Kind("image", IMAGE_FORMATS, False),
         files.VIDEO: Kind("video", VIDEO_CONTAINERS + VIDEO_CODECS, True),
         files.AUDIO: Kind("audio", AUDIO_FORMATS, False)}


def parse_choice(kind: str, text: str) -> Choice | None:
    """Read `--image`, `--video` or `--audio`'s value: a format, a level, both in either order, or "off".

        parse_choice("image", "best,webp")       # Choice("webp", "best")
        parse_choice("video", "h265")            # Choice("h265", "high"): a format alone is high
        parse_choice("audio", "off")             # None

    Args:
        kind: "image", "video" or "audio", a key of `KINDS`.
        text: the value as given, in any case.

    Returns:
        The choice, or None for "off".

    Raises:
        ValueError: the value isn't one, with a message that names the option and what each part takes,
            such as "--image webp,huge: the level is best, high, mid or low".
    """
    option = f"--{KINDS[kind].option}"
    parts = [part.strip().lower() for part in text.split(",")]
    if parts == ["off"]:
        return None
    if len(parts) > 2 or "" in parts:
        raise ValueError(f"{option} takes <format>,<level>, a format, a level, or off, not {text!r}")
    levels = [part for part in parts if part in LEVELS]
    formats = [part for part in parts if part not in LEVELS]
    if len(levels) > 1:
        raise ValueError(f"{option} {text}: give one level, of {_listed(LEVELS)}")
    problems = [_format_problem(kind, part) for part in formats]
    if len(parts) == 2 and not levels:
        # Two parts and neither is a level: a valid format names the bad part as the level.
        if problems[0] and problems[1]:
            raise ValueError(f"{option} {text}: the format is {_listed(KINDS[kind].formats)}, and the level is "
                             f"{_listed(LEVELS)}")
        raise ValueError(f"{option} {text}: the level is {_listed(LEVELS)}")
    if problems and problems[0]:
        if len(parts) == 1 and _known_format(kind, parts[0]) is None:
            raise ValueError(f"{option} {text}: give a format ({_listed(KINDS[kind].formats)}), a level "
                             f"({_listed(LEVELS)}), or both")
        raise ValueError(f"{option} {text}: {problems[0]}")
    return Choice(formats[0] if formats else None, levels[0] if levels else FORMAT_LEVEL)


def read_saved(kind: str, value: Any) -> Choice | None:
    """A choice as `targets.db` saved it ("webp,mid"), or None when it saved none or one this flnd can't read.

        read_saved("image", "webp,mid")      # Choice("webp", "mid")

    Args:
        kind: a key of `KINDS`.
        value: the row's field: the choice as text, or null when the setting is off.

    A value this flnd can't read is warned of and read as off, so one bad row never stops a run;
    `sync.record` keeps it in the row unless the option is given again.
    """
    if not isinstance(value, str) or not value:
        return None
    try:
        return parse_choice(kind, value)
    except ValueError as error:
        # A row written by another version, or by hand: the target is still synced, without it.
        log.warn("targets.db: {error}; ignored", error=error)
        return None


def choice_of(options: Options, kind: str | None) -> Choice | None:
    """The run's choice for a kind of file (`options.image` for "image"); None for no kind or no setting.

        choice_of(Options(video=Choice("h265", "low")), "video")     # Choice("h265", "low")

    Args:
        options: the run's options.
        kind: a key of `KINDS`, or None (an archive or any other file), which is never compressed.
    """
    return getattr(options, KINDS[kind].option) if kind in KINDS else None


def video_format(value: str | None) -> tuple[str | None, str | None]:
    """A `--video` format as (container, codec): `mkv:h265` is ("mkv", "h265"), `h265` (None, "h265").

        video_format("webm")      # ("webm", None)

    Args:
        value: the format part of `--video`'s value, or None for none.

    Raises:
        ValueError: it isn't a container, a codec or a pair, or the pair can't go together (`webm:h265`).
    """
    if not value:
        return None, None
    container, _, codec = value.lower().partition(":")
    if not codec and container in VIDEO_CODECS:
        container, codec = "", container
    if (container and container not in VIDEO_CONTAINERS) or (codec and codec not in VIDEO_CODECS):
        raise ValueError(f"the format is a container ({_listed(VIDEO_CONTAINERS)}), a codec "
                         f"({_listed(VIDEO_CODECS)}), or container:codec")
    if container and codec and codec not in CODECS_IN.get(container, ("h264", "h265")):
        holds = CODECS_IN.get(container, ())
        raise ValueError(f"{container} holds {_listed(holds)}, not {codec}" if holds else f"{container} holds no codec")
    return container or None, codec or None


@dataclass
class Compressor:
    """One run's compressing: the encode lock, the kinds this machine can't compress, and the errors warned of.

        compressor = Compressor()
        fields = compressor.compress(Path("clip.mp4"), "video", Choice("h265", "mid"))

    Each kind is on its own (fixes #8): a photo this machine can't compress never stops videos.
    """

    # The kinds of error already warned of, so a stalled ffmpeg on every file warns once.
    warned: set[type[Exception]] = field(default_factory=set)
    # The kinds this machine turned out unable to compress (no ffmpeg, encoder or extra); not tried again.
    off: set[str] = field(default_factory=set)
    # One encode of a `one_at_a_time` kind in the whole run, across every source's threads.
    turns: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def compress(self, path: Path, kind: str, choice: Choice) -> dict[str, Any] | None:
        """Compress one saved file, waiting for its turn when its kind takes turns, and return its history fields.

            fields = compressor.compress(Path("a.jpg"), "image", Choice(None, "low"))

        Args:
            path: the saved file; replaced by its compressed copy only when that is smaller.
            kind: a key of `KINDS`.
            choice: the format and level to compress with.

        Returns:
            What `compress_file` returns; None also once the kind is off for this run, and then the
            first file that turned it off has warned "<kind> compression stops for this run".

        It is slow: a photo takes a moment, a video minutes, on the caller's thread.
        """
        with self.turns if KINDS[kind].one_at_a_time else nullcontext():
            if kind in self.off:
                return None
            fields = compress_file(path, kind, choice, self.warned)
            if fields is None:
                # No ffmpeg, encoder or extra: every other file of the kind would fail the same way.
                self.off.add(kind)
                log.warn("{kind} compression stops for this run", kind=kind)
            return fields


def compress_file(path: Path, kind: str, choice: Choice, warned: set[type[Exception]]) -> dict[str, Any] | None:
    """Compress one saved photo, video or audio file with a choice, and return its history fields.

        fields = compress_file(Path("a.jpg"), "image", Choice(None, "low"), warned)

    Args:
        path: the saved file; replaced by its compressed copy only when that is smaller.
        kind: a key of `KINDS`; a video's format is read as a container, a codec, or both.
        choice: the format (None keeps the file's own) and the level, which is media's preset.
        warned: the kinds of error already warned of in this run, one set for the whole run; the
            first error of each kind is a warning, the ones after it only a debug line.

    Returns:
        The history fields `path`, `size`, `compressed` (the choice as text, or "none") and
        `original_size`. "none" means this file can't be made smaller here: it didn't shrink,
        can't be read or written (an `OSError`, such as a `PermissionError` or a full disk), or
        ffmpeg stalled on it (`TimeoutError`), so it isn't tried again with this choice. None means
        this machine can't compress it at all (no ffmpeg or encoder, `RuntimeError`; a missing
        extra, `ImportError`): nothing is recorded, so a later run, once that is installed,
        compresses it. A file that is gone before compression starts gives only
        `{"compressed": "none"}`, since it has no size to record; the caller's row keeps its own
        `path` and `size`.

    Raises:
        Nothing of media's; anything else, such as Ctrl-C or a mistake in flnd, isn't caught.

    It is slow, and a video encode uses every core: callers run one video at a time (`Compressor`).
    """
    from shared import media

    format, codec = choice.format, None
    if kind == files.VIDEO:
        format, codec = video_format(choice.format)
        if codec and not format and codec not in CODECS_IN.get(path.suffix.lower()[1:], ("h264", "h265")):
            # A codec alone keeps the file's container when it can hold it.
            format = "webm" if codec == "vp9" else "mp4"
    try:
        size = path.stat().st_size
    except OSError as error:
        # This file only, like the errors below: it vanished (or can't be read) between its save
        # or the listing and now. With no size there are no size fields to record, so only
        # "compressed" is returned: every caller merges the fields onto the file's history row,
        # which keeps its own path and size. Not None, which would mean this machine can't compress.
        _warn(warned, error, path)
        return {"compressed": "none"}
    try:
        out = media.compress(path, preset=choice.level, format=format, codec=codec)
    except (ValueError, OSError) as error:
        # This file only: unreadable or refused (ValueError, BrokenError and UnsafeError among them),
        # or an OSError: its new name taken (FileExistsError), ffmpeg stalled or took too long on it
        # (TimeoutError), or the file vanished, can't be opened (PermissionError) or the disk is full.
        # Each is about this file or this moment, so it fails this file and the run goes on.
        # `media.compress` writes to a temporary part first, so the original is untouched.
        _warn(warned, error, path)
        out = None
    except (RuntimeError, ImportError) as error:
        # This machine: no ffmpeg, an ffmpeg too old or without the encoder, or no Pillow. Every
        # other file of the kind would fail the same way, and installing it fixes them all.
        _warn(warned, error, path)
        return None
    if out is None:
        return {"path": str(path), "size": size, "compressed": "none", "original_size": size}
    return {"path": str(out), "size": out.stat().st_size, "compressed": str(choice), "original_size": size}


def apply(history: MutableMapping[str, Any], rows: Iterable[tuple[str, dict]], kind: str, choice: Choice,
          compressor: Compressor, *, label: str) -> int:
    """Compress a target's saved files of one kind with a setting `flnd download` just changed, recording each.

        apply(history, rows, "image", Choice("webp", "mid"), Compressor(), label="ins:example_user")

    Args:
        history: the download history, written with each file's new fields.
        rows: the target's (key, row) history rows, as `sync`'s index gives them.
        kind: a key of `KINDS`; rows of other kinds are left alone.
        choice: the new setting.
        compressor: the compressing for this command, shared by every target it changes.
        label: the target, for the progress row.

    Returns:
        How many files were compressed or tried; a file already compressed with this choice
        (`compressed_with`), or gone from the disk, isn't.

    Raises:
        OSError: the history can't be written.

    A file is replaced only when the result is smaller, so going from `low` to `best` keeps a
    file compressed earlier as it is. It is slow and can't be undone.
    """
    todo = [(key, row) for key, row in rows if files.kind_of(row["path"]) == kind
            and not compressed_with(row.get("compressed"), choice) and Path(row["path"]).is_file()]
    if not todo:
        return 0
    with log.progress(f"--{KINDS[kind].option} {choice} {label}", total=len(todo)) as bar:
        for key, row in todo:
            bar.advance()
            fields = compressor.compress(Path(row["path"]), kind, choice)
            if fields is not None:
                history[key] = {**row, **fields}
    return len(todo)


def compressed_with(compressed: Any, choice: Choice) -> bool:
    """Whether a history row's `compressed` already satisfies `choice`, so `apply` leaves its file.

        compressed_with("webp,mid", Choice(None, "mid"))      # True: "mid" keeps each file's format
        compressed_with("mid", Choice("webp", "mid"))         # False: the file isn't WebP yet

    Args:
        compressed: the row's `compressed`: the choice it was compressed with as text ("webp,mid",
            "mid", or Phase 5's "balanced"), "none", or None when it never was.
        choice: the setting being applied.

    Returns:
        True when the row was compressed at `choice`'s level and, if `choice` names a format, with
        that format too. A level-only choice keeps each file's own format, so a file compressed
        at that level into any format already matches. "none" and None never match.
    """
    text = recorded(compressed)
    if not isinstance(text, str) or text == "none":
        return False
    if choice.format is not None:
        return text == str(choice)
    # The level is after the last comma: a video format may hold a colon ("mkv:h265,high"), never a comma.
    return text.rpartition(",")[2] == choice.level


def recorded(compressed: Any) -> Any:
    """A history row's `compressed` in today's words: Phase 5's "small" reads as "low", "balanced" as "mid".

        recorded("balanced")      # "mid"; "webp,mid" and "none" stay as they are
    """
    return OLD_LEVELS.get(compressed, compressed) if isinstance(compressed, str) else compressed


def _format_problem(kind: str, value: str) -> str | None:
    """Why `value` isn't a format of this kind, or None when it is."""
    if kind == files.VIDEO:
        try:
            video_format(value)
        except ValueError as error:
            return str(error)
        return None
    return None if value in KINDS[kind].formats else f"the format is {_listed(KINDS[kind].formats)}"


def _known_format(kind: str, value: str) -> str | None:
    """The part of `value` that names a format of this kind, so a bad pair is told apart from a typo."""
    container = value.partition(":")[0]
    return container if container in KINDS[kind].formats else None


def _listed(names: Iterable[str]) -> str:
    """`a, b or c`, as an error message lists what a part takes."""
    names = list(names)
    return names[0] if len(names) == 1 else f"{', '.join(names[:-1])} or {names[-1]}"


def _warn(warned: set[type[Exception]], error: Exception, path: Path) -> None:
    """Warn of `error` the first time its kind is seen in this run, and log it at debug after that."""
    with _warning:
        first = type(error) not in warned
        warned.add(type(error))
    (log.warn if first else log.debug)("{name}: not compressed: {error}", name=path.name, error=error)