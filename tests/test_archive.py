"""Tests for `shared.archive`: packing, listing, and safe unpacking of archives."""

from __future__ import annotations

import io
import shutil
import tarfile
import tempfile
import unittest
import zipfile
from pathlib import Path

from shared import archive

HAS_7Z = bool(shutil.which("7zz") or shutil.which("7z"))
HAS_UNRAR = bool(shutil.which("unrar"))


class ArchiveCase(unittest.TestCase):
    def setUp(self) -> None:
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name)
        self.album = self.root / "album"
        (self.album / "nested").mkdir(parents=True)
        (self.album / "a.txt").write_bytes(b"alpha\n")
        (self.album / "nested" / "b.bin").write_bytes(b"\x00\x01\x02")
        self.out = self.root / "out"

    def zip_with(self, entries: dict[str, bytes]) -> Path:
        path = self.root / "made.zip"
        with zipfile.ZipFile(path, "w") as zf:
            for name, data in entries.items():
                zf.writestr(name, data)
        return path

    def tar_with(self, entries: dict[str, bytes], *, symlink: bool = False) -> Path:
        path = self.root / "made.tar"
        with tarfile.open(path, "w") as tf:
            for name, data in entries.items():
                info = tarfile.TarInfo(name)
                info.size = len(data)
                tf.addfile(info, io.BytesIO(data))
            if symlink:
                link = tarfile.TarInfo("link")
                link.type = tarfile.SYMTYPE
                link.linkname = "a.txt"
                tf.addfile(link)
        return path


class RoundTripTest(ArchiveCase):
    def test_zip(self) -> None:
        packed = archive.pack(self.album, self.root / "album.zip")
        entries = archive.ls(packed)
        self.assertEqual(
            [(e.name, e.size, e.kind) for e in entries],
            [("album/a.txt", 6, "file"), ("album/nested/b.bin", 3, "file")],
        )
        written = archive.unpack(packed, self.out)
        self.assertEqual(
            sorted(p.relative_to(self.out).as_posix() for p in written),
            ["album/a.txt", "album/nested/b.bin"],
        )
        self.assertEqual((self.out / "album" / "a.txt").read_bytes(), b"alpha\n")

    def test_tar_family(self) -> None:
        for suffix in (".tar", ".tar.gz", ".tgz", ".tar.bz2", ".tar.xz"):
            with self.subTest(suffix=suffix):
                dest = self.root / f"album{suffix}"
                packed = archive.pack(self.album, dest)
                self.assertEqual(len(archive.ls(packed)), 2)
                out = self.root / f"out-{suffix.lstrip('.')}"
                archive.unpack(packed, out)
                self.assertEqual((out / "album" / "nested" / "b.bin").read_bytes(),
                                 b"\x00\x01\x02")

    def test_several_sources(self) -> None:
        other = self.root / "other.txt"
        other.write_bytes(b"other")
        packed = archive.pack([self.album / "a.txt", other], self.root / "many.zip")
        self.assertEqual(
            sorted(e.name for e in archive.ls(packed)), ["a.txt", "other.txt"]
        )

    def test_progress_reaches_the_total(self) -> None:
        packed_calls: list[tuple[int, int]] = []
        archive.pack(self.album, self.root / "bar.zip", progress=lambda d, t: packed_calls.append((d, t)))
        self.assertEqual(packed_calls, [(1, 2), (2, 2)])

        unpack_calls: list[tuple[int, int]] = []
        archive.unpack(self.root / "bar.zip", self.out,
                       progress=lambda d, t: unpack_calls.append((d, t)))
        self.assertEqual(unpack_calls, [(1, 2), (2, 2)])

    @unittest.skipUnless(HAS_7Z, "needs 7zz or 7z")
    def test_7z(self) -> None:
        packed = archive.pack(self.album, self.root / "album.7z")
        entries = [e for e in archive.ls(packed) if e.kind == "file"]
        self.assertEqual(sorted(e.name.replace("\\", "/") for e in entries),
                         ["album/a.txt", "album/nested/b.bin"])
        archive.unpack(packed, self.out)
        self.assertEqual((self.out / "album" / "a.txt").read_bytes(), b"alpha\n")

    @unittest.skipUnless(HAS_7Z, "needs 7zz or 7z")
    def test_password(self) -> None:
        packed = archive.pack(self.root / "album", self.root / "secret.7z", password="pw")
        with self.assertRaises(archive.PasswordError):
            archive.unpack(packed, self.out)
        self.assertEqual(len(archive.ls(packed, password="pw")), 4)
        archive.unpack(packed, self.out, password="pw")
        self.assertTrue((self.out / "album" / "a.txt").is_file())

    @unittest.skipUnless(HAS_UNRAR, "needs unrar")
    def test_rar_needs_the_tool(self) -> None:
        path = self.root / "made.rar"
        path.write_bytes(b"Rar!\x1a\x07\x00")
        with self.assertRaises((archive.MissingError, archive.BrokenError)):
            archive.ls(path)


