"""Media compression: a smaller file that still looks the same, or none at all.

A library module: it imports only the stdlib, Pillow and pyoxipng (lazily), and the file, media
and tool libraries (`flnd._files`, `flnd._media`, `flnd._tools`), with flnd's error classes. It
offers:
- `shrink(path, record=…)`: compress one photo, video or song in place, and say what happened
  as an `Outcome`; the caller's `record` callback is told before the original goes, so the
  caller's own bookkeeping (flnd's history row) moves with the file;
- `kind(path)` and `tools(kind)`: what a file is to this module, and the tools string a result
  records, so a tool upgrade can retry what was kept or failed;
- `Savings`: running savings per kind, so a caller can stop trying a kind that never pays;
- `ssim(a, b)`: the structural similarity of two images' luma planes.

Who calls it in flnd: the compress step (`--compress`). The step chain (`flnd._steps`) calls
`shrink` on each saved photo, video or song, on its own threads, after the file's history row is
written. The originals these sites serve are already compressed, so every result is checked
before it replaces the original (docs/media).

How `shrink` goes:
1. *Plan* the candidates from the file's kind (by suffix) and its probe (`flnd._media`):
   - a JPEG becomes AVIF, then an optimized JPEG with its own quantization tables, then WebP;
   - a PNG goes through oxipng (lossless), then lossless WebP for flat art (256 colours or
     fewer), or AVIF for a photo;
   - a GIF becomes a lossless (animated) WebP;
   - a video becomes SVT-AV1 10-bit with Opus at 96k (or x265 with AAC when the config asks
     for HEVC, or when this ffmpeg has no SVT-AV1), `+faststart`, metadata copied;
   - a song becomes Opus at 96k.
   Photos are encoded by Pillow and pyoxipng in-process; video and sound by ffmpeg.
2. *Encode* each candidate into a part (`flnd._files.part`) of the name it would land at, so
   a crash leaves only a `.part` the next sweep removes.
3. *Verify* it: it decodes; its dimensions and frames match, and its duration within 0.1 s; a
   photo keeps SSIM of 0.98 or more against the original; and it saves 5% or more. The first
   candidate that saves 40% or more ends the search; otherwise the smallest that passed wins.
4. *Place* the winner, keeping the original's modified time (the post's date):
   - same suffix: it replaces the original in one rename;
   - a new suffix: it is renamed into place beside the original, `record` moves the history row
     to it, and only then is the original removed. A crash in between leaves both files and a
     row naming the new one, never a row naming a file that is gone.
5. Report `compressed`, `kept` (nothing smaller passed) or `failed` (an encoder or tool
   refused), with the tools string, so a tool upgrade retries `kept` and `failed` files.

SSIM is computed in-process on the luma plane over 8×8 blocks (`ssim`): about 5 ms for a
1.5 MP photo, against 15 ms or more for one ffmpeg spawn, and with no tool for photos at all.
It agrees with ffmpeg's `ssim` filter's Y figure to within about 0.001.

Rules a change must keep:
- Pillow and oxipng are imported inside the functions that use them, never at the top, so
  `flnd --help` stays fast;
- a candidate's part is dropped unless it wins, and the original is touched only by stage 4;
- `shrink` never raises for a file it can't improve: that is `kept` or `failed`. It raises only
  `KeyboardInterrupt` (a stop) and `DiskError` (the disk refused the placing rename).
"""

from __future__ import annotations

import io
import statistics
import threading
from array import array
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any

from flnd import _files, _media, _tools
from flnd.errors import DiskError, ToolError

__all__ = ["Kind", "Outcome", "Savings", "State", "kind", "shrink", "ssim", "tools"]


class State(Enum):
    """What the compress step did with a file, as its history row records it."""

    COMPRESSED = "compressed"
    # Every candidate ran, and none passed: the original was already as small as it gets.
    KEPT = "kept"
    # An encoder or tool refused (or is missing), so nothing could be tried.
    FAILED = "failed"


class Kind(Enum):
    """The kinds of file the compress step plans for; each has its own candidates."""

    JPEG = "jpeg"
    PNG = "png"
    GIF = "gif"
    VIDEO = "video"
    AUDIO = "audio"


