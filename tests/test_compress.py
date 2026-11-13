"""flnd.common.compress: `--shrink`'s kinds and level, and media's errors kept from ending a run.

`shared.media.open` is replaced by `test_engine.FakeMedia`, so no photo is decoded and no ffmpeg
runs; the screen is the fake one of `EngineCase`. Fix 7: a stalled ffmpeg (`TimeoutError`) or a missing extra
(`ImportError`) used to end a whole `flnd sync`, or fail the file it hit, with a traceback.
"""

import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from media_samples import needs_pillow
from test_engine import EngineCase, FakeMedia, compressing

from flnd.common import compress, files
from flnd.common.compress import KINDS, LEVEL, ONE_AT_A_TIME, Compressor, compress_file


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
        with compressing(side_effect=stalled):
            first = compress_file(self.file("a.mp4"), "video", warned)
            second = compress_file(self.file("b.mp4", b"other"), "video", warned)
        self.assertEqual(first, {"path": str(self.folder / "a.mp4"), "size": 5, "compressed": "none",
                                 "original_size": 5})
        self.assertEqual(second["compressed"], "none")
        self.assertEqual(self.warnings(), ["! a.mp4: not compressed: ffmpeg made no progress for 60 s"])
        self.assertEqual((self.folder / "a.mp4").read_bytes(), b"video")

    def test_a_missing_extra_leaves_the_file_for_a_later_run_and_warns_once(self):
        warned = set()
        missing = ImportError("shared.media needs Pillow for photos: pip install 'flntools[media]'")
        with compressing(side_effect=missing):
            results = [compress_file(self.file(f"{n}.jpg"), "image", warned)
                       for n in range(3)]
        # Not recorded as "none": once the extra is installed, the next flnd sync compresses them.
        self.assertEqual(results, [None, None, None])
        self.assertEqual(len(self.warnings()), 1)
        self.assertIn("0.jpg: not compressed: shared.media needs Pillow", self.warnings()[0])

    def test_each_kind_of_error_warns_once(self):
        warned = set()
        errors = [TimeoutError("stalled"), ValueError("corrupt"), TimeoutError("stalled again"), RuntimeError("no x264")]
        with compressing(side_effect=errors):
            for number in range(len(errors)):
                compress_file(self.file(f"{number}.mp4"), "video", warned)
        self.assertEqual(self.warnings(), ["! 0.mp4: not compressed: stalled", "! 1.mp4: not compressed: corrupt",
                                           "! 3.mp4: not compressed: no x264"])
        self.assertEqual(warned, {TimeoutError, ValueError, RuntimeError})

    def test_a_file_that_cant_be_opened_records_none_and_the_run_goes_on(self):
        """Review: an OSError from compressing used to fail a saved download or end a whole sync."""
        warned = set()
        errors = [PermissionError(13, "Permission denied"), None]
        with compressing(side_effect=errors):
            first = compress_file(self.file("a.jpg"), "image", warned)
            second = compress_file(self.file("b.jpg"), "image", warned)
        self.assertEqual(first, {"path": str(self.folder / "a.jpg"), "size": 5, "compressed": "none",
                                 "original_size": 5})
        self.assertEqual(second["compressed"], "none")
        self.assertEqual(self.warnings(), ["! a.jpg: not compressed: [Errno 13] Permission denied"])
        self.assertEqual((self.folder / "a.jpg").read_bytes(), b"video")

    def test_a_file_gone_before_compression_records_none_and_the_run_goes_on(self):
        """Review: `path.stat()` ran outside the caught errors, so a vanished file ended a whole sync."""
        warned = set()
        with compressing(return_value=None) as compress:
            gone = compress_file(self.folder / "gone.jpg", "image", warned)
            after = compress_file(self.file("b.jpg"), "image", warned)
        # Only the field it knows: the caller's history row keeps its own path and size.
        self.assertEqual(gone, {"compressed": "none"})
        self.assertEqual(after["compressed"], "none")
        compress.assert_called_once()
        self.assertEqual(len(self.warnings()), 1)
        self.assertIn("gone.jpg: not compressed:", self.warnings()[0])

    def test_other_errors_are_not_caught(self):
        # Ctrl-C, and a mistake in flnd itself, still end the run.
        for error in (KeyboardInterrupt(), TypeError("bad argument")):
            with compressing(side_effect=error), self.assertRaises(type(error)):
                compress_file(self.file("a.mp4"), "video", set())


@needs_pillow
class RealPhotoTest(EngineCase):
    """Through the real `media.open`, not `FakeMedia`: compressing must never swap a file for one that isn't smaller."""

    def test_an_already_small_photo_stays_byte_for_byte_and_is_recorded_as_not_compressed(self):
        import io

        from PIL import Image

        buffer = io.BytesIO()
        Image.new("RGB", (8, 8), (200, 30, 30)).save(buffer, "JPEG", quality=5, optimize=True)
        folder = self.home / "files"
        folder.mkdir(parents=True)
        path = folder / "tiny.jpg"
        path.write_bytes(buffer.getvalue())
        before = path.read_bytes()
        fields = compress_file(path, "image", set())
        self.assertEqual(path.read_bytes(), before)
        self.assertEqual(fields, {"path": str(path), "size": len(before), "compressed": "none",
                                  "original_size": len(before)})
        self.assertEqual(sorted(entry.name for entry in folder.iterdir()), ["tiny.jpg"])


