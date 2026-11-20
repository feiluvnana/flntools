"""Playlists: HLS (`.m3u8`) and segmented DASH (`.mpd`) read into segments, fetched at once.

COOKBOOK.md#for-developers says where it sits in a transfer.
"""

from __future__ import annotations

import asyncio
import math
import re
import xml.etree.ElementTree as ElementTree
from collections import deque
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, replace
from urllib.parse import urljoin, urlsplit

from flnkit import errors, text

__all__ = ["Get", "Playlist", "Segment", "Variant", "Write", "dash", "fetch_all", "hls", "pick"]

# A byte range is `(start, end)` with `end` exclusive, as a part's is.
Get = Callable[[str, tuple[int, int] | None], Awaitable[bytes]]
Write = Callable[[bytes], Awaitable[None]]

# HLS attribute lists (RFC 8216, 4.2): KEY=value or KEY="quoted, with commas".
_ATTRIBUTE = re.compile(r'([A-Z0-9-]+)=("[^"]*"|[^,]*)')
# DASH template identifiers (ISO 23009-1, 5.3.9.4.4), with an optional `%0<width>d`.
_IDENTIFIER = re.compile(r"\$(RepresentationID|Number|Time|Bandwidth)(?:%0(\d+)d)?\$|\$\$")
_DURATION = re.compile(r"P(?:([\d.]+)D)?(?:T(?:([\d.]+)H)?(?:([\d.]+)M)?(?:([\d.]+)S)?)?")
_VIDEO_CODECS = ("avc", "mp4v", "hev", "hvc", "vp", "av01")
_AUDIO_CODECS = ("mp4a", "opus", "vorbis", "aac", "ac-3", "ec-3", "flac")
_NO_KEY = "the stream is locked with a key (AES or DRM), which can't be read"
# Caps on what a hostile playlist can make this build: media segments per variant, a period's
# seconds, a `%0Nd` width, one URL's bytes, and all of one playlist's URLs' bytes.
MOST = 100_000
LONGEST = 7 * 24 * 3600
WIDEST = 20
URL_LONGEST = 8 << 10
OUTPUT_MOST = 64 << 20
# What one built segment costs beside its URL's bytes (the object and its fields, rounded up),
# so OUTPUT_MOST bounds memory whatever the playlist's shape; and the segments of all variants.
SEGMENT_COST = 256
PLAYLIST_MOST = 4 * MOST


@dataclass(frozen=True, slots=True)
class Segment:
    """One piece of a stream, in playlist order.

    Args:
        url: the absolute URL, resolved against the playlist's.
        duration: its length in seconds; 0 for an init segment.
        span: the byte range `(start, end)`, `end` exclusive; None for the whole resource.
        init: whether it is an init map (fMP4's header), written before the media after it.
        discontinuity: whether the media changes encoding here (HLS `#EXT-X-DISCONTINUITY`).
    """

    url: str
    duration: float = 0.0
    span: tuple[int, int] | None = None
    init: bool = False
    discontinuity: bool = False


@dataclass(frozen=True, slots=True)
class Variant:
    """One rendition a playlist offers.

    Args:
        url: an HLS media playlist's absolute URL; for DASH, the manifest's.
        bandwidth: bits a second; 0 when not given.
        width: pixels; 0 when not given.
        height: pixels; 0 when not given.
        codecs: the `CODECS` text, as given.
        audio: the audio to merge with it, None when it carries its own (or none).
        id: the DASH representation's id; empty for HLS.
        segments: the DASH representation's segments; empty for HLS, whose `url` lists them.
    """

    url: str
    bandwidth: int = 0
    width: int = 0
    height: int = 0
    codecs: str = ""
    audio: Variant | None = None
    id: str = ""
    segments: tuple[Segment, ...] = ()


@dataclass(frozen=True, slots=True)
class Playlist:
    """What a playlist holds: a master's variants, or a media playlist's segments.

    Args:
        variants: the renditions to `pick` from; empty for an HLS media playlist.
        segments: an HLS media playlist's segments; empty otherwise.
        ended: False for a live HLS playlist (no `#EXT-X-ENDLIST`), which may grow.
    """

    variants: tuple[Variant, ...] = ()
    segments: tuple[Segment, ...] = ()
    ended: bool = True


def pick(variants: Sequence[Variant], *, height: int | None = None) -> Variant | None:
    """The variant with the most pixels, then the most bits, at or under `height`.

    Args:
        height: the tallest to take, in pixels; None means no cap.

    Returns:
        The variant; None when none fits.
    """
    fits = [v for v in variants if height is None or v.height <= height]
    return max(fits, key=lambda v: (v.width * v.height, v.bandwidth), default=None)


