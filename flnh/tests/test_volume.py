"""Tests for Level 4 (`volume`): MBR/GPT parsing, CRC32, shrink/restore, and recovery."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from flnh import errors, volume
from flnh.volume import disk as vol_disk
from flnh.volume import table


class TableTest(unittest.TestCase):
    def test_mbr_build_parse_and_update(self) -> None:
        s0 = table.build_mbr(2048, 100_000)
        parts = table.parse_mbr(s0)
        self.assertEqual(len(parts), 1)
        self.assertEqual((parts[0].index, parts[0].start_lba, parts[0].sector_count), (1, 2048, 100_000))
        updated = table.update_mbr_partition(s0, 1, 80_000)
        self.assertEqual(table.parse_mbr(updated)[0].sector_count, 80_000)

        with self.assertRaises(errors.CorruptHeaderError):
            table.parse_mbr(b"\x00" * 512)

    def test_gpt_build_parse_update_and_crc32_checks(self) -> None:
        total = 65536
        mbr, pri, entries, bak = table.build_gpt(total, 2048, total - 2048 - 34)
        self.assertTrue(table.is_protective_mbr(mbr))
        sectors = {0: mbr, 1: pri, total - 1: bak}
        entry_sectors = len(entries) // 512
        for i in range(entry_sectors):
            sectors[2 + i] = entries[i * 512 : (i + 1) * 512]

        def reader(lba: int, count: int) -> bytes:
            return b"".join(sectors[lba + k] for k in range(count))

        info, parts, _, _ = table.parse_gpt(reader, 512, total)
        self.assertEqual(len(parts), 1)
        self.assertEqual(info["last_usable_lba"], total - 2 - entry_sectors)

        new_pri, new_ent, new_bak, _ = table.update_gpt_partition(pri, entries, 1, 40_000, 512)
        sectors[1] = new_pri
        for i in range(entry_sectors):
            sectors[2 + i] = new_ent[i * 512 : (i + 1) * 512]
        _, shrunk_parts, _, _ = table.parse_gpt(reader, 512, total)
        self.assertEqual(shrunk_parts[0].sector_count, 40_000)
        self.assertEqual(len(new_bak), 512)

        # Corrupt primary header CRC32
        bad_pri = bytearray(new_pri)
        bad_pri[48] ^= 0x01
        sectors[1] = bytes(bad_pri)
        with self.assertRaises(errors.CorruptHeaderError):
            table.parse_gpt(reader, 512, total)

    def test_backup_checksum_validation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "bak.flnh"
            table.save_backup(p, {"part_index": 1, "meta_lba": 123})
            self.assertEqual(table.load_backup(p)["meta_lba"], 123)
            p.write_text(p.read_text(encoding="utf-8").replace("123", "124"), encoding="utf-8")
            with self.assertRaises(errors.CorruptHeaderError):
                table.load_backup(p)


class VolumeRoundTripTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def _sample_folder(self, name: str = "hidden_media") -> Path:
        folder = self.root / name
        folder.mkdir()
        (folder / "doc.txt").write_text("volume-secret-payload", encoding="utf-8")
        (folder / "blob.bin").write_bytes(b"Z" * 16_384)
        return folder

    def test_mbr_hide_show_and_tail_scan_recovery(self) -> None:
        img = self.root / "usb_mbr.img"
        d = vol_disk.FileDisk.create_mbr(img, 16 * 1024 * 1024, fs_type="ntfs", used_bytes=1024 * 1024)
        self.addCleanup(d.close)
        orig_fs = d.fs_bytes
        folder = self._sample_folder()
        bak = self.root / "mbr.bak"

        res = volume.hide(folder, b"volpass", disk=d, backup_path=bak)
        self.assertFalse(folder.exists())
        self.assertTrue(d.refreshed)
        self.assertLess(d.fs_bytes, orig_fs)
        scheme, parts, _ = table.read_table(d)
        self.assertEqual(scheme, "mbr")
        self.assertEqual(parts[0].sector_count * d.sector_size, d.fs_bytes)
        self.assertTrue(bak.is_file())

        # Wrong password leaves partition table and fs_bytes untouched.
        with self.assertRaises(errors.AuthError):
            volume.show(folder, b"badpass", disk=d)
        self.assertEqual(table.read_table(d)[1][0].sector_count * d.sector_size, d.fs_bytes)

        # Recover without state entry and without backup (tail scan).
        volume.show(folder, b"volpass", disk=d)
        self.assertEqual(d.fs_bytes, orig_fs)
        self.assertEqual(table.read_table(d)[1][0].sector_count * d.sector_size, orig_fs)
        self.assertEqual((folder / "doc.txt").read_text(encoding="utf-8"), "volume-secret-payload")
        self.assertEqual((folder / "blob.bin").read_bytes(), b"Z" * 16_384)
        # Tail header sectors wiped after restore.
        meta_lba = res["meta"]["meta_lba"]
        self.assertFalse(d.read_sectors(meta_lba, 1).startswith(volume.VOL_MAGIC))

    def test_gpt_hide_and_backup_recovery(self) -> None:
        img = self.root / "usb_gpt.img"
        d = vol_disk.FileDisk.create_gpt(img, 16 * 1024 * 1024, fs_type="ext4", used_bytes=1024 * 1024)
        self.addCleanup(d.close)
        orig_fs = d.fs_bytes
        folder = self._sample_folder("gpt_secret")
        bak = self.root / "gpt.bak"

        res = volume.hide(folder, b"gptpass", disk=d, backup_path=bak)
        self.assertFalse(folder.exists())
        scheme, parts, raw = table.read_table(d)
        self.assertEqual(scheme, "gpt")
        self.assertLess(parts[0].sector_count * d.sector_size, orig_fs)
        # Backup GPT header at last LBA must stay intact and valid.
        bak_hdr = d.read_sectors(d.total_sectors - 1, 1)
        self.assertTrue(bak_hdr.startswith(table.GPT_SIG))
        self.assertLessEqual(res["meta"]["meta_lba"], raw["info"]["last_usable_lba"])

        volume.show(folder, b"gptpass", disk=d, backup_path=bak)
        self.assertEqual(d.fs_bytes, orig_fs)
        self.assertEqual(table.read_table(d)[1][0].sector_count * d.sector_size, orig_fs)
        self.assertEqual((folder / "doc.txt").read_text(encoding="utf-8"), "volume-secret-payload")

    def test_readback_verify_failure_rolls_back_and_keeps_plaintext(self) -> None:
        img = self.root / "fail.img"
        d = vol_disk.FileDisk.create_mbr(img, 8 * 1024 * 1024, fs_type="ntfs")
        self.addCleanup(d.close)
        orig_fs = d.fs_bytes
        orig_sectors = table.read_table(d)[1][0].sector_count
        folder = self._sample_folder("untouched")

        d.corrupt_next_write = True
        with self.assertRaises(errors.DiskError):
            volume.hide(folder, b"pw", disk=d)
        self.assertTrue((folder / "doc.txt").exists())
        self.assertEqual(d.fs_bytes, orig_fs)
        self.assertEqual(table.read_table(d)[1][0].sector_count, orig_sectors)


if __name__ == "__main__":
    unittest.main()
