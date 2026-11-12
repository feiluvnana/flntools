from __future__ import annotations

import bz2
import gzip
import io
import lzma
import os
import shutil
import stat
import struct
import subprocess
import sys
import tarfile
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest import mock

import py7zr

from shared import archive

TAR_SUFFIXES = (".tar", ".tar.gz", ".tgz", ".tar.bz2", ".tbz2", ".tar.xz", ".txz")
HAS_ZSTD = sys.version_info >= (3, 14)


def tree(folder: Path) -> dict[str, bytes]:
    return {path.relative_to(folder).as_posix(): path.read_bytes()
            for path in sorted(folder.rglob("*")) if path.is_file()}


def zip_with(path: Path, entries: dict[str, bytes], *, mode: int | None = None) -> Path:
    """A zip whose entries are written as given, with `mode` set on the last one."""
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as packed:
        for index, (name, content) in enumerate(entries.items()):
            info = zipfile.ZipInfo(name)
            info.compress_type = zipfile.ZIP_DEFLATED
            if mode is not None and index == len(entries) - 1:
                info.create_system = 3
                info.external_attr = mode << 16
            packed.writestr(info, content)
    return path


def tar_with(path: Path, members: list[tuple[str, bytes, bytes]]) -> Path:
    """A tar of (name, type, link target) members; a regular file holds b"ok"."""
    with tarfile.open(path, "w") as packed:
        for name, kind, target in members:
            info = tarfile.TarInfo(name)
            info.type, info.linkname = kind, target.decode()
            if kind == tarfile.REGTYPE:
                info.size = 2
                packed.addfile(info, io.BytesIO(b"ok"))
            else:
                packed.addfile(info)
    return path


