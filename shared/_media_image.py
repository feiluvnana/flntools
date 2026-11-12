"""What `Media.resize` and `Media.crop` do to a still photo, through Pillow. Private: callers use `media`.

Each function takes a Pillow image already turned upright (as displayed) and returns a new one;
`Media._edit` opens the photo, calls one, and saves the result into the chain's temporary file.
Loaded on first use, so `from shared import media` never imports Pillow. Pillow is reached only
through `_pillow`, and there is no `.raw`.

    photo = _pillow.upright(_pillow.open(path), keep=True)
    smaller = fitted(photo, 1200, None)
"""

from __future__ import annotations

from typing import Any

from shared import _pillow

__all__ = ["cut", "fitted"]


def fitted(photo: Any, width: int | None, height: int | None) -> Any:
    """`photo` scaled to fit inside `width` x `height` (either may be None), keeping its aspect ratio, up or down.

    Args:
        photo: the upright Pillow image; it is not changed.
        width: the most pixels across, or None for no limit across.
        height: the most pixels down, or None for no limit down.

    Returns:
        A resized copy; both sides are at least 1 pixel.
    """
    # An absent side is an infinite limit, so the other side alone decides the scale.
    scale = min(width / photo.width if width else float("inf"), height / photo.height if height else float("inf"))
    return _pillow.resized(photo, (max(1, round(photo.width * scale)), max(1, round(photo.height * scale))))


def cut(photo: Any, box: tuple[int, int, int, int]) -> Any:
    """The part of `photo` inside `box`, (left, top, right, bottom), keeping its metadata.

    Args:
        photo: the upright Pillow image; it is not changed.
        box: (left, top, right, bottom) in pixels, with left < right and top < bottom, inside the photo.

    Returns:
        A new image of the box's size.

    Raises:
        ValueError: the box is empty, or reaches outside the photo.
    """
    left, top, right, bottom = box
    if not (0 <= left < right <= photo.width and 0 <= top < bottom <= photo.height):
        raise ValueError(f"box {box} isn't inside the {photo.width}x{photo.height} photo")
    cropped = photo.crop(box)
    # Pillow's crop drops `info`, which holds the EXIF and ICC profile the save writes back.
    cropped.info = photo.info
    return cropped