SUFFIXES = {
    ".jpg": Kind.JPEG, ".jpeg": Kind.JPEG, ".png": Kind.PNG, ".gif": Kind.GIF,
    ".mp4": Kind.VIDEO, ".m4v": Kind.VIDEO, ".mov": Kind.VIDEO, ".webm": Kind.VIDEO,
    ".mkv": Kind.VIDEO,
    ".m4a": Kind.AUDIO, ".mp3": Kind.AUDIO, ".aac": Kind.AUDIO, ".wav": Kind.AUDIO,
    ".flac": Kind.AUDIO,
}
PHOTOS = frozenset({Kind.JPEG, Kind.PNG, Kind.GIF})

# The thresholds of stage 3, verify.
MIN_SSIM = 0.98
MIN_SAVING = 0.05
GOOD_SAVING = 0.40
DURATION_SLACK = 0.1
# A kind whose median saving, over at least this many files, is under `WORTH` is left alone.
MIN_SAMPLES = 20
WORTH = 0.15

# AVIF at quality 60 kept SSIM at about 0.985 on a re-encoded JPEG; speed 8 is the fastest
# setting that still beats an optimized JPEG on size (0.02 s for 1.5 MP on an M5).
AVIF = {"quality": 60, "speed": 8}
WEBP = {"quality": 80, "method": 4}
# oxipng's level 2 is its default: most of the saving of level 6 at a fraction of the time.
OXIPNG_LEVEL = 2
# SVT-AV1 preset 8 encodes about as fast as x264 medium; CRF 35 is
# its usual "visually the same" point for phone video. x265's equivalent is CRF 28.
AV1 = ["-c:v", "libsvtav1", "-preset", "8", "-crf", "35", "-pix_fmt", "yuv420p10le",
       "-c:a", "libopus", "-b:a", "96k"]
