"""flnd/common/files.py: part names and media validation, on a temp folder; ffmpeg and Pillow are mocked where named."""

import fnmatch
import io
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from media_samples import needs_pillow

from flnd.common import files
from shared import atomic, media, theme
from shared._theme import Screen


def jpeg(size=(64, 48)):
    from PIL import Image

    out = io.BytesIO()
    Image.effect_noise(size, 40).convert("RGB").save(out, "JPEG")
    return out.getvalue()


class FilesCase(unittest.TestCase):
    def setUp(self):
        screen = Screen(dest=io.StringIO(), inp=io.StringIO(""), width=300, tty=False, interactive=False)
        scope = theme.scope(screen=screen, color="none", unicode=True)
        scope.__enter__()
        self.addCleanup(scope.__exit__, None, None, None)
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.tmp = Path(folder.name)


class PartNameTest(FilesCase):
    def test_atomic_part_names_match_what_repair_removes(self):
        """Fix 13: every temporary file flnd writes comes from `atomic`, so sync's repair glob must match its parts."""
        with atomic.writing(self.tmp / "a.jpg") as part, atomic.writing(self.tmp / "a.jpg") as other:
            self.assertEqual(part.parent, self.tmp)
            self.assertTrue(fnmatch.fnmatch(part.name, files.PART_GLOB), part.name)
            self.assertTrue(part.name.startswith(".a.jpg."))
            self.assertNotEqual(part, other)


@needs_pillow
class CheckedTest(FilesCase):
    def test_checked_returns_the_extension_the_content_calls_for(self):
        path = self.tmp / "a.webp"
        path.write_bytes(jpeg())
        self.assertEqual(files.checked(path, "image"), ".jpg")

    def test_a_web_page_raises_not_media(self):
        path = self.tmp / "a.jpg"
        path.write_bytes(b"<!doctype html><title>Log in</title>")
        with self.assertRaises(files.NotMediaError) as caught:
            files.checked(path, "image")
        self.assertEqual(str(caught.exception), "got a web page instead of a photo")

    def test_a_broken_file_raises_broken_and_an_unsafe_one_unsafe(self):
        whole = jpeg((400, 300))
        path = self.tmp / "a.jpg"
        path.write_bytes(whole[:300])
        with self.assertRaises(media.BrokenError):
            files.checked(path, "image")
        with mock.patch("shared.media.open", side_effect=media.UnsafeError("too many pixels")), \
                self.assertRaises(media.UnsafeError):
            files.checked(path, "image")

    def test_a_file_that_cant_be_checked_here_is_kept(self):
        """Without ffmpeg, without an extra, or on a stalled ffmpeg, the file isn't known to be broken."""
        path = self.tmp / "v.mp4"
        path.write_bytes(b"\0\0\0\x18ftypisom" + bytes(100))
        for error in (RuntimeError("shared.media needs ffmpeg"), ImportError("no Pillow"), TimeoutError("stalled")):
            with mock.patch("shared.media.open", side_effect=error):
                self.assertEqual(files.checked(path, "video"), ".mp4")

    def test_without_decode_the_file_is_only_sniffed(self):
        path = self.tmp / "a.jpg"
        path.write_bytes(jpeg())
        with mock.patch("shared.media.open", side_effect=AssertionError("decoded")):
            self.assertEqual(files.checked(path, "image", decode=False), ".jpg")


if __name__ == "__main__":
    unittest.main()