async def fetch_all(segments: Sequence[Segment], get: Get, write: Write, *,
                    at_once: int = 8) -> int:
    """Fetch `segments` up to `at_once` at a time, and write each in playlist order.

    At most `at_once` segments are in flight or waiting to be written, so memory stays bounded
    by the slowest one. A failed `get` cancels the rest; retrying is the caller's.

    Args:
        get: fetches a URL, or a byte range of it (`end` exclusive).
        write: takes the next bytes, in order.
        at_once: the most fetches at once, from the caller's cap.

    Returns:
        The bytes written.
    """
    if at_once < 1:
        raise ValueError(f"at_once must be at least 1, not {at_once}")
    queue: deque[asyncio.Task[bytes]] = deque()
    pending = iter(segments)
    written = 0
    try:
        for segment in pending:
            queue.append(asyncio.ensure_future(get(segment.url, segment.span)))
            if len(queue) < at_once:
                continue
            written += await _write(queue.popleft(), write)
        while queue:
            written += await _write(queue.popleft(), write)
    finally:
        for task in queue:
            task.cancel()
        await asyncio.gather(*queue, return_exceptions=True)
    return written


async def _write(task: asyncio.Task[bytes], write: Write) -> int:
    data = await task
    await write(data)
    return len(data)


# ---------------------------------------------------------------------------------------------
# What both readers share: one error boundary, and the caps on what they build
# ---------------------------------------------------------------------------------------------

def _guarded(read: Callable[[], Playlist], where: str) -> Playlist:
    """`read()`, with any error but a `SiteError` turned into one: this is the one boundary."""
    try:
        return read()
    except errors.SiteError:
        raise
    except Exception as error:
        raise errors.SiteError(f"{where}: can't read this playlist") from error


class _Out:
    """What one playlist builds: each URL at most `URL_LONGEST`, and its URLs' bytes and
    `SEGMENT_COST` per segment at most `OUTPUT_MOST`; `plan` checks a DASH playlist first."""

    def __init__(self, where: str) -> None:
        self.where = where
        self.total = 0

    def _charge(self, size: int) -> None:
        self.total += size
        if self.total > OUTPUT_MOST:
            raise errors.SiteError(f"{self.where}: over {OUTPUT_MOST:,} bytes built")

    def segment(self, url: str, duration: float = 0.0, span: tuple[int, int] | None = None, *,
                init: bool = False, discontinuity: bool = False) -> Segment:
        """A new segment of `url` (already built by `url`), charged to the playlist."""
        self._charge(SEGMENT_COST)
        return Segment(url, duration, span, init, discontinuity)

    def plan(self, media: int, size: int) -> None:
        """Refuse, before building any, `media` segments of about `size` bytes past a cap."""
        if media > PLAYLIST_MOST:
            raise errors.SiteError(f"{self.where}: {media:,} segments in all, "
                                   f"over {PLAYLIST_MOST:,}")
        if size > OUTPUT_MOST:
            raise errors.SiteError(f"{self.where}: over {OUTPUT_MOST:,} bytes built")

    def url(self, base: str, ref: str) -> str:
        if len(ref) > URL_LONGEST:
            raise errors.SiteError(f"{self.where}: a URL over {URL_LONGEST:,} bytes")
        found = urljoin(base, ref)
        if len(found) > URL_LONGEST:
            raise errors.SiteError(f"{self.where}: a URL over {URL_LONGEST:,} bytes")
        self._charge(len(found))
        # A `file:` URI (or `javascript:`) must never reach `get`.
        if urlsplit(found).scheme not in ("http", "https"):
            raise errors.SiteError(f"{self.where}: a URL that isn't http or https "
                                   f"({text.redact(found)})")
        return found

    def count(self, count: int) -> None:
        """Refuse a variant of over `MOST` media segments; init segments aren't counted."""
        _check_count(count, self.where)


def _check_count(count: int, where: str) -> None:
    if count > MOST:
        raise errors.SiteError(f"{where}: {count:,} segments, over {MOST:,}")


# ---------------------------------------------------------------------------------------------
# HLS
# ---------------------------------------------------------------------------------------------

