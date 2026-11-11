import io
import os
import stat
import tarfile
import tempfile
import unittest
import zipfile
from pathlib import Path

from shared import archive

SUFFIXES = (".zip", ".tar", ".tar.gz", ".tgz", ".tar.bz2", ".tar.xz")


def tree(folder: Path) -> dict[str, bytes]:
    return {p.relative_to(folder).as_posix(): p.read_bytes() for p in sorted(folder.rglob("*")) if p.is_file()}


class ArchiveCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.album = self.root / "album"
        (self.album / "photos").mkdir(parents=True)
        (self.album / "empty").mkdir()
        (self.album / "a.txt").write_bytes(b"alpha")
        (self.album / "photos" / "1.jpg").write_bytes(os.urandom(5000))

    def tearDown(self):
        self.tmp.cleanup()


class RoundTripTest(ArchiveCase):
    def test_each_format(self):
        for suffix in SUFFIXES:
            with self.subTest(suffix=suffix):
                dest = archive.pack(self.album, self.root / f"bundle{suffix}")
                self.assertTrue(dest.is_file())
                self.assertFalse(dest.with_name(dest.name + ".part").exists())
                out = self.root / f"out{suffix}"
                extracted = archive.unpack(dest, out)
                self.assertEqual(tree(out / "album"), tree(self.album))
                self.assertTrue((out / "album" / "empty").is_dir())
                self.assertIn(out / "album" / "a.txt", extracted)
                listed = [name.rstrip("/") for name in archive.names(dest)]
                self.assertIn("album/photos/1.jpg", listed)

    def test_unknown_suffix(self):
        with self.assertRaises(ValueError):
            archive.pack(self.album, self.root / "bundle.rar")
        with self.assertRaises(ValueError):
            archive.names(self.root / "bundle.gz")


class RootTest(ArchiveCase):
    def test_single_source_defaults_to_its_parent(self):
        dest = archive.pack(self.album / "photos", self.root / "p.zip")
        self.assertEqual(archive.names(dest), ["photos/", "photos/1.jpg"])

    def test_explicit_root(self):
        dest = archive.pack(self.album / "photos", self.root / "p.tar", root=self.album / "photos")
        self.assertEqual(archive.names(dest), ["1.jpg"])

    def test_list_defaults_to_current_folder(self):
        previous = os.getcwd()
        os.chdir(self.album)
        try:
            dest = archive.pack(["a.txt", "photos"], self.root / "l.zip")
        finally:
            os.chdir(previous)
        self.assertEqual(archive.names(dest), ["a.txt", "photos/", "photos/1.jpg"])

    def test_list_with_root(self):
        dest = archive.pack([self.album / "a.txt", self.album / "photos" / "1.jpg"], self.root / "l.tar.gz",
                            root=self.album)
        self.assertEqual(archive.names(dest), ["a.txt", "photos/1.jpg"])

    def test_source_outside_root(self):
        with self.assertRaises(ValueError):
            archive.pack(self.album / "a.txt", self.root / "x.zip", root=self.album / "photos")


class UnsafeTest(ArchiveCase):
    def make_zip(self, name: str, *, link_to: str | None = None) -> Path:
        dest = self.root / "evil.zip"
        with zipfile.ZipFile(dest, "w") as zf:
            zf.writestr("fine.txt", b"ok")
            info = zipfile.ZipInfo(name)
            if link_to is not None:
                info.external_attr = (stat.S_IFLNK | 0o777) << 16
                zf.writestr(info, link_to)
            else:
                zf.writestr(info, b"evil")
        return dest

    def make_tar(self, name: str, *, link_to: str | None = None) -> Path:
        dest = self.root / "evil.tar"
        with tarfile.open(dest, "w") as tf:
            fine = tarfile.TarInfo("fine.txt")
            fine.size = 2
            tf.addfile(fine, io.BytesIO(b"ok"))
            info = tarfile.TarInfo(name)
            if link_to is not None:
                info.type, info.linkname = tarfile.SYMTYPE, link_to
                tf.addfile(info)
            else:
                info.size = 4
                tf.addfile(info, io.BytesIO(b"evil"))
        return dest

    def assert_refused(self, src: Path):
        out = self.root / "out"
        with self.assertRaises(archive.UnsafeError):
            archive.unpack(src, out)
        self.assertFalse(out.exists())
        self.assertFalse((self.root / "evil").exists())

    def test_zip(self):
        self.assert_refused(self.make_zip("../evil"))
        self.assert_refused(self.make_zip("/abs"))
        self.assert_refused(self.make_zip("link", link_to="../../etc"))
        self.assert_refused(self.make_zip("link", link_to="/etc/passwd"))

    def test_tar(self):
        self.assert_refused(self.make_tar("../evil"))
        self.assert_refused(self.make_tar("/abs"))
        self.assert_refused(self.make_tar("a/../../evil"))
        self.assert_refused(self.make_tar("link", link_to="../outside"))
        self.assert_refused(self.make_tar("sub/link", link_to="/etc"))

    def test_tar_device_refused(self):
        dest = self.root / "dev.tar"
        with tarfile.open(dest, "w") as tf:
            info = tarfile.TarInfo("fifo")
            info.type = tarfile.FIFOTYPE
            tf.addfile(info)
        self.assert_refused(dest)

    def test_link_inside_is_allowed(self):
        dest = self.make_tar("sub/link", link_to="../fine.txt")
        archive.unpack(dest, self.root / "out")
        self.assertEqual((self.root / "out" / "sub" / "link").read_bytes(), b"ok")


if __name__ == "__main__":
    unittest.main()
