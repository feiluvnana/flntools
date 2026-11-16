"""flnd/common/paths.py: part names and media validation, on a temp folder."""

import fnmatch
import io
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from flnd.common import paths
from shared import fs, ui


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
    def test_fs_stage_names_match_what_sweep_removes(self):
        """Every temporary file flnd writes comes from `fs.stage`, so the sweep's glob matches it."""
        with fs.stage(self.tmp / "a.jpg") as part:
            self.assertEqual(part.parent, self.tmp)
            self.assertTrue(fnmatch.fnmatch(part.name, paths.PART_GLOB), part.name)
            self.assertEqual(part.name, ".a.jpg.stage")


class CheckedTest(FilesCase):
    def test_checked_returns_the_extension_the_content_calls_for(self):
        path = self.tmp / "a.webp"
        path.write_bytes(jpeg())
        self.assertEqual(paths.checked(path, "image"), ".jpg")

    def test_a_web_page_raises_not_media(self):
        path = self.tmp / "a.jpg"
        path.write_bytes(b"<!doctype html><title>Log in</title>")
        with self.assertRaises(paths.NotMediaError) as caught:
            paths.checked(path, "image")
        self.assertEqual(str(caught.exception), "got a web page instead of a photo")


if __name__ == "__main__":
    unittest.main()
