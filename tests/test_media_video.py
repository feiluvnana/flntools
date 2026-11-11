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


class VideoCase(unittest.TestCase):
    def setUp(self):
        self.folder = Path(tempfile.mkdtemp(dir=FOLDER))


@needs_ffmpeg("libx264", "libmp3lame")
class TrimTest(VideoCase):
    def test_exact(self):
        info = media.probe(media.video.trim(sample("big.mp4"), start=1.0, end=2.0, dest=self.folder / "a.mp4"))
        self.assertAlmostEqual(info.duration or 0, 1.0, delta=1 / 60 + 0.03)
        self.assertEqual(info.video, "h264")

    def test_copy(self):
        info = media.probe(media.video.trim(sample("big.mp4"), start=1.0, end=2.0, dest=self.folder / "b.mp4",
                                            exact=False))
        self.assertEqual((info.video, info.audio), ("mpeg4", "aac"))
        self.assertGreaterEqual(info.duration or 0, 0.9)

    def test_audio_trims_exactly(self):
        info = media.probe(media.video.trim(sample("tone.mp3"), start=0.25, end=0.75, dest=self.folder / "c.mp3"))
        self.assertAlmostEqual(info.duration or 0, 0.5, delta=0.06)

    def test_animation(self):
        out = media.video.trim(sample("anim.gif"), start=0.1, end=0.3, dest=self.folder / "d.gif")
        self.assertEqual(media.probe(out).frames, 1)

    def test_in_place(self):
        path = fresh("big.mp4")
        media.video.trim(path, start=2.0, exact=False)
        self.assertLess(media.probe(path).duration or 9, 2.0)

    def test_bad_times(self):
        for start, end in ((2.0, 1.0), (1.0, 1.0), (-1.0, None), (5.0, None)):
            with self.subTest(start=start, end=end), self.assertRaises(ValueError):
                media.video.trim(sample("big.mp4"), start=start, end=end, dest=self.folder / f"{start}.mp4")
        with self.assertRaises(ValueError):
            media.video.trim(sample("srgb.jpg"), end=1.0, dest=self.folder / "x.jpg")