# HEVC is asked for to play on Apple devices without AV1 decoding, so its audio is AAC,
# which they all play, and the `hvc1` tag QuickTime needs.
HEVC = ["-c:v", "libx265", "-preset", "medium", "-crf", "28", "-tag:v", "hvc1",
        "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "128k"]
OPUS = ["-vn", "-c:a", "libopus", "-b:a", "96k"]
# The block size `ssim` averages over, as in the original SSIM paper's 8×8 windows.
SSIM_BLOCK = 8
# SSIM's stabilising constants for 8-bit samples: (0.01 × 255)² and (0.03 × 255)².
C1, C2 = 6.5025, 58.5225


@dataclass(frozen=True)
class Outcome:
    """What `shrink` did with one file.

    Args:
        state: compressed, kept or failed.
        path: the file now: the original's path, or the new one when the suffix changed.
        before: the original's size in bytes.
        after: the file's size now, in bytes (`before` when nothing changed).
        tools: the tools string for this kind, which a later run compares to retry.
        note: why a file was kept or failed, for the log; "" when compressed.
    """

    state: State
    path: Path
    before: int
    after: int
    tools: str
    note: str = ""

    @property
    def saving(self) -> float:
        """The fraction of the original's size saved, 0.0 when nothing was."""
        return 1 - self.after / self.before if self.before else 0.0


@dataclass
class Savings:
    """Each kind's savings so far, to leave alone a kind that doesn't shrink.

    A kind with at least `MIN_SAMPLES` files tried and a median saving under `WORTH` (15%) is
    not worth the encode time or the generation of quality, so `worth` says no to it. A file
    kept counts as a saving of 0. Shared by the chain's threads, so it holds a lock.
    """

    seen: dict[Kind, list[float]] = field(default_factory=dict)
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def add(self, of: Kind, saving: float) -> None:
        """Count one file of kind `of` that saved `saving` (0.0 to 1.0)."""
        with self._lock:
            self.seen.setdefault(of, []).append(max(0.0, saving))

    def worth(self, of: Kind) -> bool:
        """Whether files of kind `of` are still worth compressing."""
        with self._lock:
            known = self.seen.get(of, ())
            return len(known) < MIN_SAMPLES or statistics.median(known) >= WORTH


def kind(path: Path) -> Kind | None:
    """The kind `path`'s suffix makes it, or None for a file the compress step leaves alone."""
    return SUFFIXES.get(path.suffix.lower())


_tools_cache: dict[tuple[Kind, str], str] = {}


def tools(of: Kind, *, video: str = "av1") -> str:
    """The tools string for kind `of`: what encodes it, with versions, asked once per process.

    Args:
        of: the kind.
        video: "av1" or "hevc", the codec videos go to.

    Returns:
        For a photo, Pillow's and libavif's versions (and oxipng's for a PNG); for a video or
        song, ffmpeg's version and the encoder; "ffmpeg missing" when it is not installed, so
        installing it later retries the files that failed for it.
    """
    key = (of, video)
    if key in _tools_cache:
        return _tools_cache[key]
    if of in PHOTOS:
        import PIL
        from PIL import features

        made = f"pillow {PIL.__version__} libavif {features.version('avif') or 'none'}"
        if of is Kind.PNG:
            from importlib.metadata import version

            made += f" oxipng {version('pyoxipng')}"
    else:
        try:
            made = f"ffmpeg {_tools.ffmpeg().version} {_video_encoder(video) or 'none'}"
        except ToolError:
            made = "ffmpeg missing"
    _tools_cache[key] = made
    return made


def shrink(
    path: Path, *, record: Callable[[Outcome], None], video: str = "av1",
    stop: threading.Event | None = None, progress: Callable[[float], None] | None = None,
) -> Outcome:
    """Compress `path` if a smaller file passes every check (the module's stages 1 to 5).

    Args:
        path: a saved file of a `Kind`.
        record: called with the outcome once the result is in place and before the original
            is removed (stage 4), so the history row moves with the file. It is called for kept
            and failed files too, with the original's path.
        video: "av1" (the default) or "hevc", for devices that can't play AV1.
        stop: set to stop a running ffmpeg encode.
        progress: a video's encode progress, 0.0 to 1.0.

    Returns:
        What was done.

    Raises:
        KeyboardInterrupt: `stop` was set during an encode; the part is removed.
        DiskError: the disk refused placing the result; the original is untouched.
    """
    of = kind(path)
    assert of is not None, f"{path.name}: no kind to compress"
    used = tools(of, video=video)
    found = path.stat()
    before = found.st_size
    try:
        original = _Original.read(path, of)
        candidates = _plan(of, original, video=video, stop=stop, progress=progress)
    except (ToolError, OSError, ValueError) as error:
        return _unchanged(path, State.FAILED, before, used, str(error), record)
    try:
        landed, note = _search(path, candidates, original, before, found.st_mtime)
    finally:
        if original.image is not None:
            original.image.close()
    if landed is None:
        state = State.FAILED if note.startswith("failed: ") else State.KEPT
        return _unchanged(path, state, before, used, note.removeprefix("failed: "), record)
    return _placed(path, landed, before, used, record)


# ---------------------------------------------------------------------------------------------
# Plan, encode and verify (stages 1 to 3)


@dataclass(frozen=True)
class _Candidate:
    """One encode to try: its name for the log, its suffix, and the function that writes it.

    `write(source, part)` encodes the original at `source` into the locked `_files.Part`:
    Pillow and oxipng through `Part.write`, ffmpeg by the part's name.
    """

    name: str
    suffix: str
    write: Callable[[Path, _files.Part], None]


def _search(
    path: Path, candidates: list[_Candidate], original: _Original, before: int, mtime: float,
) -> tuple[_files.Saved | None, str]:
    """Encode and verify each candidate in a part of its own, and land the winner (stage 3).

    Each candidate is written into `_files.part(<its destination>)`, so a crash leaves only a
    `.part` that the next sweep removes. A candidate that passes and saves 40% or more lands at
    once. One that passes with less is measured and dropped while later ones are tried; if it
    stays the smallest, it is encoded again into a part that lands (the encoders are
    deterministic). A refused or failed part is dropped. The original is never touched here.

    Args:
        path: the file being compressed.
        candidates: what to try, in order.
        original: what each candidate is checked against.
        before: the original's size in bytes.
        mtime: the original's modified time, which the result keeps.

    Returns:
        Where the winner landed and "", or None and why: a note starting "failed: " when no
        candidate could run at all.

    Raises:
        KeyboardInterrupt: a stop during an encode; the part is removed.
        DiskError: the winner's part could not be renamed into place.
    """
    best: tuple[int, int] | None = None
    errors: list[str] = []
    ran = 0
    for number, candidate in enumerate(candidates):
        last = number == len(candidates) - 1
        dest = _dest(path, candidate.suffix)
        if dest != path and dest.exists():
            errors.append(f"{candidate.name}: {dest.name} exists")
            continue
        with _files.part(dest) as held:
            try:
                candidate.write(path, held)
                held.flush()
                ran += 1
                size = _verify(held.path, original, before)
            except (ToolError, OSError, ValueError, _Rejected) as error:
                held.drop()
                if not isinstance(error, _Rejected):
                    errors.append(f"{candidate.name}: {error}")
                continue
            good = 1 - size / before >= GOOD_SAVING
            if good or (last and (best is None or size < best[0])):
                held.mtime = mtime
            else:
                held.drop()
                if best is None or size < best[0]:
                    best = (size, number)
                continue
        return held.saved, ""
    if best is not None:
        winner = candidates[best[1]]
        with _files.part(_dest(path, winner.suffix)) as held:
            winner.write(path, held)
            held.mtime = mtime
        return held.saved, ""
    if errors and not ran:
        return None, "failed: " + "; ".join(errors)
    return None, "nothing smaller passed"


def _dest(path: Path, suffix: str) -> Path:
    """Where a candidate with `suffix` lands: the original's own path when the suffix is its."""
    return path if path.suffix.lower() == suffix.lower() else path.with_suffix(suffix)


class _Rejected(Exception):
    """A candidate that encoded but failed a check; the next one is tried."""


@dataclass
class _Original:
    """What every candidate is checked against: the probe and, for a photo, its luma plane."""

    probe: _media.Probe
    image: Any = None
    luma: Any = None

    @classmethod
    def read(cls, path: Path, of: Kind) -> _Original:
        """Probe `path` and, for a photo, decode it once.

        Args:
            path: the original.
            of: its kind; only a photo is decoded.

        Returns:
            Its probe, and for a photo the open image and its upright luma plane.

        Raises:
            ToolError: the file can't be read as media.
        """
        probe = _media.probe(path)
        if of not in PHOTOS:
            return cls(probe)
        from PIL import Image, ImageOps

        image = Image.open(path)
        image.load()
        return cls(probe, image, ImageOps.exif_transpose(image).convert("L"))


def _plan(
    of: Kind, original: _Original, *, video: str, stop: threading.Event | None,
    progress: Callable[[float], None] | None,
) -> list[_Candidate]:
    """The candidates for kind `of`, in the order they are tried (stage 1).

    Args:
        of: the file's kind.
        original: the decoded photo or the probe, which the candidates encode from.
        video: "av1" or "hevc".
        stop: set to kill a running ffmpeg.
        progress: a video's or song's encode progress.

    Returns:
        The candidates; each writes its part from the original's path.

    Raises:
        MissingError: a video or song needs ffmpeg (or an encoder this ffmpeg lacks).
    """
    image = original.image
    if of is Kind.JPEG:
        return [_Candidate("avif", ".avif", lambda s, p: _save(image, p, "AVIF", **AVIF)),
                _Candidate("jpeg", ".jpg", lambda s, p: _save(
                    image, p, "JPEG", quality="keep", subsampling="keep", optimize=True,
                    progressive=True)),
                _Candidate("webp", ".webp", lambda s, p: _save(image, p, "WEBP", **WEBP))]
    if of is Kind.PNG:
        flat = image.mode in ("1", "P") or image.getcolors(256) is not None
        last = (_Candidate("webp", ".webp", lambda s, p: _save(image, p, "WEBP", lossless=True,
                                                              method=4))
                if flat else
                _Candidate("avif", ".avif", lambda s, p: _save(image, p, "AVIF", **AVIF)))
        return [_Candidate("oxipng", ".png", _oxipng), last]
    if of is Kind.GIF:
        return [_Candidate("webp", ".webp", lambda s, p: _save(
            image, p, "WEBP", save_all=True, lossless=True, method=4,
            loop=image.info.get("loop", 0)))]
    duration = original.probe.duration
    if of is Kind.AUDIO:
        _need("libopus")
        return [_Candidate("opus", ".opus", lambda s, p: _media.encode(
            ["-i", str(s), *OPUS, "-map_metadata", "0", "-f", "opus", str(p.path)],
            duration=duration, progress=progress, stop=stop))]
    encoder = _video_encoder(video)
    if encoder is None:
        raise _tools.MissingError(f"this ffmpeg has no {video} encoder",
                                  hint="install an ffmpeg built with libsvtav1 or libx265")
    options = AV1 if encoder == "libsvtav1" else HEVC
    return [_Candidate(encoder, ".mp4", lambda s, p: _media.encode(
        ["-i", str(s), "-map", "0:v:0", "-map", "0:a?", *options, "-map_metadata", "0",
         "-movflags", "+faststart", "-f", "mp4", str(p.path)],
        duration=duration, progress=progress, stop=stop))]


def _video_encoder(video: str) -> str | None:
    """The encoder for `video` ("av1" or "hevc") this ffmpeg has, falling back to the other."""
    have = _media.encoders()
    order = ("libx265", "libsvtav1") if video == "hevc" else ("libsvtav1", "libx265")
    return next((name for name in order if name in have), None)


def _need(encoder: str) -> None:
    """Raise `MissingError` unless this ffmpeg has `encoder`."""
    if encoder not in _media.encoders():
        raise _tools.MissingError(f"ffmpeg is missing, or has no {encoder}",
                                  hint="install ffmpeg, or put it on PATH")


def _save(image: Any, part: _files.Part, format: str, **options: Any) -> None:
    """Encode `image` into `part` with Pillow, keeping its EXIF and colour profile.

    AVIF takes only RGB or RGBA, so a palette or greyscale image is converted first. The
    encode goes to memory, then into the part: a photo is a few MB at most.

    Args:
        image: the decoded original.
        part: the locked part to write.
        format: Pillow's format name ("AVIF", "JPEG", "WEBP").
        **options: the encoder's options, such as `quality`.
    """
    for name in ("exif", "icc_profile"):
        if image.info.get(name):
            options.setdefault(name, image.info[name])
    if format == "AVIF" and image.mode not in ("RGB", "RGBA"):
        image = image.convert("RGBA" if "A" in image.mode or "transparency" in image.info
                              else "RGB")
    out = io.BytesIO()
    image.save(out, format, **options)
    part.write(out.getvalue())


def _oxipng(source: Path, part: _files.Part) -> None:
    """Optimize a PNG losslessly with oxipng, in memory, into `part`."""
    import oxipng

    part.write(oxipng.optimize_from_memory(source.read_bytes(), level=OXIPNG_LEVEL))


def _verify(part: Path, original: _Original, before: int) -> int:
    """Check a candidate against the original (stage 3) and return its size.

    Args:
        part: the candidate.
        original: what it must match.
        before: the original's size in bytes.

    Returns:
        The candidate's size in bytes.

    Raises:
        _Rejected: it saves under 5%, changed dimensions, frames or duration, or (a photo)
            fell under SSIM 0.98.
        ToolError: it doesn't decode.
    """
    size = part.stat().st_size
    if size > before * (1 - MIN_SAVING):
        raise _Rejected("saves under 5%")
    got = _media.probe(part) if original.image is None else _photo_probe(part)
    want = original.probe
    if (got.width, got.height) != (want.width, want.height) and original.image is None:
        raise _Rejected("dimensions changed")
    if original.image is not None:
        if got.frames != want.frames:
            raise _Rejected("frames changed")
        from PIL import Image, ImageOps

        with Image.open(part) as made:
            luma = ImageOps.exif_transpose(made).convert("L")
        if luma.size != original.luma.size:
            raise _Rejected("dimensions changed")
        if ssim(original.luma, luma) < MIN_SSIM:
            raise _Rejected("SSIM under 0.98")
        return size
    if abs(got.duration - want.duration) > DURATION_SLACK:
        raise _Rejected("duration changed")
    # A full decode is the only proof every frame of a video is readable.
    _media.encode(["-i", str(part), "-f", "null", "-"], duration=0)
    return size


def _photo_probe(part: Path) -> _media.Probe:
    """A candidate photo's probe; a part is read by its content, whatever its suffix.

    Args:
        part: the candidate.

    Returns:
        Its dimensions and frame count.

    Raises:
        ToolError: it doesn't decode.
    """
    from PIL import Image, UnidentifiedImageError

    try:
        with Image.open(part) as image:
            return _media.Probe(image.width, image.height, getattr(image, "n_frames", 1))
    except (UnidentifiedImageError, OSError) as error:
        raise ToolError(f"{part.name}: doesn't decode ({error})") from error


def ssim(a: Any, b: Any) -> float:
    """The mean SSIM of two same-sized luma images ("L"), over 8×8 blocks.

    The blocks don't overlap: each block's means, variances and covariance come from one
    box-filtered downscale of the image, its square and the product, done in C by Pillow.

    Args:
        a: the original's luma plane.
        b: the candidate's, the same size.

    Returns:
        1.0 for identical images, lower as they differ; 0.98 is the step's floor.
    """
    from PIL import Image, ImageMath

    x, y = a.convert("F"), b.convert("F")
    size = (max(1, x.width // SSIM_BLOCK), max(1, x.height // SSIM_BLOCK))

    def mean(image: Any) -> Any:
        return image.resize(size, Image.Resampling.BOX)

    def times(left: Any, right: Any) -> Any:
        return ImageMath.lambda_eval(lambda e: e["l"] * e["r"], l=left, r=right)

    mx, my = mean(x), mean(y)
    xx, yy, xy = mean(times(x, x)), mean(times(y, y)), mean(times(x, y))
    score = ImageMath.lambda_eval(
        lambda e: ((e["mx"] * e["my"] * 2 + C1) * ((e["xy"] - e["mx"] * e["my"]) * 2 + C2))
        / ((e["mx"] * e["mx"] + e["my"] * e["my"] + C1)
           * (e["xx"] - e["mx"] * e["mx"] + e["yy"] - e["my"] * e["my"] + C2)),
        mx=mx, my=my, xx=xx, yy=yy, xy=xy,
    )
    # ImageStat bins a float image into 256 buckets, so the mean is taken from the raw floats.
    values = array("f", score.tobytes())
    return sum(values) / len(values)


# ---------------------------------------------------------------------------------------------
# Place (stage 4)


def _unchanged(
    path: Path, state: State, before: int, used: str, note: str,
    record: Callable[[Outcome], None],
) -> Outcome:
    """Record a file left as it was: `kept` or `failed`, with why.

    Args:
        path: the original, untouched.
        state: kept or failed.
        before: its size in bytes.
        used: the tools string.
        note: why, for the log.
        record: writes the history row.

    Returns:
        The outcome recorded.
    """
    outcome = Outcome(state, path, before, before, used, note)
    record(outcome)
    return outcome


def _placed(
    path: Path, landed: _files.Saved, before: int, used: str,
    record: Callable[[Outcome], None],
) -> Outcome:
    """Record the landed result, then remove the original when the suffix changed (stage 4).

    Args:
        path: the original.
        landed: where the winning part landed, with its size.
        before: the original's size in bytes.
        used: the tools string.
        record: moves the history row; called before the original goes.

    Returns:
        `compressed`, with the new path.

    Raises:
        DiskError: the original couldn't be removed after a new-suffix result was recorded.
    """
    dest = landed.path
    outcome = Outcome(State.COMPRESSED, dest, before, landed.size, used)
    try:
        record(outcome)
    except BaseException:
        # The row still names the original, so the new file goes and the original stays.
        if dest != path:
            dest.unlink(missing_ok=True)
        raise
    if dest != path:
        try:
            path.unlink()
        except OSError as error:
            raise DiskError(f"{path}: compressed to {dest.name}, but the original stays: "
                            f"{error}") from error
    return outcome
