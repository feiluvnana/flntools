"""flnd sync: saved target settings, repair, fetch, compress and the summary, on a temp .flnd with test sources."""

import unittest
from pathlib import Path
from unittest import mock

import httpx
from media_samples import needs_pillow, sample
from test_engine import CDN, EngineCase, history, make_source, noise

from flnd import sync
from flnd.engine import Engine
from flnd.compress import compress_file
from flnd.session import Options, video_format
from flnd.target import Target
from shared import kv


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

    def download(self, options=Options(), source=None):
        engine = self.engine(source)
        targets = engine.resolve(["aaa:a"])
        sync.record(self.home, targets, options)
        engine.run(targets, options)

    def sync(self, texts=(), steps=sync.STEPS, source=None, **overrides):
        engine = self.engine(source)
        chosen = sync.plan(engine, engine.resolve(list(texts)), overrides)
        if steps != ("info",):
            for target, options in chosen:
                sync.record(self.home, [target], options)
        return sync.run(engine, chosen, steps)

    def folder(self):
        return self.target_dir("aaa", "a")

    def names(self):
        return sorted(path.name for path in self.folder().rglob("*") if path.is_file())


class PlanTest(SyncCase):
    def test_every_recorded_target_and_unrecorded_folders_with_the_defaults(self):
        self.download(Options(categories=("posts",), compress_image="small"))
        (self.home / "downloads" / "aaa" / "older").mkdir(parents=True)
        (self.home / "downloads" / "aaa" / "older" / "x.jpg").write_bytes(b"x")
        chosen = dict(sync.plan(self.engine(), [], {}))
        self.assertEqual(chosen[Target("aaa", "user", "a")], Options(categories=("posts",), compress_image="small"))
        self.assertEqual(chosen[Target("aaa", "user", "older")], Options())
        # A folder of a source flnd doesn't have, or a zip beside the folders, isn't a target.
        (self.home / "downloads" / "zzz" / "b").mkdir(parents=True)
        (self.home / "downloads" / "aaa" / "a.zip").write_bytes(b"")
        self.assertEqual(len(sync.plan(self.engine(), [], {})), 2)
        self.sync(steps=("info",))
        self.sync(steps=("repair",))
        with kv.open(self.home / "targets.db", table="targets") as targets:
            self.assertEqual(sorted(targets), ["aaa:a", "aaa:older"])
            self.assertEqual(targets["aaa:a"]["categories"], ["posts"])

    def test_planning_creates_no_file(self):
        self.assertEqual(sync.plan(self.engine(), [], {}), [])
        self.assertFalse((self.home / "targets.db").exists())

    def test_options_given_override_the_recorded_ones_and_are_saved(self):
        self.download(Options(compress_image="small"))
        self.sync(steps=("repair",), compress_video="small", format_video="h265")
        [(target, options)] = sync.plan(self.engine(), [], {})
        self.assertEqual((options.compress_image, options.compress_video, options.format_video),
                         ("small", "small", "h265"))
        self.sync(steps=("repair",), compress_video=None, format_video=None)
        [(target, options)] = sync.plan(self.engine(), [Target("aaa", "user", "a")], {})
        self.assertEqual((options.compress_image, options.compress_video), ("small", None))

    def test_since_and_until_are_never_recorded(self):
        from datetime import date

        sync.record(self.home, [Target("aaa", "user", "a")], Options(since=date(2026, 1, 1), unpack=True))
        with kv.open(self.home / "targets.db", table="targets") as targets:
            row = targets["aaa:a"]
        self.assertNotIn("since", row)
        self.assertEqual((row["kind"], row["unpack"]), ("user", True))


class RepairTest(SyncCase):
    def test_wrong_extensions_broken_and_missing_files(self):
        self.download()
        folder = self.folder()
        # Before Phase 5 a WebP could be saved as .jpg; a cut-off JPEG is broken; one file is gone.
        (folder / "a.jpg").write_bytes(noise("WEBP"))
        whole = noise("JPEG", (400, 300))
        (folder / "b.jpg").write_bytes(whole[:300])
        (folder / "c.jpg").unlink()
        (folder / ".c.jpg.1234abcd.part").write_bytes(b"left over")
        self.site.files["b.jpg"] = whole
        self.site.serve()
        [line] = self.sync()
        self.assertEqual(self.names(), ["a.webp", "b.jpg", "c.jpg"])
        self.assertEqual((folder / "b.jpg").read_bytes(), whole)
        self.assertTrue(history(self.home)["aaa:a.jpg"]["path"].endswith("a.webp"))
        self.assertEqual((line.repaired, line.missing), (3, 0))

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
        self.sync(steps=("repair",), source=make_source("aaa", run))
        self.assertEqual([path for path in left if path.exists()], [])
        self.assertEqual(self.names(), ["a.jpg", "info.md"])

    def test_a_name_already_taken_is_reported_and_left(self):
        self.download()
        (self.folder() / "a.jpg").write_bytes(noise("WEBP"))
        (self.folder() / "a.webp").write_bytes(b"mine")
        self.sync(steps=("repair",))
        self.assertIn("a.jpg", self.names())
        self.assertTrue(any("a.jpg is a .webp, but a.webp is taken" in line for line in self.lines()))

    def test_a_file_the_source_no_longer_lists_is_missing_and_named(self):
        self.download()
        (self.folder() / "c.jpg").write_bytes(b"\xff\xd8\xff broken")
        gone = Site(self, **{"a.jpg": b"", "b.jpg": b""})
        [line] = self.sync(source=gone.source())
        self.assertEqual((line.repaired, line.missing), (0, 1))
        self.assertTrue(any("c.jpg was broken and couldn't be downloaded again" in line for line in self.lines()))
        self.assertNotIn("c.jpg", self.names())

    def test_only_repair_and_compress_make_no_request(self):
        self.download()
        (self.folder() / "c.jpg").unlink()
        self.requests.clear()
        [line] = self.sync(steps=("repair", "compress"))
        self.assertEqual(self.requests, [])
        self.assertEqual(line.missing, 1)