def hls(playlist: str, url: str) -> Playlist:
    """Read an HLS playlist: a master's variants, or a media playlist's segments.

    Args:
        playlist: the `.m3u8` text.
        url: where it came from; relative URIs resolve against it.

    Raises:
        SiteError: it can't be read (not HLS, malformed, empty, over `MOST` media segments or
            the URL caps, a URL that isn't http or https), or it is encrypted (any `METHOD`
            but `NONE`; AES-128 included, which is out of scope).
    """
    where = text.redact(url)
    return _guarded(lambda: _hls(playlist, url, _Out(where)), where)


def _hls(playlist: str, url: str, out: _Out) -> Playlist:
    if len(playlist) > OUTPUT_MOST:
        raise errors.SiteError(f"{out.where}: a playlist over {OUTPUT_MOST:,} bytes")
    lines = [line.strip() for line in playlist.splitlines() if line.strip()]
    if not lines or not lines[0].startswith("#EXTM3U"):
        raise errors.SiteError(f"{out.where}: not an HLS playlist")
    if any(line.startswith("#EXT-X-STREAM-INF:") for line in lines):
        found = Playlist(variants=_master(lines, url, out))
    else:
        found = _media(lines, url, out)
    if not found.variants and not any(not s.init for s in found.segments):
        raise errors.SiteError(f"{out.where}: an empty playlist")
    return found


def _attributes(line: str) -> dict[str, str]:
    after = line.split(":", 1)[1] if ":" in line else ""
    return {key: value.strip('"') for key, value in _ATTRIBUTE.findall(after)}


def _refuse_key(attributes: dict[str, str], where: str) -> None:
    method = attributes.get("METHOD", "NONE")
    if method != "NONE":
        raise errors.SiteError(f"{where}: the stream is encrypted ({method})", hint=_NO_KEY)


def _master(lines: list[str], url: str, out: _Out) -> tuple[Variant, ...]:
    audio: dict[str, Variant] = {}
    for line in lines:
        if line.startswith("#EXT-X-SESSION-KEY:"):
            _refuse_key(_attributes(line), out.where)
        if not line.startswith("#EXT-X-MEDIA:"):
            continue
        found = _attributes(line)
        group, uri = found.get("GROUP-ID", ""), found.get("URI")
        if found.get("TYPE") != "AUDIO" or not uri:
            continue
        if group not in audio or found.get("DEFAULT") == "YES":
            audio[group] = Variant(out.url(url, uri))
    variants, stream = [], None
    for line in lines:
        if line.startswith("#EXT-X-STREAM-INF:"):
            stream = _attributes(line)
        elif stream is not None and not line.startswith("#"):
            width, _, height = stream.get("RESOLUTION", "").partition("x")
            out.count(len(variants) + 1)
            variants.append(Variant(
                out.url(url, line), bandwidth=_meta(stream.get("BANDWIDTH")),
                width=_meta(width), height=_meta(height), codecs=stream.get("CODECS", ""),
                audio=audio.get(stream.get("AUDIO", ""))))
            stream = None
    return tuple(variants)


def _media(lines: list[str], url: str, out: _Out) -> Playlist:
    segments: list[Segment] = []
    duration, span, broken = 0.0, None, False
    # Where the last byte range ended: a BYTERANGE without `@offset` starts there.
    end, served = 0, 0
    init: tuple[str, tuple[int, int] | None] | None = None
    for line in lines:
        tag = line.split(":", 1)[0]
        if tag == "#EXTINF":
            duration = float(line.split(":", 1)[1].split(",", 1)[0] or 0)
            if not 0 <= duration < math.inf:
                raise errors.SiteError(f"{out.where}: a segment of no real length")
        elif tag == "#EXT-X-BYTERANGE":
            span = _byterange(line.split(":", 1)[1], end, out.where)
        elif tag == "#EXT-X-DISCONTINUITY":
            broken = True
        elif tag == "#EXT-X-KEY":
            _refuse_key(_attributes(line), out.where)
        elif tag == "#EXT-X-MAP":
            found = _attributes(line)
            ranged = _byterange(found["BYTERANGE"], 0, out.where) if "BYTERANGE" in found else None
            mapped = (out.url(url, found.get("URI", "")), ranged)
            if mapped != init:
                segments.append(out.segment(mapped[0], span=ranged, init=True))
                init = mapped
        elif not line.startswith("#"):
            served += 1
            out.count(served)
            segments.append(out.segment(out.url(url, line), duration, span, discontinuity=broken))
            end = span[1] if span else 0
            duration, span, broken = 0.0, None, False
    return Playlist(segments=tuple(segments), ended="#EXT-X-ENDLIST" in lines)