def spanned_zip(folder: Path, entries: dict[str, bytes], part_size: int) -> list[Path]:
    """A real spanned zip, `.z01`, `.z02`, …, `.zip`, whose offsets count from each part's start."""
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_STORED) as packed:
        for name, content in entries.items():
            packed.writestr(name, content)
    whole = buffer.getvalue()
    end = whole.rindex(b"PK\x05\x06")
    directory_size, directory_offset = struct.unpack_from("<II", whole, end + 12)
    # The split marker makes part one 4 bytes longer, so every offset moves by 4.
    body = b"PK\x07\x08" + whole[:directory_offset]
    disks = [body[start:start + part_size] for start in range(0, len(body), part_size)]
    directory = bytearray(whole[directory_offset:directory_offset + directory_size])
    position = 0
    while position < len(directory):
        offset = struct.unpack_from("<I", directory, position + 42)[0] + 4
        struct.pack_into("<H", directory, position + 34, offset // part_size)
        struct.pack_into("<I", directory, position + 42, offset % part_size)
        name_length, extra_length, comment_length = struct.unpack_from("<HHH", directory, position + 28)
        position += 46 + name_length + extra_length + comment_length
    end_record = bytearray(whole[end:])
    struct.pack_into("<HH", end_record, 4, len(disks), len(disks))
    struct.pack_into("<I", end_record, 16, 0)
    parts = [folder / f"span.z{number:02}" for number in range(1, len(disks) + 1)] + [folder / "span.zip"]
    for part, content in zip(parts, [*disks, bytes(directory) + bytes(end_record)]):
        part.write_bytes(content)
    return parts


class ArchiveCase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.album = self.root / "album"
        (self.album / "photos").mkdir(parents=True)
        (self.album / "empty").mkdir()
        (self.album / "a.txt").write_bytes(b"alpha")
        (self.album / "photos" / "1.jpg").write_bytes(os.urandom(5000))
        self.out = self.root / "out"

    def tearDown(self):
        self.temp.cleanup()

    def assert_round_trip(self, written: list[Path], **options):
        extracted = archive.unzip(written[0] if len(written) == 1 else written, self.out, **options)
        self.assertEqual(tree(self.out / "album"), tree(self.album))
        self.assertTrue((self.out / "album" / "empty").is_dir())
        self.assertEqual(sorted(extracted), sorted(self.out / "album" / name for name in tree(self.album)))
        self.assertEqual([path.name for path in self.out.iterdir()], ["album"])

    def assert_refused(self, error: type[Exception], source: Path, **options):
        with self.assertRaises(error):
            archive.unzip(source, self.out, **options)
        self.assertFalse(self.out.exists())


class RoundTripTest(ArchiveCase):
    def test_each_format(self):
        for suffix in (".zip", ".7z", *TAR_SUFFIXES, *((".tar.zst",) if HAS_ZSTD else ())):
            with self.subTest(suffix=suffix):
                written = archive.zip(self.album, self.root / f"bundle{suffix}")
                self.assertEqual(written, [self.root / f"bundle{suffix}"])
                self.assert_round_trip(written)
                shutil.rmtree(self.out)

    def test_password(self):
        for suffix in (".zip", ".7z"):
            with self.subTest(suffix=suffix):
                written = archive.zip(self.album, self.root / f"locked{suffix}", password="pw")
                self.assert_round_trip(written, password="pw")
                shutil.rmtree(self.out)

    @unittest.skipUnless(shutil.which("zip"), "needs the zip program for a ZipCrypto archive")
    def test_zipcrypto_password(self):
        locked = self.root / "crypto.zip"
        subprocess.run(["zip", "-qr", "-P", "pw", str(locked), "album"], cwd=self.root, check=True)
        self.assert_round_trip([locked], password="pw")

    def test_split_into_three_parts(self):
        for suffix in (".zip", ".7z", ".tar.gz"):
            with self.subTest(suffix=suffix):
                size = archive.zip(self.album, self.root / f"whole{suffix}")[0].stat().st_size
                parts = archive.zip(self.album, self.root / f"split{suffix}", split=size // 3 + 1)
                self.assertEqual([part.name for part in parts], [f"split{suffix}.{n:03}" for n in (1, 2, 3)])
                self.assert_round_trip([parts[0]])
                shutil.rmtree(self.out)
                self.assert_round_trip(parts)
                shutil.rmtree(self.out)

    def test_list_of_sources_named_from_their_parents(self):
        archive.zip([self.album / "a.txt", self.album / "photos"], self.root / "picked.zip")
        archive.unzip(self.root / "picked.zip", self.out)
        self.assertEqual(sorted(tree(self.out)), ["a.txt", "photos/1.jpg"])

    def test_bad_requests(self):
        with self.assertRaises(ValueError):
            archive.zip(self.album, self.root / "bundle.rar")
        with self.assertRaises(ValueError):
            archive.zip(self.album, self.root / "bundle.tar.gz", password="pw")
        with self.assertRaises(ValueError):
            archive.zip(self.album, self.root / "bundle.zip", split=0)
        with self.assertRaises(ValueError):
            bundle = archive.zip(self.album, self.root / "b.zip")[0]
            archive.unzip(bundle, self.out, conflict="merge")  # type: ignore[arg-type]

    @unittest.skipIf(HAS_ZSTD, "zstd exists on this Python")
    def test_zstd_needs_python_314(self):
        with self.assertRaisesRegex(RuntimeError, "3.14"):
            archive.zip(self.album, self.root / "bundle.tar.zst")
        (self.root / "fake.tar.zst").write_bytes(b"\x28\xb5\x2f\xfd" + bytes(20))
        with self.assertRaisesRegex(RuntimeError, "3.14"):
            archive.unzip(self.root / "fake.tar.zst", self.out)


class SingleFileTest(ArchiveCase):
    def test_compressed_file_unpacks_to_one_file(self):
        openers = {".gz": gzip.open, ".bz2": bz2.open, ".xz": lzma.open}
        if HAS_ZSTD:
            from compression import zstd  # type: ignore[import-not-found]
            openers[".zst"] = zstd.open
        for suffix, opener in openers.items():
            with self.subTest(suffix=suffix):
                with opener(self.root / f"notes.txt{suffix}", "wb") as out:
                    out.write(b"some notes")
                written = archive.unzip(self.root / f"notes.txt{suffix}", self.out)
                self.assertEqual(written, [self.out / "notes.txt"])
                self.assertEqual(written[0].read_bytes(), b"some notes")
                shutil.rmtree(self.out)


class SniffTest(ArchiveCase):
    def test_magic_bytes_win_over_the_name(self):
        for real, disguise in ((".7z", "mirror.zip"), (".zip", "mirror.bin"), (".tar", "mirror.zip"),
                               (".tar.xz", "mirror.7z")):
            with self.subTest(real=real):
                written = archive.zip(self.album, self.root / f"real{real}")[0]
                self.assert_round_trip([written.rename(self.root / disguise)])
                shutil.rmtree(self.out)

    def test_not_an_archive(self):
        (self.root / "notes.zip").write_bytes(b"just some text, no archive here")
        self.assert_refused(ValueError, self.root / "notes.zip")

    def test_spanned_zip_found_from_its_first_part(self):
        entries = {"album/a.txt": b"alpha", "album/big.bin": os.urandom(3000), "album/c.txt": b"gamma"}
        parts = spanned_zip(self.root, entries, part_size=1000)
        self.assertGreaterEqual(len(parts), 4)
        for first in (parts[0], parts[-1]):
            with self.subTest(first=first.name):
                archive.unzip(first, self.out)
                self.assertEqual(tree(self.out), entries)
                shutil.rmtree(self.out)

    def test_seven_zip_parts_found_from_the_first(self):
        size = archive.zip(self.album, self.root / "whole.7z")[0].stat().st_size
        parts = archive.zip(self.album, self.root / "photos.7z", split=size // 3 + 1, password="pw")
        self.assertEqual(parts[0].name, "photos.7z.001")
        self.assert_round_trip([parts[0]], password="pw")


class ConflictTest(ArchiveCase):
    def setUp(self):
        super().setUp()
        self.bundle = zip_with(self.root / "bundle.zip", {"a.txt": b"new", "b.txt": b"bee"})
        self.out.mkdir()
        (self.out / "a.txt").write_bytes(b"old")

    def test_error_writes_nothing(self):
        with self.assertRaisesRegex(archive.ConflictError, "a.txt"):
            archive.unzip(self.bundle, self.out)
        self.assertEqual(tree(self.out), {"a.txt": b"old"})

    def test_skip(self):
        self.assertEqual(archive.unzip(self.bundle, self.out, conflict="skip"), [self.out / "b.txt"])
        self.assertEqual(tree(self.out), {"a.txt": b"old", "b.txt": b"bee"})

    def test_overwrite(self):
        archive.unzip(self.bundle, self.out, conflict="overwrite")
        self.assertEqual(tree(self.out), {"a.txt": b"new", "b.txt": b"bee"})

    def test_rename(self):
        archive.unzip(self.bundle, self.out, conflict="rename")
        archive.unzip(self.bundle, self.out, conflict="rename")
        self.assertEqual(tree(self.out), {"a.txt": b"old", "a (1).txt": b"new", "a (2).txt": b"new",
                                          "b.txt": b"bee", "b (1).txt": b"bee"})

    def test_case_only_clash(self):
        cased = zip_with(self.root / "cased.zip", {"Photo.JPG": b"one", "photo.jpg": b"two"})
        with self.assertRaisesRegex(archive.ConflictError, "photo.jpg"):
            archive.unzip(cased, self.root / "fresh")
        self.assertFalse((self.root / "fresh").exists())
        archive.unzip(cased, self.root / "fresh", conflict="rename")
        self.assertEqual(tree(self.root / "fresh"), {"Photo.JPG": b"one", "photo (1).jpg": b"two"})


class SafetyTest(ArchiveCase):
    def test_traversal(self):
        for name in ("../evil.txt", "/etc/evil.txt", "C:\\evil.txt", "\\\\server\\share\\evil.txt",
                     "photos/../../evil.txt"):
            with self.subTest(name=name):
                zipped = zip_with(self.root / "evil.zip", {"ok.txt": b"ok", name: b"x"})
                self.assert_refused(archive.UnsafeError, zipped)
                tarred = tar_with(self.root / "evil.tar", [("ok.txt", tarfile.REGTYPE, b""), (name, tarfile.REGTYPE, b"")])
                self.assert_refused(archive.UnsafeError, tarred)
                self.assertFalse((self.root / "evil.txt").exists())

    def test_ntfs_streams_and_reserved_names_are_refused(self):
        """Fix #3: `a:b` writes NTFS stream `b` of file `a`, and Windows' device names can't be files."""
        for name in ("x/a:b", "a:b", "NUL", "dir/com1.txt", "Lpt9", "con.txt", "aux/x.txt", "x/prn", "nul.tar.gz"):
            with self.subTest(name=name):
                zipped = zip_with(self.root / "reserved.zip", {"ok.txt": b"ok", name: b"x"})
                self.assert_refused(archive.UnsafeError, zipped)
                tarred = tar_with(self.root / "reserved.tar", [("ok.txt", tarfile.REGTYPE, b""), (name, tarfile.REGTYPE, b"")])
                self.assert_refused(archive.UnsafeError, tarred)

    def test_names_like_reserved_ones_are_allowed(self):
        for name in ("console", "com10", "nul_x", "lpt0.txt", "dir/connect.txt", "auxiliary/x"):
            with self.subTest(name=name):
                shutil.rmtree(self.out, ignore_errors=True)
                zipped = zip_with(self.root / "fine.zip", {name: b"x"})
                self.assertEqual(archive.unzip(zipped, self.out), [self.out / name])

    def test_links_are_always_refused(self):
        linked = zip_with(self.root / "link.zip", {"ok.txt": b"ok", "inside": b"ok.txt"}, mode=stat.S_IFLNK | 0o777)
        self.assert_refused(archive.UnsafeError, linked)
        for kind, target in ((tarfile.SYMTYPE, b"ok.txt"), (tarfile.LNKTYPE, b"ok.txt"), (tarfile.SYMTYPE, b"/etc")):
            with self.subTest(kind=kind, target=target):
                tar = tar_with(self.root / "link.tar", [("ok.txt", tarfile.REGTYPE, b""), ("link", kind, target)])
                self.assert_refused(archive.UnsafeError, tar)

    def test_symlink_chain(self):
        chain = tar_with(self.root / "chain.tar", [("a/b", tarfile.SYMTYPE, b".."), ("a/b/c", tarfile.SYMTYPE, b".."),
                                                   ("a/b/c/x", tarfile.REGTYPE, b"")])
        self.assert_refused(archive.UnsafeError, chain)
        self.assertFalse((self.root / "x").exists())

    def test_devices_pipes_and_sockets(self):
        for kind in (tarfile.CHRTYPE, tarfile.BLKTYPE, tarfile.FIFOTYPE):
            with self.subTest(kind=kind):
                self.assert_refused(archive.UnsafeError, tar_with(self.root / "dev.tar", [("dev", kind, b"")]))
        for mode in (stat.S_IFCHR, stat.S_IFIFO, stat.S_IFSOCK):
            with self.subTest(mode=mode):
                device = zip_with(self.root / "dev.zip", {"dev": b""}, mode=mode | 0o644)
                self.assert_refused(archive.UnsafeError, device)

    def test_bombs(self):
        bundle = archive.zip(self.album, self.root / "bundle.zip")[0]
        self.assert_refused(archive.BombError, bundle, limits=archive.Limits(size=1000))
        self.assert_refused(archive.BombError, bundle, limits=archive.Limits(files=3))
        self.assert_refused(archive.BombError, zip_with(self.root / "zeros.zip", {"zeros": bytes(1_000_000)}))
        tar = archive.zip(self.album, self.root / "bundle.tar.gz")[0]
        self.assert_refused(archive.BombError, tar, limits=archive.Limits(size=1000))
        archive.unzip(zip_with(self.root / "zeros.zip", {"zeros": bytes(1_000_000)}), self.out,
                      limits=archive.Limits(ratio=10_000))
        self.assertEqual((self.out / "zeros").stat().st_size, 1_000_000)

    def test_size_that_lies_is_stopped_midway(self):
        # A gzip trailer's size is only modulo 4 GB, so it can say anything: this one says 10 bytes.
        compressed = bytearray(gzip.compress(bytes(5_000_000)))
        struct.pack_into("<I", compressed, len(compressed) - 4, 10)
        (self.root / "zeros.gz").write_bytes(compressed)
        writes: list[int] = []
        real_write = archive._Output.write

        def counted_write(output, chunk):
            writes.append(len(chunk))
            return real_write(output, chunk)

        with mock.patch.object(archive._Output, "write", counted_write):
            self.assert_refused(archive.BombError, self.root / "zeros.gz")
        self.assertLess(sum(writes), 5_000_000)

    def test_nested_archive_is_not_unpacked(self):
        inner = archive.zip(self.album, self.root / "inner.zip")[0]
        outer = archive.zip(inner, self.root / "outer.tar")[0]
        self.assertEqual(archive.unzip(outer, self.out), [self.out / "inner.zip"])


class ErrorTest(ArchiveCase):
    def test_wrong_or_missing_password(self):
        for suffix in (".zip", ".7z"):
            locked = archive.zip(self.album, self.root / f"locked{suffix}", password="pw")[0]
            with self.subTest(suffix=suffix):
                self.assert_refused(archive.PasswordError, locked, password="wrong")
                self.assert_refused(archive.PasswordError, locked)
        # Without header encryption, a 7z lists fine and only fails once its contents are read.
        with py7zr.SevenZipFile(self.root / "contents.7z", "w", password="pw") as packed:
            packed.writeall(self.album, "album")
        self.assert_refused(archive.PasswordError, self.root / "contents.7z", password="wrong")
        self.assert_refused(archive.PasswordError, self.root / "contents.7z")

    def test_rar_without_unrar(self):
        (self.root / "a.rar").write_bytes(b"Rar!\x1a\x07\x01\x00" + bytes(40))
        with mock.patch("shutil.which", return_value=None):
            with self.assertRaisesRegex(RuntimeError, "unrar"):
                archive.unzip(self.root / "a.rar", self.out)
        self.assertFalse(self.out.exists())

    def test_rar_link_refused_from_listing(self):
        (self.root / "a.rar").write_bytes(b"Rar!\x1a\x07\x01\x00" + bytes(40))
        listing = (b"\nArchive: a.rar\nDetails: RAR 5\n\n        Name: notes.txt\n        Type: File\n"
                   b"        Size: 5\n Packed size: 5\n\n        Name: escape\n        Type: Symbolic link\n"
                   b"      Target: ../../etc\n        Size: 9\n")
        finished = subprocess.CompletedProcess([], 0, stdout=listing, stderr=b"")
        with mock.patch("shutil.which", return_value="/usr/bin/unrar"), \
                mock.patch("subprocess.run", return_value=finished):
            self.assert_refused(archive.UnsafeError, self.root / "a.rar")

    @unittest.skipUnless(shutil.which("unrar") and shutil.which("rar"), "needs the rar and unrar programs")
    def test_rar_round_trip(self):
        subprocess.run(["rar", "a", "-r", "-idq", "-ppw", "bundle.rar", "album"], cwd=self.root, check=True)
        self.assert_round_trip([self.root / "bundle.rar"], password="pw")

    def test_failed_zip_leaves_no_file(self):
        with self.assertRaises(FileNotFoundError):
            archive.zip([self.album, self.root / "missing"], self.root / "out" / "bundle.zip")
        (self.album / "broken").symlink_to(self.root / "nowhere")
        for suffix in (".zip", ".7z", ".tar.gz"):
            with self.subTest(suffix=suffix):
                with self.assertRaises(OSError):
                    archive.zip(self.album, self.root / "out" / f"bundle{suffix}", split=1000)
                self.assertEqual(list((self.root / "out").iterdir()), [])


if __name__ == "__main__":
    unittest.main()
