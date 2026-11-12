"""flnd.compress: one file compressed for a run, and media's errors kept from ending it.

`shared.media.compress` is replaced, so no photo is decoded and no ffmpeg runs; the screen is
the fake one of `EngineCase`. Fix 7: a stalled ffmpeg (`TimeoutError`) or a missing extra
(`ImportError`) used to end a whole `flnd sync`, or fail the file it hit, with a traceback.
"""

import unittest
from pathlib import Path
from unittest import mock

import httpx
from media_samples import needs_pillow
from test_engine import CDN, EngineCase, history, make_source, noise
from test_sync import SyncCase

from flnd.compress import compress_file
from flnd.session import Options

VIDEO_BYTES = b"\0\0\0\x18ftypisom" + bytes(64)


class CompressFileTest(EngineCase):
    def setUp(self):
        super().setUp()
        self.folder = self.home / "files"
        self.folder.mkdir(parents=True)

    def file(self, name, body=b"video"):
        path = self.folder / name
        path.write_bytes(body)
        return path

    def warnings(self):
        return [line for line in self.lines() if line.startswith("!")]

    def test_a_stalled_ffmpeg_records_the_file_as_not_compressed_and_warns_once(self):
        warned = set()
        stalled = TimeoutError("ffmpeg made no progress for 60 s")
        with mock.patch("shared.media.compress", side_effect=stalled):
            first = compress_file(self.file("a.mp4"), "video", Options(compress_video="small"), warned)
            second = compress_file(self.file("b.mp4", b"other"), "video", Options(compress_video="small"), warned)
        self.assertEqual(first, {"path": str(self.folder / "a.mp4"), "size": 5, "compressed": "none",
                                 "original_size": 5})
        self.assertEqual(second["compressed"], "none")
        self.assertEqual(self.warnings(), ["! a.mp4: not compressed: ffmpeg made no progress for 60 s"])
        self.assertEqual((self.folder / "a.mp4").read_bytes(), b"video")

    def test_a_missing_extra_leaves_the_file_for_a_later_run_and_warns_once(self):
        warned = set()
        missing = ImportError("shared.media needs Pillow for photos: pip install 'flntools[media]'")
        with mock.patch("shared.media.compress", side_effect=missing):
            results = [compress_file(self.file(f"{n}.jpg"), "photo", Options(compress_image="small"), warned)
                       for n in range(3)]
        # Not recorded as "none": once the extra is installed, the next flnd sync compresses them.
        self.assertEqual(results, [None, None, None])
        self.assertEqual(len(self.warnings()), 1)
        self.assertIn("0.jpg: not compressed: shared.media needs Pillow", self.warnings()[0])

    def test_each_kind_of_error_warns_once(self):
        warned = set()
        errors = [TimeoutError("stalled"), ValueError("corrupt"), TimeoutError("stalled again"), RuntimeError("no x264")]
        with mock.patch("shared.media.compress", side_effect=errors):
            for number in range(len(errors)):
                compress_file(self.file(f"{number}.mp4"), "video", Options(compress_video="small"), warned)
        self.assertEqual(self.warnings(), ["! 0.mp4: not compressed: stalled", "! 1.mp4: not compressed: corrupt",
                                           "! 3.mp4: not compressed: no x264"])
        self.assertEqual(warned, {TimeoutError, ValueError, RuntimeError})

    def test_a_file_that_cant_be_opened_records_none_and_the_run_goes_on(self):
        """Review: an OSError from media.compress used to fail a saved download or end a whole sync."""
        warned = set()
        errors = [PermissionError(13, "Permission denied"), None]
        with mock.patch("shared.media.compress", side_effect=errors):
            first = compress_file(self.file("a.jpg"), "photo", Options(compress_image="small"), warned)
            second = compress_file(self.file("b.jpg"), "photo", Options(compress_image="small"), warned)
        self.assertEqual(first, {"path": str(self.folder / "a.jpg"), "size": 5, "compressed": "none",
                                 "original_size": 5})
        self.assertEqual(second["compressed"], "none")
        self.assertEqual(self.warnings(), ["! a.jpg: not compressed: [Errno 13] Permission denied"])
        self.assertEqual((self.folder / "a.jpg").read_bytes(), b"video")

    def test_a_file_gone_before_compression_records_none_and_the_run_goes_on(self):
        """Review: `path.stat()` ran outside the caught errors, so a vanished file ended a whole sync."""
        warned = set()
        with mock.patch("shared.media.compress", return_value=None) as compress:
            gone = compress_file(self.folder / "gone.jpg", "photo", Options(compress_image="small"), warned)
            after = compress_file(self.file("b.jpg"), "photo", Options(compress_image="small"), warned)
        # Only the field it knows: the caller's history row keeps its own path and size.
        self.assertEqual(gone, {"compressed": "none"})
        self.assertEqual(after["compressed"], "none")
        compress.assert_called_once()
        self.assertEqual(len(self.warnings()), 1)
        self.assertIn("gone.jpg: not compressed:", self.warnings()[0])

    def test_other_errors_are_not_caught(self):
        # Ctrl-C, and a mistake in flnd itself, still end the run.
        for error in (KeyboardInterrupt(), TypeError("bad argument")):
            with mock.patch("shared.media.compress", side_effect=error), self.assertRaises(type(error)):
                compress_file(self.file("a.mp4"), "video", Options(compress_video="small"), set())


