"""`Engine.sync` and the saved target settings (flnd/settings.py, flnd/repair.py): the plan, the
records, `Engine.download`'s use of them, repair, the fetch and the summary.

Each test runs on a temporary `.flnd` with test sources on `httpx.MockTransport` (see `EngineCase`),
and the fake screen. `shared.media.open` is replaced by `test_engine.FakeMedia` wherever a test only
needs to know what was shrunk. `shrink=False` here is `--shrink off`, as `download` is given it on the
command line; the default, None, keeps the saved setting.
"""

import datetime
import unittest
from dataclasses import replace
from pathlib import Path
from unittest import mock

import httpx
from media_samples import needs_pillow
from test_engine import CDN, EngineCase, FakeMedia, compressing, history, make_source, noise

from flnd import Engine, Range, TargetReport
from flnd.common import files
from flnd.session import Options
from flnd.source import File, Post, Source, Target, target
from shared import kv

class Site:
    """A test source's posts: `files` maps a name to its body; its `posts` category yields every one."""

    def __init__(self, case, **files):
        self.case, self.files = case, files

    def source(self, code="aaa"):
        return make_source(code, files=[File(f"{CDN}/{name}", name=name) for name in self.files])

    def serve(self):
        for name, body in self.files.items():
            self.case.routes[f"{CDN}/{name}"] = httpx.Response(200, content=body)


def recorded(record):
    """An `Engine._run` that only calls `record(chosen)`, and reports each target done, empty."""
    def run(engine, chosen):
        record(chosen)
        return [TargetReport(target, (), 0) for target, _ in chosen]

    return run


@needs_pillow
class SyncCase(EngineCase):
    def setUp(self):
        super().setUp()
        self.site = Site(self, **{"a.jpg": noise("JPEG"), "b.jpg": noise("JPEG"), "c.jpg": noise("JPEG")})
        self.site.serve()

    def engine(self, source=None):
        source = source or self.site.source()
        return Engine(home=self.home, sources={source.code: source})

    def download(self, options=Options(), source=None, texts=("aaa:a",), shrink=None):
        """`Engine.download` with `options`' --from and --time, and `shrink` as given: None keeps the saved one."""
        report = self.engine(source).download(list(texts), categories=options.categories,
                                              time=Range(options.since, options.until), shrink=shrink)
        return report.files

    def sync(self, texts=(), source=None):
        """`Engine.sync`, giving one `TargetReport` per target."""
        return list(self.engine(source).sync(list(texts)).targets)

    def plan(self, targets=()):
        return self.engine()._plan(list(targets))

    def saved(self):
        with kv.open(self.home / files.TARGETS, table="targets") as targets:
            return dict(targets.items())

    def folder(self):
        return self.target_dir("aaa", "a")

    def names(self):
        return sorted(path.name for path in self.folder().rglob("*") if path.is_file())


