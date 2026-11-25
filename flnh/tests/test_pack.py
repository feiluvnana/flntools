"""Tests for `flnh.pack`: zero-encryption PAX tar streaming, checksums, and path safety."""

from __future__ import annotations

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


if __name__ == "__main__":
    unittest.main()
