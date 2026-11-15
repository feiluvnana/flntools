"""`Media.merge` and `Media.raw`: video/timeline operations on the generated samples.

ffmpeg runs for real; `built_commands` wraps its two runners to record each command, so a test can
pin that DASH and like clips are copied, never encoded, and that no image2 reader is ever used.
"""

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from media_samples import FOLDER, Image, needs_ffmpeg, run, sample
from shared import _ffmpeg, media


def built_commands():
    """Patch the two ffmpeg runners to record every command and still run it."""
    commands: list[list[str]] = []
    real_run, real_encode = _ffmpeg.run, _ffmpeg.encode

    def recorded_run(args, **kwargs):
        commands.append(args)
        return real_run(args, **kwargs)

    def recorded_encode(args, **kwargs):
        commands.append(args)
        return real_encode(args, **kwargs)

    patches = [mock.patch.object(_ffmpeg, "run", recorded_run), mock.patch.object(_ffmpeg, "encode", recorded_encode)]
    return commands, patches


def parts(folder: Path) -> list[str]:
    return [path.name for path in folder.iterdir() if path.name.endswith(".part")]


class VideoCase(unittest.TestCase):
    def setUp(self):
        self.folder = Path(tempfile.mkdtemp(dir=FOLDER))


@needs_ffmpeg("libx264", "libvpx-vp9", "libopus")
class MergeTest(VideoCase):
    def photos(self) -> list[Path]:
        sizes = ((64, 48), (30, 60), (100, 20))
        paths = []
        for index, size in enumerate(sizes):
            path = self.folder / f"p{index}.png"
            Image.new("RGB", size, ("red", "green", "blue")[index]).save(path)
            paths.append(path)
        return paths

    def slideshow(self, **options) -> media.Media:
        first, *rest = self.photos()
        return media.open(first).merge(*rest, **options)

    def test_slideshow(self):
        out = self.slideshow()
        info = out.info
        self.assertAlmostEqual(info.duration or 0, 3.0, delta=0.1)
        self.assertEqual((info.width, info.height, info.video, info.format), (64, 48, "h264", "mp4"))

    def test_animations(self):
        for extension in ("gif", "webp"):
            with self.subTest(extension=extension):
                info = self.slideshow(format=extension).info
                self.assertEqual((info.animated, info.frames, info.width), (True, 3, 64))

    def test_photos_with_audio(self):
        info = self.slideshow(audio=sample("a.m4a")).info
        self.assertEqual(info.audio, "aac")
        self.assertAlmostEqual(info.duration or 0, 1.0, delta=0.1)

    def test_raw_returns_pyav_container(self):
        import av.container

        with media.open(sample("v.mp4")) as clip:
            raw = clip.raw
            self.assertIsInstance(raw, av.container.InputContainer)
            self.assertEqual(len(raw.streams.video), 1)
        with self.assertRaises(media.Error):
            _ = clip.raw

    def test_dash_is_a_copy(self):
        commands, patches = built_commands()
        with patches[0], patches[1]:
            for extension in (".mp4", ".mkv"):
                saved = media.open(sample("v.mp4")).merge(audio=sample("a.m4a"), format=extension).save(
                    self.folder / f"d{extension}")
                info = media.open(saved).info
                self.assertEqual((info.video, info.audio), ("mpeg4", "aac"))
                self.assertAlmostEqual(info.duration or 0, 1.0, delta=0.1)
        self.assertTrue(all("copy" in command for command in commands))

    def test_dash_errors(self):
        with self.assertRaisesRegex(media.BrokenError, "no audio stream"):
            media.open(sample("v.mp4")).merge(audio=sample("v.mp4"))
        with self.assertRaises(media.BrokenError):
            media.open(sample("v.mp4")).merge(audio=sample("a.m4a"), format="webm")
        self.assertEqual(list(self.folder.iterdir()), [])
        self.assertEqual(parts(FOLDER), [])
        truncated = self.folder / "cut.m4a"
        truncated.write_bytes(sample("a.m4a").read_bytes()[:3000])
        with self.assertRaises(media.BrokenError):
            media.open(sample("v.mp4")).merge(audio=truncated)

    def test_merge_webm_video_with_aac_audio(self):
        out = media.open(sample("vp9.webm")).merge(audio=sample("a.m4a"))
        info = out.info
        self.assertEqual((info.format, info.video, info.audio), ("mkv", "vp9", "aac"))

    def test_clips_join_by_copy(self):
        commands, patches = built_commands()
        with patches[0], patches[1]:
            info = media.open(sample("av.mp4")).merge(sample("av.mp4")).info
        self.assertEqual(info.video, "mpeg4")
        self.assertAlmostEqual(info.duration or 0, 2.0, delta=0.1)
        concat = [command for command in commands if "concat" in command]
        self.assertEqual(len(concat), 1)
        listing = concat[0][concat[0].index("-i", concat[0].index("concat")) + 1]
        self.assertTrue(listing.startswith("file:") and listing.endswith("list.ffconcat"))
        self.assertTrue(all("image2" not in " ".join(command) for command in commands))

    def test_different_clips_are_encoded(self):
        webm = self.folder / "other.webm"
        run("-f", "lavfi", "-i", "testsrc=size=32x32:rate=25:duration=1", "-c:v", "libvpx-vp9", str(webm))
        info = media.open(sample("av.mp4")).merge(webm).info
        self.assertEqual((info.video, info.width, info.height, info.audio), ("h264", 64, 48, "aac"))
        self.assertAlmostEqual(info.duration or 0, 2.0, delta=0.15)

    def test_refusals(self):
        with self.assertRaises(ValueError):
            self.slideshow(audio=sample("a.m4a"), format="gif")
        with self.assertRaises(media.Error):
            media.open(self.photos()[0]).merge(sample("v.mp4"))
        with self.assertRaises(media.Error):
            media.open(sample("a.m4a")).merge(sample("a.m4a"))
        with self.assertRaises(ValueError):
            self.slideshow(format="jpg")


if __name__ == "__main__":
    unittest.main()