def _byterange(value: str, after: int, where: str) -> tuple[int, int]:
    length, _, offset = value.strip('"').partition("@")
    start = int(offset) if offset else after
    if start < 0 or int(length) < 0:
        raise errors.SiteError(f"{where}: a byte range that ends before it starts")
    return start, start + int(length)


# ---------------------------------------------------------------------------------------------
# DASH
# ---------------------------------------------------------------------------------------------

def dash(manifest: str, url: str) -> Playlist:
    """Read a static DASH manifest's representations, each with its segments.

    Video representations are the variants, each with the audio of most bandwidth; an MPD
    without video offers its audio. Handled: `BaseURL` at every level, `SegmentTemplate` with
    `$Number$` or `$Time$` (and `SegmentTimeline`), `SegmentList`, and a plain `BaseURL` file.

    Args:
        manifest: the `.mpd` XML text.
        url: where it came from; relative URLs resolve against it.

    Raises:
        SiteError: it can't be read (not an MPD, a DOCTYPE, malformed, a template of unknown
            length, a variant with no media segment or over `MOST`, a period over `LONGEST`
            seconds, over the URL caps, a URL that isn't http or https); or it is live
            (`type="dynamic"`), has more than one period, or is protected
            (`ContentProtection`).
    """
    where = text.redact(url)
    return _guarded(lambda: _dash(manifest, url, _Out(where)), where)


def _dash(manifest: str, url: str, out: _Out) -> Playlist:
    where = out.where
    # A DOCTYPE is how entities get in (billion laughs); no real MPD needs one.
    if re.search(r"<!DOCTYPE", manifest, re.IGNORECASE):
        raise errors.SiteError(f"{where}: a DASH manifest with a DOCTYPE")
    try:
        root = ElementTree.fromstring(manifest)
    except ElementTree.ParseError:
        raise errors.SiteError(f"{where}: not a DASH manifest") from None
    if _tag(root) != "MPD":
        raise errors.SiteError(f"{where}: not a DASH manifest")
    if root.get("type") == "dynamic":
        raise errors.SiteError(f"{where}: a live DASH stream",
                               hint="only an ended stream can be saved")
    periods = _children(root, "Period")
    if len(periods) != 1:
        raise errors.SiteError(f"{where}: {len(periods)} periods; only one can be read")
    period = periods[0]
    length = _seconds(period.get("duration") or root.get("mediaPresentationDuration"))
    if length is not None and length > LONGEST:
        raise errors.SiteError(f"{where}: a period over {LONGEST:,} s")
    base = _base(_base(url, root), period)
    chosen = []
    for adaptation in _children(period, "AdaptationSet"):
        for representation in _children(adaptation, "Representation"):
            if any(_tag(e) == "ContentProtection" for e in (*adaptation, *representation)):
                raise errors.SiteError(f"{where}: the stream is protected", hint=_NO_KEY)
            kind = _kind(adaptation, representation)
            if kind in ("video", "audio"):
                chosen.append((kind, (period, adaptation, representation)))
    # Every representation's count first, so a playlist over the caps builds nothing.
    planned = [_planned(levels, _base(base, levels[1]), length, where) for _, levels in chosen]
    out.plan(sum(count for count, _ in planned), sum(size for _, size in planned))
    found: dict[str, list[Variant]] = {"video": [], "audio": []}
    for kind, levels in chosen:
        found[kind].append(_representation(levels, url, _base(base, levels[1]), length, out))
    audio = max(found["audio"], key=lambda v: v.bandwidth, default=None)
    if not found["video"] and not found["audio"]:
        raise errors.SiteError(f"{where}: no video or audio in the manifest")
    if not found["video"]:
        return Playlist(variants=tuple(found["audio"]))
    return Playlist(variants=tuple(replace(v, audio=audio) for v in found["video"]))


def _tag(element: ElementTree.Element) -> str:
    return element.tag.rsplit("}", 1)[-1]


def _children(element: ElementTree.Element, name: str) -> list[ElementTree.Element]:
    return [child for child in element if _tag(child) == name]


def _child(element: ElementTree.Element, name: str) -> ElementTree.Element | None:
    return next(iter(_children(element, name)), None)


def _base(base: str, element: ElementTree.Element) -> str:
    found = _child(element, "BaseURL")
    return urljoin(base, found.text.strip()) if found is not None and found.text else base


