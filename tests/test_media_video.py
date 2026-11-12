"""`Media.trim`, `Media.frames` and `Media.merge`: anything with a timeline, on the generated samples.

ffmpeg runs for real; `built_commands` wraps its two runners to record each command, so a test can
pin that DASH and like clips are copied, never encoded, and that no image2 reader is ever used.
"""

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from media_samples import FOLDER, Image, fresh, needs_ffmpeg, run, sample
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


@needs_ffmpeg("libx264", "libmp3lame")
class TrimTest(VideoCase):
    def test_exact(self):
        clip = media.open(sample("big.mp4")).trim(start=1.0, end=2.0)
        self.assertAlmostEqual(clip.duration or 0, 1.0, delta=1 / 60 + 0.03)
        self.assertEqual(clip.codec, "h264")

    def test_copy(self):
        info = media.open(sample("big.mp4")).trim(start=1.0, end=2.0, exact=False).info
        self.assertEqual((info.video, info.audio), ("mpeg4", "aac"))
        self.assertGreaterEqual(info.duration or 0, 0.9)

    def test_audio_trims_exactly(self):
        clip = media.open(sample("tone.mp3")).trim(start=0.25, end=0.75)
        self.assertAlmostEqual(clip.duration or 0, 0.5, delta=0.06)

    def test_animation(self):
        out = media.open(sample("anim.gif")).trim(start=0.1, end=0.3).save(self.folder / "d.gif")
        self.assertEqual(media.open(out).info.frames, 1)

    def test_in_place(self):
        path = fresh("big.mp4")
        media.open(path).trim(start=2.0, exact=False).save()
        self.assertLess(media.open(path).duration or 9, 2.0)

    def test_bad_times(self):
        for start, end in ((2.0, 1.0), (1.0, 1.0), (-1.0, None), (5.0, None)):
            with self.subTest(start=start, end=end), self.assertRaises(ValueError):
                media.open(sample("big.mp4")).trim(start=start, end=end)
        with self.assertRaises(media.KindError):
            media.open(sample("srgb.jpg")).trim(end=1.0)


@needs_ffmpeg()
class FramesTest(VideoCase):
    def test_every(self):
        paths = media.open(sample("big.mp4")).frames(every=1.0, dest=self.folder)
        self.assertEqual([path.name for path in paths], ["big_00000.000.jpg", "big_00001.000.jpg",
                                                         "big_00002.000.jpg"])
        with Image.open(paths[0]) as image:
            self.assertEqual(image.size, (360, 640))

    def test_at_and_count(self):
        clip = media.open(sample("big.mp4"))
        at = clip.frames(at=[2.5, 0.5], dest=self.folder / "at")
        self.assertEqual([path.name for path in at], ["big_00000.500.jpg", "big_00002.500.jpg"])
        counted = clip.frames(count=4, dest=self.folder / "count", format="png")
        self.assertEqual(len(counted), 4)
        self.assertEqual(counted, sorted(counted))
        self.assertEqual(counted[0].name, "big_00000.375.png")

    def test_every_frame(self):
        self.assertEqual(len(media.open(sample("v.mp4")).frames(dest=self.folder)), 10)

    def test_rotated_video_is_upright(self):
        paths = media.open(sample("meta.mp4")).frames(count=1, dest=self.folder)
        with Image.open(paths[0]) as image:
            self.assertEqual(image.size, (48, 64))

    def test_animation(self):
        paths = media.open(sample("anim.gif")).frames(dest=self.folder, format="png")
        self.assertEqual([path.name for path in paths], ["anim_00000.000.png", "anim_00000.100.png",
                                                         "anim_00000.300.png", "anim_00000.400.png"])

    def test_a_trimmed_clip_names_its_frames_after_the_file_it_started_from(self):
        paths = media.open(sample("big.mp4")).trim(end=1.0, exact=False).frames(count=1, dest=self.folder)
        self.assertTrue(paths[0].name.startswith("big_"))

    def test_limit(self):
        with self.assertRaisesRegex(ValueError, "limit"):
            media.open(sample("big.mp4")).frames(dest=self.folder / "limited", limit=5)
        self.assertFalse((self.folder / "limited").exists())

    def test_existing_name(self):
        (self.folder / "v_00000.000.jpg").write_bytes(b"x")
        with self.assertRaises(FileExistsError):
            media.open(sample("v.mp4")).frames(every=0.5, dest=self.folder)
        self.assertEqual(len(list(self.folder.iterdir())), 1)

    def test_refusals(self):
        with self.assertRaises(ValueError):
            media.open(sample("v.mp4")).frames(every=1.0, count=2, dest=self.folder)
        with self.assertRaises(ValueError):
            media.open(sample("v.mp4")).frames(format="mp4", dest=self.folder)
        with self.assertRaisesRegex(media.KindError, "frames is for videos and animations; srgb.jpg is a photo"):
            media.open(sample("srgb.jpg")).frames(dest=self.folder)


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
        out = self.slideshow(duration=2.0)
        self.assertAlmostEqual(out.duration or 0, 6.0, delta=0.1)
        self.assertEqual((out.width, out.height, out.codec, out.format), (64, 48, "h264", "mp4"))

    def test_frames_at_fps(self):
        self.assertAlmostEqual(self.slideshow(fps=10).duration or 0, 0.3, delta=0.05)

    def test_animations(self):
        for extension in ("gif", "webp"):
            with self.subTest(extension=extension):
                info = self.slideshow(format=extension).info
                self.assertEqual((info.animated, info.frames, info.width), (True, 3, 64))

    def test_photos_with_audio(self):
        info = self.slideshow(audio=sample("a.m4a"), duration=0.2).info
        self.assertEqual(info.audio, "aac")
        self.assertAlmostEqual(info.duration or 0, 0.6, delta=0.1)

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
        with self.assertRaises(RuntimeError):
            media.open(sample("v.mp4")).merge(audio=sample("a.m4a"), format="webm")
        self.assertEqual(list(self.folder.iterdir()), [])
        self.assertEqual(parts(FOLDER), [])
        truncated = self.folder / "cut.m4a"
        truncated.write_bytes(sample("a.m4a").read_bytes()[:3000])
        with self.assertRaises(media.BrokenError):
            media.open(sample("v.mp4")).merge(audio=truncated)

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
        with self.assertRaises(media.KindError):
            media.open(self.photos()[0]).merge(sample("v.mp4"))
        with self.assertRaises(ValueError):
            self.slideshow(fps=2, duration=1)
        with self.assertRaises(media.KindError):
            media.open(sample("a.m4a")).merge(sample("a.m4a"))
        with self.assertRaises(ValueError):
            self.slideshow(format="jpg")


if __name__ == "__main__":
    unittest.main()
