"""Photos for shared.media, through Pillow and pi-heif. Private: callers use `media`.

It wraps Pillow, with pi-heif registered for HEIC, and imports it on first use. Every open names
the formats it allows, so no decoder that runs another program (EPS starts Ghostscript) is ever
reached, and Pillow's decompression-bomb check stays on. It exposes no `.raw`: callers get
`media`'s own `Media` and `Info`, and the Pillow images here stay inside the `media` files.

    photo = open(Path("a.heic"))                      # None when Pillow doesn't know the file
    if photo is not None:
        info = probe(Path("a.heic"), photo)           # the Info record, without decoding pixels
        save(upright(photo, keep=True), Path("a.jpg"), "JPEG", quality=90)
"""

from __future__ import annotations

import functools
import io
import re
import warnings
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any

from shared import _media

if TYPE_CHECKING:
    from PIL import Image

# Private: what `media`'s files call.
__all__ = [
    "animation", "check", "durations", "load", "open", "probe", "resized", "save", "save_animation",
]

READABLE = (
    "JPEG", "PNG", "GIF", "WEBP", "HEIF", "AVIF", "TIFF", "BMP", "ICO", "TGA", "PPM",
    "JPEG2000", "QOI", "PSD", "DDS",
)
NO_ALPHA = ("JPEG", "BMP", "PPM")
LOSSY = ("JPEG", "WEBP", "AVIF", "JPEG2000")
# EXIF tag numbers from the Exif 2.3 spec. 0x8769 and 0x8825 point to the Exif and GPS sub-blocks.
# DATE_ORIGINAL and OFFSET_ORIGINAL are when taken and zone; DATE_TIME is when modified.
IFD_EXIF, IFD_GPS = 0x8769, 0x8825
ORIENTATION, DATE_TIME = 0x0112, 0x0132
DATE_ORIGINAL, OFFSET, OFFSET_ORIGINAL = 0x9003, 0x9010, 0x9011


@functools.cache
def load() -> Any:
    """PIL.Image ready: HEIC registered, and 89 MP warning silenced so big photos open."""
    try:
        from PIL import Image
    except ImportError:
        raise _media.MissingError(
            "shared.media needs Pillow for photos: pip install 'flntools[media]'"
        ) from None
    try:
        import pi_heif

        pi_heif.register_heif_opener()
    except ImportError:
        pass
    warnings.simplefilter("ignore", Image.DecompressionBombWarning)
    return Image


def open(path: Path, *, memory: bool = True) -> Image.Image | None:
    """The photo at `path`, or None when Pillow doesn't recognize it (it may be a video).

        photo = open(Path("a.jpg"), memory=False)
        if photo is not None:
            photo.close()

    Args:
        path: the file to open.
        memory: True to read the whole file into memory first, so no file handle stays open behind
            animation nobody closes; False keeps file open, and caller closes image.

    Returns:
        The opened image, or None when Pillow doesn't know the format.

    Raises:
        UnsafeError: decompression bomb, or more pixels than 2x `MAX_IMAGE_PIXELS`.
        BrokenError: a file Pillow recognizes but can't read, such as a truncated one.
    """
    Image = load()
    try:
        # Allow-list: Pillow tries only these decoders, so EPS (Ghostscript) never runs.
        image = Image.open(io.BytesIO(path.read_bytes()) if memory else path, formats=READABLE)
    # Pillow raises this only past 2x MAX_IMAGE_PIXELS; warning is silenced in `load`.
    except Image.DecompressionBombError as error:
        raise _media.UnsafeError(f"{path.name}: {error}") from None
    # Not a photo, so the caller tries ffmpeg; this is a normal answer, not a failure.
    except Image.UnidentifiedImageError:
        return None
    # File Pillow recognized but couldn't read is broken; don't treat as video.
    except (OSError, SyntaxError, ValueError) as error:
        raise _media.BrokenError(f"{path.name}: {error}") from None
    frames = getattr(image, "n_frames", 1)
    # Bomb check is on total of every frame, so animation can't hide size in frame count.
    if frames * image.width * image.height > 2 * Image.MAX_IMAGE_PIXELS:
        image.close()
        msg = f"{path.name}: {frames} frames of {image.width}x{image.height} is too many pixels"
        raise _media.UnsafeError(msg)
    return image


