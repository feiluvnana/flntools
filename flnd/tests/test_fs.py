"""`flnkit.files`: parts, the home lock, small writes, walks and stale-part sweeps.

Two runs on one home are played by two opens in one process: a POSIX `flock` belongs to an
open file description, so they exclude each other as two processes do.
"""

import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from flnd.errors import DiskError
from flnkit import clock as _time
from flnkit import files as _files


class FsCase(unittest.TestCase):
    def setUp(self):
        self.folder = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.dest = self.folder / "a.txt"

    def names(self):
        return sorted(path.name for path in self.folder.iterdir())

    def part(self, dest=None):
        return _files.part(dest or self.dest, resume=True)


class PartTest(FsCase):
    def test_a_part_sits_beside_dest_and_lands_on_it_with_its_size_and_time(self):
        with self.part() as held:
            self.assertEqual(held.path, self.folder / "a.txt.part")
            held.write(b"new")
            held.mtime = 1_000_000.0
            self.assertFalse(self.dest.exists())
        self.assertEqual((self.dest.read_bytes(), self.names()), (b"new", ["a.txt"]))
        found = self.dest.stat()
        self.assertEqual(held.saved, _files.Saved(self.dest, 3, found.st_mtime_ns))
        self.assertEqual(found.st_mtime, 1_000_000.0)
        # The folder is made when missing.
        deep = self.folder / "x" / "y" / "a.jpg"
        with self.part(deep) as held:
            held.write(b"x")
        self.assertEqual(deep.read_bytes(), b"x")

    def test_a_long_name_gets_a_short_part_with_a_digest(self):
        dest = self.folder / ("Tiếng Việt 日本語 " * 10 + ".mp4")
        self.assertLessEqual(len(dest.name.encode()), 255)
        with self.assertRaises(ValueError), self.part(dest) as held:
            self.assertLessEqual(len((held.path.name + ".json.part").encode()), 255)
            self.assertRegex(held.path.name, r"^.+\.[0-9a-f]{8}\.part$")
            held.write(b"x")
            raise ValueError("boom")
        with self.part(dest) as again:
            self.assertEqual((again.path, again.size), (held.path, 1))
        self.assertEqual(dest.read_bytes(), b"x")

    def test_a_failed_download_keeps_its_part_and_the_next_run_resumes_it(self):
        self.dest.write_text("old")
        with self.assertRaises(KeyboardInterrupt), self.part() as held:
            held.write(b"half")
            held.note(etag='"e1"')
            raise KeyboardInterrupt
        self.assertEqual(self.dest.read_text(), "old")
        self.assertEqual(self.names(), ["a.txt", "a.txt.part", "a.txt.part.json"])
        with self.part() as again:
            self.assertEqual((again.size, again.meta), (4, {"etag": '"e1"'}))
            again.write(b" more")
        self.assertEqual((self.dest.read_bytes(), self.names()), (b"half more", ["a.txt"]))

    def test_an_empty_or_dropped_part_is_removed(self):
        with self.assertRaises(ValueError), self.part():
            raise ValueError("nothing written")
        with self.part() as held:
            held.write(b"a web page")
            held.drop()
        self.assertEqual(self.names(), [])

    def test_a_reset_starts_over_and_forgets_the_sidecar(self):
        (self.folder / "a.txt.part").write_bytes(b"stale bytes")
        (self.folder / "a.txt.part.json").write_text('{"etag": "\\"old\\""}')
        with self.part() as held:
            self.assertEqual(held.meta, {"etag": '"old"'})
            held.reset()
            self.assertEqual((held.size, held.meta), (0, {}))
            held.write(b"fresh")
        self.assertEqual((self.dest.read_bytes(), self.names()), (b"fresh", ["a.txt"]))

    def test_write_at_head_and_cut(self):
        with self.part() as held:
            held.write_at(3, b"def")
            held.write_at(0, b"abc")
            self.assertEqual(held.head(4), b"abcd")
            held.cut(5)
        self.assertEqual(self.dest.read_bytes(), b"abcde")

    def test_write_over_a_stale_part_and_a_failure_leaves_nothing(self):
        (self.folder / "a.txt.part").write_bytes(b"left by a crash, longer")
        self.assertEqual(_files.write(self.dest, b"one\ntwo\n"), self.dest)
        self.assertEqual(self.dest.read_bytes(), b"one\ntwo\n")
        _files.write(self.dest, "text")
        self.assertEqual((self.dest.read_text(), self.names()), ("text", ["a.txt"]))
        self.dest.unlink()
        with self.assertRaises(ValueError), _files.part(self.dest) as held:
            held.write(b"half")
            raise ValueError("boom")
        self.assertEqual(self.names(), [])
        with mock.patch("os.replace", side_effect=OSError("disk full")), \
                self.assertRaises(DiskError) as caught:
            _files.write(self.dest, b"new")
        self.assertIsInstance(caught.exception.__cause__, OSError)
        self.assertEqual(self.names(), [])


