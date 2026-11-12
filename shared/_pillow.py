"""Photos for shared.media, through Pillow and pi-heif. Private: callers use `media`.

Every open names the formats it allows, so no decoder that runs another program (EPS starts
Ghostscript) is ever reached, and Pillow's decompression-bomb check stays on.
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

    With `memory`, for a file probe has accepted as a photo, it is read into memory first, so no
    file handle stays open behind an animation nobody closes. Without it, the caller closes it.
    """
    Image = load()
    try:
        image = Image.open(io.BytesIO(path.read_bytes()) if memory else path, formats=READABLE)
    except Image.DecompressionBombError as error:
        raise _media.UnsafeError(f"{path.name}: {error}") from None
    except Image.UnidentifiedImageError:
        return None
    except (OSError, SyntaxError, ValueError) as error:
        raise _media.BrokenError(f"{path.name}: {error}") from None
    frames = getattr(image, "n_frames", 1)
    if frames * image.width * image.height > 2 * Image.MAX_IMAGE_PIXELS:
        image.close()
        raise _media.UnsafeError(f"{path.name}: {frames} frames of {image.width}x{image.height} is too many pixels")
    return image


def probe(path: Path, image: Image.Image) -> _media.Info:
    exif = image.getexif()
    tags = _tags(exif)
    width, height = image.size
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
    block = exif.get_ifd(IFD_EXIF)
    text, offset = block.get(DATE_ORIGINAL), block.get(OFFSET_ORIGINAL)
    if not text:
        text, offset = exif.get(DATE_TIME), block.get(OFFSET)
    try:
        moment = datetime.strptime(str(text).strip("\0 "), "%Y:%m:%d %H:%M:%S")
    except ValueError:
        return None
    if offset and len(str(offset)) >= 6:
        sign = -1 if str(offset)[0] == "-" else 1
        hours, minutes = int(str(offset)[1:3]), int(str(offset)[4:6])
        moment = moment.replace(tzinfo=timezone(sign * timedelta(hours=hours, minutes=minutes)))
    return moment


def _location(exif: Image.Exif) -> tuple[float, float] | None:
    gps = exif.get_ifd(IFD_GPS)
    try:
        latitude = sum(float(part) / 60 ** index for index, part in enumerate(gps[2]))
        longitude = sum(float(part) / 60 ** index for index, part in enumerate(gps[4]))
    except (KeyError, TypeError, ValueError, ZeroDivisionError):
        return None
    latitude *= -1 if gps.get(1) == "S" else 1
    longitude *= -1 if gps.get(3) == "W" else 1
    return None if latitude == longitude == 0 else (latitude, longitude)


def durations(image: Image.Image) -> list[tuple[int, int]]:
    """(frame index, milliseconds) for every frame of an animation."""
    found = []
    for index in range(getattr(image, "n_frames", 1)):
        image.seek(index)
        if image.format == "WEBP":
            image.load()  # Pillow learns a WebP frame's duration only by decoding it
        found.append((index, int(image.info.get("duration") or 100)))
    image.seek(0)
    return found


def animation(image: Image.Image, *, resolution: int | None = None, fps: float | None = None, start: float = 0.0,
              end: float | None = None) -> tuple[list[Image.Image], list[int]]:
    """The frames shown from `start` to `end`, as RGBA, with their delays in milliseconds.

    Frames are dropped to keep under `fps`, and the time of each one dropped goes to the frame before.
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
    """A copy turned the way it is displayed, holding only the metadata a save should write."""
    from PIL import ImageOps

    info = dict(image.info)
    turned = ImageOps.exif_transpose(image) or image.copy()
    turned.info = {key: info[key] for key in ("transparency", "background") if key in info}
    icc = info.get("icc_profile")
    if keep:
        turned.info.update({key: info[key] for key in ("xmp", "comment") if key in info})
        if exif := turned.getexif():
            turned.info["exif"] = exif.tobytes()
    if icc and (keep or not _is_srgb(icc)):
        # A Display P3 photo looks wrong without its profile; sRGB is what a viewer assumes anyway.
        turned.info["icc_profile"] = icc
    return turned


def _is_srgb(icc: bytes) -> bool:
    try:
        from PIL import ImageCms

        return "srgb" in ImageCms.getProfileDescription(ImageCms.ImageCmsProfile(io.BytesIO(icc))).lower()
    except (ImportError, OSError, ValueError):
        return b"sRGB" in icc[:512]


def shrink(image: Image.Image, resolution: int | None) -> Image.Image:
    """`image` with its shorter side capped at `resolution`, never scaled up."""
    if not resolution or min(image.size) <= resolution:
        return image
    scale = resolution / min(image.size)
    return resized(image, (max(1, round(image.width * scale)), max(1, round(image.height * scale))))


def resized(image: Image.Image, size: tuple[int, int]) -> Image.Image:
    Image = load()
    copy = image.resize(size, Image.Resampling.LANCZOS)
    copy.info = image.info
    return copy


def save(image: Image.Image, part: Path, format: str, *, quality: int, optimize: bool = False) -> None:
    """Write one still `image` as `format` (Pillow's name), in a mode that format holds."""
    image = _writable(image, format)
    if format == "ICO" and max(image.size) > 256:
        image.thumbnail((256, 256))
    image.save(part, format=format, **_options(format, quality, optimize), **_metadata(image))


def save_animation(frames: list[Image.Image], delays: list[int], part: Path, format: str, *, quality: int) -> None:
    """Write `frames`, each shown for its delay in milliseconds, as an animated GIF, WebP or APNG."""
    first, *rest = [frame.convert("RGBA") for frame in frames]
    options = dict(quality=quality) if format == "WEBP" else {}
    first.save(part, format=format, save_all=True, append_images=rest, duration=delays, loop=0, **options)


def _writable(image: Image.Image, format: str) -> Image.Image:
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
    if format == "JPEG":
        return dict(quality=quality, optimize=optimize, progressive=optimize)
    if format in ("WEBP", "AVIF"):
        return dict(quality=quality)
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
    """Decode every pixel of every frame; a truncated or corrupt photo raises BrokenError."""
    image = open(path)
    if image is None:
        raise _media.BrokenError(f"{path.name}: not a photo")
    try:
        for index in range(getattr(image, "n_frames", 1)):
            image.seek(index)
            image.load()
    except (OSError, SyntaxError, ValueError, EOFError) as error:
        raise _media.BrokenError(f"{path.name}: {error}") from None
