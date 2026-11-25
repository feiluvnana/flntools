"""Tests for Level 3 (`vault`): streaming AES-256-GCM + scrypt pack/unpack and tamper checks."""

from __future__ import annotations

import os
import tarfile
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from flnh import errors, vault


class VaultRoundTripTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def test_folder_tree_unicode_empty_dir_and_zero_plaintext_leak(self) -> None:
        src = self.root / "secret_photos"
        (src / "sub_empty").mkdir(parents=True)
        (src / "nested").mkdir()
        (src / "nested" / "diary_日本語.txt").write_text("confidential-entry-42", encoding="utf-8")
        (src / "blob.bin").write_bytes(b"\xab\xcd" * 2048)

        vf = self.root / "out.flnh"
        size, sha = vault.pack_to_file(src, vf, b"pass123", chunk_size=1024)
        self.assertGreater(size, 1024)
        self.assertEqual(len(sha), 64)

        raw = vf.read_bytes()
        self.assertNotIn(b"diary_", raw)
        self.assertNotIn(b"sub_empty", raw)
        self.assertNotIn(b"confidential-entry-42", raw)

        dest = self.root / "restored"
        kind = vault.unpack_from_file(vf, dest, b"pass123")
        self.assertEqual(kind, "dir")
        self.assertTrue((dest / "sub_empty").is_dir())
        self.assertEqual(
            (dest / "nested" / "diary_日本語.txt").read_text(encoding="utf-8"),
            "confidential-entry-42",
        )
        self.assertEqual((dest / "blob.bin").read_bytes(), b"\xab\xcd" * 2048)

    def test_single_file_and_keyfile_round_trip(self) -> None:
        doc = self.root / "wallet.dat"
        doc.write_bytes(b"seed-phrase-data" * 100)
        key = os.urandom(64)

        vf = self.root / "file.flnh"
        vault.pack_to_file(doc, vf, key, is_keyfile=True)

        with self.assertRaises(errors.AuthError):
            vault.unpack_from_file(vf, self.root / "bad.dat", b"wrong-key")
        self.assertFalse((self.root / "bad.dat").exists())

        out = self.root / "wallet_restored.dat"
        kind = vault.unpack_from_file(vf, out, key)
        self.assertEqual(kind, "file")
        self.assertEqual(out.read_bytes(), b"seed-phrase-data" * 100)

    def test_tamper_truncation_and_trailing_bytes_detection(self) -> None:
        src = self.root / "data"
        src.mkdir()
        (src / "big.bin").write_bytes(os.urandom(3500))
        vf = self.root / "t.flnh"
        vault.pack_to_file(src, vf, b"pw", chunk_size=1024)
        good = bytearray(vf.read_bytes())

        # Bad magic
        bad_magic = bytearray(good)
        bad_magic[0] ^= 0xFF
        vf.write_bytes(bad_magic)
        with self.assertRaises(errors.CorruptHeaderError):
            vault.verify_vault(vf, b"pw")

        # Tamper inside chunk 0 -> AuthError (first frame fails tag)
        bad_c0 = bytearray(good)
        bad_c0[vault.HEADER_SIZE + vault.CHUNK_HDR_SIZE + 5] ^= 0x01
        vf.write_bytes(bad_c0)
        with self.assertRaises(errors.AuthError):
            vault.verify_vault(vf, b"pw")

        # Tamper inside chunk > 0 -> CorruptHeaderError
        c0_len = int.from_bytes(good[vault.HEADER_SIZE : vault.HEADER_SIZE + 4], "big")
        c1_off = vault.HEADER_SIZE + vault.CHUNK_HDR_SIZE + c0_len + vault.CHUNK_HDR_SIZE
        bad_c1 = bytearray(good)
        bad_c1[c1_off + 2] ^= 0x01
        vf.write_bytes(bad_c1)
        with self.assertRaises(errors.CorruptHeaderError):
            vault.verify_vault(vf, b"pw")

        # Truncated file
        vf.write_bytes(good[:-10])
        with self.assertRaises(errors.CorruptHeaderError):
            vault.verify_vault(vf, b"pw")

        # Trailing garbage bytes
        vf.write_bytes(good + b"\x00")
        with self.assertRaises(errors.CorruptHeaderError):
            vault.verify_vault(vf, b"pw")

    def test_path_traversal_member_rejected(self) -> None:
        vf = self.root / "evil.flnh"
        with open(vf, "wb") as raw:
            enc = vault._EncryptStream(raw, b"pw", flags=0)
            with tarfile.open(fileobj=enc, mode="w|", format=tarfile.PAX_FORMAT) as tar:
                info = tarfile.TarInfo("../escape.txt")
                data = b"pwned"
                info.size = len(data)
                tar.addfile(info, fileobj=mock.MagicMock(read=mock.Mock(side_effect=[data, b""])))
            enc.finish()
        with self.assertRaises(errors.CorruptHeaderError):
            vault.unpack_from_file(vf, self.root / "dest", b"pw")
        self.assertFalse((self.root / "escape.txt").exists())

    def test_hide_and_show_preserves_plaintext_on_failure(self) -> None:
        src = self.root / "keep_me"
        src.mkdir()
        (src / "a.txt").write_text("safe", encoding="utf-8")

        with (
            mock.patch.object(vault, "verify_vault", side_effect=errors.DiskError("io error")),
            self.assertRaises(errors.DiskError),
        ):
            vault.hide(src, b"pw")
        self.assertTrue((src / "a.txt").exists())

        vault.hide(src, b"pw")
        self.assertFalse(src.exists())

        with self.assertRaises(errors.AuthError):
            vault.show(src, b"wrong")
        self.assertFalse(src.exists())

        vault.show(src, b"pw")
        self.assertEqual((src / "a.txt").read_text(encoding="utf-8"), "safe")


if __name__ == "__main__":
    unittest.main()
