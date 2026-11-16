"""Tests for `flnd._files`: parts, their locks, small writes, walks and stale-part sweeps.

Two processes sharing a home are played by two threads: a POSIX `flock` belongs to an open file
description, so two opens in one process exclude each other just as two processes do.
"""

import os
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from flnd import _files, _time
from flnd.errors import DiskError


class FsCase(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.folder = Path(folder.name)
        self.dest = self.folder / "a.txt"

    def names(self):
        return sorted(path.name for path in self.folder.iterdir())


class PartTest(FsCase):
    def test_a_part_sits_beside_dest_and_lands_on_it_with_its_size_and_time(self):
        with _files.part(self.dest, resume=True) as held:
            self.assertEqual(held.path, self.folder / "a.txt.part")
            held.write(b"new")
            held.mtime = 1_000_000.0
            self.assertFalse(self.dest.exists())
        self.assertEqual(self.dest.read_bytes(), b"new")
        self.assertEqual(self.names(), ["a.txt"])
        found = self.dest.stat()
        self.assertEqual(held.saved, _files.Saved(self.dest, 3, found.st_mtime_ns))
        self.assertEqual(found.st_mtime, 1_000_000.0)

    def test_the_folder_is_made_when_missing(self):
        dest = self.folder / "x" / "y" / "a.jpg"
        with _files.part(dest, resume=True) as held:
            held.write(b"x")
        self.assertEqual(dest.read_bytes(), b"x")

    def test_a_long_name_gets_a_short_part_with_a_digest(self):
        dest = self.folder / ("Tiếng Việt 日本語 " * 10 + ".mp4")
        self.assertLessEqual(len(dest.name.encode()), 255)
        with self.assertRaises(ValueError):
            with _files.part(dest, resume=True) as held:
                self.assertLessEqual(len((held.path.name + ".json.part").encode()), 255)
                self.assertRegex(held.path.name, r"^.+\.[0-9a-f]{8}\.part$")
                held.write(b"x")
                raise ValueError("boom")
        with _files.part(dest, resume=True) as again:
            self.assertEqual((again.path, again.size), (held.path, 1))
        self.assertEqual(dest.read_bytes(), b"x")

    def test_a_failed_download_keeps_its_part_and_the_next_run_resumes_it(self):
        self.dest.write_text("old")
        with self.assertRaises(KeyboardInterrupt):
            with _files.part(self.dest, resume=True) as held:
                held.write(b"half")
                held.note(etag='"e1"')
                raise KeyboardInterrupt
        self.assertEqual(self.dest.read_text(), "old")
        self.assertEqual(self.names(), ["a.txt", "a.txt.part", "a.txt.part.json"])
        with _files.part(self.dest, resume=True) as again:
            self.assertEqual((again.size, again.meta), (4, {"etag": '"e1"'}))
            again.write(b" more")
        self.assertEqual(self.dest.read_bytes(), b"half more")
        self.assertEqual(self.names(), ["a.txt"])

    def test_an_empty_or_dropped_part_is_removed(self):
        with self.assertRaises(ValueError):
            with _files.part(self.dest, resume=True):
                raise ValueError("nothing written")
        with _files.part(self.dest, resume=True) as held:
            held.write(b"a web page")
            held.drop()
        self.assertEqual(self.names(), [])

    def test_a_reset_starts_over_and_forgets_the_sidecar(self):
        (self.folder / "a.txt.part").write_bytes(b"stale bytes")
        (self.folder / "a.txt.part.json").write_text('{"etag": "\\"old\\""}')
        with _files.part(self.dest, resume=True) as held:
            self.assertEqual(held.meta, {"etag": '"old"'})
            held.reset()
            self.assertEqual((held.size, held.meta), (0, {}))
            held.write(b"fresh")
        self.assertEqual(self.dest.read_bytes(), b"fresh")
        self.assertEqual(self.names(), ["a.txt"])

    def test_write_at_and_head_use_the_descriptor(self):
        with _files.part(self.dest, resume=True) as held:
            held.write_at(3, b"def")
            held.write_at(0, b"abc")
            self.assertEqual(held.head(4), b"abcd")
            held.cut(5)
        self.assertEqual(self.dest.read_bytes(), b"abcde")

    def test_a_failed_rename_is_a_disk_error_and_removes_a_writes_part(self):
        with mock.patch("os.replace", side_effect=OSError("disk full")):
            with self.assertRaises(DiskError) as ctx:
                _files.write(self.dest, b"new")
        self.assertIsInstance(ctx.exception.__cause__, OSError)
        self.assertEqual(self.names(), [])


class LockTest(FsCase):
    def test_a_second_download_of_a_held_part_is_busy(self):
        with _files.part(self.dest, resume=True) as held:
            held.write(b"mine")
            with self.assertRaises(_files.BusyError) as ctx:
                with _files.part(self.dest, resume=True):
                    self.fail("the block must not run")
        self.assertIsInstance(ctx.exception, DiskError)
        self.assertIn("being saved by another run", str(ctx.exception))
        self.assertEqual(self.dest.read_bytes(), b"mine")

    def test_a_part_landed_between_open_and_lock_is_busy_not_rewritten(self):
        """The second run opened the old part just before the first renamed it into place."""
        first = _files.part(self.dest, resume=True)
        held = first.__enter__()
        held.write(b"whole")
        real = _files._lock

        def late(fd, *, wait):
            first.__exit__(None, None, None)
            return real(fd, wait=wait)

        with mock.patch.object(_files, "_lock", late):
            with self.assertRaises(_files.BusyError):
                with _files.part(self.dest, resume=True):
                    self.fail("the block must not run")
        self.assertEqual(self.dest.read_bytes(), b"whole")
        self.assertEqual(self.names(), ["a.txt"])

    def test_a_small_write_waits_for_the_other_writer(self):
        order = []
        with _files.part(self.dest) as held:
            held.write(b"first")
            other = threading.Thread(target=lambda: order.append(_files.write(self.dest, b"second")))
            other.start()
            other.join(0.2)
            self.assertTrue(other.is_alive())
            order.append("first")
        other.join(5)
        self.assertEqual(order, ["first", self.dest])
        self.assertEqual(self.dest.read_bytes(), b"second")
        self.assertEqual(self.names(), ["a.txt"])

    def test_two_runs_on_one_home_save_a_file_once(self):
        """Two writers race for one file: one saves it, the other is told it's busy."""
        start = threading.Barrier(2)
        outcomes = []

        def run(data):
            start.wait()
            try:
                with _files.part(self.dest, resume=True) as held:
                    held.write(data)
                    _time.sleep(0.05)
                outcomes.append("saved")
            except _files.BusyError:
                outcomes.append("busy")

        threads = [threading.Thread(target=run, args=(data,)) for data in (b"one", b"two")]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(5)
        self.assertEqual(sorted(outcomes), ["busy", "saved"])
        self.assertIn(self.dest.read_bytes(), (b"one", b"two"))
        self.assertEqual(self.names(), ["a.txt"])


class CountTest(FsCase):
    """The disk bookkeeping of one download, counted in system calls, not timed."""

    CALLS = ("open", "stat", "lstat", "fstat", "utime", "replace", "unlink", "close")

    def count(self, body):
        counts = dict.fromkeys(self.CALLS, 0)
        patches = []
        for name in self.CALLS:
            real = getattr(os, name)

            def spy(*args, _real=real, _name=name, **kwargs):
                counts[_name] += 1
                return _real(*args, **kwargs)

            patches.append(mock.patch.object(_files.os, name, spy))
        for patch in patches:
            patch.start()
        try:
            body()
        finally:
            for patch in patches:
                patch.stop()
        return {name: n for name, n in counts.items() if n}

    def download(self, dest):
        with _files.part(dest, resume=True) as held:
            held.write(b"x" * 1000)
            held.mtime = 1_000_000.0

    def test_a_new_download_opens_once_and_never_stats_a_path(self):
        counts = self.count(lambda: self.download(self.dest))
        self.assertEqual(counts, {"open": 1, "fstat": 1, "utime": 1, "replace": 1, "close": 1})

    def test_a_resumed_download_adds_one_stat_to_check_its_part(self):
        (self.folder / "a.txt.part").write_bytes(b"half")
        counts = self.count(lambda: self.download(self.dest))
        self.assertEqual(counts, {"open": 2, "stat": 1, "fstat": 2, "utime": 1, "replace": 1,
                                  "close": 1})


class WriteTest(FsCase):
    def test_write_bytes_or_text_over_a_stale_part(self):
        (self.folder / "a.txt.part").write_bytes(b"left by a crash, longer")
        self.assertEqual(_files.write(self.dest, b"one\ntwo\n"), self.dest)
        self.assertEqual(self.dest.read_bytes(), b"one\ntwo\n")
        _files.write(self.dest, "text")
        self.assertEqual(self.dest.read_text(), "text")
        self.assertEqual(self.names(), ["a.txt"])

    def test_a_failed_write_leaves_nothing(self):
        with self.assertRaises(ValueError):
            with _files.part(self.dest) as held:
                held.write(b"half")
                raise ValueError("boom")
        self.assertEqual(self.names(), [])


class WindowsReplaceTest(FsCase):
    def held(self, times):
        real, calls = os.replace, []

        def replace(source, target):
            calls.append(target)
            if len(calls) <= times:
                raise PermissionError(13, "locked", str(target))
            real(source, target)

        return calls, replace

    def test_windows_retries_permission_error(self):
        calls, replace = self.held(3)
        (self.folder / "x").write_bytes(b"new")
        with mock.patch.object(_files.os, "replace", replace), \
                mock.patch.object(_files, "WINDOWS", True), _time.use(_time.Fake()) as fake:
            _files._replace(self.folder / "x", self.dest)
        self.assertEqual(self.dest.read_bytes(), b"new")
        self.assertEqual(len(calls), 4)
        self.assertEqual(fake.slept, [0.05] * 3)

    def test_windows_gives_up_after_about_a_second(self):
        calls, replace = self.held(100)
        (self.folder / "x").write_bytes(b"new")
        with mock.patch.object(_files.os, "replace", replace), \
                mock.patch.object(_files, "WINDOWS", True), _time.use(_time.Fake()):
            with self.assertRaises(PermissionError):
                _files._replace(self.folder / "x", self.dest)
        self.assertEqual(len(calls), 20)


class WalkTest(FsCase):
    def test_walk_yields_files_recursively_and_skips_parts(self):
        (self.folder / "sub" / "deep").mkdir(parents=True)
        for name in ("a.jpg", "sub/b.MP4", "sub/deep/c.txt", "x.jpg.part", "x.jpg.part.json",
                     ".x.jpg.stage", ".x.jpg.stage.lock", ".x.jpg.stage.json", ".old.1234abcd.part",
                     ".hidden.jpg"):
            (self.folder / name).write_text("x")
        found = sorted(os.path.relpath(e.path, self.folder) for e in _files.walk(self.folder))
        self.assertEqual(found, [".hidden.jpg", "a.jpg", os.path.join("sub", "b.MP4"),
                                 os.path.join("sub", "deep", "c.txt")])
        self.assertTrue(all(isinstance(e, os.DirEntry) for e in _files.walk(self.folder)))


class SweepTest(FsCase):
    def age(self, path, seconds, now):
        os.utime(path, (now - seconds, now - seconds))

    def test_sweep_keeps_parts_a_week_and_clears_orphans_after_an_hour(self):
        now = 1_000_000.0
        files = {
            "new.jpg.part": 4000, "new.jpg.part.json": 4000,      # resumable: kept
            "old.jpg.part": 800_000, "old.jpg.part.json": 800_000,  # a week old: removed
            "lone.jpg.part.json": 4000,                            # orphan sidecar: removed
            "young.jpg.part.json": 100,                            # orphan, too new: kept
            ".legacy.mp4.stage": 800_000, ".legacy.mp4.stage.lock": 800_000,  # removed
            ".gone.stage.lock": 4000,                              # orphan old lock: removed
            "real.mp4": 800_000,                                   # media: kept
        }
        for name, seconds in files.items():
            (self.folder / name).write_text("x")
            self.age(self.folder / name, seconds, now)
        fake = _time.Fake()
        fake.advance(now - fake.wall)
        with _time.use(fake):
            removed = _files.sweep(self.folder)
        self.assertEqual(removed, 6)
        self.assertEqual(self.names(), ["new.jpg.part", "new.jpg.part.json", "real.mp4",
                                        "young.jpg.part.json"])

    def test_sweep_skips_a_part_another_run_holds(self):
        with _files.part(self.folder / "a", resume=True) as held:
            held.write(b"x")
            os.utime(held.path, (0, 0))
            self.assertEqual(_files.sweep(self.folder), 0)
            self.assertEqual(self.names(), ["a.part"])
        self.assertEqual(self.names(), ["a"])

    def test_sweep_missing_folder_returns_zero(self):
        self.assertEqual(_files.sweep(self.folder / "nope"), 0)


if __name__ == "__main__":
    unittest.main()
