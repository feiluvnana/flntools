"""`Media.audio`: a file's sound on its own, through ffmpeg, on the generated samples.

`_ffmpeg.encode` is swapped for a failing stand-in where a test pins that a stream is copied, not encoded.
"""

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from media_samples import FOLDER, needs_ffmpeg, sample
from shared import _ffmpeg, media


@needs_ffmpeg("libmp3lame")
class AudioTest(unittest.TestCase):
    def setUp(self):
        self.folder = Path(tempfile.mkdtemp(dir=FOLDER))

    def test_aac_is_copied_into_m4a(self):
        with mock.patch.object(_ffmpeg, "encode", side_effect=AssertionError("encoded")):
            out = media.open(sample("av.mkv")).audio()
            self.assertEqual((out.kind, out.codec), ("audio", "aac"))
            saved = out.save(self.folder / "a.m4a")
        self.assertEqual(media.open(saved).codec, "aac")

    def test_mp3_default_bitrate(self):
        info = media.open(sample("av.mkv")).audio(format="mp3").info
        self.assertEqual(info.audio, "mp3")
        self.assertAlmostEqual((info.bitrate or 0) / 1000, 192, delta=20)  # the 1 s file carries an ID3 header

    def test_mp3_bitrate(self):
        info = media.open(sample("av.mkv")).audio(format="mp3", bitrate=64_000).info
        self.assertAlmostEqual((info.bitrate or 0) / 1000, 64, delta=8)

    def test_no_audio(self):
        with self.assertRaisesRegex(media.BrokenError, "v.mp4 has no audio stream"):
            media.open(sample("v.mp4")).audio()

    def test_refusals(self):
        with self.assertRaises(media.KindError):
            media.open(sample("av.mkv")).audio(format="mp4")
        with self.assertRaises(ValueError):
            media.open(sample("av.mkv")).audio(format="mp3", bitrate=1000)
        taken = self.folder / "taken.m4a"
        taken.write_bytes(b"x")
        with self.assertRaises(FileExistsError):
            media.open(sample("av.mkv")).audio().save(taken)

    def test_a_photo_is_a_kind_error(self):
        with self.assertRaisesRegex(media.KindError, "audio is for videos and audio; srgb.jpg is a photo"):
            media.open(sample("srgb.jpg")).audio()


if __name__ == "__main__":
    unittest.main()
