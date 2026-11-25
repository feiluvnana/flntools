"""Safety guard and static hygiene tests for `flnh`."""

from __future__ import annotations

import ast
import tempfile
import unittest
from pathlib import Path

from flnh import errors, volume
from flnh.volume import disk as vol_disk
from flnh.volume import table

SRC_DIR = Path(__file__).resolve().parent.parent / "src"


class SafetyGuardsTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def test_windows_system_disk_guards(self) -> None:
        self.assertTrue(
            vol_disk.check_win_system(
                drive="C:", system_drive="C:", root_entries=set(), gpt_type_guids=set()
            )
        )
        self.assertTrue(
            vol_disk.check_win_system(
                drive="D:",
                system_drive="C:",
                root_entries={"pagefile.sys"},
                gpt_type_guids=set(),
            )
        )
        self.assertTrue(
            vol_disk.check_win_system(
                drive="D:",
                system_drive="C:",
                root_entries=set(),
                gpt_type_guids={table.EFI_SYSTEM_GUID},
            )
        )
        self.assertEqual(
            vol_disk.check_win_system(
                drive="E:",
                system_drive="C:",
                root_entries={"photos"},
                gpt_type_guids={table.BASIC_DATA_GUID},
            ),
            "",
        )

    def test_linux_and_darwin_system_disk_guards(self) -> None:
        for mnt in ("/", "/boot", "/boot/efi"):
            self.assertTrue(
                vol_disk.check_linux_system(
                    mountpoints={mnt}, swap_devices=set(), disk_devices={"/dev/sdb1"}
                )
            )
        self.assertTrue(
            vol_disk.check_linux_system(
                mountpoints={"/mnt/usb"},
                swap_devices={"/dev/sdb2"},
                disk_devices={"/dev/sdb1", "/dev/sdb2"},
            )
        )
        self.assertEqual(
            vol_disk.check_linux_system(
                mountpoints={"/mnt/usb"},
                swap_devices={"/dev/nvme0n1p2"},
                disk_devices={"/dev/sdb1"},
            ),
            "",
        )
        self.assertTrue(
            vol_disk.check_darwin_system(
                mountpoints={"/Volumes/Data"}, apfs_roles=set(), is_boot_disk=True
            )
        )
        self.assertTrue(
            vol_disk.check_darwin_system(
                mountpoints={"/System/Volumes/Data"}, apfs_roles=set(), is_boot_disk=False
            )
        )
        self.assertTrue(
            vol_disk.check_darwin_system(
                mountpoints={"/Volumes/Ext"}, apfs_roles={"Recovery"}, is_boot_disk=False
            )
        )
        self.assertEqual(
            vol_disk.check_darwin_system(
                mountpoints={"/Volumes/USB"}, apfs_roles=set(), is_boot_disk=False
            ),
            "",
        )

    def test_volume_refuses_os_disk_unsupported_fs_and_busy_volume(self) -> None:
        folder = self.root / "data"
        folder.mkdir()
        (folder / "f.txt").write_text("hi", encoding="utf-8")

        sys_disk = vol_disk.FileDisk.create_mbr(
            self.root / "sys.img", 8 * 1024 * 1024, system_reason="disk hosts system drive C:"
        )
        self.addCleanup(sys_disk.close)
        with self.assertRaises(errors.SafetyError):
            volume.hide(folder, b"pw", disk=sys_disk)

        for fs in ("exfat", "fat32", "vfat"):
            fat_disk = vol_disk.FileDisk.create_mbr(
                self.root / f"{fs}.img", 8 * 1024 * 1024, fs_type=fs
            )
            self.addCleanup(fat_disk.close)
            with self.assertRaises(errors.UnsupportedError) as caught:
                volume.hide(folder, b"pw", disk=fat_disk)
            self.assertIn("--level vault", caught.exception.hint)

        busy_disk = vol_disk.FileDisk.create_mbr(
            self.root / "busy.img", 8 * 1024 * 1024, busy=True
        )
        self.addCleanup(busy_disk.close)
        with self.assertRaises(errors.BusyError):
            volume.hide(folder, b"pw", disk=busy_disk)
        self.assertTrue((folder / "f.txt").exists())


class StaticCodeHygieneTest(unittest.TestCase):
    def test_no_shell_true_print_flnd_imports_or_long_lines(self) -> None:
        for path in sorted(SRC_DIR.rglob("*.py")):
            source = path.read_text(encoding="utf-8")
            for idx, line in enumerate(source.splitlines(), 1):
                self.assertLessEqual(len(line), 100, f"{path}:{idx} exceeds 100 chars")
            tree = ast.parse(source)
            for node in ast.walk(tree):
                if isinstance(node, ast.Call):
                    if isinstance(node.func, ast.Name):
                        self.assertNotEqual(node.func.id, "print", f"{path}:{node.lineno} uses print")
                    for kw in node.keywords:
                        if kw.arg == "shell" and isinstance(kw.value, ast.Constant):
                            self.assertFalse(kw.value.value, f"{path}:{node.lineno} uses shell=True")
                elif isinstance(node, ast.Import):
                    for alias in node.names:
                        self.assertFalse(alias.name.startswith("flnd"), f"{path} imports flnd")
                elif isinstance(node, ast.ImportFrom) and node.module:
                    self.assertFalse(node.module.startswith("flnd"), f"{path} imports flnd")


if __name__ == "__main__":
    unittest.main()
