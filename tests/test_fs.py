"""Tests for `shared._fs`: atomic writes, stages, locks, walks and stale stage sweeps."""

import os
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


class StageTest(FsCase):
    def test_stage_has_a_fixed_hidden_name_and_lands_on_dest(self):
        with fs.stage(self.dest) as temp:
            self.assertEqual(temp, self.folder / ".a.txt.stage")
            temp.write_text("new")
            self.assertFalse(self.dest.exists())
        self.assertEqual(self.dest.read_text(), "new")
        self.assertEqual(self.names(), ["a.txt"])

    def test_an_error_keeps_the_stage_for_the_next_run_to_resume(self):
        self.dest.write_text("old")
        with self.assertRaises(ValueError):
            with fs.stage(self.dest) as temp:
                temp.write_text("half")
                raise ValueError("boom")
        self.assertEqual(self.dest.read_text(), "old")
        self.assertEqual(self.names(), [".a.txt.stage", "a.txt"])
        with fs.stage(self.dest) as again:
            self.assertEqual(again.read_text(), "half")
            with again.open("a") as file:
                file.write(" more")
        self.assertEqual(self.dest.read_text(), "half more")

    def test_keep_false_or_interrupt_removes_the_stage(self):
        self.dest.write_text("old")
        for error in (ValueError("boom"), KeyboardInterrupt()):
            with self.subTest(error=type(error).__name__):
                with self.assertRaises(type(error)):
                    with fs.stage(self.dest, keep=False) as temp:
                        temp.write_text("half")
                        raise error
                self.assertEqual(self.dest.read_text(), "old")
                self.assertEqual(self.names(), ["a.txt"])

    def test_a_second_writer_gets_busy_error_after_timeout(self):
        with fs.stage(self.dest):
            with self.assertRaises(fs.BusyError) as ctx:
                with fs.stage(self.dest, timeout=0.01):
                    self.fail("block must not run")
        self.assertIsInstance(ctx.exception, fs.Error)

    def test_a_block_that_moves_the_stage_away_wins(self):
        with fs.stage(self.dest) as temp:
            temp.write_text("x")
            os.replace(temp, self.folder / "b.txt")
        self.assertEqual(self.names(), ["b.txt"])

    def test_failed_replace_wraps_os_error_in_fs_error(self):
        with mock.patch("os.replace", side_effect=OSError("disk full")):
            with self.assertRaises(fs.Error) as ctx:
                with fs.stage(self.dest, keep=False) as temp:
                    temp.write_text("new")
        self.assertIsInstance(ctx.exception.__cause__, OSError)
        self.assertEqual(self.names(), [])


class WriteTest(FsCase):
    def test_write_bytes_or_text_over_a_stale_stage(self):
        (self.folder / ".a.txt.stage").write_bytes(b"left by a crash, longer")
        self.assertEqual(fs.write(self.dest, b"one\ntwo\n"), self.dest)
        self.assertEqual(self.dest.read_bytes(), b"one\ntwo\n")
        fs.write(self.dest, "text")
        self.assertEqual(self.dest.read_text(), "text")
        self.assertEqual(self.names(), ["a.txt"])

    def test_replace_false_refuses_an_existing_dest(self):
        self.dest.write_text("old")
        with self.assertRaises(fs.ExistsError) as ctx:
            fs.write(self.dest, b"new", replace=False)
        self.assertIsInstance(ctx.exception, FileExistsError)
        self.assertIsInstance(ctx.exception, shared.Error)
        self.assertEqual(self.dest.read_text(), "old")
        self.assertEqual(self.names(), ["a.txt"])

    def test_replace_false_never_overwrites_a_file_created_meanwhile(self):
        real = fs._write_bytes

        def racing(temp, data):
            real(temp, data)
            self.dest.write_text("theirs")

        with mock.patch.object(fs, "_write_bytes", racing):
            with self.assertRaises(fs.ExistsError):
                fs.write(self.dest, b"mine", replace=False)
        self.assertEqual(self.dest.read_text(), "theirs")
        self.assertEqual(self.names(), ["a.txt"])

    def test_replace_false_without_hard_links_checks_then_replaces(self):
        with mock.patch.object(fs.os, "link", side_effect=OSError("no links")):
            fs.write(self.dest, b"new", replace=False)
            self.assertEqual(self.dest.read_bytes(), b"new")
            with self.assertRaises(fs.ExistsError):
                fs.write(self.dest, b"again", replace=False)
        self.assertEqual(self.names(), ["a.txt"])

    def test_sync_flushes_before_replace(self):
        with mock.patch.object(fs.os, "fsync", wraps=os.fsync) as spy:
            out = fs.write(self.dest, b"durable", sync=True)
        self.assertGreaterEqual(spy.call_count, 1)
        self.assertEqual(out, self.dest)
        self.assertEqual(self.dest.read_bytes(), b"durable")


