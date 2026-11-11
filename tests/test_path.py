import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from shared import path


class FolderTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_mkdir(self):
        made = path.mkdir(str(self.root / "a" / "b"))
        self.assertEqual(made, self.root / "a" / "b")
        self.assertTrue(made.is_dir())
        self.assertEqual(path.mkdir(made), made)

    def test_unique(self):
        target = self.root / "clip.mp4"
        self.assertEqual(path.unique(target), target)
        target.write_bytes(b"")
        self.assertEqual(path.unique(target), self.root / "clip (1).mp4")
        (self.root / "clip (1).mp4").write_bytes(b"")
        self.assertEqual(path.unique(str(target)), self.root / "clip (2).mp4")

    def test_unique_keeps_tar_suffix(self):
        (self.root / "bundle.tar.gz").write_bytes(b"")
        self.assertEqual(path.unique(self.root / "bundle.tar.gz"), self.root / "bundle (1).tar.gz")

    def test_expand(self):
        with mock.patch.dict(os.environ, {"HOME": str(self.root), "FLN_TEST_DIR": "sub"}):
            self.assertEqual(path.expand("~/$FLN_TEST_DIR/c.txt"), (self.root / "sub" / "c.txt").resolve())
        self.assertTrue(path.expand("rel").is_absolute())


class AtomicTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.target = self.root / "out" / "clip.mp4"

    def tearDown(self):
        self.tmp.cleanup()

    def files(self):
        return sorted(p.name for p in self.target.parent.iterdir())

    def test_success_leaves_only_target(self):
        with path.atomic(self.target) as f:
            f.write(b"data")
        self.assertEqual(self.files(), ["clip.mp4"])
        self.assertEqual(self.target.read_bytes(), b"data")

    def test_text_mode(self):
        with path.atomic(self.target, mode="w", encoding="utf-8") as f:
            f.write("é")
        self.assertEqual(self.target.read_text(encoding="utf-8"), "é")

    def test_exception_leaves_nothing(self):
        with self.assertRaises(RuntimeError):
            with path.atomic(self.target) as f:
                f.write(b"half")
                raise RuntimeError("boom")
        self.assertEqual(self.files(), [])

    def test_existing_target_replaced_only_on_success(self):
        self.target.parent.mkdir(parents=True)
        self.target.write_bytes(b"old")
        with self.assertRaises(RuntimeError):
            with path.atomic(self.target) as f:
                f.write(b"new")
                raise RuntimeError("boom")
        self.assertEqual(self.target.read_bytes(), b"old")
        with path.atomic(self.target) as f:
            f.write(b"new")
        self.assertEqual(self.target.read_bytes(), b"new")
        self.assertEqual(self.files(), ["clip.mp4"])

    def test_read_mode_refused(self):
        with self.assertRaises(ValueError):
            with path.atomic(self.target, mode="rb"):
                pass


if __name__ == "__main__":
    unittest.main()
