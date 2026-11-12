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
import warnings
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any

from shared import _media

if TYPE_CHECKING:
    from PIL import Image

# Private: what `media`'s files call.
__all__ = ["animation", "check", "durations", "load", "open", "probe", "resized", "save", "save_animation",
           "shrink", "upright"]

READABLE = ("JPEG", "PNG", "GIF", "WEBP", "HEIF", "AVIF", "TIFF", "BMP", "ICO", "TGA", "PPM", "JPEG2000", "QOI",
            "PSD", "DDS")
NO_ALPHA = ("JPEG", "BMP", "PPM")
LOSSY = ("JPEG", "WEBP", "AVIF", "JPEG2000")
# EXIF tag numbers from the Exif 2.3 spec. 0x8769 and 0x8825 point to the Exif and GPS sub-blocks.
# DATE_ORIGINAL and OFFSET_ORIGINAL are when the picture was taken and its zone; DATE_TIME is when the
# file was last changed, which `_created` uses only when the original is missing.
IFD_EXIF, IFD_GPS = 0x8769, 0x8825
ORIENTATION, DATE_TIME, DATE_ORIGINAL, OFFSET, OFFSET_ORIGINAL = 0x0112, 0x0132, 0x9003, 0x9010, 0x9011


@functools.cache
def load() -> Any:
    """PIL.Image, ready: HEIC registered, and the 89 MP warning silenced so big phone photos open."""
    try:
        from PIL import Image
    except ImportError:
        raise ImportError("shared.media needs Pillow for photos: pip install 'flntools[media]'") from None
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
            an animation nobody closes; False to keep the file open, and the caller closes the image.

    Returns:
        The opened image, or None when Pillow doesn't know the format.

    Raises:
        UnsafeError: a decompression bomb, or more pixels than twice `MAX_IMAGE_PIXELS` over all frames.
        BrokenError: a file Pillow recognizes but can't read, such as a truncated one.
    """
    Image = load()
    try:
        # `formats` is the allow-list: Pillow tries only these decoders, so EPS (Ghostscript) never runs.
        image = Image.open(io.BytesIO(path.read_bytes()) if memory else path, formats=READABLE)
    # Pillow raises this only past twice MAX_IMAGE_PIXELS; the warning below that is silenced in `load`.
    except Image.DecompressionBombError as error:
        raise _media.UnsafeError(f"{path.name}: {error}") from None
    # Not a photo, so the caller tries ffmpeg; this is a normal answer, not a failure.
    except Image.UnidentifiedImageError:
        return None
    # A file Pillow recognized but couldn't read is broken, and says so rather than being taken for a video.
    except (OSError, SyntaxError, ValueError) as error:
        raise _media.BrokenError(f"{path.name}: {error}") from None
    frames = getattr(image, "n_frames", 1)
    # The bomb check is on the total of every frame, so an animation can't hide its size in its frame count.
    if frames * image.width * image.height > 2 * Image.MAX_IMAGE_PIXELS:
        image.close()
        raise _media.UnsafeError(f"{path.name}: {frames} frames of {image.width}x{image.height} is too many pixels")
    return image


def probe(path: Path, image: Image.Image) -> _media.Info:
    """The `Info` for an opened photo: its kind, format, size, animation, EXIF date and place, and tags.

    Args:
        path: the file, for `Info.path` and its size on disk.
        image: the photo `open` returned. Its header and EXIF are read without decoding pixels, except
            that an animated WebP's frames are decoded once, to learn their delays.

    Returns:
        The photo's `Info`, with `width` and `height` as displayed.
    """
    exif = image.getexif()
    tags = _tags(exif)
    width, height = image.size
    # EXIF orientations 5 to 8 turn the picture a quarter turn, so the displayed width and height swap.
    if exif.get(ORIENTATION) in (5, 6, 7, 8):
        width, height = height, width
    animated = bool(getattr(image, "is_animated", False))
    frames = getattr(image, "n_frames", 1)
    duration = sum(delay for _, delay in durations(image)) / 1000 if animated else None
    name = (image.format or "").lower()
    return _media.Info(
        path=path, kind="image", animated=animated, format="jpeg" if name == "mpo" else name,
        size=path.stat().st_size, width=width, height=height, duration=duration, frames=frames,
        fps=frames / duration if animated and duration else None, created=_created(exif), location=_location(exif),
        tags=tags)


def _tags(exif: Image.Exif) -> dict[str, str]:
    """The readable EXIF tags of a photo, as name to text; binary and pointer fields are dropped."""
    from PIL import ExifTags

    tags: dict[str, str] = {}
    blocks = [(dict(exif), ExifTags.TAGS), (exif.get_ifd(IFD_EXIF), ExifTags.TAGS), (exif.get_ifd(IFD_GPS),
                                                                                       ExifTags.GPSTAGS)]
    for block, names in blocks:
        for key, value in block.items():
            # Binary fields (MakerNote, thumbnails) and the pointers to the other blocks are left out.
            if key in (IFD_EXIF, IFD_GPS) or isinstance(value, (bytes, dict)):
                continue
            tags[names.get(key, str(key))] = str(value)
    return tags


def _created(exif: Image.Exif) -> datetime | None:
    """When the photo was taken, from EXIF, timezone-aware when the zone is recorded; None when it isn't a date."""
    block = exif.get_ifd(IFD_EXIF)
    text, offset = block.get(DATE_ORIGINAL), block.get(OFFSET_ORIGINAL)
    # Without the original date, the file's last-changed time is used, with the zone from OffsetTime.
    if not text:
        text, offset = exif.get(DATE_TIME), block.get(OFFSET)
    try:
        moment = datetime.strptime(str(text).strip("\0 "), "%Y:%m:%d %H:%M:%S")
    except ValueError:
        return None
    # The zone is "+HH:MM" or "-HH:MM"; a shorter or missing one leaves the time naive.
    if offset and len(str(offset)) >= 6:
        sign = -1 if str(offset)[0] == "-" else 1
        hours, minutes = int(str(offset)[1:3]), int(str(offset)[4:6])
        moment = moment.replace(tzinfo=timezone(sign * timedelta(hours=hours, minutes=minutes)))
    return moment


