"""flnd.common.compress: the table of kinds, the `--image`, `--video` and `--audio` values, and media's errors kept from ending a run.

`shared.media.open` is replaced by `test_engine.FakeMedia`, so no photo is decoded and no ffmpeg
runs; the screen is the fake one of `EngineCase`. Fix 7: a stalled ffmpeg (`TimeoutError`) or a missing extra
(`ImportError`) used to end a whole `flnd sync`, or fail the file it hit, with a traceback.
"""

import unittest
from pathlib import Path
from unittest import mock

from media_samples import needs_pillow
from test_engine import EngineCase, compressing

from flnd.common import files
from flnd.common.compress import (KINDS, LEVELS, Choice, Compressor, compress_file, compressed_with, parse_choice, recorded,
                           video_format)

LOW = Choice(None, "low")


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
            first = compress_file(self.file("a.mp4"), "video", LOW, warned)
            second = compress_file(self.file("b.mp4", b"other"), "video", LOW, warned)
        self.assertEqual(first, {"path": str(self.folder / "a.mp4"), "size": 5, "compressed": "none",
                                 "original_size": 5})
        self.assertEqual(second["compressed"], "none")
        self.assertEqual(self.warnings(), ["! a.mp4: not compressed: ffmpeg made no progress for 60 s"])
        self.assertEqual((self.folder / "a.mp4").read_bytes(), b"video")

    def test_a_missing_extra_leaves_the_file_for_a_later_run_and_warns_once(self):
        warned = set()
        missing = ImportError("shared.media needs Pillow for photos: pip install 'flntools[media]'")
        with compressing(side_effect=missing):
            results = [compress_file(self.file(f"{n}.jpg"), "image", LOW, warned)
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
                compress_file(self.file(f"{number}.mp4"), "video", LOW, warned)
        self.assertEqual(self.warnings(), ["! 0.mp4: not compressed: stalled", "! 1.mp4: not compressed: corrupt",
                                           "! 3.mp4: not compressed: no x264"])
        self.assertEqual(warned, {TimeoutError, ValueError, RuntimeError})

    def test_a_file_that_cant_be_opened_records_none_and_the_run_goes_on(self):
        """Review: an OSError from compressing used to fail a saved download or end a whole sync."""
        warned = set()
        errors = [PermissionError(13, "Permission denied"), None]
        with compressing(side_effect=errors):
            first = compress_file(self.file("a.jpg"), "image", LOW, warned)
            second = compress_file(self.file("b.jpg"), "image", LOW, warned)
        self.assertEqual(first, {"path": str(self.folder / "a.jpg"), "size": 5, "compressed": "none",
                                 "original_size": 5})
        self.assertEqual(second["compressed"], "none")
        self.assertEqual(self.warnings(), ["! a.jpg: not compressed: [Errno 13] Permission denied"])
        self.assertEqual((self.folder / "a.jpg").read_bytes(), b"video")

    def test_a_file_gone_before_compression_records_none_and_the_run_goes_on(self):
        """Review: `path.stat()` ran outside the caught errors, so a vanished file ended a whole sync."""
        warned = set()
        with compressing(return_value=None) as compress:
            gone = compress_file(self.folder / "gone.jpg", "image", LOW, warned)
            after = compress_file(self.file("b.jpg"), "image", LOW, warned)
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
                compress_file(self.file("a.mp4"), "video", LOW, set())


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
        fields = compress_file(path, "image", Choice(None, "low"), set())
        self.assertEqual(path.read_bytes(), before)
        self.assertEqual(fields, {"path": str(path), "size": len(before), "compressed": "none",
                                  "original_size": len(before)})
        self.assertEqual(sorted(entry.name for entry in folder.iterdir()), ["tiny.jpg"])


class TableTest(unittest.TestCase):
    def test_one_table_of_three_kinds(self):
        self.assertEqual({kind: spec.option for kind, spec in KINDS.items()},
                         {"image": "image", "video": "video", "audio": "audio"})
        # Only videos take turns: one x264 encode already uses every core.
        self.assertEqual([kind for kind, spec in KINDS.items() if spec.one_at_a_time], ["video"])
        self.assertEqual(KINDS["image"].formats, ("jpg", "webp", "avif", "png", "gif", "tif", "bmp"))
        self.assertEqual(KINDS["audio"].formats, ("mp3", "m4a", "aac", "opus", "ogg", "flac"))

    def test_every_level_is_a_media_level(self):
        from shared import _media

        self.assertEqual(LEVELS, tuple(_media.PRESETS))
        # cli.md's table: best is new; high, mid and low are Phase 5's high, balanced and small.
        self.assertEqual(_media.PRESETS["best"][:4], (95, None, None, None))
        self.assertEqual(_media.PRESETS["mid"][:3], (80, 1080, 2304))
        self.assertEqual(_media.PRESETS["low"][:4], (70, 720, 1536, 30))

    def test_each_level_reaches_media_as_its_level(self):
        calls = []
        with compressing(lambda path, **settings: calls.append(settings["level"])), \
                mock.patch.object(Path, "stat", return_value=mock.Mock(st_size=1)):
            for level in LEVELS:
                compress_file(Path("a.jpg"), "image", Choice(None, level), set())
        self.assertEqual(calls, list(LEVELS))

    def test_old_preset_names_read_as_new(self):
        self.assertEqual([recorded(name) for name in ("small", "balanced", "high", "webp,mid", "none", None)],
                         ["low", "mid", "high", "webp,mid", "none", None])

    def test_a_level_only_choice_matches_that_level_in_any_format(self):
        mid = Choice(None, "mid")
        self.assertEqual([compressed_with(row, mid) for row in ("webp,mid", "mid", "balanced", "avif,mid",
                                                                "webp,low", "low", "none", None)],
                         [True, True, True, True, False, False, False, False])
        self.assertEqual([compressed_with(row, Choice("webp", "mid")) for row in ("webp,mid", "mid", "avif,mid")],
                         [True, False, False])
        self.assertTrue(compressed_with("mkv:h265,high", Choice(None, "high")))


class ParseChoiceTest(unittest.TestCase):
    def test_a_format_a_level_or_both_in_either_order(self):
        rows = [("image", "webp,best", Choice("webp", "best")), ("image", "best,webp", Choice("webp", "best")),
                ("image", "mid", Choice(None, "mid")), ("image", "AVIF", Choice("avif", "high")),
                ("video", "mkv:h265,low", Choice("mkv:h265", "low")), ("video", "h265", Choice("h265", "high")),
                ("audio", "opus,mid", Choice("opus", "mid")), ("audio", "off", None)]
        for kind, text, expected in rows:
            with self.subTest(text=text):
                self.assertEqual(parse_choice(kind, text), expected)

    def test_it_prints_as_the_command_line_writes_it(self):
        self.assertEqual([str(Choice("webp", "mid")), str(Choice(None, "low"))], ["webp,mid", "low"])
        self.assertEqual(parse_choice("video", str(Choice("mkv:h265", "low"))), Choice("mkv:h265", "low"))

    def test_bad_values_name_what_each_part_takes(self):
        rows = [("image", "webp,huge", "--image webp,huge: the level is best, high, mid or low"),
                ("image", "heic,mid", "--image heic,mid: the format is jpg, webp, avif, png, gif, tif or bmp"),
                ("image", "huge", "--image huge: give a format (jpg, webp, avif, png, gif, tif or bmp), a level "
                                  "(best, high, mid or low), or both"),
                ("video", "webm:h265", "--video webm:h265: webm holds vp9, not h265"),
                ("video", "mp4:av1", "--video mp4:av1: the format is a container"),
                ("image", "mid,low", "--image mid,low: give one level"),
                ("audio", "wav", "--audio wav: give a format (mp3, m4a, aac, opus, ogg or flac)"),
                ("image", "webp,mid,low", "--image takes <format>,<level>"), ("image", ",mid", "--image takes")]
        for kind, text, message in rows:
            with self.subTest(text=text), self.assertRaises(ValueError) as caught:
                parse_choice(kind, text)
            self.assertIn(message, str(caught.exception))


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

        with compressing(fake), mock.patch.object(Path, "stat", return_value=mock.Mock(st_size=1)):
            for name, value in (("a.mp4", "h265"), ("b.webm", "h265"), ("c.mp4", "vp9"), ("d.webm", "mkv:h265")):
                compress_file(Path(name), "video", Choice(value, "low"), set())
        self.assertEqual(calls, [("a.mp4", None, "h265"), ("b.webm", "mp4", "h265"), ("c.mp4", "webm", "vp9"),
                                 ("d.webm", "mkv", "h265")])


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
            results = [compressor.compress(folder / name, files.kind_of(name) or "", LOW)
                       for name in ("a.jpg", "b.jpg", "c.mp4")]
        self.assertEqual(calls, ["a.jpg", "c.mp4"])
        self.assertEqual((results[0], results[1], results[2]["compressed"]), (None, None, "none"))
        self.assertEqual(compressor.off, {"image"})
        self.assertIn("! image compression stops for this run", self.lines())


if __name__ == "__main__":
    unittest.main()