class LockTest(FsCase):
    def test_a_second_lock_on_a_held_file_is_refused_until_it_is_released(self):
        path = self.folder / "sub" / ".lock"
        with _files.lock(path) as first, _files.lock(path) as second:
            self.assertEqual((first, second), (True, False))
        with _files.lock(path) as again:
            self.assertTrue(again)
        self.assertEqual(path.read_bytes(), b"", "the lock file is only a lock")

    def test_the_lock_is_released_after_an_error(self):
        path = self.folder / ".lock"
        with self.assertRaises(ValueError), _files.lock(path):
            raise ValueError("boom")
        with _files.lock(path) as free:
            self.assertTrue(free)

    def test_a_file_system_without_locks_counts_as_free(self):
        error = OSError(_files.errno.ENOLCK, "no locks")
        with mock.patch.object(_files, "fcntl", create=True) as fcntl, \
                mock.patch.object(_files, "WINDOWS", False):
            fcntl.flock.side_effect = error
            with _files.lock(self.folder / ".lock") as free:
                self.assertTrue(free)


class CountTest(FsCase):
    """The disk bookkeeping of one download, counted in system calls, not timed."""

    def test_system_calls_of_a_new_and_a_resumed_download(self):
        calls = ("open", "stat", "lstat", "fstat", "utime", "replace", "unlink", "close")
        new = {"open": 1, "fstat": 1, "utime": 1, "replace": 1, "close": 1}
        # A resumed part is opened again after the failed exclusive open, and sized by fstat.
        resumed = {"open": 2, "fstat": 2, "utime": 1, "replace": 1, "close": 1}
        for name, expected in (("new.txt", new), ("resumed.txt", resumed)):
            if name == "resumed.txt":
                (self.folder / "resumed.txt.part").write_bytes(b"half")
            counts = dict.fromkeys(calls, 0)
            with mock.patch.multiple(_files.os, **{call: self.spy(call, counts) for call in calls}):
                with self.part(self.folder / name) as held:
                    held.write(b"x" * 1000)
                    held.mtime = 1_000_000.0
            self.assertEqual({call: n for call, n in counts.items() if n}, expected, name)

    def test_the_ledger_checks_a_saved_file_on_disk_with_one_stat(self):
        # P8: one `stat` tells a file with bytes on disk, where `is_file` and `stat` took two.
        from flnd.engine import crawl

        ledger = object.__new__(crawl.Ledger)
        ledger.folder, ledger._queued, ledger._db, ledger._again = self.folder, {}, None, False
        self.dest.write_bytes(b"x")
        counts = {"stat": 0}
        with mock.patch.object(os, "stat", self.spy("stat", counts)):
            self.assertEqual(ledger._held(self.dest, "exm:a"), "a.txt")
            self.assertIsNone(ledger._held(self.folder / "b.txt", "exm:b"))
        self.assertEqual(counts["stat"], 2)

    def spy(self, name, counts):
        real = getattr(os, name)

        def spy(*args, **kwargs):
            counts[name] += 1
            return real(*args, **kwargs)

        return spy