class SettingsTest(SyncCase):
    def test_shrink_is_saved_per_target_and_off_clears_it(self):
        self.download(shrink=True)
        self.assertIs(self.saved()["aaa:a"]["shrink"], True)
        [(target, options)] = self.plan()
        self.assertEqual(options, Options(shrink=True))
        self.download(shrink=False)
        self.assertIs(self.saved()["aaa:a"]["shrink"], False)
        self.assertEqual(self.plan([target])[0][1], Options())

    def test_from_and_time_are_never_saved(self):
        self.download(Options(categories=("posts",), since=datetime.date(2026, 1, 1), until=datetime.date(2026, 6, 30)))
        row = self.saved()["aaa:a"]
        # `complete` is what was listed to its end, not a setting: here the one category, `posts`.
        self.assertEqual(sorted(row), ["at", "complete", "kind", "shrink"])
        self.assertEqual(sorted(row["complete"]), ["posts"])
        self.assertEqual(self.plan()[0][1], Options())

    def test_a_run_says_which_saved_settings_it_uses(self):
        self.download(shrink=True)
        self.download()
        self.assertIn("• aaa:a: --shrink (saved)", self.lines())
        self.download(shrink=False)
        self.screen.dest.truncate(0)
        self.screen.dest.seek(0)
        self.download()
        self.assertFalse(any("(saved)" in line for line in self.lines()))

    def test_the_saved_settings_reach_the_run_and_targets_with_others_run_together(self):
        runs = []
        with mock.patch.object(Engine, "_run", recorded(runs.append)):
            self.download(texts=("aaa:a",), shrink=True)
            self.download(Options(categories=("posts",)), texts=("aaa:a", "aaa:b"))
        self.assertEqual(runs[0], [(Target("aaa", "user", "a"), Options(shrink=True))])
        posts = Options(categories=("posts",))
        self.assertEqual(runs[1:], [[(Target("aaa", "user", "a"), replace(posts, shrink=True)),
                                     (Target("aaa", "user", "b"), posts)]])

    def test_every_recorded_target_and_unrecorded_folders(self):
        self.download(shrink=True)
        (self.home / files.MEDIA / "aaa" / "older").mkdir(parents=True)
        (self.home / files.MEDIA / "aaa" / "older" / "x.jpg").write_bytes(b"x")
        chosen = dict(self.plan())
        self.assertEqual(chosen[Target("aaa", "user", "older")], Options())
        # A folder of a source flnd doesn't have, or a file beside the folders, isn't a target.
        (self.home / files.MEDIA / "zzz" / "b").mkdir(parents=True)
        (self.home / files.MEDIA / "aaa" / "a.zip").write_bytes(b"")
        self.assertEqual(len(self.plan()), 2)
        self.sync()
        self.assertEqual(sorted(self.saved()), ["aaa:a", "aaa:older"])

    def test_planning_creates_no_file(self):
        self.assertEqual(self.plan(), [])
        self.assertFalse((self.home / files.TARGETS).exists())


