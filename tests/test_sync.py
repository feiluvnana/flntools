"""flnd sync and the saved target settings: plan, record, download's use of them, repair, fetch and the summary.

Each test runs on a temporary `.flnd` with test sources on `httpx.MockTransport` (see `EngineCase`),
and the fake screen. `shared.media.compress` is replaced wherever a test only needs to know what was
compressed with what.
"""

import datetime
import unittest
from pathlib import Path
from unittest import mock

import httpx
from media_samples import needs_pillow
from test_engine import CDN, EngineCase, history, make_source, noise

from flnd import sync
from flnd.compress import Choice
from flnd.engine import Engine
from flnd.session import Options
from flnd.target import Target
from shared import kv

WEBP_MID = Choice("webp", "mid")


class Site:
    """A test source's posts: `files` maps a name to its body; `run` saves every one."""

    def __init__(self, case, **files):
        self.case, self.files = case, files

    def source(self, code="aaa"):
        def run(session):
            for name in self.files:
                session.save(f"{CDN}/{name}", name, category="posts")

        return make_source(code, run)

    def serve(self):
        for name, body in self.files.items():
            self.case.routes[f"{CDN}/{name}"] = httpx.Response(200, content=body)


@needs_pillow
class SyncCase(EngineCase):
    def setUp(self):
        super().setUp()
        self.site = Site(self, **{"a.jpg": noise("JPEG"), "b.jpg": noise("JPEG"), "c.jpg": noise("JPEG")})
        self.site.serve()

    def engine(self, source=None):
        source = source or self.site.source()
        return Engine(home=self.home, sources={source.code: source})

    def download(self, options=Options(), source=None, texts=("aaa:a",), **given):
        engine = self.engine(source)
        return sync.download(engine, engine.resolve(list(texts)), options, given)

    def sync(self, texts=(), source=None):
        engine = self.engine(source)
        return sync.run(engine, sync.plan(engine, engine.resolve(list(texts))))

    def saved(self):
        with kv.open(self.home / "targets.db", table="targets") as targets:
            return dict(targets.items())

    def folder(self):
        return self.target_dir("aaa", "a")

    def names(self):
        return sorted(path.name for path in self.folder().rglob("*") if path.is_file())