def probe(path: Path, image: Image.Image) -> _media.Info:
    """The `Info` for opened photo: format, size, animation, EXIF date and place, tags.

    Args:
        path: the file, for `Info.path` and its size on disk.
        image: photo `open` returned. Header and EXIF read without decoding pixels, except
            that an animated WebP's frames are decoded once, to learn their delays.

    Returns:
        The photo's `Info`, with `width` and `height` as displayed.
    """
    exif = image.getexif()
    tags = _tags(exif)
    width, height = image.size
    # EXIF orientations 5 to 8 turn quarter turn; displayed width and height swap.
    if exif.get(ORIENTATION) in (5, 6, 7, 8):
        width, height = height, width
    name = (image.format or "").lower()
    is_mpo = name == "mpo"
    animated = False if is_mpo else bool(getattr(image, "is_animated", False))
    frames = 1 if is_mpo else getattr(image, "n_frames", 1)
    delays = _scan_delays(path, name) if animated else None
    if animated and delays is None:
        delays = [delay for _, delay in durations(image)]
    duration = sum(delays) / 1000 if delays else None
    return _media.Info(
        path=path, kind="image", animated=animated, format="jpeg" if is_mpo else name,
        size=path.stat().st_size, width=width, height=height, duration=duration, frames=frames,
        fps=frames / duration if animated and duration else None,
        created=_created(exif), location=_location(exif),
        tags=tags)


def _tags(exif: Image.Exif) -> dict[str, str]:
    """The readable EXIF tags of a photo, as name to text; binary and pointer fields are dropped."""
    from PIL import ExifTags

    tags: dict[str, str] = {}
    blocks = [
        (dict(exif), ExifTags.TAGS),
        (exif.get_ifd(IFD_EXIF), ExifTags.TAGS),
        (exif.get_ifd(IFD_GPS), ExifTags.GPSTAGS),
    ]
    for block, names in blocks:
        for key, value in block.items():
            # Binary fields (MakerNote, thumbnails) and pointers to other blocks left out.
            if key in (IFD_EXIF, IFD_GPS) or isinstance(value, (bytes, dict)):
                continue
            tags[names.get(key, str(key))] = str(value)
    return tags


def _created(exif: Image.Exif) -> datetime | None:
    """When photo was taken, from EXIF, timezone-aware if recorded; None if not a date."""
    block = exif.get_ifd(IFD_EXIF)
    text, offset = block.get(DATE_ORIGINAL), block.get(OFFSET_ORIGINAL)
    # Without original date, file changed time is used, with zone from OffsetTime.
    if not text:
        text, offset = exif.get(DATE_TIME), block.get(OFFSET)
    try:
        moment = datetime.strptime(str(text).strip("\0 "), "%Y:%m:%d %H:%M:%S")
    except ValueError:
        return None
    # The zone is "+HH:MM" or "-HH:MM"; a blank or malformed one leaves the time naive.
    if offset and (match := re.fullmatch(r"([+-])(\d{2}):(\d{2})", str(offset).strip("\0 "))):
        sign = -1 if match[1] == "-" else 1
        hours, minutes = int(match[2]), int(match[3])
        if hours < 24 and minutes < 60:
            moment = moment.replace(
                tzinfo=timezone(sign * timedelta(hours=hours, minutes=minutes))
            )
    return moment


def _location(exif: Image.Exif) -> tuple[float, float] | None:
    """(lat, lon) in degrees from GPS block, signed by hemisphere; None when absent or 0, 0."""
    gps = exif.get_ifd(IFD_GPS)
    # Coordinates are deg, min, sec as three rationals; weights 1, 1/60, 1/3600 fold them.
    try:
        latitude = sum(float(part) / 60 ** index for index, part in enumerate(gps[2]))
        longitude = sum(float(part) / 60 ** index for index, part in enumerate(gps[4]))
    # A missing or malformed GPS field is no location at all, not an error the caller should see.
    except (KeyError, TypeError, ValueError, ZeroDivisionError):
        return None
    # South and west are negative; GPS stores them as unsigned values with an S or W letter.
    latitude *= -1 if gps.get(1) == "S" else 1
    longitude *= -1 if gps.get(3) == "W" else 1
    return None if latitude == longitude == 0 else (latitude, longitude)