class DownloadAppliesTest(SyncCase):
    """A download that changes a target's saved setting applies it to the files already saved, then to new ones."""

    def setUp(self):
        super().setUp()
        self.compressed = []

        def compress(path, **settings):
            # Each file is shrunk at media's level, in its own format: no format is asked for.
            self.assertNotIn("format", settings)
            self.compressed.append((Path(path).name, settings["level"]))
            return None

        self.enterContext(compressing(compress))

    def test_a_changed_setting_compresses_the_files_already_saved_first(self):
        self.download()
        self.assertEqual(self.compressed, [])
        self.site.files["d.jpg"] = noise("JPEG")
        self.site.serve()
        self.download(shrink=True)
        # The three saved before, in the history's order, then the new one as it is downloaded.
        self.assertEqual(sorted(self.compressed[:3]), [(name, "mid") for name in ("a.jpg", "b.jpg", "c.jpg")])
        self.assertEqual(self.compressed[3:], [("d.jpg", "mid")])
        rows = history(self.home)
        self.assertEqual({rows[f"aaa:{name}.jpg"]["compressed"] for name in "abcd"}, {"none"})

    def test_the_same_setting_again_or_off_compresses_nothing_saved(self):
        self.download(shrink=True)
        self.assertEqual(len(self.compressed), 3)
        self.download(shrink=True)
        self.download()
        self.download(shrink=False)
        self.assertEqual(len(self.compressed), 3)

    def test_a_file_already_compressed_with_the_new_setting_is_left(self):
        self.download()
        with kv.open(self.home / files.HISTORY, table="downloads") as rows:
            rows["aaa:a.jpg"] = {**rows["aaa:a.jpg"], "compressed": "mid"}
            # Another level is compressed again.
            rows["aaa:b.jpg"] = {**rows["aaa:b.jpg"], "compressed": "low"}
        self.download(shrink=True)
        self.assertEqual(sorted(name for name, _ in self.compressed), ["b.jpg", "c.jpg"])

    def shrink(self, fail_on=None):
        """Replace `shared.media.open` with a `FakeMedia` whose compress writes a smaller copy in place.

        Args:
            fail_on: the call, counted from 1, that raises KeyboardInterrupt instead, as Ctrl-C would.
        """
        def compress(path, **settings):
            path = Path(path)
            self.compressed.append((path.name, settings["level"]))
            if len(self.compressed) == fail_on:
                raise KeyboardInterrupt
            body = path.read_bytes()
            path.write_bytes(body[:len(body) // 2])
            return path

        return compressing(compress)

    def test_a_compressed_file_updates_its_history_row(self):
        self.download()
        before = history(self.home)["aaa:a.jpg"]["size"]
        with self.shrink():
            self.download(shrink=True)
        row = history(self.home)["aaa:a.jpg"]
        photo = self.folder() / "a.jpg"
        self.assertEqual((Path(row["path"]), row["size"], row["compressed"], row["original_size"]),
                         (photo, photo.stat().st_size, "mid", before))
        self.assertEqual(self.names(), ["a.jpg", "b.jpg", "c.jpg"])

    def test_an_apply_cut_short_keeps_the_old_setting_and_is_finished_next_time(self):
        self.download()
        with self.shrink(fail_on=2), self.assertRaises(KeyboardInterrupt):
            self.download(shrink=True)
        self.assertIs(self.saved()["aaa:a"]["shrink"], False)
        first, cut = self.compressed[0][0], self.compressed[1][0]
        self.compressed.clear()
        with self.shrink():
            self.download(shrink=True)
        rest = sorted({"a.jpg", "b.jpg", "c.jpg"} - {first})
        self.assertIn(cut, rest)
        self.assertEqual(sorted(name for name, _ in self.compressed), rest)
        self.assertIs(self.saved()["aaa:a"]["shrink"], True)
        self.assertEqual(self.names(), ["a.jpg", "b.jpg", "c.jpg"])

    def test_photos_and_videos_are_all_shrunk_at_the_level(self):
        self.site.files["v.mp4"] = b"\0\0\0\x18ftypisom" + bytes(64)
        self.site.serve()
        with mock.patch.object(FakeMedia, "check", lambda self: self):
            self.download()
            self.download(shrink=True)
        self.assertEqual(sorted(self.compressed), [("a.jpg", "mid"), ("b.jpg", "mid"), ("c.jpg", "mid"),
                                                   ("v.mp4", "mid")])


class RepairTest(SyncCase):
    def test_broken_empty_and_missing_files_and_parts(self):
        self.download()
        folder = self.folder()
        whole = noise("JPEG", (400, 300))
        (folder / "a.jpg").write_bytes(b"")
        (folder / "b.jpg").write_bytes(whole[:300])
        (folder / "c.jpg").unlink()
        (folder / ".c.jpg.1234abcd.part").write_bytes(b"left over")
        self.site.files["b.jpg"] = whole
        self.site.serve()
        [line] = self.sync()
        self.assertEqual(self.names(), ["a.jpg", "b.jpg", "c.jpg"])
        self.assertEqual((folder / "b.jpg").read_bytes(), whole)
        self.assertGreater((folder / "a.jpg").stat().st_size, 0)
        self.assertEqual((line.repaired, line.missing), (3, 0))

    def test_a_recent_resumable_part_is_kept_for_the_fetch_and_every_other_leftover_goes(self):
        """B6: sync's fetch resumes a download's part, so repair keeps it while it is recent, with its `.part.json`."""
        import os
        import time

        from flnd import repair
        from flnd.common import files

        folder = self.home / files.MEDIA / "aaa" / "example_user"
        folder.mkdir(parents=True)
        made = {name: folder / name for name in (".a.jpg.11111111.part", ".a.jpg.11111111.part.json",
                                                  ".b.jpg.22222222.part", ".b.jpg.22222222.part.json",
                                                  ".c.jpg.33333333.part", ".d.jpg.44444444.part.json")}
        for path in made.values():
            path.write_bytes(b"x")
        old = time.time() - (files.RESUMABLE_DAYS + 1) * 24 * 3600
        os.utime(made[".b.jpg.22222222.part"], (old, old))
        repair.repair({}, folder, [], repair.Tally("aaa:example_user"))
        self.assertEqual(sorted(path.name for path in folder.iterdir()),
                         [".a.jpg.11111111.part", ".a.jpg.11111111.part.json"])

    def test_audio_is_only_sniffed_so_a_mostly_playable_file_is_kept(self):
        from shared import media

        mp3 = b"ID3\x04\0\0\0\0\0\0" + bytes(64)
        self.site.files.update({"s.mp3": mp3, "t.mp3": mp3, "u.mp3": mp3})
        self.site.serve()
        self.download()
        folder = self.folder()
        (folder / "t.mp3").write_bytes(b"<!DOCTYPE html><html><body>Log in</body></html>")
        (folder / "u.mp3").write_bytes(b"")
        decoded = []

        real_open = media.open

        def check(path, *args, **options):
            # One bad packet in s.mp3, which a decode would find; repair must not decode audio.
            # Only a decoding check opens the file, so an open stands for the decode here.
            decoded.append(Path(path).name)
            if Path(path).suffix == ".mp3":
                raise media.BrokenError(f"{Path(path).name}: a bad packet")
            return real_open(path)

        with mock.patch("shared.media.open", check):
            [line] = self.sync()
        self.assertFalse([name for name in decoded if name.endswith(".mp3")])
        self.assertEqual((folder / "s.mp3").read_bytes(), mp3)
        self.assertEqual(((folder / "t.mp3").read_bytes(), (folder / "u.mp3").read_bytes()), (mp3, mp3))
        self.assertEqual((line.repaired, line.missing), (2, 0))

    def test_a_file_with_the_wrong_extension_is_left_as_it_is(self):
        # Files are named by their content when saved, so repair no longer renames (simplify.md).
        self.download()
        (self.folder() / "a.jpg").write_bytes(noise("WEBP"))
        self.sync()
        self.assertIn("a.jpg", self.names())
        self.assertTrue(history(self.home)["aaa:a.jpg"]["path"].endswith("a.jpg"))

    def test_repair_removes_every_temp_file_flnd_writes(self):
        """Fix 13: each temporary file a download or `write` makes, left by a run killed before its rename, goes."""
        import os

        real_replace, left = os.replace, []

        def killed_after(src, dst):
            # The rename happens, then the part is put back, as a run killed at that point would leave it.
            real_replace(src, dst)
            Path(src).write_bytes(b"left over")
            left.append(Path(src))

        files = [File(f"{CDN}/a.jpg", name="a.jpg"), File.text("info.md", "# Title\n")]

        with mock.patch.object(os, "replace", killed_after):
            self.download(source=make_source("aaa", files=files))
        # The download's resumable part moves into `atomic`'s part, which moves onto a.jpg: two renames.
        self.assertEqual(sorted(path.name.split(".")[1] for path in left), ["a", "a", "info"])
        self.sync(source=make_source("aaa", files=files))
        self.assertEqual([path for path in left if path.exists()], [])
        self.assertEqual(self.names(), ["a.jpg", "info.md"])

    def test_a_file_the_source_no_longer_lists_is_missing_and_named(self):
        self.download()
        (self.folder() / "c.jpg").write_bytes(b"\xff\xd8\xff broken")
        gone = Site(self, **{"a.jpg": b"", "b.jpg": b""})
        [line] = self.sync(source=gone.source())
        self.assertEqual((line.repaired, line.missing), (0, 1))
        self.assertTrue(any("c.jpg was broken and couldn't be downloaded again" in line for line in self.lines()))
        self.assertNotIn("c.jpg", self.names())


class FetchTest(SyncCase):
    def test_new_posts_get_the_saved_settings_and_saved_files_are_never_compressed(self):
        # Sync has no compress step (so fixes #8, a photo's error stopping videos there, is moot):
        # a saved setting reaches only what the fetch downloads.
        compressed = []

        def compress(path, **settings):
            compressed.append(Path(path).name)
            return None

        self.download()
        with kv.open(self.home / files.TARGETS, table="targets") as targets:
            targets["aaa:a"] = {**targets["aaa:a"], "shrink": True}
        self.site.files["d.jpg"] = noise("JPEG")
        self.site.serve()
        with compressing(compress):
            self.sync()
        self.assertEqual(compressed, ["d.jpg"])

    def test_sync_takes_every_category(self):
        runs = []
        self.download(Options(categories=("posts",)), shrink=True)
        record = recorded(lambda chosen: runs.extend(options for _, options in chosen))
        with mock.patch.object(Engine, "_run", record):
            self.sync()
        self.assertEqual(runs, [Options(shrink=True, stop_at_saved=True)])


class HistoryTest(SyncCase):
    def test_the_history_is_read_once_per_run(self):
        """Fix 12: repair, compress and the tally each scanned the whole history, per target."""
        self.download(texts=("aaa:a", "aaa:b", "aaa:c"))
        real_iter, scans = kv.Store.__iter__, []

        def counted(store):
            scans.append(store)
            return real_iter(store)

        with mock.patch.object(kv.Store, "__iter__", counted):
            lines = self.sync()
        history_scans = [store for store in scans if store._table == "downloads"]
        self.assertEqual(len(lines), 3)
        self.assertEqual(len(history_scans), 1)


class SummaryTest(SyncCase):
    def test_one_line_per_target_then_the_totals(self):
        # History keys are per source, so aaa:b, saving the same names, finds them saved and stays empty.
        self.download(texts=("aaa:a", "aaa:b"))
        (self.folder() / "c.jpg").unlink()
        gone = Site(self, **{"a.jpg": b"", "b.jpg": b""})
        self.requests.clear()
        lines = self.sync(source=gone.source())
        self.assertEqual([(str(line.target), line.repaired, line.missing) for line in lines],
                         [("aaa:a", 0, 1), ("aaa:b", 0, 0)])
        summary = [line for line in self.lines() if line.startswith("• aaa:a: 2 files") or line.startswith("• total")]
        self.assertEqual(len(summary), 2, self.lines())
        self.assertIn("; 0 repaired, 1 missing", summary[0])
        self.assertTrue(summary[1].startswith("• total: 2 targets, 2 files, "), summary[1])
        self.assertTrue(summary[1].endswith("; 0 repaired, 1 missing"), summary[1])


class RepairListsToTheEndTest(SyncCase):
    """fixes #1: a sync that finds a lost or broken file lists that target past the saved-run stop."""

    NAMES = [f"p{number:02}.jpg" for number in range(12)]

    def feed(self):
        names = self.NAMES

        class Feed(Source):
            code, name, domains, folder, key = "aaa", "AAA", ("aaa.example",), "", "{path}"

            @target("{user}")
            def user(self, site, user):
                return None

            @user.category(newest_first=True)
            def posts(self, site, context):
                for name in names:
                    yield Post(name, [File(f"{CDN}/{name}", name=name)])

        return Feed

    def setUp(self):
        super().setUp()
        for name in self.NAMES:
            self.routes[f"{CDN}/{name}"] = httpx.Response(200, content=noise("JPEG"))

    def test_a_lost_and_a_broken_file_deep_in_a_complete_feed_are_saved_again(self):
        source = self.feed()
        self.engine(source).download(["aaa:a"])
        self.assertIn("posts", self.saved()["aaa:a"]["complete"])
        (self.folder() / "p10.jpg").unlink()
        # Empty is broken for every kind of file: a download that never got its bytes.
        (self.folder() / "p09.jpg").write_bytes(b"")
        [report] = self.sync(source=source)
        self.assertEqual(report.repaired, 2)
        self.assertEqual(self.names(), self.NAMES)

    def test_a_target_with_nothing_lost_still_stops_at_saved_posts(self):
        source = self.feed()
        self.engine(source).download(["aaa:a"])
        self.requests.clear()
        self.sync(source=source)
        self.assertEqual(self.requests, [])


if __name__ == "__main__":
    unittest.main()