def _kind(adaptation: ElementTree.Element, representation: ElementTree.Element) -> str:
    for element in (representation, adaptation):
        for name in ("mimeType", "contentType"):
            if value := element.get(name):
                return value.split("/")[0].lower()
    codecs = (representation.get("codecs") or adaptation.get("codecs") or "").lower()
    if codecs.startswith(_VIDEO_CODECS):
        return "video"
    return "audio" if codecs.startswith(_AUDIO_CODECS) else ""


def _planned(levels: tuple[ElementTree.Element, ...], base: str, length: float | None,
             where: str) -> tuple[int, int]:
    """A representation's media segments and about the bytes they'll take, before building."""
    representation = levels[2]
    base = _base(base, representation)
    template = _template(levels)
    listed = [found for level in levels if (found := _child(level, "SegmentList")) is not None]
    if template:
        runs = _timeline(template, _scale(template, where), length, where)
        count = sum(count for _, _, count in runs)
        values = {"RepresentationID": representation.get("id", ""),
                  "Bandwidth": representation.get("bandwidth", "0"),
                  "Number": _int(template.get("startNumber"), 1),
                  "Time": runs[0][0] if runs else 0}
        first = len(base) + len(_fill(str(template.get("media", "")), values, where))
        return count, count * (SEGMENT_COST + first)
    if listed:
        refs = [entry.get("media", "") for entry in _children(listed[-1], "SegmentURL")]
        return len(refs), sum(SEGMENT_COST + len(base) + len(ref) for ref in refs)
    return 1, SEGMENT_COST + len(base)


def _representation(levels: tuple[ElementTree.Element, ...], url: str, base: str,
                    length: float | None, out: _Out) -> Variant:
    """One representation's variant; `levels` are its period, adaptation set and itself."""
    adaptation, representation = levels[1], levels[2]
    base = _base(base, representation)
    template = _template(levels)
    listed = [found for level in levels if (found := _child(level, "SegmentList")) is not None]
    if template:
        segments = _templated(template, representation, base, length, out)
    elif listed:
        segments = _listed(listed[-1], base, out)
    else:
        segments = [out.segment(out.url(base, ""))]
    if not any(not segment.init for segment in segments):
        raise errors.SiteError(f"{out.where}: a representation with no segment")
    return Variant(url, bandwidth=_meta(representation.get("bandwidth")),
                   width=_meta(representation.get("width") or adaptation.get("width")),
                   height=_meta(representation.get("height") or adaptation.get("height")),
                   codecs=representation.get("codecs") or adaptation.get("codecs") or "",
                   id=representation.get("id", ""), segments=tuple(segments))


def _template(levels: tuple[ElementTree.Element, ...]) -> dict[str, object]:
    """The `SegmentTemplate` a representation inherits: attributes merged, the nearest timeline."""
    merged: dict[str, object] = {}
    for level in levels:
        found = _child(level, "SegmentTemplate")
        if found is None:
            continue
        merged.update(found.attrib)
        if (timeline := _child(found, "SegmentTimeline")) is not None:
            merged["timeline"] = timeline
    return merged


def _scale(template: dict[str, object], where: str) -> int:
    scale = _int(template.get("timescale"), 1)
    if scale <= 0:
        raise errors.SiteError(f"{where}: a timescale that isn't positive")
    return scale


def _templated(template: dict[str, object], representation: ElementTree.Element, base: str,
               length: float | None, out: _Out) -> list[Segment]:
    scale = _scale(template, out.where)
    number = _int(template.get("startNumber"), 1)
    runs = _timeline(template, scale, length, out.where)
    fill = {"RepresentationID": representation.get("id", ""),
            "Bandwidth": representation.get("bandwidth", "0")}

    def made(name: str, at: int, n: int) -> str:
        return out.url(base, _fill(name, {**fill, "Number": n, "Time": at}, out.where))

    segments = []
    if initialization := template.get("initialization"):
        segments.append(out.segment(made(str(initialization), 0, number), init=True))
    media = str(template.get("media", ""))
    n = number
    for start, ticks, count in runs:
        for k in range(count):
            segments.append(out.segment(made(media, start + k * ticks, n), ticks / scale))
            n += 1
    return segments