@needs_ffmpeg()
class FramesTest(VideoCase):
    def test_every(self):
        paths = media.video.frames(sample("big.mp4"), every=1.0, dest_dir=self.folder)
        self.assertEqual([path.name for path in paths], ["big_00000.000.jpg", "big_00001.000.jpg",
                                                         "big_00002.000.jpg"])
        with Image.open(paths[0]) as image:
            self.assertEqual(image.size, (360, 640))

    def test_at_and_count(self):
        at = media.video.frames(sample("big.mp4"), at=[2.5, 0.5], dest_dir=self.folder / "at")
        self.assertEqual([path.name for path in at], ["big_00000.500.jpg", "big_00002.500.jpg"])
        counted = media.video.frames(sample("big.mp4"), count=4, dest_dir=self.folder / "count", format="png")
        self.assertEqual(len(counted), 4)
        self.assertEqual(counted, sorted(counted))
        self.assertEqual(counted[0].name, "big_00000.375.png")

    def test_every_frame(self):
        self.assertEqual(len(media.video.frames(sample("v.mp4"), dest_dir=self.folder)), 10)

    def test_rotated_video_is_upright(self):
        paths = media.video.frames(sample("meta.mp4"), count=1, dest_dir=self.folder)
        with Image.open(paths[0]) as image:
            self.assertEqual(image.size, (48, 64))

    def test_animation(self):
        paths = media.video.frames(sample("anim.gif"), dest_dir=self.folder, format="png")
        self.assertEqual([path.name for path in paths], ["anim_00000.000.png", "anim_00000.100.png",
                                                         "anim_00000.300.png", "anim_00000.400.png"])

    def test_limit(self):
        with self.assertRaisesRegex(ValueError, "limit"):
            media.video.frames(sample("big.mp4"), dest_dir=self.folder / "limited", limit=5)
        self.assertFalse((self.folder / "limited").exists())

    def test_existing_name(self):
        (self.folder / "v_00000.000.jpg").write_bytes(b"x")
        with self.assertRaises(FileExistsError):
            media.video.frames(sample("v.mp4"), every=0.5, dest_dir=self.folder)
        self.assertEqual(len(list(self.folder.iterdir())), 1)

    def test_refusals(self):
        with self.assertRaises(ValueError):
            media.video.frames(sample("v.mp4"), every=1.0, count=2, dest_dir=self.folder)
        with self.assertRaises(ValueError):
            media.video.frames(sample("v.mp4"), format="mp4", dest_dir=self.folder)
        with self.assertRaises(ValueError):
            media.video.frames(sample("srgb.jpg"), dest_dir=self.folder)


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

    def test_slideshow(self):
        info = media.probe(media.video.merge(self.photos(), dest=self.folder / "s.mp4", duration=2.0))
        self.assertAlmostEqual(info.duration or 0, 6.0, delta=0.1)
        self.assertEqual((info.width, info.height, info.video), (64, 48, "h264"))

    def test_frames_at_fps(self):
        info = media.probe(media.video.merge(self.photos(), dest=self.folder / "f.mp4", fps=10))
        self.assertAlmostEqual(info.duration or 0, 0.3, delta=0.05)

    def test_animations(self):
        for extension in (".gif", ".webp"):
            with self.subTest(extension=extension):
                info = media.probe(media.video.merge(self.photos(), dest=self.folder / f"a{extension}"))
                self.assertEqual((info.animated, info.frames, info.width), (True, 3, 64))

    def test_photos_with_audio(self):
        info = media.probe(media.video.merge(self.photos(), dest=self.folder / "m.mp4", audio=sample("a.m4a"),
                                             duration=0.2))
        self.assertEqual(info.audio, "aac")
        self.assertAlmostEqual(info.duration or 0, 0.6, delta=0.1)

    def test_dash_is_a_copy(self):
        commands, patches = built_commands()
        with patches[0], patches[1]:
            for extension in (".mp4", ".mkv"):
                info = media.probe(media.video.merge([sample("v.mp4")], audio=sample("a.m4a"),
                                                     dest=self.folder / f"d{extension}"))
                self.assertEqual((info.video, info.audio), ("mpeg4", "aac"))
                self.assertAlmostEqual(info.duration or 0, 1.0, delta=0.1)
        self.assertTrue(all("copy" in command for command in commands))

    def test_dash_errors(self):
        with self.assertRaisesRegex(media.BrokenError, "no audio stream"):
            media.video.merge([sample("v.mp4")], audio=sample("v.mp4"), dest=self.folder / "x.mp4")
        with self.assertRaises(RuntimeError):
            media.video.merge([sample("v.mp4")], audio=sample("a.m4a"), dest=self.folder / "x.webm")
        self.assertEqual(list(self.folder.iterdir()), [])
        truncated = self.folder / "cut.m4a"
        truncated.write_bytes(sample("a.m4a").read_bytes()[:3000])
        with self.assertRaises(media.BrokenError):
            media.video.merge([sample("v.mp4")], audio=truncated, dest=self.folder / "y.mp4")

    def test_clips_join_by_copy(self):
        commands, patches = built_commands()
        with patches[0], patches[1]:
            info = media.probe(media.video.merge([sample("av.mp4"), sample("av.mp4")], dest=self.folder / "j.mp4"))
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
        info = media.probe(media.video.merge([sample("av.mp4"), webm], dest=self.folder / "e.mp4"))
        self.assertEqual((info.video, info.width, info.height, info.audio), ("h264", 64, 48, "aac"))
        self.assertAlmostEqual(info.duration or 0, 2.0, delta=0.15)

    def test_refusals(self):
        with self.assertRaises(ValueError):
            media.video.merge([], dest=self.folder / "x.mp4")
        with self.assertRaises(ValueError):
            media.video.merge(self.photos(), audio=sample("a.m4a"), dest=self.folder / "x.gif")
        with self.assertRaises(ValueError):
            media.video.merge([*self.photos(), sample("v.mp4")], dest=self.folder / "x.mp4")
        with self.assertRaises(ValueError):
            media.video.merge(self.photos(), fps=2, duration=1, dest=self.folder / "x.mp4")


if __name__ == "__main__":
    unittest.main()
