"""`media.info` and `Media.raw` for still photos, through Pillow, on the generated samples."""

import tempfile
import unittest
from pathlib import Path

from media_samples import FOLDER, Image, needs_pillow, sample
from shared import media


@needs_pillow
class ImageRawTest(unittest.TestCase):
    def setUp(self):
        self.folder = Path(tempfile.mkdtemp(dir=FOLDER))

    def test_info_without_open_handle(self):
        meta = media.info(sample("big.jpg"))
        self.assertEqual((meta.kind, meta.format, meta.width, meta.height), ("image", "jpeg", 4000, 3000))

    def test_raw_returns_pillow_image_and_closes(self):
        with media.open(sample("exif.jpg")) as photo:
            raw = photo.raw
            self.assertIsInstance(raw, Image.Image)
            cropped = raw.crop((0, 0, 20, 20))
            out = self.folder / "cropped.png"
            cropped.save(out)
        self.assertEqual(media.info(out).width, 20)
        with self.assertRaises(media.Error):
            _ = photo.raw


if __name__ == "__main__":
    unittest.main()