class SettingsTest(SyncCase):
    def test_each_option_is_saved_on_its_own_and_off_clears_it(self):
        self.download(image=WEBP_MID)
        self.assertEqual(self.saved()["aaa:a"]["image"], "webp,mid")
        self.download(video=Choice("h265", "low"))
        row = self.saved()["aaa:a"]
        self.assertEqual((row["kind"], row["image"], row["video"], row["audio"]), ("user", "webp,mid", "h265,low", None))
        [(target, options)] = sync.plan(self.engine(), [])
        self.assertEqual(options, Options(image=WEBP_MID, video=Choice("h265", "low")))
        self.download(image=None)
        self.assertEqual(sync.plan(self.engine(), [target])[0][1], Options(video=Choice("h265", "low")))

    def test_from_and_time_are_never_saved(self):
        self.download(Options(categories=("posts",), since=datetime.date(2026, 1, 1), until=datetime.date(2026, 6, 30)))
        row = self.saved()["aaa:a"]
        self.assertEqual(sorted(row), ["at", "audio", "image", "kind", "video"])
        self.assertEqual(sync.plan(self.engine(), [])[0][1], Options())

    def test_a_run_says_which_saved_settings_it_uses(self):
        self.download(image=WEBP_MID, audio=Choice("opus", "mid"))
        self.screen.out.truncate(0)
        self.screen.out.seek(0)
        self.download(audio=None)
        self.assertIn("• aaa:a: --image webp,mid (saved)", self.lines())
        self.assertFalse(any("--audio" in line for line in self.lines()))
        self.download()
        self.assertEqual(sum("(saved)" in line for line in self.lines()), 2)

    def test_the_saved_settings_reach_the_run_and_targets_with_others_run_apart(self):
        runs = []
        with mock.patch.object(Engine, "run", lambda engine, targets, options: runs.append((targets, options)) or 0):
            self.download(texts=("aaa:a",), image=WEBP_MID)
            self.download(Options(categories=("posts",)), texts=("aaa:a", "aaa:b"))
        self.assertEqual(runs[0], ([Target("aaa", "user", "a")], Options(image=WEBP_MID)))
        self.assertEqual(sorted(runs[1:], key=lambda run: run[0][0].value),
                         [([Target("aaa", "user", "a")], Options(categories=("posts",), image=WEBP_MID)),
                          ([Target("aaa", "user", "b")], Options(categories=("posts",)))])

    def test_phase_5_rows_read_into_the_new_form(self):
        with kv.open(self.home / "targets.db", table="targets") as targets:
            targets["aaa:a"] = {"kind": "user", "categories": ["posts"], "unpack": True, "zip": True,
                                "compress_image": "small", "compress_video": None, "format_image": None,
                                "format_video": None, "at": "2026-11-01T00:00:00+00:00"}
            targets["aaa:b"] = {"kind": "user", "categories": None, "unpack": False, "zip": False,
                                "compress_image": "balanced", "compress_video": "high", "format_image": "webp",
                                "format_video": "mkv:h265", "at": "2026-11-01T00:00:00+00:00"}
        chosen = dict(sync.plan(self.engine(), []))
        self.assertEqual(chosen[Target("aaa", "user", "a")], Options(image=Choice(None, "low")))
        self.assertEqual(chosen[Target("aaa", "user", "b")], Options(image=WEBP_MID, video=Choice("mkv:h265", "high")))
        # A sync records them again in today's form, changing no setting.
        self.sync()
        rows = self.saved()
        self.assertEqual({name: rows["aaa:b"][name] for name in ("image", "video", "audio")},
                         {"image": "webp,mid", "video": "mkv:h265,high", "audio": None})
        self.assertNotIn("unpack", rows["aaa:a"])
        self.assertNotIn("categories", rows["aaa:a"])

    def test_every_recorded_target_and_unrecorded_folders(self):
        self.download(image=WEBP_MID)
        (self.home / "downloads" / "aaa" / "older").mkdir(parents=True)
        (self.home / "downloads" / "aaa" / "older" / "x.jpg").write_bytes(b"x")
        chosen = dict(sync.plan(self.engine(), []))
        self.assertEqual(chosen[Target("aaa", "user", "older")], Options())
        # A folder of a source flnd doesn't have, or a file beside the folders, isn't a target.
        (self.home / "downloads" / "zzz" / "b").mkdir(parents=True)
        (self.home / "downloads" / "aaa" / "a.zip").write_bytes(b"")
        self.assertEqual(len(sync.plan(self.engine(), [])), 2)
        self.sync()
        self.assertEqual(sorted(self.saved()), ["aaa:a", "aaa:older"])

    def test_a_saved_setting_this_flnd_cant_read_is_kept_until_given_again(self):
        self.download()
        with kv.open(self.home / "targets.db", table="targets") as targets:
            targets["aaa:a"] = {**targets["aaa:a"], "image": "webp,huge", "video": "h265,low"}
        self.assertEqual(sync.plan(self.engine(), [])[0][1], Options(video=Choice("h265", "low")))
        self.sync()
        self.download(video=None)
        row = self.saved()["aaa:a"]
        self.assertEqual((row["image"], row["video"]), ("webp,huge", None))
        # Given again, even as off, it is replaced.
        self.download(image=None)
        self.assertIsNone(self.saved()["aaa:a"]["image"])

    def test_planning_creates_no_file(self):
        self.assertEqual(sync.plan(self.engine(), []), [])
        self.assertFalse((self.home / "targets.db").exists())