class TableTest(unittest.TestCase):
    def test_photos_videos_and_audio_are_the_kinds(self):
        self.assertEqual(KINDS, (files.IMAGE, files.VIDEO, files.AUDIO))

    def test_only_videos_take_turns(self):
        # One x264 encode already uses every core.
        self.assertEqual(ONE_AT_A_TIME, frozenset({files.VIDEO}))

    def test_the_level_is_a_media_level(self):
        from shared import _media

        # cli.md's table: mid is Phase 5's balanced, the level `--shrink` uses.
        self.assertEqual(LEVEL, "mid")
        self.assertEqual(_media.PRESETS["mid"][:3], (80, 1080, 2304))

    def test_media_gets_the_level_and_no_format(self):
        calls = []

        def compress(path, **settings):
            calls.append(settings)
            return None

        with compressing(compress), mock.patch.object(Path, "stat", return_value=mock.Mock(st_size=1)):
            for name, kind in (("a.jpg", "image"), ("a.mp4", "video"), ("a.m4a", "audio")):
                compress_file(Path(name), kind, set())
        self.assertEqual([settings["level"] for settings in calls], ["mid", "mid", "mid"])
        self.assertFalse(any("format" in settings for settings in calls))


class OwnCodecTest(EngineCase):
    def test_a_video_is_compressed_in_its_own_codec(self):
        """Without a codec media writes H.264 for any video, so an HEVC file would change codec."""
        calls = []

        def compress(path, **settings):
            calls.append((Path(path).name, settings["codec"]))
            return None

        with compressing(compress), mock.patch.object(Path, "stat", return_value=mock.Mock(st_size=1)):
            for codec in ("h264", "hevc", "vp9", "av1"):
                with mock.patch.object(FakeMedia, "video_codec", codec):
                    compress_file(Path(f"{codec}.mp4"), "video", set())
        self.assertEqual(calls, [("h264.mp4", "h264"), ("hevc.mp4", "h265"), ("vp9.mp4", "vp9"), ("av1.mp4", None)])


class CompressorTest(EngineCase):
    def test_a_photo_this_machine_cant_compress_never_stops_videos(self):
        """Fix 8: one photo's RuntimeError turned off video compression; now each kind is on its own."""
        folder = self.home / "files"
        folder.mkdir(parents=True)
        for name in ("a.jpg", "b.jpg", "c.mp4"):
            (folder / name).write_bytes(b"data")
        calls = []

        def compress(path, **settings):
            calls.append(Path(path).name)
            if Path(path).suffix == ".jpg":
                raise RuntimeError("this Pillow has no AVIF encoder")
            return None

        compressor = Compressor()
        with compressing(compress):
            results = [compressor.compress(folder / name, files.kind_of(name)) for name in ("a.jpg", "b.jpg", "c.mp4")]
        self.assertEqual(calls, ["a.jpg", "c.mp4"])
        self.assertEqual((results[0], results[1], results[2]["compressed"]), (None, None, "none"))
        self.assertEqual(compressor.off, {"image"})
        self.assertIn("! image compression stops for this run", self.lines())



class ApplyTest(EngineCase):
    def test_photos_shrink_at_once_videos_one_at_a_time_and_rows_are_stamped(self):
        folder = self.home / "files"
        folder.mkdir(parents=True)
        names = [f"{number}.jpg" for number in range(6)] + ["a.mp4", "b.mp4"]
        rows = []
        for name in names:
            (folder / name).write_bytes(b"data")
            rows.append((f"exm:{name}", {"path": str(folder / name), "size": 4}))
        lock = threading.Lock()
        running, peaks = {"image": 0, "video": 0}, {"image": 0, "video": 0}

        def compress_file(path, kind, warned):
            with lock:
                running[kind] += 1
                peaks[kind] = max(peaks[kind], running[kind])
            time.sleep(0.02)
            path.write_bytes(b"da")
            with lock:
                running[kind] -= 1
            return {"path": str(path), "size": 2, "compressed": LEVEL, "original_size": 4}

        history = dict(rows)
        with mock.patch.object(compress, "compress_file", compress_file):
            done = compress.apply(history, rows, Compressor(), label="exm:example_user")
        self.assertEqual(done, 8)
        self.assertGreater(peaks["image"], 1)
        self.assertEqual(peaks["video"], 1)
        for name in names:
            self.assertTrue(files.unchanged(folder / name, history[f"exm:{name}"]), name)


if __name__ == "__main__":
    unittest.main()
