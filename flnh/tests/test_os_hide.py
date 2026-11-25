"""Tests for Levels 1 & 2 (`flag` and `lock`) across Windows, macOS, and Linux."""

from __future__ import annotations

import os
import stat
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from flnh import errors, os_hide
from flnh.os_hide import darwin, linux


class CurrentOSHideTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def test_level1_flag_folder_and_file_vanish_and_restore(self) -> None:
        folder = self.root / "secret_docs"
        folder.mkdir()
        (folder / "note.txt").write_text("hello", encoding="utf-8")
        single = self.root / "diary.txt"
        single.write_text("top secret", encoding="utf-8")

        res_dir = os_hide.hide(folder, level="flag")
        res_file = os_hide.hide(single, level="flag")
        self.assertFalse(folder.exists())
        self.assertFalse(single.exists())
        self.assertNotEqual(res_dir["hidden_path"], res_file["hidden_path"])
        if sys.platform == "win32":
            from flnh.os_hide import win32

            attrs = win32.get_attrs(Path(res_dir["hidden_path"]))
            self.assertTrue(attrs & win32.FILE_ATTRIBUTE_HIDDEN)
            self.assertTrue(attrs & win32.FILE_ATTRIBUTE_SYSTEM)

        restored_dir, lvl_dir = os_hide.show(folder)
        restored_file, lvl_file = os_hide.show(single)
        self.assertEqual((restored_dir, lvl_dir), (folder, "flag"))
        self.assertEqual((restored_file, lvl_file), (single, "flag"))
        self.assertEqual((folder / "note.txt").read_text(encoding="utf-8"), "hello")
        self.assertEqual(single.read_text(encoding="utf-8"), "top secret")

    def test_level1_no_collision_for_multiple_folders(self) -> None:
        a = self.root / "alpha"
        b = self.root / "beta"
        a.mkdir()
        b.mkdir()
        (a / "a.txt").write_text("A", encoding="utf-8")
        (b / "b.txt").write_text("B", encoding="utf-8")

        ra = os_hide.hide(a, level="flag")
        rb = os_hide.hide(b, level="flag")
        self.assertNotEqual(ra["hidden_path"], rb["hidden_path"])
        os_hide.show(a)
        os_hide.show(b)
        self.assertEqual((a / "a.txt").read_text(encoding="utf-8"), "A")
        self.assertEqual((b / "b.txt").read_text(encoding="utf-8"), "B")

    def test_level2_lock_denies_access_and_checks_password(self) -> None:
        folder = self.root / "locked_box"
        folder.mkdir()
        secret = folder / "key.txt"
        secret.write_text("vault-key", encoding="utf-8")

        res = os_hide.hide(folder, level="lock", password="correct-horse", track=False)
        self.assertFalse(folder.exists())
        hidden = Path(res["hidden_path"])
        if sys.platform == "win32" or os.geteuid() != 0:
            with self.assertRaises(PermissionError):
                list(hidden.iterdir())

        with self.assertRaises(errors.AuthError):
            os_hide.show(folder, get_password=lambda: "wrong-pass")
        self.assertFalse(folder.exists())
        if sys.platform == "win32" or os.geteuid() != 0:
            with self.assertRaises(PermissionError):
                list(hidden.iterdir())

        restored, lvl = os_hide.show(folder, get_password=lambda: "correct-horse")
        self.assertEqual((restored, lvl), (folder, "lock"))
        self.assertEqual((folder / "key.txt").read_text(encoding="utf-8"), "vault-key")
        self.assertFalse((folder / ".flnh.meta").exists())

    def test_level2_lock_single_file(self) -> None:
        doc = self.root / "id.txt"
        doc.write_text("passport", encoding="utf-8")
        res = os_hide.hide(doc, level="lock", password="pw", track=False)
        self.assertFalse(doc.exists())
        if sys.platform == "win32" or os.geteuid() != 0:
            with self.assertRaises(PermissionError):
                list(Path(res["hidden_path"]).iterdir())
        os_hide.show(doc, get_password=lambda: "pw")
        self.assertEqual(doc.read_text(encoding="utf-8"), "passport")

    def test_idempotent_refusals(self) -> None:
        folder = self.root / "item"
        folder.mkdir()
        os_hide.hide(folder, level="flag")
        with self.assertRaises(errors.UsageError):
            os_hide.hide(folder, level="flag")
        os_hide.show(folder)
        with self.assertRaises(errors.UsageError):
            os_hide.show(folder)


class LinuxBackendTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def test_flag_dot_rename_and_dot_hidden_preservation(self) -> None:
        dot_hidden = self.root / ".hidden"
        dot_hidden.write_text("preexisting\n", encoding="utf-8")
        folder = self.root / "pics"
        folder.mkdir()
        (folder / "1.jpg").write_bytes(b"img")

        res = linux.hide(folder, level="flag")
        hidden = Path(res["hidden_path"])
        self.assertEqual(hidden.name, ".pics")
        self.assertFalse(folder.exists())
        self.assertEqual(dot_hidden.read_text(encoding="utf-8").splitlines(), ["preexisting", ".pics"])

        linux.show(folder)
        self.assertTrue(folder.is_dir())
        self.assertEqual(dot_hidden.read_text(encoding="utf-8").splitlines(), ["preexisting"])

    def test_lock_chmod_and_pass_with_cleanup(self) -> None:
        folder = self.root / "vault_dir"
        folder.mkdir()
        (folder / "x.txt").write_text("secret", encoding="utf-8")

        modes: list[int] = []
        orig_chmod = os.chmod

        def fake_chmod(p: Path, mode: int) -> None:
            modes.append(mode)
            if os.name != "nt":
                orig_chmod(p, mode)

        with mock.patch("os.chmod", side_effect=fake_chmod):
            res = linux.hide(folder, level="lock", password="pw", track=False)
            self.assertIn(0o000, modes)
            with self.assertRaises(errors.AuthError):
                linux.show(folder, get_password=lambda: "bad")
            _, lvl = linux.show(folder, get_password=lambda: "pw")
            self.assertEqual(lvl, "lock")
        self.assertTrue(folder.is_dir())
        self.assertFalse((self.root / ".hidden").exists())
        self.assertFalse(Path(res["hidden_path"]).exists())


class DarwinBackendTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def test_flag_and_lock_via_chflags(self) -> None:
        folder = self.root / "mac_secret"
        folder.mkdir()
        flags_map: dict[str, int] = {}
        orig_lstat = Path.lstat

        class FakeStat:
            def __init__(self, real: os.stat_result, fl: int) -> None:
                self._real = real
                self.st_flags = fl

            def __getattr__(self, name: str) -> object:
                return getattr(self._real, name)

        def fake_lstat(p: Path) -> FakeStat:
            return FakeStat(orig_lstat(p), flags_map.get(str(p), 0))

        def fake_chflags(p: Path, val: int) -> None:
            flags_map[str(p)] = val

        with (
            mock.patch.object(os, "chflags", fake_chflags, create=True),
            mock.patch.object(Path, "lstat", fake_lstat),
            mock.patch("os.chmod"),
        ):
            darwin.hide(folder, level="lock", password="macpw", track=False)
            self.assertTrue(flags_map[str(folder)] & darwin.UF_HIDDEN)
            self.assertTrue(flags_map[str(folder)] & darwin.UF_IMMUTABLE)
            with self.assertRaises(errors.AuthError):
                darwin.show(folder, get_password=lambda: "nope")
            restored, lvl = darwin.show(folder, get_password=lambda: "macpw")
            self.assertEqual((restored, lvl), (folder, "lock"))
            self.assertEqual(flags_map[str(folder)], 0)
            self.assertFalse(any(p.name.startswith(".flnh.") for p in self.root.iterdir()))
            self.assertTrue(stat.S_ISDIR(orig_lstat(folder).st_mode))


if __name__ == "__main__":
    unittest.main()