class DownloadAppliesTest(SyncCase):
    """A download that changes a target's saved setting applies it to the files already saved, then to new ones."""

    def setUp(self):
        super().setUp()
        self.compressed = []

        def compress(path, **settings):
            self.compressed.append((Path(path).name, settings["format"], settings["preset"]))
            return None

        patcher = mock.patch("shared.media.compress", compress)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_a_changed_setting_compresses_the_files_already_saved_first(self):
        self.download()
        self.assertEqual(self.compressed, [])
        self.site.files["d.jpg"] = noise("JPEG")
        self.site.serve()
        self.download(image=WEBP_MID)
        # The three saved before, in the history's order, then the new one as it is downloaded.
        self.assertEqual(sorted(self.compressed[:3]), [(name, "webp", "mid") for name in ("a.jpg", "b.jpg", "c.jpg")])
        self.assertEqual(self.compressed[3:], [("d.jpg", "webp", "mid")])
        rows = history(self.home)
        self.assertEqual({rows[f"aaa:{name}.jpg"]["compressed"] for name in "abcd"}, {"none"})

    def test_the_same_setting_again_or_off_compresses_nothing_saved(self):
        self.download(image=WEBP_MID)
        self.assertEqual(len(self.compressed), 3)
        self.download(image=WEBP_MID)
        self.download()
        self.download(image=None)
        self.assertEqual(len(self.compressed), 3)

    def test_a_file_already_compressed_with_the_new_setting_is_left(self):
        self.download()
        with kv.open(self.home / "history.db", table="downloads") as rows:
            # Phase 5 wrote "balanced"; it reads as "mid", today's name.
            rows["aaa:a.jpg"] = {**rows["aaa:a.jpg"], "compressed": "balanced"}
            # Another level is compressed again, whatever its format.
            rows["aaa:b.jpg"] = {**rows["aaa:b.jpg"], "compressed": "webp,low"}
        self.download(image=Choice(None, "mid"))
        self.assertEqual(sorted(name for name, _, _ in self.compressed), ["b.jpg", "c.jpg"])

    def shrink(self, fail_on=None):
        """Replace `shared.media.compress` with one that writes a smaller copy, in the format asked for.

        Args:
            fail_on: the call, counted from 1, that raises KeyboardInterrupt instead, as Ctrl-C would.
        """
        def compress(path, **settings):
            path = Path(path)
            self.compressed.append((path.name, settings["format"], settings["preset"]))
            if len(self.compressed) == fail_on:
                raise KeyboardInterrupt
            out = path.with_suffix(f".{settings['format']}") if settings["format"] else path
            body = path.read_bytes()
            path.unlink()
            out.write_bytes(body[:len(body) // 2])
            return out

        return mock.patch("shared.media.compress", compress)

    def test_a_compressed_file_updates_its_history_row(self):
        self.download()
        before = history(self.home)["aaa:a.jpg"]["size"]
        with self.shrink():
            self.download(image=WEBP_MID)
        row = history(self.home)["aaa:a.jpg"]
        webp = self.folder() / "a.webp"
        self.assertEqual((Path(row["path"]), row["size"], row["compressed"], row["original_size"]),
                         (webp, webp.stat().st_size, "webp,mid", before))
        self.assertNotIn("a.jpg", self.names())

    def test_an_apply_cut_short_keeps_the_old_setting_and_is_finished_next_time(self):
        self.download()
        with self.shrink(fail_on=2), self.assertRaises(KeyboardInterrupt):
            self.download(image=WEBP_MID)
        self.assertIsNone(self.saved()["aaa:a"]["image"])
        first, cut = self.compressed[0][0], self.compressed[1][0]
        self.compressed.clear()
        with self.shrink():
            self.download(image=WEBP_MID)
        rest = sorted({"a.jpg", "b.jpg", "c.jpg"} - {first})
        self.assertIn(cut, rest)
        self.assertEqual(sorted(name for name, _, _ in self.compressed), rest)
        self.assertEqual(self.saved()["aaa:a"]["image"], "webp,mid")
        self.assertEqual(self.names(), ["a.webp", "b.webp", "c.webp"])

    def test_a_level_alone_keeps_files_at_that_level_in_any_format(self):
        self.download()
        with self.shrink():
            self.download(image=WEBP_MID)
            self.download(image=Choice(None, "mid"))
            # webp,mid to mid: each file keeps its format, and they are already mid.
            self.assertEqual(len(self.compressed), 3)
            self.assertEqual(self.saved()["aaa:a"]["image"], "mid")
            self.download(image=Choice(None, "low"))
            self.assertEqual(self.compressed[3:], [(name, None, "low") for name in ("a.webp", "b.webp", "c.webp")])
            self.download(image=Choice(None, "mid"))
            self.compressed.clear()
            # mid to webp,mid: a file compressed at mid in its own format isn't WebP yet.
            for name in ("a", "b", "c"):
                with kv.open(self.home / "history.db", table="downloads") as rows:
                    rows[f"aaa:{name}.jpg"] = {**rows[f"aaa:{name}.jpg"], "compressed": "mid"}
            self.download(image=WEBP_MID)
        self.assertEqual(sorted(self.compressed), [(name, "webp", "mid") for name in ("a.webp", "b.webp", "c.webp")])

    def test_only_the_changed_kind_is_applied(self):
        self.site.files["v.mp4"] = b"\0\0\0\x18ftypisom" + bytes(64)
        self.site.serve()
        with mock.patch("shared.media.check"):
            self.download()
            self.download(video=Choice("h265", "low"))
        self.assertEqual(self.compressed, [("v.mp4", None, "low")])


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

        def check(path, *args, **options):
            # One bad packet in s.mp3, which a decode would find; repair must not decode audio.
            decoded.append(Path(path).name)
            if Path(path).suffix == ".mp3":
                raise media.BrokenError(f"{Path(path).name}: a bad packet")

        with mock.patch("shared.media.check", check):
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

        def run(session):
            session.save(f"{CDN}/a.jpg", "a.jpg", category="posts")
            session.folder = "post"
            session.write("info.md", "# Title\n")

        with mock.patch.object(os, "replace", killed_after):
            self.download(source=make_source("aaa", run))
        self.assertEqual(sorted(path.name.split(".")[1] for path in left), ["a", "info"])
        self.sync(source=make_source("aaa", run))
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
        with kv.open(self.home / "targets.db", table="targets") as targets:
            targets["aaa:a"] = {**targets["aaa:a"], "image": "webp,mid"}
        self.site.files["d.jpg"] = noise("JPEG")
        self.site.serve()
        with mock.patch("shared.media.compress", compress):
            self.sync()
        self.assertEqual(compressed, ["d.jpg"])

    def test_sync_takes_each_sources_default_categories(self):
        runs = []
        self.download(Options(categories=("files",)), image=WEBP_MID)
        with mock.patch.object(Engine, "run", lambda engine, targets, options: runs.append(options) or 0):
            self.sync()
        self.assertEqual(runs, [Options(image=WEBP_MID)])


class HistoryTest(SyncCase):
    def test_the_history_is_read_once_per_run(self):
        """Fix 12: repair, compress and the tally each scanned the whole history, per target."""
        self.download(texts=("aaa:a", "aaa:b", "aaa:c"))
        real_iter, scans = kv.KV.__iter__, []

        def counted(store):
            scans.append(store)
            return real_iter(store)

        with mock.patch.object(kv.KV, "__iter__", counted):
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
        size = sum(path.stat().st_size for path in self.folder().iterdir())
        self.assertEqual([(line.target, line.files, line.size, line.missing) for line in lines],
                         [("aaa:a", 2, size, 1), ("aaa:b", 0, 0, 0)])
        summary = [line for line in self.lines() if line.startswith("• aaa:a: 2 files") or line.startswith("• total")]
        self.assertEqual(len(summary), 2, self.lines())
        self.assertIn("; 0 repaired, 1 missing", summary[0])
        self.assertTrue(summary[1].startswith("• total: 2 targets, 2 files, "), summary[1])
        self.assertTrue(summary[1].endswith("; 0 repaired, 1 missing"), summary[1])


if __name__ == "__main__":
    unittest.main()