def durations(image: Image.Image) -> list[tuple[int, int]]:
    """(frame index, milliseconds) for every frame of an animation; a still photo has one frame.

    Args:
        image: the opened photo; it is left on its first frame afterwards.
    """
    found = []
    for index in range(getattr(image, "n_frames", 1)):
        image.seek(index)
        if image.format == "WEBP":
            image.load()  # Pillow learns a WebP frame's duration only by decoding it
        # A missing or zero delay is read as 100 ms, the usual default for GIF and WebP frames.
        found.append((index, int(image.info.get("duration") or 100)))
    image.seek(0)
    return found


def animation(
    image: Image.Image, *, resolution: int | None = None, fps: float | None = None,
    start: float = 0.0, end: float | None = None,
) -> tuple[list[Image.Image], list[int]]:
    """The frames shown from `start` to `end`, as RGBA, with delays in milliseconds.

    Frames dropped to keep under `fps`, and duration of dropped frames goes to previous frame.

    Args:
        image: the opened animation; it is left on its first frame afterwards.
        resolution: the most pixels on each frame's shorter side, or None to keep the size.
        fps: a cap on the frame rate, or None to keep every frame.
        start: seconds from the beginning; frames that end before it are skipped.
        end: seconds from beginning; frames at or after it skipped, None for no end.

    Returns:
        The frames and their delays, paired by index.
    """
    frames: list[Image.Image] = []
    delays: list[int] = []
    clock, next_slot = 0.0, 0.0
    for index, delay in durations(image):
        shown, clock = clock, clock + delay / 1000
        if clock <= start or (end is not None and shown >= end):
            continue
        if frames and fps and shown < next_slot - 1e-6:
            delays[-1] += delay
            continue
        image.seek(index)
        frames.append(shrink(image.convert("RGBA"), resolution))
        delays.append(delay)
        next_slot = shown + 1 / fps if fps else 0.0
    image.seek(0)
    return frames, delays


def upright(image: Image.Image, *, keep: bool) -> Image.Image:
    """A copy turned the way it is displayed, holding only the metadata a save should write.

    Args:
        image: the photo, as Pillow opened it, with its EXIF orientation not yet applied.
        keep: True to carry EXIF, XMP and comments over too, as `Media._edit` and an unstripped
            `compress` do; False keeps only transparency, the background and a colour profile
            that isn't sRGB.

    Returns:
        A new image, turned upright; its `info` is rebuilt, so the caller's file is not changed.
    """
    from PIL import ImageOps

    info = dict(image.info)
    turned = ImageOps.exif_transpose(image) or image.copy()
    turned.info = {key: info[key] for key in ("transparency", "background") if key in info}
    icc = info.get("icc_profile")
    if keep:
        if "comment" in info:
            turned.info["comment"] = info["comment"]
        if "xmp" in info:
            turned.info["xmp"] = _strip_xmp_orientation(info["xmp"])
        # Turned copy's EXIF is written back with orientation already applied.
        if exif := turned.getexif():
            turned.info["exif"] = exif.tobytes()
    if icc and (keep or not _is_srgb(icc)):
        # A Display P3 photo looks wrong without its profile; sRGB is what a viewer assumes anyway.
        turned.info["icc_profile"] = icc
    return turned


def _strip_xmp_orientation(xmp: bytes) -> bytes:
    """Remove tiff:Orientation from XMP bytes after pixels have been turned upright."""
    xmp = re.sub(rb'\s*tiff:Orientation=["\']\d+["\']', b"", xmp)
    return re.sub(rb"<tiff:Orientation>\d+</tiff:Orientation>", b"", xmp)


def _scan_delays(path: Path, fmt: str) -> list[int] | None:
    """Fast binary scan of GIF or WebP animation frame delays in milliseconds."""
    if fmt not in ("gif", "webp"):
        return None
    data = path.read_bytes()
    return _gif_delays(data) if fmt == "gif" else _webp_delays(data)


