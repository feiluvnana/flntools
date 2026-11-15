"""Tests for `shared._fs`: atomic writes, locks, stale part sweeps, and scans."""

import os
import stat
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import shared
from shared import _clock as clock
from shared import _fs as fs


class FsCase(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.folder = Path(folder.name)
        self.dest = self.folder / "a.txt"

    def names(self):
        return sorted(path.name for path in self.folder.iterdir())


class WritingTest(FsCase):
    def test_part_is_hidden_next_to_dest_and_moved_onto_it(self):
        with fs.writing(self.dest) as part:
            self.assertEqual(part.parent, self.folder)
            self.assertRegex(part.name, r"^\.a\.txt\.[0-9a-f]{8}\.part$")
            part.write_text("new")
            self.assertFalse(self.dest.exists())
        self.assertEqual(self.dest.read_text(), "new")
        self.assertEqual(self.names(), ["a.txt"])

    def test_replace_defaults_to_true_and_can_refuse_existing(self):
        self.dest.write_text("old")
        with fs.writing(self.dest) as part:
            part.write_text("replaced")
        self.assertEqual(self.dest.read_text(), "replaced")
        with self.assertRaises(fs.ExistsError) as ctx:
            with fs.writing(self.dest, replace=False):
                self.fail("block must not run")
        self.assertIsInstance(ctx.exception, fs.Error)
        self.assertIsInstance(ctx.exception, FileExistsError)
        self.assertIsInstance(ctx.exception, shared.Error)

    def test_error_or_interrupt_leaves_dest_and_removes_part(self):
        self.dest.write_text("old")
        for error in (ValueError("boom"), KeyboardInterrupt()):
            with self.subTest(error=type(error).__name__):
                with self.assertRaises(type(error)):
                    with fs.writing(self.dest) as part:
                        part.write_text("half")
                        raise error
                self.assertEqual(self.dest.read_text(), "old")
                self.assertEqual(self.names(), ["a.txt"])

    def test_failed_replace_wraps_os_error_in_fs_error(self):
        with mock.patch("os.replace", side_effect=OSError("disk full")):
            with self.assertRaises(fs.Error) as ctx:
                with fs.writing(self.dest) as part:
                    part.write_text("new")
        self.assertIsInstance(ctx.exception.__cause__, OSError)
        self.assertEqual(self.names(), [])

    def test_sync_flushes_part_before_replace(self):
        with mock.patch.object(fs.os, "fsync", wraps=os.fsync) as spy:
            fs.write(self.dest, b"durable", sync=True)
        self.assertGreaterEqual(spy.call_count, 1)
        self.assertEqual(self.dest.read_bytes(), b"durable")


class WindowsReplaceTest(FsCase):
    def held(self, times):
        real, calls = os.replace, []

        def replace(source, target):
            calls.append(target)
            if len(calls) <= times:
                raise PermissionError(13, "locked", str(target))
            real(source, target)

        return mock.patch.object(fs.os, "replace", replace), calls

    def test_windows_retries_permission_error(self):
        patch, calls = self.held(3)
        with patch, mock.patch.object(fs, "WINDOWS", True), clock.use(clock.Fake()) as fake:
            fs.write(self.dest, b"new")
        self.assertEqual(self.dest.read_bytes(), b"new")
        self.assertEqual(len(calls), 4)
        self.assertEqual(fake.slept, [0.05] * 3)

    def test_windows_gives_up_with_fs_error(self):
        self.dest.write_bytes(b"old")
        patch, calls = self.held(100)
        with patch, mock.patch.object(fs, "WINDOWS", True), clock.use(clock.Fake()):
            with self.assertRaises(fs.Error) as ctx:
                fs.write(self.dest, b"new")
        self.assertIsInstance(ctx.exception.__cause__, PermissionError)
        self.assertEqual(self.dest.read_bytes(), b"old")
        self.assertEqual(len(calls), 20)


class WriteTest(FsCase):
    def test_write_bytes_and_replace_false(self):
        fs.write(self.dest, b"one\ntwo\n")
        self.assertEqual(self.dest.read_bytes(), b"one\ntwo\n")
        with self.assertRaises(fs.ExistsError):
            fs.write(self.dest, b"two", replace=False)
        self.assertEqual(self.dest.read_bytes(), b"one\ntwo\n")

    @unittest.skipIf(sys.platform == "win32", "Windows has no POSIX modes")
    def test_mode_applied_on_creation(self):
        fs.write(self.dest, b"secret", mode=0o600)
        self.assertEqual(stat.S_IMODE(self.dest.stat().st_mode), 0o600)


class LockTest(FsCase):
    def test_lock_folder_and_file_path(self):
        with fs.lock(self.folder):
            self.assertTrue((self.folder / ".lock").exists())
        custom = self.folder / "sub" / "custom.lock"
        with fs.lock(custom):
            self.assertTrue(custom.exists())

    def test_lock_timeout_raises_fs_error(self):
        lock_file = self.folder / "a.lock"
        with fs.lock(lock_file):
            timeout_err = fs.filelock.Timeout(str(lock_file))
            with mock.patch("filelock.FileLock.acquire", side_effect=timeout_err):
                with self.assertRaises(fs.Error):
                    with fs.lock(lock_file, timeout=0.01):
                        pass


class SweepAndScanTest(FsCase):
    def test_scan_collects_files_and_dirs_in_one_pass(self):
        sub = self.folder / "sub"
        sub.mkdir()
        (self.folder / "top.txt").write_text("a")
        (sub / "nested.txt").write_text("b")
        result = fs.scan(self.folder)
        self.assertEqual(sorted(e.name for e in result.files), ["nested.txt", "top.txt"])
        self.assertEqual([e.name for e in result.dirs], ["sub"])

    def test_scan_missing_folder_raises_fs_error(self):
        with self.assertRaises(fs.Error) as ctx:
            fs.scan(self.folder / "missing")
        self.assertIsInstance(ctx.exception.__cause__, OSError)

    def test_sweep_respects_settle_and_keep_and_cleans_orphan_sidecars(self):
        bare_old = self.folder / ".a.11111111.part"
        bare_new = self.folder / ".b.22222222.part"
        resumable_mid = self.folder / ".c.33333333.part"
        resumable_mid_side = self.folder / ".c.33333333.part.json"
        resumable_old = self.folder / ".d.44444444.part"
        resumable_old_side = self.folder / ".d.44444444.part.json"
        orphan_side = self.folder / ".e.55555555.part.info"
        keep_file = self.folder / "real.mp4"
        for p in (
            bare_old, bare_new, resumable_mid, resumable_mid_side,
            resumable_old, resumable_old_side, orphan_side, keep_file,
        ):
            p.write_text("x")

        now = 1_000_000.0
        os.utime(bare_old, (now - 4000, now - 4000))
        os.utime(bare_new, (now - 100, now - 100))
        os.utime(resumable_mid, (now - 4000, now - 4000))
        os.utime(resumable_old, (now - 800_000, now - 800_000))
        os.utime(orphan_side, (now - 4000, now - 4000))

        fake = clock.Fake()
        fake.advance(now - fake.wall())
        with clock.use(fake):
            removed = fs.sweep(self.folder, keep=7 * 86_400, settle=3_600)
        self.assertEqual(removed, 4)
        self.assertFalse(bare_old.exists())
        self.assertTrue(bare_new.exists())
        self.assertTrue(resumable_mid.exists())
        self.assertTrue(resumable_mid_side.exists())
        self.assertFalse(resumable_old.exists())
        self.assertFalse(resumable_old_side.exists())
        self.assertFalse(orphan_side.exists())
        self.assertTrue(keep_file.exists())

    def test_sweep_missing_folder_returns_zero(self):
        self.assertEqual(fs.sweep(self.folder / "nope"), 0)
