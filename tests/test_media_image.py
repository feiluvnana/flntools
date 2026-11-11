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
        info = media.probe(path)
        return info.width, info.height

    def test_width(self):
        out = media.image.resize(sample("big.jpg"), width=1200, dest=self.folder / "a.jpg")
        self.assertEqual(self.size(out), (1200, 900))

    def test_box_fits_inside(self):
        out = media.image.resize(sample("big.jpg"), width=800, height=800, dest=self.folder / "a.webp")
        self.assertEqual(self.size(out), (800, 600))
        self.assertEqual(media.probe(out).format, "webp")

    def test_scales_up(self):
        out = media.image.resize(sample("small.png"), height=200, dest=self.folder / "a.png")
        self.assertEqual(self.size(out), (400, 200))

    def test_resolution_never_scales_up(self):
        out = media.image.resize(sample("small.png"), resolution=500, dest=self.folder / "a.png")
        self.assertEqual(self.size(out), (100, 50))

    def test_two_ways_at_once(self):
        with self.assertRaises(ValueError):
            media.image.resize(sample("big.jpg"), width=10, resolution=10, dest=self.folder / "a.jpg")
        with self.assertRaises(ValueError):
            media.image.resize(sample("big.jpg"), dest=self.folder / "a.jpg")

    def test_in_place(self):
        path = fresh("small.png")
        self.assertEqual(media.image.resize(path, width=10), path)
        self.assertEqual(self.size(path), (10, 5))
        self.assertEqual(len(list(path.parent.iterdir())), 1)

    def test_metadata_kept_unless_stripped(self):
        kept = media.image.resize(sample("exif.jpg"), width=20, dest=self.folder / "kept.jpg")
        self.assertEqual(media.probe(kept).tags["Model"], "Pixel 99")
        stripped = media.image.resize(sample("exif.jpg"), width=20, dest=self.folder / "stripped.jpg", strip=True)
        self.assertNotIn("Model", media.probe(stripped).tags)

    def test_animated_is_refused(self):
        with self.assertRaisesRegex(ValueError, "media.convert"):
            media.image.resize(sample("anim.webp"), width=10, dest=self.folder / "a.webp")


@needs_pillow
class CropTest(unittest.TestCase):
    def setUp(self):
        self.folder = Path(tempfile.mkdtemp(dir=FOLDER))

    def test_box_is_as_displayed(self):
        # exif.jpg is stored 80x40, red with a blue left half, and displayed turned to 40x80.
        out = media.image.crop(sample("exif.jpg"), box=(0, 0, 40, 40), dest=self.folder / "top.png")
        with Image.open(out) as image:
            self.assertEqual(image.size, (40, 40))
            red, green, blue = image.convert("RGB").getpixel((20, 20))
            self.assertGreater(blue, red)

    def test_box_outside(self):
        for box in ((0, 0, 41, 10), (10, 10, 10, 20), (-1, 0, 5, 5)):
            with self.subTest(box=box), self.assertRaises(ValueError):
                media.image.crop(sample("exif.jpg"), box=box, dest=self.folder / f"{box[2]}.jpg")

    def test_in_place(self):
        path = fresh("small.png")
        media.image.crop(path, box=(0, 0, 10, 10))
        self.assertEqual(media.probe(path).width, 10)


if __name__ == "__main__":
    unittest.main()