class CompressTest(SyncCase):
    def test_existing_files_are_compressed_once(self):
        self.site.files = {"big.jpg": sample("big.jpg").read_bytes(), "s.png": sample("small.png").read_bytes()}
        self.site.serve()
        self.download()
        mine = noise("JPEG")
        (self.folder() / "mine.jpg").write_bytes(mine)
        [line] = self.sync(steps=("compress", "info"), compress_image="small")
        rows = history(self.home)
        self.assertEqual(rows["aaa:big.jpg"]["compressed"], "small")
        self.assertEqual(rows["aaa:s.png"]["compressed"], "none")
        self.assertEqual(line.saved, rows["aaa:big.jpg"]["original_size"] - rows["aaa:big.jpg"]["size"])
        with mock.patch("shared.media.compress", side_effect=AssertionError("compressed again")):
            self.sync(steps=("compress",), compress_image="high")
        # A file put there by hand isn't in the history, and is left alone.
        self.assertEqual((self.folder() / "mine.jpg").read_bytes(), mine)

    def test_without_a_compress_setting_nothing_is_compressed(self):
        self.download()
        with mock.patch("shared.media.compress", side_effect=AssertionError("compressed")):
            self.sync(steps=("compress",))
        self.assertNotIn("compressed", history(self.home)["aaa:a.jpg"])


class InfoTest(SyncCase):
    def test_only_info_changes_nothing_and_counts_match_the_history(self):
        self.site.files["v.mp4"] = b"\0\0\0\x18ftypisom" + bytes(64)
        self.site.serve()
        self.download()
        (self.folder() / "a.jpg").write_bytes(noise("WEBP"))
        (self.folder() / "c.jpg").unlink()
        before = {path.name: path.read_bytes() for path in self.folder().iterdir()}
        self.requests.clear()
        [line] = self.sync(steps=("info",))
        self.assertEqual(self.requests, [])
        self.assertEqual({path.name: path.read_bytes() for path in self.folder().iterdir()}, before)
        size = sum(len(body) for body in before.values())
        self.assertEqual((line.target, line.files, line.photos, line.videos, line.size, line.saved, line.repaired,
                          line.missing), ("aaa:a", 3, 2, 1, size, 0, 0, 1))
        table = [line for line in self.lines() if line.startswith("• target") or line.startswith("• aaa:a")
                 or line.startswith("• total")]
        self.assertEqual(len(table), 3)
        self.assertEqual(table[0].split(), ["•", "target", "files", "photos", "videos", "size", "saved", "repaired",
                                            "missing"])
        self.assertEqual(table[1].split()[1:5], ["aaa:a", "3", "2", "1"])


class VideoFormatTest(unittest.TestCase):
    def test_the_three_forms(self):
        self.assertEqual(video_format("mp4"), ("mp4", None))
        self.assertEqual(video_format("h265"), (None, "h265"))
        self.assertEqual(video_format("mkv:h265"), ("mkv", "h265"))
        self.assertEqual(video_format(None), (None, None))
        for bad in ("webm:h265", "avi:vp9", "flv", "mp4:av1"):
            with self.assertRaises(ValueError, msg=bad):
                video_format(bad)

    def test_a_codec_alone_keeps_the_container_that_can_hold_it(self):
        calls = []

        def fake(path, **settings):
            calls.append((Path(path).name, settings["format"], settings["codec"]))

        with mock.patch("shared.media.compress", fake), mock.patch.object(Path, "stat", return_value=mock.Mock(st_size=1)):
            for name, value in (("a.mp4", "h265"), ("b.webm", "h265"), ("c.mp4", "vp9"), ("d.webm", "mkv:h265")):
                compress_file(Path(name), "video", Options(compress_video="small", format_video=value), set())
        self.assertEqual(calls, [("a.mp4", None, "h265"), ("b.webm", "mp4", "h265"), ("c.mp4", "webm", "vp9"),
                                 ("d.webm", "mkv", "h265")])


if __name__ == "__main__":
    unittest.main()