class SafetyTest(ArchiveCase):
    def assert_refused(self, error: type[BaseException], path: Path) -> None:
        with self.assertRaises(error):
            archive.unpack(path, self.out)
        self.assertFalse((self.root / "evil").exists())

    def test_traversal_names(self) -> None:
        for name in ("../evil", "a/../../evil", "/abs/evil", "C:/evil", "..\\evil"):
            with self.subTest(name=name):
                self.assert_refused(archive.UnsafeError, self.zip_with({name: b"x"}))

    def test_symlink_in_a_tar_is_refused(self) -> None:
        self.assert_refused(archive.UnsafeError, self.tar_with({"a.txt": b"a"}, symlink=True))

    def test_limits(self) -> None:
        zipped = self.zip_with({"a.txt": b"aaaa", "b.txt": b"bbbb"})
        with self.assertRaises(archive.UnsafeError):
            archive.ls(zipped, limits=archive.Limits(files=1))
        with self.assertRaises(archive.UnsafeError):
            archive.ls(zipped, limits=archive.Limits(size=3))
        with self.assertRaises(archive.UnsafeError):
            archive.ls(zipped, limits=archive.Limits(ratio=0.01))

    def test_nested_archives_are_not_unpacked(self) -> None:
        inner = self.zip_with({"inner.txt": b"inner"})
        outer = self.zip_with({"nested.zip": inner.read_bytes()})
        archive.unpack(outer, self.out)
        self.assertTrue((self.out / "nested.zip").is_file())
        self.assertFalse((self.out / "inner.txt").exists())


class ConflictTest(ArchiveCase):
    def setUp(self) -> None:
        super().setUp()
        self.zipped = self.zip_with({"a.txt": b"new"})
        self.out.mkdir()
        (self.out / "a.txt").write_bytes(b"old")

    def test_error_is_the_default(self) -> None:
        with self.assertRaises(archive.UnsafeError):
            archive.unpack(self.zipped, self.out)
        self.assertEqual((self.out / "a.txt").read_bytes(), b"old")

    def test_skip_keeps_the_existing_file(self) -> None:
        self.assertEqual(archive.unpack(self.zipped, self.out, conflict="skip"), [])
        self.assertEqual((self.out / "a.txt").read_bytes(), b"old")

    def test_overwrite_replaces_it(self) -> None:
        archive.unpack(self.zipped, self.out, conflict="overwrite")
        self.assertEqual((self.out / "a.txt").read_bytes(), b"new")

    def test_unknown_conflict_mode(self) -> None:
        with self.assertRaises(ValueError):
            archive.unpack(self.zipped, self.out, conflict="maybe")  # type: ignore[arg-type]


class ErrorTest(ArchiveCase):
    def test_missing_file(self) -> None:
        with self.assertRaises(archive.BrokenError):
            archive.ls(self.root / "nope.zip")

    def test_corrupt_file(self) -> None:
        bad = self.root / "bad.zip"
        bad.write_bytes(b"not an archive")
        with self.assertRaises(archive.BrokenError):
            archive.ls(bad)

    def test_unsupported_pack_extension(self) -> None:
        with self.assertRaises(ValueError):
            archive.pack(self.album, self.root / "album.rar")

    def test_missing_source_leaves_no_file(self) -> None:
        with self.assertRaises(archive.Error):
            archive.pack(self.root / "missing", self.root / "none.zip")
        self.assertFalse((self.root / "none.zip").exists())

    def test_error_hierarchy(self) -> None:
        self.assertTrue(issubclass(archive.UnsafeError, ValueError))
        self.assertTrue(issubclass(archive.BrokenError, ValueError))
        self.assertTrue(issubclass(archive.PasswordError, ValueError))


if __name__ == "__main__":
    unittest.main()