def _timeline(template: dict[str, object], scale: int, length: float | None,
              where: str) -> list[tuple[int, int, int]]:
    """The template's runs of segments: `(start, duration, count)`, in its ticks."""
    timeline = template.get("timeline")
    entries = _children(timeline, "S") if isinstance(timeline, ElementTree.Element) else []
    origin = _int(template.get("presentationTimeOffset"),
                  _int(entries[0].get("t")) if entries else 0)
    last = None if length is None else origin + round(length * scale)
    if not isinstance(timeline, ElementTree.Element):
        step = _int(template.get("duration"))
        if step <= 0 or last is None:
            raise errors.SiteError(f"{where}: a segment template without a length")
        count = math.ceil((last - origin) / step)
        _check_count(count, where)
        return [(0, step, max(0, count))]
    runs: list[tuple[int, int, int]] = []
    at, total = 0, 0
    # The start of the last segment so far; None before the first.
    latest: int | None = None
    for index, entry in enumerate(entries):
        start, ticks, repeat = _int(entry.get("t"), at), _int(entry.get("d")), _int(entry.get("r"))
        if ticks <= 0:
            raise errors.SiteError(f"{where}: a timeline segment of no length")
        # A packager's rounding may overlap the last segment by a tick or two, about 1% of
        # d. The t is kept as given: the server names the segment by it ($Time$).
        slack = 0 if latest is None else max(2, ticks // 100)
        if start < at - slack or repeat < -1:
            raise errors.SiteError(f"{where}: a timeline that runs backwards")
        if latest is not None and start <= latest:
            raise errors.SiteError(f"{where}: a timeline that runs backwards")
        at = start
        if repeat < 0:
            following = entries[index + 1].get("t") if index + 1 < len(entries) else None
            end = _int(following) if following is not None else last
            if end is None:
                raise errors.SiteError(f"{where}: an open segment timeline without a length")
            count = max(0, math.ceil((end - at) / ticks))
        else:
            count = 1 + repeat
        total += count
        _check_count(total, where)
        runs.append((at, ticks, count))
        if count:
            latest = at + ticks * (count - 1)
        at += ticks * count
    return runs


def _fill(name: str, values: dict[str, object], where: str) -> str:
    """`name` with its template identifiers filled, refused before it grows past a URL's cap."""
    pieces, size, last = [], 0, 0
    for match in _IDENTIFIER.finditer(name):
        value = _identifier(match, values, where)
        pieces += [name[last:match.start()], value]
        size += match.start() - last + len(value)
        if size > URL_LONGEST:
            raise errors.SiteError(f"{where}: a URL over {URL_LONGEST:,} bytes")
        last = match.end()
    pieces.append(name[last:])
    return "".join(pieces)


def _identifier(match: re.Match[str], values: dict[str, object], where: str) -> str:
    if match.group(0) == "$$":
        return "$"
    value, width = values[match.group(1)], match.group(2)
    if width and int(width) > WIDEST:
        raise errors.SiteError(f"{where}: a number padded wider than {WIDEST}")
    return f"{int(str(value)):0{width}d}" if width else str(value)


def _listed(listed: ElementTree.Element, base: str, out: _Out) -> list[Segment]:
    where = out.where
    _check_count(len(_children(listed, "SegmentURL")), where)
    segments = []
    if (initialization := _child(listed, "Initialization")) is not None:
        segments.append(out.segment(out.url(base, initialization.get("sourceURL", "")),
                                span=_range(initialization.get("range"), out.where), init=True))
    scale = _int(listed.get("timescale"), 1)
    if scale <= 0:
        raise errors.SiteError(f"{out.where}: a timescale that isn't positive")
    duration = _int(listed.get("duration")) / scale
    for entry in _children(listed, "SegmentURL"):
        segments.append(out.segment(out.url(base, entry.get("media", "")), duration,
                                _range(entry.get("mediaRange"), out.where)))
    return segments


def _range(value: str | None, where: str) -> tuple[int, int] | None:
    """A DASH `first-last` byte range (inclusive) as `(start, end)`, `end` exclusive."""
    if not value:
        return None
    first, _, last = value.partition("-")
    if int(last) < int(first):
        raise errors.SiteError(f"{where}: a byte range that ends before it starts")
    return int(first), int(last) + 1


def _seconds(value: str | None) -> float | None:
    """An ISO 8601 duration (`PT1H2M3.5S`, `P1DT2H`) in seconds; None when missing or odd."""
    found = _DURATION.fullmatch(value or "")
    if not value or not found:
        return None
    days, hours, minutes, seconds = (float(part or 0) for part in found.groups())
    return ((days * 24 + hours) * 60 + minutes) * 60 + seconds


def _int(value: object, default: int = 0) -> int:
    """A timing number: `default` when missing; anything but digits raises `ValueError`."""
    return default if value is None or value == "" else int(str(value))


def _meta(value: object) -> int:
    """A descriptive number (bandwidth, width, height): 0 when missing or odd."""
    try:
        return int(str(value))
    except ValueError:
        return 0
