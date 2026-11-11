import tempfile
import unittest
from pathlib import Path
from unittest import mock

from media_samples import FOLDER, needs_ffmpeg, sample
from shared import _ffmpeg, media


@needs_ffmpeg("libmp3lame")
class ExtractTest(unittest.TestCase):
    def setUp(self):
        self.folder = Path(tempfile.mkdtemp(dir=FOLDER))

    def test_copy_into_m4a(self):
        with mock.patch.object(_ffmpeg, "encode", side_effect=AssertionError("encoded")):
            out = media.audio.extract(sample("av.mkv"), dest=self.folder / "a.m4a")
        info = media.probe(out)
        self.assertEqual((info.kind, info.audio), ("audio", "aac"))

    def test_mp3_default_bitrate(self):
        info = media.probe(media.audio.extract(sample("av.mkv"), dest=self.folder / "a.mp3"))
        self.assertEqual(info.audio, "mp3")
        self.assertAlmostEqual((info.bitrate or 0) / 1000, 192, delta=20)  # the 1 s file carries an ID3 header

    def test_mp3_bitrate(self):
        info = media.probe(media.audio.extract(sample("av.mkv"), dest=self.folder / "b.mp3", bitrate=64_000))
        self.assertAlmostEqual((info.bitrate or 0) / 1000, 64, delta=8)

    def test_no_audio(self):
        with self.assertRaisesRegex(media.BrokenError, "v.mp4 has no audio stream"):
            media.audio.extract(sample("v.mp4"), dest=self.folder / "a.m4a")

    def test_refusals(self):
        with self.assertRaises(ValueError):
            media.audio.extract(sample("av.mkv"), dest=self.folder / "a.mp4")
        with self.assertRaises(ValueError):
            media.audio.extract(sample("av.mkv"), dest=self.folder / "c.mp3", bitrate=1000)
        taken = self.folder / "taken.m4a"
        taken.write_bytes(b"x")
        with self.assertRaises(FileExistsError):
            media.audio.extract(sample("av.mkv"), dest=taken)


if __name__ == "__main__":
    unittest.main()