def _gif_delays(data: bytes) -> list[int] | None:
    """Frame delays in ms from GIF Graphic Control Extensions, or None if malformed."""
    if len(data) < 13 or data[:6] not in (b"GIF87a", b"GIF89a"):
        return None
    pos = 13 + (3 << ((data[10] & 0x07) + 1) if data[10] & 0x80 else 0)
    delays: list[int] = []
    pending: int | None = None
    while pos < len(data):
        block = data[pos]
        if block == 0x3B:
            return delays or None
        if block == 0x21 and pos + 1 < len(data):
            if data[pos + 1] == 0xF9 and pos + 6 < len(data) and data[pos + 2] == 4:
                pending = int.from_bytes(data[pos + 4:pos + 6], "little") * 10
            pos = _skip_gif_subblocks(data, pos + 2 if data[pos + 1] == 0xF9 else pos + 2)
        elif block == 0x2C and pos + 10 <= len(data):
            delays.append(pending or 100)
            pending = None
            lct = 3 << ((data[pos + 9] & 0x07) + 1) if data[pos + 9] & 0x80 else 0
            pos = _skip_gif_subblocks(data, pos + 10 + lct + 1)
        else:
            return None
    return delays or None


def _skip_gif_subblocks(data: bytes, pos: int) -> int:
    while pos < len(data):
        size = data[pos]
        pos += 1 + size
        if size == 0:
            return pos
    return len(data)


def _webp_delays(data: bytes) -> list[int] | None:
    """Frame delays in ms from WebP ANMF chunk headers, or None if malformed."""
    if len(data) < 12 or data[:4] != b"RIFF" or data[8:12] != b"WEBP":
        return None
    pos, delays = 12, []
    while pos + 8 <= len(data):
        fourcc = data[pos:pos + 4]
        size = int.from_bytes(data[pos + 4:pos + 8], "little")
        payload = pos + 8
        if fourcc == b"ANMF" and payload + 16 <= len(data):
            delays.append(int.from_bytes(data[payload + 12:payload + 15], "little") or 100)
        pos = payload + size + (size & 1)
    return delays or None


def _is_srgb(icc: bytes) -> bool:
    """Whether colour profile `icc` is sRGB, read from description or its bytes."""
    try:
        from PIL import ImageCms

        desc = ImageCms.getProfileDescription(ImageCms.ImageCmsProfile(io.BytesIO(icc)))
        return "srgb" in desc.lower()
    # Without littleCMS, or for unreadable profile, sRGB name in first bytes decides.
    except (ImportError, OSError, ValueError):
        return b"sRGB" in icc[:512]


def shrink(image: Image.Image, resolution: int | None) -> Image.Image:
    """`image` with its shorter side capped at `resolution`, never scaled up.

    Args:
        image: the picture, upright.
        resolution: max pixels on shorter side; None or already small returns `image`.

    Returns:
        A resized copy, or `image` unchanged when nothing needed shrinking.
    """
    if not resolution or min(image.size) <= resolution:
        return image
    scale = resolution / min(image.size)
    return resized(image, (max(1, round(image.width * scale)), max(1, round(image.height * scale))))


def resized(image: Image.Image, size: tuple[int, int]) -> Image.Image:
    """`image` resized to `size` with Lanczos filtering, keeping its `info` (metadata) on the copy.

    Args:
        image: the picture to resize; it is not changed.
        size: the new (width, height) in pixels.

    Returns:
        A new image of `size`.
    """
    Image = load()
    copy = image.resize(size, Image.Resampling.LANCZOS)
    copy.info = image.info
    return copy


def save(
    image: Image.Image, part: Path, format: str, *, quality: int, optimize: bool = False
) -> None:
    """Write one still `image` as `format` (Pillow's name), in a mode that format holds.

    Args:
        image: the picture to write; converted first when its mode isn't one `format` holds.
        part: the file to write, a temporary path the caller then renames into place.
        format: Pillow's format name, such as "JPEG", "PNG" or "WEBP".
        quality: 1-100, for a lossy format; ignored by the lossless ones.
        optimize: spend more time for a smaller file: JPEG's optimized tables and progressive scan,
            PNG's optimizer; False by default.

    Raises:
        OSError: the file can't be written.
    """
    image = _writable(image, format)
    # ICO entries are at most 256 pixels a side, so a bigger picture is shrunk to fit first.
    if format == "ICO" and max(image.size) > 256:
        image.thumbnail((256, 256))
    image.save(part, format=format, **_options(format, quality, optimize), **_metadata(image))


