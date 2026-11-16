"""Tests for `flnd._shrink` and `flnd._media`: plan, encode, verify and place, on synthetic media."""

from __future__ import annotations

import os
import random
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from PIL import Image, ImageDraw, ImageFilter

from flnd import _media, _shrink, _tools

HAS_FFMPEG = bool(shutil.which("ffmpeg") and shutil.which("ffprobe"))
# A day in 2024, as a post's date sets a file's modified time.
DATED = 1_704_110_400


def photo(width: int, height: int, seed: int = 1) -> Image.Image:
    """A synthetic photo: a gradient with shapes and light noise, blurred like a camera's."""
    image = Image.linear_gradient("L").resize((width, height)).convert("RGB")
    draw = ImageDraw.Draw(image)
    rng = random.Random(seed)
    for _ in range(40):
        x, y = rng.randrange(width), rng.randrange(height)
        draw.ellipse((x, y, x + rng.randrange(10, width // 2), y + rng.randrange(10, height // 2)),
                     fill=(rng.randrange(256), rng.randrange(256), rng.randrange(256)))
    noise = Image.effect_noise((width, height), 18).convert("RGB")
    return Image.blend(image, noise, 0.15).filter(ImageFilter.GaussianBlur(1))


class ShrinkCase(unittest.TestCase):
    def setUp(self) -> None:
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name)
        self.recorded: list[_shrink.Outcome] = []

    def shrink(self, path: Path, **options) -> _shrink.Outcome:
        return _shrink.shrink(path, record=self.recorded.append, **options)

    def dated(self, path: Path) -> Path:
        os.utime(path, (DATED, DATED))
        return path

    def assert_no_parts(self) -> None:
        self.assertEqual([p.name for p in self.root.iterdir() if p.name.endswith(".part")], [])


class PhotoTest(ShrinkCase):
    def test_a_jpeg_becomes_a_smaller_avif_that_looks_the_same_and_keeps_its_date(self) -> None:
        source = self.root / "a.jpg"
        photo(640, 800).save(source, quality=92)
        self.dated(source)
        outcome = self.shrink(source)
        self.assertEqual(outcome.state, _shrink.State.COMPRESSED)
        self.assertEqual(outcome.path, self.root / "a.avif")
        self.assertFalse(source.exists())
        self.assertGreaterEqual(outcome.saving, 0.40)
        self.assertEqual(int(outcome.path.stat().st_mtime), DATED)
        self.assertEqual(self.recorded, [outcome])
        self.assertIn("pillow", outcome.tools)
        with Image.open(outcome.path) as made:
            self.assertEqual(made.size, (640, 800))
        self.assert_no_parts()

    def test_a_jpeg_keeps_its_exif(self) -> None:
        source = self.root / "a.jpg"
        exif = Image.Exif()
        exif[0x010F] = "Example camera"
        photo(320, 240).save(source, quality=92, exif=exif)
        outcome = self.shrink(source)
        with Image.open(outcome.path) as made:
            self.assertEqual(made.getexif().get(0x010F), "Example camera")

    def test_when_avif_fails_the_next_candidate_is_tried(self) -> None:
        source = self.root / "a.jpg"
        photo(320, 240).save(source, quality=95, optimize=False)
        real = _shrink._save

        def no_avif(image, part, format, **options):
            if format == "AVIF":
                raise OSError("encoder refused")
            return real(image, part, format, **options)

        with mock.patch.object(_shrink, "_save", no_avif):
            outcome = self.shrink(source)
        self.assertEqual(outcome.state, _shrink.State.COMPRESSED)
        self.assertIn(outcome.path.suffix, (".jpg", ".webp"))

    def test_a_flat_png_goes_through_oxipng_or_lossless_webp(self) -> None:
        source = self.root / "art.png"
        image = Image.new("RGB", (400, 300), "white")
        ImageDraw.Draw(image).rectangle((20, 20, 200, 200), fill="red")
        image.save(source, compress_level=0)
        outcome = self.shrink(source)
        self.assertEqual(outcome.state, _shrink.State.COMPRESSED)
        with Image.open(outcome.path) as made, Image.open(io_copy(image)) as want:
            self.assertEqual(made.convert("RGB").tobytes(), want.convert("RGB").tobytes())

    def test_an_animated_gif_becomes_an_animated_webp_with_every_frame(self) -> None:
        source = self.root / "loop.gif"
        frames = [photo(120, 90, seed).convert("P") for seed in range(4)]
        frames[0].save(source, save_all=True, append_images=frames[1:], duration=100, loop=0)
        outcome = self.shrink(source)
        if outcome.state is _shrink.State.COMPRESSED:
            with Image.open(outcome.path) as made:
                self.assertEqual(made.n_frames, 4)
        else:
            self.assertEqual(outcome.state, _shrink.State.KEPT)
            self.assertTrue(source.is_file())

    def test_a_file_nothing_beats_is_kept_untouched(self) -> None:
        source = self.root / "a.jpg"
        photo(160, 120).save(source, quality=90)
        self.dated(source)
        before = source.read_bytes()
        # No candidate saves 99%, so none passes.
        with mock.patch.object(_shrink, "MIN_SAVING", 0.99):
            outcome = self.shrink(source)
        self.assertEqual((outcome.state, outcome.path), (_shrink.State.KEPT, source))
        self.assertEqual(source.read_bytes(), before)
        self.assertEqual(int(source.stat().st_mtime), DATED)
        self.assertEqual(self.recorded, [outcome])
        self.assert_no_parts()

    def test_a_candidate_under_the_ssim_floor_is_refused(self) -> None:
        source = self.root / "a.jpg"
        photo(320, 240).save(source, quality=95)
        with mock.patch.object(_shrink, "ssim", lambda a, b: 0.5):
            outcome = self.shrink(source)
        self.assertEqual(outcome.state, _shrink.State.KEPT)
        self.assertTrue(source.is_file())

    def test_a_file_that_isnt_an_image_fails(self) -> None:
        source = self.root / "broken.jpg"
        source.write_bytes(b"<html>login</html>")
        outcome = self.shrink(source)
        self.assertEqual(outcome.state, _shrink.State.FAILED)
        self.assertEqual(source.read_bytes(), b"<html>login</html>")

    def test_a_new_suffix_never_overwrites_another_file(self) -> None:
        source = self.root / "a.jpg"
        photo(320, 240).save(source, quality=95)
        (self.root / "a.avif").write_bytes(b"someone else's")
        outcome = self.shrink(source)
        self.assertEqual((self.root / "a.avif").read_bytes(), b"someone else's")
        self.assertTrue(source.is_file() or outcome.path != self.root / "a.avif")

    def test_a_failed_record_leaves_the_original(self) -> None:
        source = self.root / "a.jpg"
        photo(320, 240).save(source, quality=95)

        def refuse(outcome):
            if outcome.state is _shrink.State.COMPRESSED:
                raise OSError("disk full")

        with self.assertRaises(OSError):
            _shrink.shrink(source, record=refuse)
        self.assertTrue(source.is_file())
        self.assertFalse((self.root / "a.avif").exists())


def io_copy(image: Image.Image):
    """`image` as an in-memory PNG, to reopen it as a file."""
    import io

    out = io.BytesIO()
    image.save(out, "PNG")
    out.seek(0)
    return out


class SsimTest(unittest.TestCase):
    def test_ssim_is_one_for_the_same_image_and_drops_with_damage(self) -> None:
        image = photo(256, 256).convert("L")
        self.assertAlmostEqual(_shrink.ssim(image, image), 1.0, places=4)
        blurred = image.filter(ImageFilter.GaussianBlur(4))
        self.assertLess(_shrink.ssim(image, blurred), 0.9)

    @unittest.skipUnless(HAS_FFMPEG, "needs ffmpeg")
    def test_ssim_agrees_with_ffmpegs_y_figure(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            first, second = Path(folder, "a.png"), Path(folder, "b.png")
            image = photo(320, 240).convert("L")
            image.save(first)
            image.filter(ImageFilter.GaussianBlur(0.8)).save(second)
            said = subprocess.run(
                ["ffmpeg", "-hide_banner", "-i", first, "-i", second, "-lavfi", "ssim",
                 "-f", "null", "-"], capture_output=True, text=True, check=True).stderr
            theirs = float(said.split("SSIM Y:")[1].split()[0])
            with Image.open(first) as a, Image.open(second) as b:
                ours = _shrink.ssim(a, b)
        self.assertAlmostEqual(ours, theirs, delta=0.01)


class SavingsTest(unittest.TestCase):
    def test_a_kind_that_doesnt_shrink_is_left_alone_after_enough_files(self) -> None:
        savings = _shrink.Savings()
        for _ in range(_shrink.MIN_SAMPLES - 1):
            savings.add(_shrink.Kind.VIDEO, 0.05)
        self.assertTrue(savings.worth(_shrink.Kind.VIDEO))
        savings.add(_shrink.Kind.VIDEO, 0.05)
        self.assertFalse(savings.worth(_shrink.Kind.VIDEO))
        self.assertTrue(savings.worth(_shrink.Kind.JPEG))


class ImportTest(unittest.TestCase):
    def test_the_cli_never_imports_the_codecs_or_the_archive_readers(self) -> None:
        import sys

        code = ("import sys, flnd.cli; print(sorted(m for m in ('PIL', 'oxipng', 'flnd._shrink', "
                "'flnd._unpack', 'flnd._media', 'zipfile', 'tarfile') if m in sys.modules))")
        out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True,
                             check=True).stdout
        self.assertEqual(out.strip(), "[]")


class ToolsTest(unittest.TestCase):
    def test_without_ffmpeg_a_video_fails_and_names_the_missing_tool(self) -> None:
        with tempfile.TemporaryDirectory() as folder, _tools.fake({}), \
                mock.patch.dict(_shrink._tools_cache, clear=True), \
                mock.patch.object(_media, "_encoders", None):
            clip = Path(folder, "clip.mp4")
            clip.write_bytes(b"\x00\x00\x00\x18ftypmp42")
            outcome = _shrink.shrink(clip, record=lambda o: None)
        self.assertEqual(outcome.state, _shrink.State.FAILED)
        self.assertEqual(outcome.tools, "ffmpeg missing")
        self.assertIn("ffprobe", outcome.note)

    def test_kinds_come_from_the_suffix(self) -> None:
        self.assertEqual(_shrink.kind(Path("a.JPG")), _shrink.Kind.JPEG)
        self.assertEqual(_shrink.kind(Path("a.mp4")), _shrink.Kind.VIDEO)
        self.assertIsNone(_shrink.kind(Path("a.webp")))
        self.assertIsNone(_shrink.kind(Path("a.zip")))


@unittest.skipUnless(HAS_FFMPEG, "needs ffmpeg")
class VideoTest(ShrinkCase):
    def clip(self) -> Path:
        path = self.root / "clip.mp4"
        subprocess.run(
            ["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "testsrc2=size=320x240:rate=30:duration=2",
             "-f", "lavfi", "-i", "sine=duration=2", "-c:v", "libx264", "-crf", "8",
             "-c:a", "aac", "-shortest", str(path)], check=True)
        os.utime(path, (DATED, DATED))
        return path

    def test_a_video_is_probed_once_and_reencoded_with_the_same_duration(self) -> None:
        source = self.clip()
        probe = _media.probe(source)
        self.assertEqual((probe.width, probe.height), (320, 240))
        self.assertIs(_media.probe(source), probe)
        seen: list[float] = []
        outcome = self.shrink(source, progress=seen.append)
        if outcome.state is _shrink.State.FAILED:
            self.skipTest(f"this ffmpeg can't encode: {outcome.note}")
        self.assertEqual(outcome.path, source)
        if outcome.state is _shrink.State.COMPRESSED:
            made = _media.probe(source)
            self.assertAlmostEqual(made.duration, probe.duration, delta=0.1)
            self.assertEqual(int(source.stat().st_mtime), DATED)
            self.assertTrue(seen and seen[-1] <= 1.0)
        self.assert_no_parts()

    def test_a_stop_kills_the_encode_and_leaves_the_original(self) -> None:
        import threading

        source = self.clip()
        before = source.read_bytes()
        stop = threading.Event()
        stop.set()
        with self.assertRaises(KeyboardInterrupt):
            self.shrink(source, stop=stop)
        self.assertEqual(source.read_bytes(), before)
        self.assert_no_parts()


if __name__ == "__main__":
    unittest.main()
