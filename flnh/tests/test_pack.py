"""Tests for `flnh.pack`: zero-encryption PAX tar streaming, checksums, and path safety."""

from __future__ import annotations

import io
import os
import tarfile
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from flnh import errors, pack


class PackRoundTripTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def test_folder_tree_unicode_empty_dir_and_sha256(self) -> None:
        src = self.root / "secret_photos"
        (src / "sub_empty").mkdir(parents=True)
        (src / "nested").mkdir()
        (src / "nested" / "diary_日本語.txt").write_text("entry-42", encoding="utf-8")
        (src / "blob.bin").write_bytes(b"\xab\xcd" * 2048)

        ar = self.root / "out.tar"
        size, sha, kind = pack.pack_to_file(src, ar)
        self.assertEqual(kind, "dir")
        self.assertGreater(size, 4096)
        self.assertEqual(len(sha), 64)

        dest = self.root / "restored"
        pack.unpack_from_file(ar, dest, kind="dir")
        self.assertTrue((dest / "sub_empty").is_dir())
        self.assertEqual((dest / "nested" / "diary_日本語.txt").read_text(encoding="utf-8"), "entry-42")
        self.assertEqual((dest / "blob.bin").read_bytes(), b"\xab\xcd" * 2048)

    def test_single_file_round_trip_and_existing_dest_refusal(self) -> None:
        doc = self.root / "wallet.dat"
        doc.write_bytes(b"data-bytes" * 100)

        ar = self.root / "file.tar"
        _size, _sha, kind = pack.pack_to_file(doc, ar)
        self.assertEqual(kind, "file")

        out = self.root / "wallet_restored.dat"
        pack.unpack_from_file(ar, out, kind="file")
        self.assertEqual(out.read_bytes(), b"data-bytes" * 100)

        with self.assertRaises(errors.UsageError):
            pack.unpack_from_file(ar, out, kind="file")

    def test_path_traversal_member_rejected(self) -> None:
        ar = self.root / "evil.tar"
        with tarfile.open(ar, mode="w", format=tarfile.PAX_FORMAT) as tar:
            info = tarfile.TarInfo("../escape.txt")
            data = b"pwned"
            info.size = len(data)
            tar.addfile(info, fileobj=mock.MagicMock(read=mock.Mock(side_effect=[data, b""])))
        with self.assertRaises(errors.CorruptHeaderError):
            pack.unpack_from_file(ar, self.root / "dest", kind="dir")
        self.assertFalse((self.root / "escape.txt").exists())


    def _tar(self, name: str, *entries: tarfile.TarInfo) -> Path:
        ar = self.root / name
        with tarfile.open(ar, mode="w", format=tarfile.PAX_FORMAT) as tar:
            for info in entries:
                tar.addfile(info, io.BytesIO(b"x" * info.size) if info.isfile() else None)
        return ar

    @unittest.skipIf(os.name == "nt", "symlinks need privileges on Windows")
    def test_writing_through_a_symlink_that_points_outside_is_refused(self) -> None:
        outside = self.root / "outside"
        outside.mkdir()
        link = tarfile.TarInfo("./a")
        link.type, link.linkname = tarfile.SYMTYPE, str(outside)
        evil = tarfile.TarInfo("./a/evil.txt")
        evil.size = 1
        ar = self._tar("through.tar", link, evil)
        with self.assertRaises(errors.CorruptHeaderError):
            pack.unpack_from_file(ar, self.root / "dest", kind="dir")
        self.assertFalse((outside / "evil.txt").exists())

    def test_check_archive_refuses_what_extracting_would(self) -> None:
        up = tarfile.TarInfo("../up.txt")
        up.size = 1
        dev = tarfile.TarInfo("./dev")
        dev.type = tarfile.CHRTYPE
        for name, info in (("up.tar", up), ("dev.tar", dev)):
            with self.subTest(name), self.assertRaises(errors.CorruptHeaderError):
                pack.check_archive(self._tar(name, info), kind="dir")
        ok = tarfile.TarInfo("./fine.txt")
        ok.size = 1
        pack.check_archive(self._tar("ok.tar", ok), kind="dir")
        junk = self.root / "junk.tar"
        junk.write_bytes(b"not a tar at all" * 64)
        with self.assertRaises(errors.CorruptHeaderError):
            pack.check_archive(junk, kind="dir")

    def test_item_id_keeps_case(self) -> None:
        self.assertNotEqual(pack.item_id("/data/A/docs"), pack.item_id("/data/a/docs"))

    @unittest.skipIf(os.name == "nt", "owners are a Unix thing")
    def test_a_root_restore_gives_files_back_to_their_owners(self) -> None:
        src = self.root / "mine"
        (src / "sub").mkdir(parents=True)
        (src / "sub" / "a.txt").write_text("a", encoding="utf-8")
        ar = self.root / "mine.tar"
        pack.pack_to_file(src, ar)
        owners: list[tuple[str, int, int]] = []

        def chown(path: object, uid: int, gid: int, **_: object) -> None:
            owners.append((Path(str(path)).name, uid, gid))

        with mock.patch("os.geteuid", return_value=0), mock.patch("os.chown", chown):
            pack.unpack_from_file(ar, self.root / "back", kind="dir")
        me = (os.getuid(), os.getgid())
        self.assertIn(("a.txt", *me), owners)
        self.assertIn(("sub", *me), owners)


if __name__ == "__main__":
    unittest.main()