@needs_pillow
class EngineCompressTest(EngineCase):
    def saver(self, *names):
        def run(session):
            for name in names:
                session.save(f"{CDN}/{name}", name, category="posts")

        return make_source("aaa", run)

    def test_a_stalled_video_encode_doesnt_fail_the_run(self):
        for name in ("1.mp4", "2.mp4"):
            self.routes[f"{CDN}/{name}"] = httpx.Response(200, content=VIDEO_BYTES)
        self.routes[f"{CDN}/a.jpg"] = httpx.Response(200, content=noise("JPEG"))
        with mock.patch("shared.media.compress", side_effect=TimeoutError("ffmpeg made no progress for 60 s")):
            total = self.run_engine(self.saver("1.mp4", "2.mp4", "a.jpg"), targets=["aaa:a"],
                                    options=Options(compress_image="small", compress_video="small"))
        self.assertEqual(total, 3)
        rows = history(self.home)
        self.assertEqual([rows[f"aaa:{name}"]["compressed"] for name in ("1.mp4", "2.mp4", "a.jpg")], ["none"] * 3)
        self.assertIn("✓ aaa:a", self.lines())
        self.assertFalse(any(line.startswith("✗") for line in self.lines()), self.lines())
        self.assertEqual(sum("not compressed" in line for line in self.lines()), 1)

    def test_a_missing_extra_while_compressing_a_photo_keeps_the_download(self):
        self.routes[f"{CDN}/a.jpg"] = httpx.Response(200, content=noise("JPEG"))
        with mock.patch("shared.media.compress", side_effect=ImportError("no Pillow")):
            total = self.run_engine(self.saver("a.jpg"), targets=["aaa:a"], options=Options(compress_image="small"))
        self.assertEqual(total, 1)
        self.assertNotIn("compressed", history(self.home)["aaa:a.jpg"])
        self.assertIn("! a.jpg: not compressed: no Pillow", self.lines())


class SyncCompressTest(SyncCase):
    def test_a_stalled_ffmpeg_doesnt_end_a_sync(self):
        self.download()
        with mock.patch("shared.media.compress", side_effect=TimeoutError("ffmpeg took longer than 30 s to open it")):
            [line] = self.sync(steps=("compress", "info"), compress_image="small")
        self.assertEqual(line.files, 3)
        self.assertEqual(sum("not compressed: ffmpeg took longer" in line for line in self.lines()), 1)
        rows = history(self.home)
        self.assertEqual([rows[f"aaa:{name}"]["compressed"] for name in ("a.jpg", "b.jpg", "c.jpg")], ["none"] * 3)

    def test_a_missing_extra_doesnt_end_a_sync_and_leaves_the_photos_for_the_next(self):
        self.download()
        with mock.patch("shared.media.compress", side_effect=ImportError("no Pillow")):
            [line] = self.sync(steps=("compress", "info"), compress_image="small")
        self.assertEqual(line.files, 3)
        self.assertFalse(any("compressed" in row for row in history(self.home).values()))
        self.assertEqual(sum("not compressed: no Pillow" in line for line in self.lines()), 1)


if __name__ == "__main__":
    unittest.main()
