"""Part names (`flnd._files`) and media validation (`flnd._transfer.checked`), on a temp folder."""

import fnmatch
import io
import tempfile
import unittest
from pathlib import Path

from flnd import _files, _transfer
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
            self.assertTrue(fnmatch.fnmatch(held.path.name, "*.part"), held.path.name)
            self.assertEqual(held.path.name, "a.jpg.part")


class CheckedTest(FilesCase):
    def test_checked_returns_the_extension_the_content_calls_for(self):
        path = self.tmp / "a.webp"
        path.write_bytes(jpeg())
        self.assertEqual(_transfer.checked(path, "image"), ".jpg")

    def test_a_web_page_raises_not_media(self):
        path = self.tmp / "a.jpg"
        path.write_bytes(b"<!doctype html><title>Log in</title>")
        with self.assertRaises(SiteError) as caught:
            _transfer.checked(path, "image")
        self.assertEqual(str(caught.exception), "got a web page instead of a photo")


class DigestTest(FilesCase):
    """A part opened with `digest=True` gives the SHA-256 of exactly what landed."""

    def sha(self, data):
        import hashlib

        return hashlib.sha256(data).digest()

    def test_a_straight_write_is_hashed_as_it_goes_without_reading_back(self):
        from unittest import mock

        with mock.patch("flnd._files._digest_fd", side_effect=AssertionError("read back")), \
                _files.part(self.tmp / "a.bin", resume=True, digest=True) as held:
            held.write(b"one ")
            held.write(b"two")
        self.assertEqual(held.saved.hash, self.sha(b"one two"))

    def test_a_resumed_part_is_read_back_once(self):
        dest = self.tmp / "a.bin"
        with self.assertRaises(RuntimeError), _files.part(dest, resume=True) as held:
            held.write(b"kept ")
            raise RuntimeError("the network dropped")
        with _files.part(dest, resume=True, digest=True) as held:
            self.assertEqual(held.size, 5)
            held.write(b"rest")
        self.assertEqual(held.saved.hash, self.sha(b"kept rest"))

    def test_write_at_a_reset_and_a_tool_writing_by_name_are_hashed_as_they_landed(self):
        with _files.part(self.tmp / "a.bin", resume=True, digest=True) as held:
            held.write_at(4, b"tail")
            held.write_at(0, b"head")
        self.assertEqual(held.saved.hash, self.sha(b"headtail"))
        with _files.part(self.tmp / "b.bin", resume=True, digest=True) as held:
            held.write(b"first mirror")
            held.reset()
            held.write(b"second")
        self.assertEqual(held.saved.hash, self.sha(b"second"))
        with _files.part(self.tmp / "c.bin", resume=True, digest=True) as held:
            held.path.write_bytes(b"from ffmpeg")
        self.assertEqual(held.saved.hash, self.sha(b"from ffmpeg"))

    def test_without_digest_there_is_no_hash(self):
        with _files.part(self.tmp / "a.bin") as held:
            held.write(b"x")
        self.assertIsNone(held.saved.hash)
        self.assertEqual(_files.digest(held.saved.path), self.sha(b"x"))


class LinkTest(FilesCase):
    def test_a_link_replaces_the_name_atomically_and_shares_the_file(self):
        one = _files.write(self.tmp / "one.jpg", b"same")
        two = _files.write(self.tmp / "two.jpg", b"same")
        self.assertTrue(_files.link(one, two))
        self.assertEqual((one.stat().st_ino, one.stat().st_nlink), (two.stat().st_ino, 2))
        self.assertEqual(sorted(path.name for path in self.tmp.iterdir()), ["one.jpg", "two.jpg"])

    def test_a_file_system_without_links_changes_nothing(self):
        import errno
        from unittest import mock

        one = _files.write(self.tmp / "one.jpg", b"same")
        two = _files.write(self.tmp / "two.jpg", b"copy")
        for code in (errno.EXDEV, errno.EPERM, errno.EMLINK):
            with self.subTest(code=code), \
                    mock.patch("os.link", side_effect=OSError(code, "no links")):
                self.assertFalse(_files.link(one, two))
            self.assertEqual(two.read_bytes(), b"copy")


if __name__ == "__main__":
    unittest.main()
