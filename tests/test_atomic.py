"""shared/_atomic.py: the part file next to the destination, the one-step replace, and the cleanup, in a temp folder."""

import os
import stat
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from shared import _atomic


class AtomicCase(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.folder = Path(folder.name)
        self.dest = self.folder / "a.txt"

    def names(self):
        return sorted(path.name for path in self.folder.iterdir())


class WritingTest(AtomicCase):
    def test_the_part_is_hidden_next_to_dest_and_moved_onto_it(self):
        with _atomic.writing(self.dest) as part:
            self.assertEqual(part.parent, self.folder)
            self.assertRegex(part.name, r"^\.a\.txt\.[0-9a-f]{8}\.part$")
            part.write_text("new")
            self.assertFalse(self.dest.exists())
        self.assertEqual(self.dest.read_text(), "new")
        self.assertEqual(self.names(), ["a.txt"])

    def test_two_writers_get_their_own_parts(self):
        with _atomic.writing(self.dest) as first, _atomic.writing(self.dest) as second:
            self.assertNotEqual(first, second)

    def test_an_existing_dest_is_refused_unless_replaced(self):
        self.dest.write_text("old")
        with self.assertRaises(FileExistsError):
            with _atomic.writing(self.dest):
                self.fail("the block must not run")
        with _atomic.writing(self.dest, replace=True) as part:
            part.write_text("new")
        self.assertEqual(self.dest.read_text(), "new")

    def test_an_error_or_ctrl_c_leaves_dest_as_it_was_and_no_part(self):
        self.dest.write_text("old")
        for error in (ValueError("broken"), KeyboardInterrupt()):
            with self.subTest(error=type(error).__name__):
                with self.assertRaises(type(error)):
                    with _atomic.writing(self.dest, replace=True) as part:
                        part.write_text("half")
                        raise error
                self.assertEqual(self.dest.read_text(), "old")
                self.assertEqual(self.names(), ["a.txt"])

    def test_a_block_that_writes_nothing_leaves_dest_alone(self):
        self.dest.write_text("old")
        with _atomic.writing(self.dest, replace=True) as part:
            part.write_text("bigger")
            part.unlink()
        self.assertEqual(self.dest.read_text(), "old")

    def test_a_failed_rename_leaves_no_part(self):
        with mock.patch("os.replace", side_effect=OSError("disk gone")):
            with self.assertRaises(OSError):
                with _atomic.writing(self.dest) as part:
                    part.write_text("new")
        self.assertEqual(self.names(), [])


class WriteTest(AtomicCase):
    def test_bytes_are_written_as_they_are(self):
        # No "\r\n" on Windows: the file is opened in binary mode there.
        _atomic.write(self.dest, b"one\ntwo\n")
        self.assertEqual(self.dest.read_bytes(), b"one\ntwo\n")
        _atomic.write(self.dest, b"three")
        self.assertEqual(self.dest.read_bytes(), b"three")
        self.assertEqual(self.names(), ["a.txt"])

    @unittest.skipIf(sys.platform == "win32", "Windows has no POSIX modes")
    def test_mode_is_set_from_the_start(self):
        self.dest.write_text("old")
        os.chmod(self.dest, 0o644)
        modes = []
        real_replace = os.replace

        def replace(part, dest):
            modes.append(stat.S_IMODE(os.stat(part).st_mode))
            real_replace(part, dest)

        with mock.patch("os.replace", replace):
            _atomic.write(self.dest, b"secret", mode=0o600)
        self.assertEqual(modes, [0o600])
        self.assertEqual(stat.S_IMODE(self.dest.stat().st_mode), 0o600)

    def test_a_failed_write_leaves_no_part(self):
        with mock.patch("os.fdopen", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                _atomic.write(self.dest, b"x")
        self.assertEqual(self.names(), [])


if __name__ == "__main__":
    unittest.main()
