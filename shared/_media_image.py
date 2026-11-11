"""media.image: what only means something for a still photo.

    media.image.resize("a.jpg", width=1200, dest="a.small.jpg")
    media.image.crop("a.jpg", box=(0, 0, 1080, 1080), dest="a.square.jpg")
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from shared import _media, _pillow
from shared._media import PathLike


def resize(src: PathLike, *, dest: PathLike | None = None, width: int | None = None, height: int | None = None,
           resolution: int | None = None, quality: int = 90, strip: bool = False) -> Path:
    """Scale a photo, keeping its aspect ratio, and return the path written.

        media.image.resize("a.jpg", width=1200)                     # replaces a.jpg
        media.image.resize("a.jpg", width=800, height=800, dest="a.thumb.webp")

    `width` and/or `height` give a box to fit into, scaling up when it is larger; `resolution`
    caps the shorter side instead, never scaling up. Give exactly one of the two ways.
    Sizes are as displayed. No `dest=` replaces `src`. Metadata is kept unless `strip`.
    """
    if (width is None and height is None) == (resolution is None):
        raise ValueError("give width= and/or height=, or resolution=")
    return _edit(src, dest, quality, strip, lambda photo: _fitted(photo, width, height, resolution))


def crop(src: PathLike, *, box: tuple[int, int, int, int], dest: PathLike | None = None, quality: int = 90,
         strip: bool = False) -> Path:
    """Keep the part of a photo inside `box`, (left, top, right, bottom) as displayed, and return the path.

        media.image.crop("a.jpg", box=(0, 0, 1080, 1080), dest="a.square.jpg")

    A box that is empty or reaches outside the photo raises `ValueError`.
    """
    left, top, right, bottom = box

    def cut(photo: Any) -> Any:
        if not (0 <= left < right <= photo.width and 0 <= top < bottom <= photo.height):
            raise ValueError(f"box {box} isn't inside the {photo.width}x{photo.height} photo")
        cropped = photo.crop(box)
        cropped.info = photo.info
        return cropped

    return _edit(src, dest, quality, strip, cut)


def _edit(src: PathLike, dest: PathLike | None, quality: int, strip: bool, change: Any) -> Path:
    src = Path(src)
    out = Path(dest) if dest is not None else src
    extension = _media.writable(out)
    if extension not in _media.STILLS:
        raise ValueError(f"{out.name}: media.image writes photos, not {extension}")
    if dest is not None and out.exists():
        raise FileExistsError(f"{out} already exists")
    info = _media.probe(src)
    if info.kind != "image" or info.animated:
        raise ValueError(f"{src.name} isn't a still photo; use media.convert, which keeps animation, "
                         "or media.video.frames")
    photo = _pillow.open(src)
    assert photo is not None
    with _media.writing(out, replace=dest is None) as part:
        _pillow.save(change(_pillow.upright(photo, keep=not strip)), part, _media.STILLS[extension], quality=quality)
    return out


def _fitted(photo: Any, width: int | None, height: int | None, resolution: int | None) -> Any:
    if resolution is not None:
        return _pillow.shrink(photo, resolution)
    scale = min(width / photo.width if width else float("inf"), height / photo.height if height else float("inf"))
    return _pillow.resized(photo, (max(1, round(photo.width * scale)), max(1, round(photo.height * scale))))