def _location(exif: Image.Exif) -> tuple[float, float] | None:
    """(latitude, longitude) in degrees from the GPS block, signed by the hemisphere; None when absent or 0, 0."""
    gps = exif.get_ifd(IFD_GPS)
    # Each coordinate is degrees, minutes, seconds as three rationals; weights 1, 1/60 and 1/3600 fold them.
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


def animation(image: Image.Image, *, resolution: int | None = None, fps: float | None = None, start: float = 0.0,
              end: float | None = None) -> tuple[list[Image.Image], list[int]]:
    """The frames shown from `start` to `end`, as RGBA, with their delays in milliseconds.

    Frames are dropped to keep under `fps`, and the time of each one dropped goes to the frame before.

    Args:
        image: the opened animation; it is left on its first frame afterwards.
        resolution: the most pixels on each frame's shorter side, or None to keep the size.
        fps: a cap on the frame rate, or None to keep every frame.
        start: seconds from the beginning; frames that end before it are skipped.
        end: seconds from the beginning; frames that start at or after it are skipped, None for no end.

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
        turned.info.update({key: info[key] for key in ("xmp", "comment") if key in info})
        # The turned copy's EXIF is written back with its orientation already applied, so it's re-serialized.
        if exif := turned.getexif():
            turned.info["exif"] = exif.tobytes()
    if icc and (keep or not _is_srgb(icc)):
        # A Display P3 photo looks wrong without its profile; sRGB is what a viewer assumes anyway.
        turned.info["icc_profile"] = icc
    return turned


def _is_srgb(icc: bytes) -> bool:
    """Whether the colour profile `icc` is sRGB, read from its description, or from its bytes when that fails."""
    try:
        from PIL import ImageCms

        return "srgb" in ImageCms.getProfileDescription(ImageCms.ImageCmsProfile(io.BytesIO(icc))).lower()
    # ImageCms needs littleCMS; without it, or for a profile it can't read, the sRGB name in the first bytes decides.
    except (ImportError, OSError, ValueError):
        return b"sRGB" in icc[:512]


def shrink(image: Image.Image, resolution: int | None) -> Image.Image:
    """`image` with its shorter side capped at `resolution`, never scaled up.

    Args:
        image: the picture, upright.
        resolution: the most pixels on the shorter side; None or a picture already that small returns `image` itself.

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


def save(image: Image.Image, part: Path, format: str, *, quality: int, optimize: bool = False) -> None:
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


def save_animation(frames: list[Image.Image], delays: list[int], part: Path, format: str, *, quality: int) -> None:
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
    first.save(part, format=format, save_all=True, append_images=rest, duration=delays, loop=0, **options)


def _writable(image: Image.Image, format: str) -> Image.Image:
    """`image` in a mode `format` holds: flattened onto white for a format with no alpha, else converted."""
    Image = load()
    alpha = image.mode in ("RGBA", "LA", "PA") or (image.mode == "P" and "transparency" in image.info)
    if format in NO_ALPHA and alpha:
        # Formats without transparency get it flattened onto white, as a viewer shows it.
        rgba = image.convert("RGBA")
        flat = Image.new("RGB", image.size, "white")
        flat.paste(rgba, mask=rgba.getchannel("A"))
        flat.info = image.info
        return flat
    keeps = ("RGB", "L") if format in NO_ALPHA else ("RGB", "RGBA", "L", "LA", "P") if format in ("PNG", "GIF") else (
        "RGB", "RGBA", "L")
    if image.mode in keeps:
        return image
    converted = image.convert("RGBA" if alpha else "RGB")
    converted.info = image.info
    return converted


def _options(format: str, quality: int, optimize: bool) -> dict[str, Any]:
    """Pillow's save options for `format`: the quality, and the optimizer when `optimize` is set."""
    if format == "JPEG":
        # Progressive JPEG is the slower-to-write kind, so it rides on `optimize` with the tables.
        return dict(quality=quality, optimize=optimize, progressive=optimize)
    if format in ("WEBP", "AVIF"):
        return dict(quality=quality)
    # JPEG 2000 takes its quality as layer rates, not a 1-100 value; `quality / 2` is this module's mapping.
    if format == "JPEG2000":
        return dict(quality_mode="dB", quality_layers=[quality / 2])
    if format == "PNG":
        return dict(optimize=optimize)
    if format == "TIFF":
        return dict(compression="tiff_adobe_deflate")
    return {}


def _metadata(image: Image.Image) -> dict[str, Any]:
    return {key: image.info[key] for key in ("exif", "icc_profile", "xmp", "comment") if image.info.get(key)}


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
        for index in range(getattr(image, "n_frames", 1)):
            image.seek(index)
            image.load()
    except (OSError, SyntaxError, ValueError, EOFError) as error:
        raise _media.BrokenError(f"{path.name}: {error}") from None