class WindowsReplaceTest(FsCase):
    def test_windows_retries_a_permission_error_for_about_a_second(self):
        for locked, tries, slept in ((3, 4, [0.05] * 3), (100, 20, None)):
            calls = []

            def replace(source, target, locked=locked, calls=calls):
                calls.append(target)
                if len(calls) <= locked:
                    raise PermissionError(13, "locked", str(target))
                os.rename(source, target)

            (self.folder / "x").write_bytes(b"new")
            with self.subTest(locked=locked), mock.patch.object(_files.os, "replace", replace), \
                    mock.patch.object(_files, "WINDOWS", True), _time.use(_time.Fake()) as fake:
                if slept is None:
                    with self.assertRaises(PermissionError):
                        _files._replace(self.folder / "x", self.dest)
                else:
                    _files._replace(self.folder / "x", self.dest)
                    self.assertEqual((self.dest.read_bytes(), fake.slept), (b"new", slept))
                self.assertEqual(len(calls), tries)


class WalkSweepTest(FsCase):
    def test_walk_yields_files_recursively_and_skips_parts(self):
        (self.folder / "sub" / "deep").mkdir(parents=True)
        for name in ("a.jpg", "sub/b.MP4", "sub/deep/c.txt", "x.jpg.part", "x.jpg.part.json",
                     ".old.1234abcd.part", ".hidden.jpg"):
            (self.folder / name).write_text("x")
        entries = list(_files.walk(self.folder))
        self.assertEqual(sorted(os.path.relpath(e.path, self.folder) for e in entries),
                         [".hidden.jpg", "a.jpg", os.path.join("sub", "b.MP4"),
                          os.path.join("sub", "deep", "c.txt")])
        self.assertTrue(all(isinstance(e, os.DirEntry) for e in entries))

    def test_sweep_keeps_parts_a_week_and_clears_orphans_after_an_hour(self):
        now = 1_000_000.0
        ages = {
            "new.jpg.part": 4000, "new.jpg.part.json": 4000,        # resumable: kept
            "old.jpg.part": 800_000, "old.jpg.part.json": 800_000,  # a week old: removed
            "lone.jpg.part.json": 4000,                              # orphan sidecar: removed
            "young.jpg.part.json": 100,                              # orphan, too new: kept
            "real.mp4": 800_000,                                     # media: kept
        }
        for name, seconds in ages.items():
            (self.folder / name).write_text("x")
            os.utime(self.folder / name, (now - seconds, now - seconds))
        fake = _time.Fake()
        fake.advance(now - fake.wall)
        with _time.use(fake):
            self.assertEqual(_files.sweep(self.folder), 3)
        self.assertEqual(self.names(), ["new.jpg.part", "new.jpg.part.json", "real.mp4",
                                        "young.jpg.part.json"])

    def test_sweep_keeps_a_six_day_part_and_a_young_orphan_sidecar(self):
        now = 1_000_000.0
        ages = {"new.jpg.part": 518_400, "new.jpg.part.json": 518_400,  # six days: kept
                "young.jpg.part.json": 3_000}                          # orphan, 50 min: kept
        for name, seconds in ages.items():
            (self.folder / name).write_text("x")
            os.utime(self.folder / name, (now - seconds, now - seconds))
        fake = _time.Fake()
        fake.advance(now - fake.wall)
        with _time.use(fake):
            self.assertEqual(_files.sweep(self.folder), 0)
        self.assertEqual(self.names(), sorted(ages))

    def test_sweep_of_a_missing_folder_removes_nothing(self):
        self.assertEqual(_files.sweep(self.folder / "nope"), 0)


if __name__ == "__main__":
    unittest.main()
