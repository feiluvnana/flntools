"""Part names, digests and links (`flnkit.files`)."""

import errno
import fnmatch
import hashlib
import io
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from flnd.ui.log import ui
from flnkit import files as _files


def sha(data):
    return hashlib.sha256(data).digest()


class FilesCase(unittest.TestCase):
    def setUp(self):
        self.enterContext(ui.scope(dest=io.StringIO(), width=300, color=False))
        self.tmp = Path(self.enterContext(tempfile.TemporaryDirectory()))

    def part(self, name, **options):
        return _files.part(self.tmp / name, resume=True, digest=True, **options)


class FilesTest(FilesCase):
    def test_part_names_match_what_sweep_removes(self):
        with _files.part(self.tmp / "a.jpg") as held:
            self.assertEqual(held.path, self.tmp / "a.jpg.part")
            self.assertTrue(fnmatch.fnmatch(held.path.name, "*.part"))

    def test_a_straight_write_is_hashed_as_it_goes_without_reading_back(self):
        with mock.patch("flnkit.files._digest_fd", side_effect=AssertionError("read back")), \
                self.part("a.bin") as held:
            held.write(b"one ")
            held.write(b"two")
        self.assertEqual(held.saved.hash, sha(b"one two"))

    def test_a_resumed_part_is_read_back_once(self):
        with self.assertRaises(RuntimeError), _files.part(self.tmp / "a.bin", resume=True) as held:
            held.write(b"kept ")
            raise RuntimeError("the network dropped")
        with self.part("a.bin") as held:
            self.assertEqual(held.size, 5)
            held.write(b"rest")
        self.assertEqual(held.saved.hash, sha(b"kept rest"))

    def test_write_at_a_reset_and_a_tool_writing_by_name_are_hashed_as_they_landed(self):
        with self.part("a.bin") as held:
            held.write_at(4, b"tail")
            held.write_at(0, b"head")
        self.assertEqual(held.saved.hash, sha(b"headtail"))
        with self.part("b.bin") as held:
            held.write(b"first mirror")
            held.reset()
            held.write(b"second")
        self.assertEqual(held.saved.hash, sha(b"second"))
        with self.part("c.bin") as held:
            held.path.write_bytes(b"from ffmpeg")
        self.assertEqual(held.saved.hash, sha(b"from ffmpeg"))

    def test_without_digest_there_is_no_hash(self):
        with _files.part(self.tmp / "a.bin") as held:
            held.write(b"x")
        self.assertIsNone(held.saved.hash)
        self.assertEqual(_files.digest(held.saved.path), sha(b"x"))

    def test_a_link_replaces_the_name_and_shares_the_file(self):
        one = _files.write(self.tmp / "one.jpg", b"same")
        two = _files.write(self.tmp / "two.jpg", b"same")
        self.assertTrue(_files.link(one, two))
        self.assertEqual((one.stat().st_ino, one.stat().st_nlink), (two.stat().st_ino, 2))
        self.assertEqual(sorted(path.name for path in self.tmp.iterdir()), ["one.jpg", "two.jpg"])

    def test_a_file_system_without_links_changes_nothing(self):
        one = _files.write(self.tmp / "one.jpg", b"same")
        two = _files.write(self.tmp / "two.jpg", b"copy")
        for code in (errno.EXDEV, errno.EPERM, errno.EMLINK):
            with self.subTest(code=code), mock.patch("os.link", side_effect=OSError(code, "no")):
                self.assertFalse(_files.link(one, two))
            self.assertEqual(two.read_bytes(), b"copy")


if __name__ == "__main__":
    unittest.main()