class TouchTest(FsCase):
    def test_touch_sets_both_times(self):
        self.dest.write_text("x")
        fs.touch(self.dest, mtime=1_000_000.0)
        stat = self.dest.stat()
        self.assertEqual((stat.st_atime, stat.st_mtime), (1_000_000.0, 1_000_000.0))
        with self.assertRaises(fs.Error):
            fs.touch(self.folder / "missing", mtime=0)


class WalkTest(FsCase):
    def test_walk_yields_files_recursively_and_skips_stages(self):
        (self.folder / "sub" / "deep").mkdir(parents=True)
        for name in ("a.jpg", "sub/b.MP4", "sub/deep/c.txt", ".x.jpg.stage", ".x.jpg.stage.lock",
                     ".x.jpg.stage.json", ".old.1234abcd.part", ".hidden.jpg"):
            (self.folder / name).write_text("x")
        found = sorted(os.path.relpath(e.path, self.folder) for e in fs.walk(self.folder))
        self.assertEqual(found, [".hidden.jpg", "a.jpg", os.path.join("sub", "b.MP4"),
                                 os.path.join("sub", "deep", "c.txt")])
        media = sorted(e.name for e in fs.walk(self.folder, suffix=(".jpg", ".mp4")))
        self.assertEqual(media, [".hidden.jpg", "a.jpg", "b.MP4"])
        self.assertTrue(all(isinstance(e, os.DirEntry) for e in fs.walk(self.folder)))


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


class LockTest(FsCase):
    def test_lock_folder_and_file_path(self):
        with fs.lock(self.folder) as flock:
            self.assertIsInstance(flock, fs.filelock.FileLock)
            self.assertTrue((self.folder / ".lock").exists())
        custom = self.folder / "sub" / "custom.lock"
        with fs.lock(custom) as flock:
            self.assertIsInstance(flock, fs.filelock.FileLock)
            self.assertTrue(custom.exists())

    def test_lock_timeout_raises_fs_error(self):
        lock_file = self.folder / "a.lock"
        with fs.lock(lock_file):
            timeout_err = fs.filelock.Timeout(str(lock_file))
            with mock.patch("filelock.FileLock.acquire", side_effect=timeout_err):
                with self.assertRaises(fs.Error):
                    with fs.lock(lock_file, timeout=0.01):
                        pass


class LockErrorTest(FsCase):
    def test_an_os_error_inside_the_block_is_not_wrapped(self):
        with self.assertRaises(FileNotFoundError) as ctx:
            with fs.lock(self.folder / "a.lock"):
                open(self.folder / "missing")
        self.assertNotIsInstance(ctx.exception, fs.Error)

    def test_timeout_raises_busy_error(self):
        path = self.folder / "a.lock"
        with fs.lock(path):
            with self.assertRaises(fs.BusyError):
                with fs.lock(path, timeout=0):
                    self.fail("block must not run")


class SweepTest(FsCase):
    def test_sweep_respects_settle_and_keep_and_cleans_orphan_sidecars(self):
        bare_old = self.folder / ".a.11111111.part"
        bare_new = self.folder / ".b.stage"
        resumable_mid = self.folder / ".c.stage"
        resumable_mid_side = self.folder / ".c.stage.json"
        resumable_old = self.folder / ".d.stage"
        resumable_old_side = self.folder / ".d.stage.json"
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
        fake.advance(now - fake.wall)
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

    def test_sweep_skips_a_held_stage_and_clears_orphan_locks(self):
        held = self.folder / ".a.stage"
        orphan_lock = self.folder / ".b.stage.lock"
        for path in (held, orphan_lock):
            path.write_text("x")
            os.utime(path, (0, 0))
        with fs.stage(self.folder / "a", keep=False):
            removed = fs.sweep(self.folder)
            self.assertEqual(self.names(), [".a.stage", ".a.stage.lock"])
            held.unlink()
        self.assertEqual(removed, 1)
        self.assertEqual(self.names(), [])

    def test_sweep_missing_folder_returns_zero(self):
        self.assertEqual(fs.sweep(self.folder / "nope"), 0)