def save_animation(
    frames: list[Image.Image], delays: list[int], part: Path, format: str, *, quality: int
) -> None:
    """Write `frames`, each shown for its delay in milliseconds, as an animated GIF, WebP or APNG.

    Args:
        frames: the pictures in order; each is converted to RGBA, so transparency survives.
        delays: milliseconds each frame is shown for, one per frame.
        part: the file to write, a temporary path the caller renames into place.
        format: Pillow's name for the animation: "GIF", "WEBP" or "PNG" (APNG).
        quality: 1-100, used for WebP only; GIF and PNG take no quality.

    Raises:
        OSError: the file can't be written.
    """
    first, *rest = [frame.convert("RGBA") for frame in frames]
    options = dict(quality=quality) if format == "WEBP" else {}
    # loop=0 repeats forever, which is what an animated picture in a viewer normally does.
    first.save(
        part, format=format, save_all=True, append_images=rest, duration=delays, loop=0, **options
    )


def _writable(image: Image.Image, format: str) -> Image.Image:
    """`image` in a mode `format` holds: flattened onto white if no alpha, else converted."""
    Image = load()
    if image.mode.startswith("I"):
        scaled = image.point(lambda p: p / 256).convert("L")
        scaled.info = dict(image.info)
        image = scaled
    alpha = image.mode in ("RGBA", "LA", "PA") or (
        image.mode == "P" and "transparency" in image.info
    )
    if format in NO_ALPHA and alpha:
        rgba = image.convert("RGBA")
        flat = Image.new("RGB", image.size, "white")
        flat.paste(rgba, mask=rgba.getchannel("A"))
        flat.info = image.info
        return flat
    if format in ("JPEG", "TIFF") and image.mode == "CMYK":
        return image
    if image.mode == "CMYK":
        return _cmyk_to_srgb(image)
    keeps = {
        "JPEG": ("RGB", "L"), "BMP": ("RGB", "L"), "PPM": ("RGB", "L"),
        "PNG": ("RGB", "RGBA", "L", "LA", "P"), "GIF": ("RGB", "RGBA", "L", "LA", "P"),
    }.get(format, ("RGB", "RGBA", "L"))
    if image.mode in keeps:
        return image
    converted = image.convert("RGBA" if alpha else "RGB")
    converted.info = image.info
    return converted


def _cmyk_to_srgb(image: Image.Image) -> Image.Image:
    """Convert CMYK image to sRGB and drop its CMYK ICC profile."""
    info = {k: v for k, v in image.info.items() if k != "icc_profile"}
    if icc := image.info.get("icc_profile"):
        try:
            from PIL import ImageCms

            src = ImageCms.ImageCmsProfile(io.BytesIO(icc))
            dst = ImageCms.createProfile("sRGB")
            converted = ImageCms.profileToProfile(image, src, dst, outputMode="RGB")
            if converted is not None:
                converted.info = info
                return converted
        except (ImportError, OSError, ValueError):
            pass
    converted = image.convert("RGB")
    converted.info = info
    return converted


def _options(format: str, quality: int, optimize: bool) -> dict[str, Any]:
    """Pillow's save options for `format`: the quality, and the optimizer when `optimize` is set."""
    if format == "JPEG":
        # Progressive JPEG is the slower-to-write kind, so it rides on `optimize` with the tables.
        return dict(quality=quality, optimize=optimize, progressive=optimize)
    if format in ("WEBP", "AVIF"):
        return dict(quality=quality)
    # JPEG 2000 takes layer rates, not 1-100; `quality / 2` is this module's mapping.
    if format == "JPEG2000":
        return dict(quality_mode="dB", quality_layers=[quality / 2])
    if format == "PNG":
        return dict(optimize=optimize)
    if format == "TIFF":
        return dict(compression="tiff_adobe_deflate")
    return {}


def _metadata(image: Image.Image) -> dict[str, Any]:
    keys = ("exif", "icc_profile", "xmp", "comment")
    return {k: image.info[k] for k in keys if image.info.get(k)}


def check(path: Path) -> None:
    """Decode every pixel of every frame; a truncated or corrupt photo raises BrokenError.

    Args:
        path: the photo to decode, read from disk in full.

    Raises:
        BrokenError: the file isn't a photo Pillow knows, or a frame fails to decode.
        UnsafeError: a decompression bomb, as `open` reports it.
    """
    image = open(path)
    if image is None:
        raise _media.BrokenError(f"{path.name}: not a photo")
    try:
        count = 1 if image.format == "MPO" else getattr(image, "n_frames", 1)
        for index in range(count):
            image.seek(index)
            image.load()
    except (OSError, SyntaxError, ValueError, EOFError) as error:
        raise _media.BrokenError(f"{path.name}: {error}") from None
