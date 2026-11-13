"""`--shrink` (plan/phase-7/shrink.md): one flag, saved per target, a balanced compress in each file's own format.

The engine runs on `httpx.MockTransport` through tests/test_engine.py's `EngineCase`, with
`shared.media.open` replaced by a fake that records what it was asked and writes a smaller (or a
bigger) copy. The real-media tests shrink generated samples with Pillow and ffmpeg, and are skipped
without them. The CLI runs through tests/test_commands.py's `RunsCase`, which fakes each run.
"""

import unittest
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from media_samples import fresh, needs_ffmpeg, needs_pillow, run, PICTURE
from test_engine import CDN, EngineCase, make_source

from flnd import Engine
from flnd.common import compress, files
from flnd.source import File
from shared import kv, media


class Shrinking:
    """Stands in for `media.open(path)`: records each compress, and writes a copy `ratio` times the size."""

    def __init__(self, ratio=0.5, codec=None):
        self.ratio, self.codec = ratio, codec
        self.calls: list[tuple[str, str, dict]] = []

    @contextmanager
    def patched(self):
        with mock.patch("shared.media.open", self.open):
            yield self

    def open(self, path):
        shrinking = self
        path = Path(path)

        class Opened:
            size = path.stat().st_size
            info = SimpleNamespace(video=shrinking.codec)

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return None

            def check(self):
                return self

            def compress(self, level, **settings):
                shrinking.calls.append((path.name, level, settings))
                out = path.with_name(f".{path.name}.small")
                out.write_bytes(b"s" * max(int(self.size * shrinking.ratio), 1))

                class Result:
                    size = out.stat().st_size

                    def save(self, dest=None):
                        out.replace(path)
                        return path

                return Result()

        return Opened()


class ShrinkCase(EngineCase):
    def setUp(self):
        super().setUp()
        self.listed = ["a.jpg"]
        self.source = make_source("aaa", files=lambda *_: [File(f"{CDN}/{name}", name=name) for name in self.listed])
        self.routes.update({f"{CDN}/{name}": self.photo for name in ("a.jpg", "b.jpg")})

    def photo(self, request):
        import httpx

        return httpx.Response(200, content=b"\xff\xd8\xff" + b"p" * 2000)

    def engine(self):
        return Engine(home=self.home, sources={"aaa": self.source})

    def row(self):
        with kv.open(self.home / files.TARGETS, table="targets") as targets:
            return targets["aaa:example_user"]

    def history(self):
        with kv.open(self.home / files.HISTORY, table="downloads") as rows:
            return {key: row for key, row in rows.items()}


class EngineShrinkTest(ShrinkCase):
    def test_shrink_saves_on_and_shrinks_the_files_already_saved(self):
        self.engine().download(["aaa:example_user"])
        self.assertIs(self.row()["shrink"], False)
        with Shrinking().patched() as fake:
            report = self.engine().download(["aaa:example_user"], shrink=True)
        self.assertEqual(fake.calls, [("a.jpg", "mid", {"codec": None})])
        self.assertIs(self.row()["shrink"], True)
        self.assertEqual(self.history()["aaa:a.jpg"]["compressed"], "mid")
        self.assertEqual(report.targets[0].settings, "")
        self.assertIn("--shrink aaa:example_user", "\n".join(self.lines()))

    def test_the_saved_setting_is_announced_and_used(self):
        self.engine().download(["aaa:example_user"], shrink=True)
        self.listed.append("b.jpg")
        with Shrinking().patched() as fake:
            report = self.engine().download(["aaa:example_user"])
        self.assertEqual(report.targets[0].settings, "--shrink (saved)")
        self.assertIn("• aaa:example_user: --shrink (saved)", self.lines())
        self.assertEqual([name for name, _, _ in fake.calls], ["b.jpg"])

    def test_shrink_off_saves_off_and_touches_no_file(self):
        self.engine().download(["aaa:example_user"], shrink=True)
        self.listed.append("b.jpg")
        with Shrinking().patched() as fake:
            self.engine().download(["aaa:example_user"], shrink=False)
        self.assertEqual(fake.calls, [])
        self.assertIs(self.row()["shrink"], False)
        self.assertNotIn("compressed", self.history()["aaa:b.jpg"])

    def test_a_later_sync_shrinks_only_the_new_files(self):
        with Shrinking().patched():
            self.engine().download(["aaa:example_user"], shrink=True)
        self.listed.append("b.jpg")
        with Shrinking().patched() as fake:
            self.engine().sync()
        self.assertEqual([name for name, _, _ in fake.calls], ["b.jpg"])
        self.assertEqual({row["compressed"] for row in self.history().values()}, {"mid"})

    def test_turning_it_on_again_skips_the_files_already_shrunk(self):
        with Shrinking().patched():
            self.engine().download(["aaa:example_user"], shrink=True)
        self.engine().download(["aaa:example_user"], shrink=False)
        with Shrinking().patched() as fake:
            self.engine().download(["aaa:example_user"], shrink=True)
        self.assertEqual(fake.calls, [])

    def test_a_file_that_comes_out_bigger_keeps_its_original(self):
        self.engine().download(["aaa:example_user"])
        path = self.target_dir("aaa", "example_user") / "a.jpg"
        before = path.read_bytes()
        with Shrinking(ratio=2).patched():
            self.engine().download(["aaa:example_user"], shrink=True)
        self.assertEqual(path.read_bytes(), before)
        self.assertEqual(self.history()["aaa:a.jpg"]["compressed"], "none")


class RealMediaTest(unittest.TestCase):
    @needs_pillow
    def test_a_jpeg_stays_a_jpeg(self):
        path = fresh("big.jpg")
        fields = compress.compress_file(path, files.IMAGE, set())
        self.assertEqual(Path(fields["path"]), path)
        self.assertEqual(media.sniff(path), ".jpg")

    @needs_ffmpeg("libx264")
    def test_an_mp4_stays_an_mp4(self):
        path = fresh("big.mp4")
        fields = compress.compress_file(path, files.VIDEO, set())
        self.assertEqual(Path(fields["path"]).suffix, ".mp4")
        self.assertEqual(media.open(Path(fields["path"])).info.video, "h264")

    @needs_ffmpeg("libx265")
    def test_an_h265_mp4_stays_h265(self):
        path = fresh("big.mp4").with_name("h265.mp4")
        run("-f", "lavfi", "-i", "testsrc2=size=360x640:rate=30:duration=2", "-c:v", "libx265", "-crf", "5",
            "-tag:v", "hvc1", str(path))
        fields = compress.compress_file(path, files.VIDEO, set())
        self.assertEqual(Path(fields["path"]).suffix, ".mp4")
        self.assertEqual(media.open(Path(fields["path"])).info.video, "hevc")


if __name__ == "__main__":
    unittest.main()
