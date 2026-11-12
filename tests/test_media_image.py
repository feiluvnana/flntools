"""`Media.resize` and `Media.crop`: still photos, through Pillow, on the generated samples. Nothing is faked."""

import tempfile
import unittest
from pathlib import Path

from media_samples import FOLDER, Image, fresh, needs_pillow, sample
from shared import media


@needs_pillow
class ResizeTest(unittest.TestCase):
    def setUp(self):
        self.folder = Path(tempfile.mkdtemp(dir=FOLDER))

    def size(self, path: Path) -> tuple:
        photo = media.open(path)
        return photo.width, photo.height

    def test_width(self):
        out = media.open(sample("big.jpg")).resize(width=1200).save(self.folder / "a.jpg")
        self.assertEqual(self.size(out), (1200, 900))

    def test_box_fits_inside(self):
        out = media.open(sample("big.jpg")).resize(width=800, height=800).convert("webp").save(self.folder / "a.webp")
        self.assertEqual(self.size(out), (800, 600))
        self.assertEqual(media.open(out).format, "webp")

    def test_scales_up(self):
        out = media.open(sample("small.png")).resize(height=200).save(self.folder / "a.png")
        self.assertEqual(self.size(out), (400, 200))

    def test_a_size_is_needed(self):
        with self.assertRaises(ValueError):
            media.open(sample("big.jpg")).resize()

    def test_in_place(self):
        path = fresh("small.png")
        self.assertEqual(media.open(path).resize(width=10).save(), path)
        self.assertEqual(self.size(path), (10, 5))
        self.assertEqual(len(list(path.parent.iterdir())), 1)

    def test_metadata_kept(self):
        kept = media.open(sample("exif.jpg")).resize(width=20).save(self.folder / "kept.jpg")
        self.assertEqual(media.open(kept).tags["Model"], "Pixel 99")

    def test_animated_is_a_kind_error(self):
        with self.assertRaisesRegex(media.KindError, "resize is for still photos; anim.webp is an animation"):
            media.open(sample("anim.webp")).resize(width=10)

    def test_heic_is_converted_first(self):
        with self.assertRaisesRegex(ValueError, "can't write"):
            media.open(sample("tiny.heic")).resize(width=10)
        out = media.open(sample("tiny.heic")).convert("png").resize(width=32).save(self.folder / "h.png")
        self.assertEqual(self.size(out)[0], 32)


@needs_pillow
class CropTest(unittest.TestCase):
    def setUp(self):
        self.folder = Path(tempfile.mkdtemp(dir=FOLDER))

    def test_box_is_as_displayed(self):
        # exif.jpg is stored 80x40, red with a blue left half, and displayed turned to 40x80.
        out = media.open(sample("exif.jpg")).crop((0, 0, 40, 40)).convert("png").save(self.folder / "top.png")
        with Image.open(out) as image:
            self.assertEqual(image.size, (40, 40))
            red, green, blue = image.convert("RGB").getpixel((20, 20))
            self.assertGreater(blue, red)

    def test_box_outside(self):
        for box in ((0, 0, 41, 10), (10, 10, 10, 20), (-1, 0, 5, 5)):
            with self.subTest(box=box), self.assertRaises(ValueError):
                media.open(sample("exif.jpg")).crop(box)

    def test_in_place(self):
        path = fresh("small.png")
        media.open(path).crop((0, 0, 10, 10)).save()
        self.assertEqual(media.open(path).width, 10)


if __name__ == "__main__":
    unittest.main()
