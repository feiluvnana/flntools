"""flnd/common/paths.py: part names and media validation, on a temp folder."""

import fnmatch
import io
import tempfile
import unittest
from pathlib import Path

from flnd.common import paths
from flnd import _files
from flnd._ui import ui
from flnd.errors import SiteError


def jpeg(size=(64, 48)):
    return b"\xff\xd8\xff\xe0\x00\x10JFIF\x00\x01\x01\x00\x00\x01\x00\x01\x00\x00"


class FilesCase(unittest.TestCase):
    def setUp(self):
        scope = ui.scope(dest=io.StringIO(), width=300, color=False)
        scope.__enter__()
        self.addCleanup(scope.__exit__, None, None, None)
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.tmp = Path(folder.name)


class PartNameTest(FilesCase):
    def test_part_names_match_what_sweep_removes(self):
        """Every file flnd writes goes through a part, so the sweep's glob matches it."""
        with _files.part(self.tmp / "a.jpg") as held:
            self.assertEqual(held.path.parent, self.tmp)
            self.assertTrue(fnmatch.fnmatch(held.path.name, paths.PART_GLOB), held.path.name)
            self.assertEqual(held.path.name, "a.jpg.part")


class CheckedTest(FilesCase):
    def test_checked_returns_the_extension_the_content_calls_for(self):
        path = self.tmp / "a.webp"
        path.write_bytes(jpeg())
        self.assertEqual(paths.checked(path, "image"), ".jpg")

    def test_a_web_page_raises_not_media(self):
        path = self.tmp / "a.jpg"
        path.write_bytes(b"<!doctype html><title>Log in</title>")
        with self.assertRaises(SiteError) as caught:
            paths.checked(path, "image")
        self.assertEqual(str(caught.exception), "got a web page instead of a photo")


if __name__ == "__main__":
    unittest.main()
