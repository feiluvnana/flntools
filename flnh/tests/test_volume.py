"""Tests for Level 3 (`--volume`): MBR/GPT parsing, CRC32, shrink/restore, and recovery."""

from __future__ import annotations

import shutil
import struct
import tempfile
import unittest
from pathlib import Path
from unittest import mock

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
        (folder / "doc.txt").write_text("volume-payload", encoding="utf-8")
        (folder / "blob.bin").write_bytes(b"Z" * 16_384)
        return folder

    def test_mbr_hide_show_and_tail_scan_recovery(self) -> None:
        img = self.root / "usb_mbr.img"
        d = vol_disk.FileDisk.create_mbr(img, 16 * 1024 * 1024, fs_type="ntfs", used_bytes=1024 * 1024)
        self.addCleanup(d.close)
        orig_fs = d.fs_bytes
        folder = self._sample_folder()
        bak = self.root / "mbr.bak"

        res = volume.hide(folder, disk=d, backup_path=bak)
        self.assertFalse(folder.exists())
        self.assertTrue(d.refreshed)
        self.assertLess(d.fs_bytes, orig_fs)
        scheme, parts, _ = table.read_table(d)
        self.assertEqual(scheme, "mbr")
        self.assertEqual(parts[0].sector_count * d.sector_size, d.fs_bytes)
        self.assertTrue(bak.is_file())

        # Recover without state entry and without backup (tail scan).
        volume.show(folder, disk=d)
        self.assertEqual(d.fs_bytes, orig_fs)
        self.assertEqual(table.read_table(d)[1][0].sector_count * d.sector_size, orig_fs)
        self.assertEqual((folder / "doc.txt").read_text(encoding="utf-8"), "volume-payload")
        self.assertEqual((folder / "blob.bin").read_bytes(), b"Z" * 16_384)
        meta_lba = res["meta"]["meta_lba"]
        self.assertFalse(d.read_sectors(meta_lba, 1).startswith(volume.VOL_MAGIC))

    def test_gpt_hide_and_backup_recovery(self) -> None:
        img = self.root / "usb_gpt.img"
        d = vol_disk.FileDisk.create_gpt(img, 16 * 1024 * 1024, fs_type="ext4", used_bytes=1024 * 1024)
        self.addCleanup(d.close)
        orig_fs = d.fs_bytes
        folder = self._sample_folder("gpt_secret")
        bak = self.root / "gpt.bak"

        res = volume.hide(folder, disk=d, backup_path=bak)
        self.assertFalse(folder.exists())
        scheme, parts, raw = table.read_table(d)
        self.assertEqual(scheme, "gpt")
        self.assertLess(parts[0].sector_count * d.sector_size, orig_fs)
        bak_hdr = d.read_sectors(d.total_sectors - 1, 1)
        self.assertTrue(bak_hdr.startswith(table.GPT_SIG))
        self.assertLessEqual(res["meta"]["meta_lba"], raw["info"]["last_usable_lba"])

        volume.show(folder, disk=d, backup_path=bak)
        self.assertEqual(d.fs_bytes, orig_fs)
        self.assertEqual(table.read_table(d)[1][0].sector_count * d.sector_size, orig_fs)
        self.assertEqual((folder / "doc.txt").read_text(encoding="utf-8"), "volume-payload")

    def test_tampered_tail_header_refused_and_readback_failure_rolls_back(self) -> None:
        img = self.root / "fail.img"
        d = vol_disk.FileDisk.create_mbr(img, 8 * 1024 * 1024, fs_type="ntfs")
        self.addCleanup(d.close)
        orig_fs = d.fs_bytes
        orig_sectors = table.read_table(d)[1][0].sector_count
        folder = self._sample_folder("untouched")

        with (
            mock.patch.object(volume, "_hash_sectors", return_value="0" * 64),
            self.assertRaises(errors.DiskError),
        ):
            volume.hide(folder, disk=d)
        self.assertTrue((folder / "doc.txt").exists())
        self.assertEqual(d.fs_bytes, orig_fs)
        self.assertEqual(table.read_table(d)[1][0].sector_count, orig_sectors)

        res = volume.hide(folder, disk=d)
        meta_lba = res["meta"]["meta_lba"]
        tampered = bytearray(d.read_sectors(meta_lba, 1))
        tampered[volume.VOL_HDR_SIZE] ^= 0xFF
        d.write_sectors(meta_lba, bytes(tampered))
        with self.assertRaises(errors.CorruptHeaderError):
            volume.show(folder, disk=d)

    def test_zero_temp_dir_for_streaming_and_low_space_fallback(self) -> None:
        img = self.root / "stream.img"
        d = vol_disk.FileDisk.create_mbr(img, 8 * 1024 * 1024, fs_type="ntfs")
        self.addCleanup(d.close)
        folder = self._sample_folder("streamed")

        with mock.patch.object(
            tempfile, "TemporaryDirectory", side_effect=AssertionError("unexpected temp dir")
        ):
            volume.hide(folder, disk=d)
            volume.show(folder, disk=d)
        self.assertEqual((folder / "doc.txt").read_text(encoding="utf-8"), "volume-payload")

        volume.hide(folder, disk=d)
        with mock.patch.object(shutil, "disk_usage", return_value=mock.Mock(free=0)):
            volume.show(folder, disk=d)
        self.assertEqual((folder / "doc.txt").read_text(encoding="utf-8"), "volume-payload")

    def test_win32_shrink_ioctl_and_struct_layout(self) -> None:
        self.assertEqual(vol_disk.FSCTL_SHRINK_VOLUME, 0x000901B0)
        prep = vol_disk.pack_shrink_info(1, 12345)
        self.assertEqual(len(prep), 24)
        req, flags, sectors = struct.unpack("<I4xQq", prep)
        self.assertEqual((req, flags, sectors), (1, 0, 12345))
        commit = vol_disk.pack_shrink_info(2, 12345)
        self.assertEqual(struct.unpack("<I4xQq", commit), (2, 0, 0))

    def test_win32_ps_resize_and_manages_table_order(self) -> None:
        fake_win = object.__new__(vol_disk.Win32Disk)
        fake_win.drive = "E:"
        fake_win.device_id = "\\\\.\\PhysicalDrive1"
        fake_win._lock_handle = None
        fake_win._raw_handle = 100
        fake_win._k32 = mock.MagicMock()
        fake_win._open_handle = mock.MagicMock(return_value=101)
        fake_win._unlock_volume = mock.MagicMock()

        calls: list[list[str]] = []

        def fake_run(cmd: list[str]) -> mock.Mock:
            calls.append(cmd)
            if "Write-Output" in cmd[-1]:
                return mock.Mock(returncode=0, stdout="123456789\n", stderr="")
            return mock.Mock(returncode=0, stdout="", stderr="")

        with mock.patch.object(vol_disk, "_run", side_effect=fake_run):
            self.assertEqual(fake_win.max_shrink_bytes(1), 123456789)
            fake_win.shrink_fs(1, 500_000_000)
            fake_win.expand_fs(1, 600_000_000)

        self.assertEqual(len(calls), 3)
        self.assertIn("Resize-Partition -DriveLetter E -Size 499998720", calls[1][-1])
        self.assertIn("Get-PartitionSupportedSize -DriveLetter E", calls[2][-1])
        fake_win._k32.CloseHandle.assert_any_call(100)
        self.assertEqual(fake_win._raw_handle, 101)

        img = self.root / "managed.img"
        d = vol_disk.FileDisk.create_gpt(img, 8 * 1024 * 1024, fs_type="ntfs")
        self.addCleanup(d.close)
        d.manages_table = True  # type: ignore[misc]
        events: list[str] = []
        orig_shrink = d.shrink_fs
        orig_expand = d.expand_fs
        orig_write = d.write_sectors

        def managed_shrink(idx: int, new_bytes: int) -> None:
            orig_shrink(idx, new_bytes)
            _, _, raw = table.read_table(d)
            pri, entries, bak, _ = table.update_gpt_partition(
                raw["hdr"], raw["entries"], idx, new_bytes // d.sector_size, d.sector_size
            )
            volume._write_table_gpt(d, raw["info"], pri, entries, bak)

        def tracked_write(lba: int, data: bytes) -> None:
            if set(data) == {0}:
                events.append("zero_meta")
            orig_write(lba, data)

        def tracked_expand(idx: int, new_bytes: int) -> None:
            events.append("expand_fs")
            orig_expand(idx, new_bytes)

        d.shrink_fs = managed_shrink  # type: ignore[method-assign]
        d.write_sectors = tracked_write  # type: ignore[method-assign]
        d.expand_fs = tracked_expand  # type: ignore[method-assign]
        folder = self._sample_folder("managed")
        volume.hide(folder, disk=d)
        volume.show(folder, disk=d)
        self.assertEqual(events, ["zero_meta", "expand_fs"])
        self.assertEqual((folder / "doc.txt").read_text(encoding="utf-8"), "volume-payload")

    def test_volume_progress_stages_and_monotonic_counts(self) -> None:
        img = self.root / "prog.img"
        d = vol_disk.FileDisk.create_mbr(img, 8 * 1024 * 1024, fs_type="ntfs")
        self.addCleanup(d.close)
        folder = self._sample_folder("big")
        (folder / "blob.bin").write_bytes(b"X" * (1200 * 1024))

        hide_events: list[tuple[str, int, int]] = []
        volume.hide(folder, disk=d, on_progress=lambda s, a, b: hide_events.append((s, a, b)))
        hide_stages = [s for s, _, _ in hide_events]
        self.assertIn("shrinking", hide_stages)
        self.assertIn("writing", hide_stages)
        self.assertIn("verifying", hide_stages)
        self.assertIn("cleaning", hide_stages)
        writing = [(a, b) for s, a, b in hide_events if s == "writing"]
        self.assertEqual(writing[-1][0], writing[-1][1])
        self.assertEqual([a for a, _ in writing], sorted(a for a, _ in writing))

        show_events: list[tuple[str, int, int]] = []
        volume.show(folder, disk=d, on_progress=lambda s, a, b: show_events.append((s, a, b)))
        show_stages = [s for s, _, _ in show_events]
        self.assertIn("verifying", show_stages)
        self.assertIn("restoring", show_stages)
        self.assertIn("expanding", show_stages)
        restoring = [(a, b) for s, a, b in show_events if s == "restoring"]
        self.assertEqual(restoring[-1][0], restoring[-1][1])


if __name__ == "__main__":
    unittest.main()
