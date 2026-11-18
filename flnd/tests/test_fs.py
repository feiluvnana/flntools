"""`flnkit.files`: parts, their locks, small writes, walks and stale-part sweeps.

Two runs sharing a home are played by two threads: a POSIX `flock` belongs to an open file
description, so two opens in one process exclude each other as two processes do.
"""

import os
import tempfile
import threading
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
    def test_a_second_download_of_a_held_part_is_busy(self):
        with self.part() as held:
            held.write(b"mine")
            with self.assertRaises(_files.BusyError) as caught, self.part():
                self.fail("the block must not run")
        self.assertIsInstance(caught.exception, DiskError)
        self.assertIn("being saved by another run", str(caught.exception))
        self.assertEqual(self.dest.read_bytes(), b"mine")

    @unittest.skipIf(os.name == "nt", "Windows cannot rename an open file")
    def test_a_part_landed_between_open_and_lock_is_busy_not_rewritten(self):
        first = self.part()
        first.__enter__().write(b"whole")
        real = _files._lock

        def late(fd, *, wait):
            first.__exit__(None, None, None)
            return real(fd, wait=wait)

        with mock.patch.object(_files, "_lock", late), \
                self.assertRaises(_files.BusyError), self.part():
            self.fail("the block must not run")
        self.assertEqual((self.dest.read_bytes(), self.names()), (b"whole", ["a.txt"]))

    def test_a_small_write_waits_for_the_other_writer(self):
        order = []
        with _files.part(self.dest) as held:
            held.write(b"first")
            other = threading.Thread(target=lambda: order.append(_files.write(self.dest, b"2")))
            other.start()
            other.join(0.1)
            self.assertTrue(other.is_alive())
            order.append("first")
        other.join(5)
        self.assertEqual(order, ["first", self.dest])
        self.assertEqual((self.dest.read_bytes(), self.names()), (b"2", ["a.txt"]))

    def test_two_runs_on_one_home_save_a_file_once(self):
        start, outcomes = threading.Barrier(2), []

        def run(data):
            start.wait()
            try:
                with self.part() as held:
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

    def test_system_calls_of_a_new_and_a_resumed_download(self):
        calls = ("open", "stat", "lstat", "fstat", "utime", "replace", "unlink", "close")
        new = {"open": 1, "fstat": 1, "utime": 1, "replace": 1, "close": 1}
        # A resumed part adds one open, one stat and one fstat to check it.
        resumed = {"open": 2, "stat": 1, "fstat": 2, "utime": 1, "replace": 1, "close": 1}
        for name, expected in (("new.txt", new), ("resumed.txt", resumed)):
            if name == "resumed.txt":
                (self.folder / "resumed.txt.part").write_bytes(b"half")
            counts = dict.fromkeys(calls, 0)
            with mock.patch.multiple(_files.os, **{call: self.spy(call, counts) for call in calls}):
                with self.part(self.folder / name) as held:
                    held.write(b"x" * 1000)
                    held.mtime = 1_000_000.0
            self.assertEqual({call: n for call, n in counts.items() if n}, expected, name)

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
                     ".x.jpg.stage", ".x.jpg.stage.lock", ".x.jpg.stage.json",
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
            ".legacy.mp4.stage": 800_000, ".legacy.mp4.stage.lock": 800_000,  # removed
            ".gone.stage.lock": 4000,                                # orphan old lock: removed
            "real.mp4": 800_000,                                     # media: kept
        }
        for name, seconds in ages.items():
            (self.folder / name).write_text("x")
            os.utime(self.folder / name, (now - seconds, now - seconds))
        fake = _time.Fake()
        fake.advance(now - fake.wall)
        with _time.use(fake):
            self.assertEqual(_files.sweep(self.folder), 6)
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

    def test_sweep_skips_a_held_part_and_a_missing_folder(self):
        with self.part(self.folder / "a") as held:
            held.write(b"x")
            os.utime(held.path, (0, 0))
            self.assertEqual(_files.sweep(self.folder), 0)
            self.assertEqual(self.names(), ["a.part"])
        self.assertEqual(self.names(), ["a"])
        self.assertEqual(_files.sweep(self.folder / "nope"), 0)


if __name__ == "__main__":
    unittest.main()
