import os
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from io import StringIO
from pathlib import Path
from unittest import mock

from media_samples import FOLDER, Image, fresh, needs_ffmpeg, needs_pillow, run, sample, wide_profile
from shared import _ffmpeg, _media, media, theme
from shared._theme import Screen

ROOT = Path(__file__).resolve().parent.parent


def run_python(code: str, env: dict | None = None) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, "-c", code], cwd=ROOT, capture_output=True, text=True, timeout=60,
                          env={**os.environ, **(env or {})})


def write(folder: Path, name: str, data: bytes) -> Path:
    path = folder / name
    path.write_bytes(data)
    return path


def parts(folder: Path) -> list[str]:
    return [path.name for path in folder.iterdir() if path.name.endswith(".part")]


class SniffTest(unittest.TestCase):
    def setUp(self):
        self.folder = Path(tempfile.mkdtemp(dir=FOLDER))

    def sniffed(self, data: bytes) -> str | None:
        return media.sniff(write(self.folder, "x.part", data))

    def ftyp(self, brand: bytes, *compatible: bytes) -> bytes:
        body = b"ftyp" + brand + b"\0\0\0\0" + b"".join(compatible)
        return (len(body) + 4).to_bytes(4, "big") + body + b"\0" * 40

    def test_every_row(self):
        rows = [
            (b"\xff\xd8\xff\xe0", ".jpg"), (b"\x89PNG\r\n\x1a\n", ".png"), (b"GIF87a", ".gif"), (b"GIF89a", ".gif"),
            (b"RIFF\0\0\0\0WEBPVP8 ", ".webp"), (b"RIFF\0\0\0\0WAVEfmt ", ".wav"), (b"RIFF\0\0\0\0AVI LIST", ".avi"),
            (b"II*\0", ".tif"), (b"MM\0*", ".tif"), (b"BM\0\0", ".bmp"), (b"\0\0\1\0\1\0", ".ico"),
            (self.ftyp(b"avif"), ".avif"), (self.ftyp(b"mif1", b"miaf", b"avif"), ".avif"),
            (self.ftyp(b"heic"), ".heic"), (self.ftyp(b"mif1", b"heic"), ".heic"), (self.ftyp(b"qt  "), ".mov"),
            (self.ftyp(b"M4A "), ".m4a"), (self.ftyp(b"3gp5"), ".3gp"), (self.ftyp(b"isom"), ".mp4"),
            (self.ftyp(b"dash"), ".mp4"), (b"\x1a\x45\xdf\xa3\x01\0\0\0B\x82\x84webm", ".webm"),
            (b"\x1a\x45\xdf\xa3\x01\0\0\0B\x82\x88matroska", ".mkv"), (b"G" + b"\0" * 187 + b"G", ".ts"),
            (b"FLV\x01", ".flv"), (b"OggS" + b"\0" * 24 + b"OpusHead", ".opus"),
            (b"OggS" + b"\0" * 24 + b"\x01vorbis", ".ogg"), (b"OggS" + b"\0" * 24 + b"\x80theora", ".ogv"),
            (b"fLaC", ".flac"), (b"ID3\x04", ".mp3"), (b"\xff\xfb\x90", ".mp3"), (b"FORM\0\0\0\0AIFF", ".aiff"),
            (b"  <html>", ".html"), (b"\xef\xbb\xbf<!doctype html>", ".html"), (b'\n{"error": 1}', ".json"),
            (b"[1, 2]", ".json"), (b"hello", None), (b"", None),
        ]
        for data, expected in rows:
            with self.subTest(data=data[:12]):
                self.assertEqual(self.sniffed(data), expected)

    def test_needs_neither_pillow_nor_ffmpeg(self):
        code = ("import sys; sys.modules['PIL'] = None; sys.modules['imageio_ffmpeg'] = None\n"
                "from shared import media\n"
                f"print(media.sniff({str(sample('page.html'))!r}))")
        self.assertEqual(run_python(code).stdout.strip(), ".html")


class KindOfTest(unittest.TestCase):
    """`media.kind_of`, the one table of file kinds that flnd reads too."""

    def test_kinds_by_extension(self):
        rows = {".jpg": "image", "JPEG": "image", ".heic": "image", ".heif": "image", ".gif": "image",
                ".webp": "image", ".avif": "image", ".tiff": "image", "mp4": "video", ".m4v": "video",
                ".mov": "video", ".webm": "video", ".mkv": "video", ".flv": "video", ".mp3": "audio",
                ".m4a": "audio", ".aac": "audio", ".opus": "audio", ".ogg": "audio", ".oga": "audio",
                ".flac": "audio", ".wav": "audio", ".zip": None, ".7z": None, ".md": None, "": None}
        for extension, kind in rows.items():
            with self.subTest(extension=extension):
                self.assertEqual(media.kind_of(extension), kind)

    def test_every_level_is_a_preset(self):
        # flnd's --image, --video and --audio levels are media's preset names, and the old names are gone.
        self.assertEqual(tuple(_media.PRESETS), ("best", "high", "mid", "low"))


class LazyImportTest(unittest.TestCase):
    def test_log_imports_no_media(self):
        code = "import sys\nfrom shared import log\nprint('PIL' in sys.modules, 'shared._media' in sys.modules)"
        self.assertEqual(run_python(code).stdout.strip(), "False False")

    def test_media_imports_neither_pillow_nor_its_namespaces(self):
        code = ("import sys\nfrom shared import media\n"
                "print('PIL' in sys.modules, 'shared._media_video' in sys.modules, 'imageio_ffmpeg' in sys.modules)")
        self.assertEqual(run_python(code).stdout.strip(), "False False False")

    def test_namespaces_share_the_errors(self):
        from shared import _media_video

        self.assertIs(media.video, _media_video)
        self.assertIs(media.image.__dict__["_media"].BrokenError, media.BrokenError)
        with self.assertRaises(AttributeError):
            media.nothing  # noqa: B018

    @needs_pillow
    def test_photo_without_pillow(self):
        code = ("import sys; sys.modules['PIL'] = None\nfrom shared import media\n"
                f"try:\n    media.probe({str(sample('srgb.jpg'))!r})\nexcept ImportError as error:\n    print(error)")
        self.assertEqual(run_python(code).stdout.strip(),
                         "shared.media needs Pillow for photos: pip install 'flntools[media]'")

    @needs_ffmpeg()
    def test_video_without_pillow(self):
        code = ("import sys; sys.modules['PIL'] = None\nfrom shared import media\n"
                f"print(media.probe({str(sample('v.mp4'))!r}).video)")
        self.assertEqual(run_python(code).stdout.strip(), "mpeg4")


@needs_pillow
class PhotoProbeTest(unittest.TestCase):
    def test_exif_photo(self):
        info = media.probe(sample("exif.jpg"))
        self.assertEqual((info.kind, info.format, info.animated, info.frames), ("image", "jpeg", False, 1))
        self.assertEqual((info.width, info.height), (40, 80))  # orientation 6 swaps them
        self.assertEqual(info.created, datetime(2024, 5, 1, 10, 30, tzinfo=timezone(timedelta(hours=9))))
        assert info.location is not None
        self.assertAlmostEqual(info.location[0], 48.8582, places=4)
        self.assertAlmostEqual(info.location[1], 2.2945, places=4)
        self.assertEqual(info.tags["Model"], "Pixel 99")
        self.assertNotIn("MakerNote", info.tags)
        self.assertEqual(info.size, sample("exif.jpg").stat().st_size)

    def test_unknown_date_and_zero_gps(self):
        info = media.probe(sample("zero.jpg"))
        self.assertIsNone(info.created)
        self.assertIsNone(info.location)
        self.assertEqual(info.tags["DateTimeOriginal"], "0000:00:00 00:00:00")

    def test_formats(self):
        for name, format in (("rgba.png", "png"), ("anim.webp", "webp"), ("anim.gif", "gif"), ("tiny.heic", "heif")):
            with self.subTest(name=name):
                self.assertEqual(media.probe(sample(name)).format, format)

    def test_animation(self):
        for name in ("anim.gif", "anim.webp", "anim.png"):
            with self.subTest(name=name):
                info = media.probe(sample(name))
                self.assertTrue(info.animated)
                self.assertEqual(info.frames, 4)
                self.assertAlmostEqual(info.duration or 0, 0.5)
                self.assertAlmostEqual(info.fps or 0, 8.0)

    def test_png_named_jpg(self):
        renamed = Path(tempfile.mkdtemp(dir=FOLDER)) / "real.jpg"
        renamed.write_bytes(sample("rgba.png").read_bytes())
        self.assertEqual(media.probe(renamed).format, "png")

    def test_every_writable_format_reads_back(self):
        folder = Path(tempfile.mkdtemp(dir=FOLDER))
        for extension, format in _media.STILLS.items():
            with self.subTest(extension=extension):
                written = media.convert(sample("srgb.jpg"), dest=folder / f"x{extension}")
                self.assertEqual(media.probe(written).format, format.lower())


@needs_ffmpeg()
class VideoProbeTest(unittest.TestCase):
    def test_video_only(self):
        info = media.probe(sample("v.mp4"))
        self.assertEqual((info.kind, info.format, info.video, info.audio), ("video", "mp4", "mpeg4", None))
        self.assertEqual((info.width, info.height, info.fps), (64, 48, 10.0))
        self.assertAlmostEqual(info.duration or 0, 1.0, places=1)

    def test_audio_only(self):
        info = media.probe(sample("a.m4a"))
        self.assertEqual((info.kind, info.format, info.audio, info.sample_rate, info.channels),
                         ("audio", "m4a", "aac", 44100, 1))
        self.assertIsNone(info.width)

    def test_metadata_and_rotation(self):
        info = media.probe(sample("meta.mp4"))
        self.assertEqual((info.width, info.height), (48, 64))
        self.assertEqual(info.created, datetime(2024, 5, 1, 10, 0, tzinfo=timezone.utc))
        self.assertEqual(info.location, (48.8577, 2.295))
        self.assertEqual(info.tags["title"], "hi\nStream #0:5: Video: h264, 9999x9999")
        self.assertEqual(info.video, "mpeg4")

    def test_other_containers(self):
        for name, format in (("x.avi", "avi"), ("x.ts", "ts"), ("x.mpg", "mpeg"), ("x.flv", "flv"),
                             ("x.wmv", "asf"), ("tone.flac", "flac"), ("tone.wav", "wav"), ("tone.aiff", "aiff")):
            with self.subTest(name=name):
                info = media.probe(sample(name))
                self.assertEqual(info.format, format)


class ParserTest(unittest.TestCase):
    """ffmpeg's summary, as printed by 4.4, 6.1 and 9.0, parsed without running it."""

    FFMPEG_4 = (
        "Input #0, mov,mp4,m4a,3gp,3g2,mj2, from 'file:/tmp/a.mp4':\n"
        "  Metadata:\n"
        "    major_brand     : isom\n"
        "    creation_time   : 2021-06-01T08:00:00.000000Z\n"
        "    Stream #0:9: Video: h264, 9999x9999: a hostile key\n"
        "  Duration: 00:01:02.50, start: 0.000000, bitrate: 1205 kb/s\n"
        "    Stream #0:0(und): Video: h264 (High) (avc1 / 0x31637661), yuv420p, 1920x1080 [SAR 1:1 DAR 16:9], "
        "1100 kb/s, 29.97 fps, 29.97 tbr, 30k tbn, 59.94 tbc (default)\n"
        "    Metadata:\n"
        "      creation_time   : 2021-06-01T08:00:00.000000Z\n"
        "    Side data:\n"
        "      displaymatrix: rotation of -90.00 degrees\n"
        "    Stream #0:1(und): Audio: aac (LC) (mp4a / 0x6134706D), 48000 Hz, stereo, fltp, 96 kb/s (default)\n"
        "Stream mapping:\n"
        "  Stream #0:0 -> #0:0 (h264 (native) -> wrapped_avframe (native))\n")
    FFMPEG_9 = (
        "Input #0, matroska,webm, from 'file:/tmp/b.webm':\n"
        "  Metadata:\n"
        "    title           : hi\n"
        "                    : Stream #0:5: Video: h264, 9999x9999\n"
        "  Duration: N/A, start: 0.000000, bitrate: N/A\n"
        "  Stream #0:0: Audio: opus, 48000 Hz, 5.1, fltp (default)\n"
        "    Metadata:\n"
        "      title           : x\n"
        "                      : Stream #0:6: Video: h264, 9999x9999\n"
        "Stream mapping:\n")

    def test_ffmpeg_4(self):
        fields = _ffmpeg.summary(self.FFMPEG_4)
    def test_fake_stream_line_is_not_counted(self):
        fields = _ffmpeg.summary(self.FFMPEG_9)
        self.assertIsNone(fields["video"])
        self.assertIsNone(fields["duration"])
        self.assertEqual((fields["audio"], fields["channels"]), ("opus", 6))

    def test_ffmetadata_escapes(self):
        text = ";FFMETADATA1\ntitle=a\\=b\\;c\\#d\\\\e\\\nStream \\#0:5: Video\nartist=x\n[CHAPTER]\ntitle=chapter\n"
        self.assertEqual(_ffmpeg.ffmetadata(text), {"title": "a=b;c#d\\e\nStream #0:5: Video", "artist": "x"})

    def test_versions(self):
        self.assertEqual(_ffmpeg.parse_version("ffmpeg version 4.2.7 Copyright"), (4, 2))
        self.assertEqual(_ffmpeg.parse_version("ffmpeg version 4.4.2-0ubuntu0.22.04.1 Copyright"), (4, 4))
        self.assertEqual(_ffmpeg.parse_version("ffmpeg version n7.1 Copyright"), (7, 1))
        self.assertIsNone(_ffmpeg.parse_version("ffmpeg version N-12345-gabc Copyright"))


@needs_pillow
class ConvertPhotoTest(unittest.TestCase):
    def setUp(self):
        self.folder = Path(tempfile.mkdtemp(dir=FOLDER))

    def test_orientation_applied_and_metadata_kept(self):
        out = media.convert(sample("exif.jpg"), dest=self.folder / "x.png")
        with Image.open(out) as image:
            self.assertEqual(image.size, (40, 80))
            self.assertNotIn(0x0112, image.getexif())
            self.assertEqual(image.getexif()[0x0110], "Pixel 99")
            self.assertEqual(image.info["icc_profile"], wide_profile())

    def test_transparency_flattened_onto_white(self):
        out = media.convert(sample("rgba.png"), dest=self.folder / "x.jpg")
        with Image.open(out) as image:
            self.assertEqual(image.mode, "RGB")
            self.assertGreater(min(image.getpixel((5, 5))), 240)

    def test_quality_changes_size(self):
        low = media.convert(sample("big.jpg"), dest=self.folder / "low.webp", quality=30)
        high = media.convert(sample("big.jpg"), dest=self.folder / "high.webp", quality=90)
        self.assertLess(low.stat().st_size, high.stat().st_size)

    def test_ico_is_scaled_to_256(self):
        out = media.convert(sample("big.jpg"), dest=self.folder / "x.ico")
        self.assertLessEqual(max(media.probe(out).width or 0, media.probe(out).height or 0), 256)

    def test_heic(self):
        out = media.convert(sample("tiny.heic"), dest=self.folder / "x.png")
        self.assertEqual(media.probe(out).width, 64)

    def test_animations_keep_frames(self):
        for source, extension in (("anim.webp", ".gif"), ("anim.gif", ".png"), ("anim.png", ".webp")):
            with self.subTest(source=source):
                out = media.convert(sample(source), dest=self.folder / f"{source}{extension}")
                info = media.probe(out)
                self.assertEqual((info.frames, info.animated), (4, True))
                self.assertAlmostEqual(info.duration or 0, 0.5)

    def test_animated_to_still_format(self):
        with self.assertRaisesRegex(ValueError, "media.video.frames"):
            media.convert(sample("anim.webp"), dest=self.folder / "x.jpg")

    def test_refusals(self):
        with self.assertRaisesRegex(ValueError, "media.video.merge"):
            media.convert(sample("srgb.jpg"), dest=self.folder / "x.mp4")
        with self.assertRaisesRegex(ValueError, ".xyz"):
            media.convert(sample("srgb.jpg"), dest=self.folder / "x.xyz")
        with self.assertRaisesRegex(ValueError, "fps="):
            media.convert(sample("srgb.jpg"), dest=self.folder / "x.png", fps=10)
        existing = write(self.folder, "taken.png", b"x")
        with self.assertRaises(FileExistsError):
            media.convert(sample("srgb.jpg"), dest=existing)
        self.assertEqual(parts(self.folder), [])

    def test_failure_leaves_no_part(self):
        with mock.patch.object(_media._pillow, "save", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                media.convert(sample("srgb.jpg"), dest=self.folder / "x.png")
        self.assertEqual(list(self.folder.iterdir()), [])


@needs_ffmpeg("libx264", "libvpx-vp9", "libopus", "libmp3lame", "libvorbis")
class ConvertVideoTest(unittest.TestCase):
    def setUp(self):
        self.folder = Path(tempfile.mkdtemp(dir=FOLDER))

    def test_copy_when_the_container_holds_the_codecs(self):
        out = media.convert(sample("av.mkv"), dest=self.folder / "x.mp4")
        info = media.probe(out)
        self.assertEqual((info.video, info.audio), ("mpeg4", "aac"))

    def test_encoded_when_it_does_not(self):
        info = media.probe(media.convert(sample("vp9.webm"), dest=self.folder / "x.mp4"))
        self.assertEqual((info.video, info.audio), ("vp9", "opus") if False else ("vp9", info.audio))
        self.assertIn(info.audio, ("aac", "opus"))

    def test_webm(self):
        info = media.probe(media.convert(sample("big.mp4"), dest=self.folder / "x.webm"))
        self.assertEqual((info.format, info.video, info.audio), ("webm", "vp9", "opus"))

    def test_gif_is_at_most_15_fps(self):
        info = media.probe(media.convert(sample("big.mp4"), dest=self.folder / "x.gif"))
        self.assertTrue(info.animated)
        self.assertLessEqual(info.fps or 99, 15.5)

    def test_animation_to_video(self):
        for source in ("anim.gif", "anim.webp"):
            with self.subTest(source=source):
                info = media.probe(media.convert(sample(source), dest=self.folder / f"{source}.mp4"))
                self.assertEqual(info.video, "h264")
                self.assertAlmostEqual(info.duration or 0, 0.5, delta=0.15)

    def test_video_to_webp_animation(self):
        info = media.probe(media.convert(sample("v.mp4"), dest=self.folder / "x.webp"))
        self.assertTrue(info.animated)
        self.assertEqual(info.frames, 10)

    def test_fps(self):
        info = media.probe(media.convert(sample("big.mp4"), dest=self.folder / "x.mp4", fps=30))
        self.assertEqual(info.fps, 30.0)

    def test_audio(self):
        for extension, codec in ((".mp3", "mp3"), (".opus", "opus"), (".ogg", "vorbis"), (".wav", "pcm_s16le")):
            with self.subTest(extension=extension):
                info = media.probe(media.convert(sample("tone.flac"), dest=self.folder / f"x{extension}"))
                self.assertEqual(info.audio, codec)
        self.assertEqual(media.probe(media.convert(sample("a.m4a"), dest=self.folder / "y.m4a")).audio, "aac")

    def test_metadata_kept(self):
        info = media.probe(media.convert(sample("meta.mp4"), dest=self.folder / "x.mkv"))
        self.assertEqual(info.created, datetime(2024, 5, 1, 10, 0, tzinfo=timezone.utc))
        self.assertEqual(info.tags["title"].split("\n")[0], "hi")

    def test_kind_changes_are_refused(self):
        with self.assertRaisesRegex(ValueError, "use media.video.frames to get photos"):
            media.convert(sample("v.mp4"), dest=self.folder / "x.jpg")
        with self.assertRaisesRegex(ValueError, "media.audio.extract"):
            media.convert(sample("av.mkv"), dest=self.folder / "x.mp3")
        with self.assertRaisesRegex(ValueError, "h265"):
            media.convert(sample("v.mp4"), dest=self.folder / "x.webm", codec="h265")


def record(built: list):
    """A stand-in for _ffmpeg.encode that keeps the command and writes a 1-byte result."""
    def encode(args, **_):
        built.append(args)
        Path(args[-1].removeprefix("file:")).write_bytes(b"x")
    return mock.patch.object(_ffmpeg, "encode", side_effect=encode)


class FakeScreenCase(unittest.TestCase):
    def setUp(self):
        self.folder = Path(tempfile.mkdtemp(dir=FOLDER))
        self.screen = Screen(out=StringIO(), inp=StringIO(""), width=80, tty=False, interactive=False)
        self.scope = theme.scope(screen=self.screen, color="none", unicode=True)
        self.scope.__enter__()

    def tearDown(self):
        self.scope.__exit__(None, None, None)


@needs_pillow
class CompressPhotoTest(FakeScreenCase):
    def test_presets_shrink_in_order(self):
        sizes = []
        for preset in ("low", "mid", "high"):
            out = media.compress(sample("big.jpg"), dest=self.folder / f"{preset}.jpg", preset=preset)
            assert out is not None
            media.check(out)
            sizes.append(out.stat().st_size)
        self.assertLess(sizes[0], sizes[1])
        self.assertLess(sizes[1], sizes[2])
        self.assertLess(sizes[2], sample("big.jpg").stat().st_size)
        self.assertEqual((media.probe(self.folder / "low.jpg").width, media.probe(self.folder / "low.jpg").height),
                         (2048, 1536))

    def test_best_keeps_the_full_resolution_and_strips_metadata(self):
        best = media.compress(sample("exif.jpg"), dest=self.folder / "best.jpg", preset="best")
        high = media.compress(sample("exif.jpg"), dest=self.folder / "high.jpg", preset="high")
        if best is None or high is None:
            self.skipTest("the sample is already smaller than quality 95")
        before, after = media.probe(sample("exif.jpg")), media.probe(best)
        self.assertEqual((after.width, after.height), (before.width, before.height))
        self.assertIsNone(after.location)
        self.assertGreater(best.stat().st_size, high.stat().st_size)

    def test_in_place_keeps_format(self):
        for name in ("big.jpg", "noise.gif"):
            with self.subTest(name=name):
                path = fresh(name)
                before = media.probe(path)
                out = media.compress(path, preset="low", quality=40, resolution=100)
                self.assertEqual(out, path)
                self.assertEqual(media.probe(path).format, before.format)
                self.assertEqual(len(list(path.parent.iterdir())), 1)

    def test_stripped(self):
        out = media.compress(sample("exif.jpg"), dest=self.folder / "x.jpg", quality=10)
        assert out is not None
        with Image.open(out) as image:
            self.assertEqual(image.size, (40, 80))
            self.assertEqual(len(image.getexif()), 0)
            self.assertNotIn("xmp", image.info)
            self.assertEqual(image.info["icc_profile"], wide_profile())
        srgb = media.compress(sample("srgb.jpg"), dest=self.folder / "s.jpg", quality=10)
        assert srgb is not None
        with Image.open(srgb) as image:
            self.assertNotIn("icc_profile", image.info)

    def test_strip_false_keeps_exif(self):
        out = media.compress(sample("exif.jpg"), dest=self.folder / "x.jpg", quality=10, strip=False)
        info = media.probe(out or self.folder / "missing")
        self.assertEqual(info.tags["DateTimeOriginal"], "2024:05:01 10:30:00")
        self.assertIsNotNone(info.location)

    def test_format(self):
        path = fresh("tiny.heic")
        self.assertIsNone(media.compress(path))
        out = media.compress(path, format="jpg", quality=10)
        self.assertEqual(out, path.with_suffix(".jpg"))
        self.assertFalse(path.exists())
        with self.assertRaisesRegex(ValueError, "use media.video.merge"):
            media.compress(sample("srgb.jpg"), format="mp4")
        with self.assertRaisesRegex(ValueError, "different formats"):
            media.compress(sample("srgb.jpg"), format="jpg", dest=self.folder / "x.webp")

    def test_existing_new_name(self):
        path = fresh("tiny.heic")
        write(path.parent, "tiny.jpg", b"taken")
        with self.assertRaises(FileExistsError):
            media.compress(path, format="jpg")
        self.assertTrue(path.exists())

    def test_not_smaller_returns_none(self):
        path = fresh("small.png")
        before = path.read_bytes()
        self.assertIsNone(media.compress(path))
        self.assertEqual(path.read_bytes(), before)
        self.assertIn("compressing saved nothing", self.screen.out.getvalue())

    def test_bad_settings(self):
        for settings in ({"preset": "tiny"}, {"codec": "av1"}, {"quality": 0}, {"quality": 101}, {"fps": 0},
                         {"audio_bitrate": 8_000}):
            with self.subTest(settings=settings), self.assertRaises(ValueError):
                media.compress(sample("srgb.jpg"), **settings)


@needs_ffmpeg("libx264", "libx265", "libvpx-vp9", "libopus", "libmp3lame")
class CompressVideoTest(FakeScreenCase):
    def compressed(self, name: str, **settings) -> media.Info:
        out = media.compress(sample(name), dest=self.folder / f"{len(list(self.folder.iterdir()))}"
                             f"{settings.pop('extension', Path(name).suffix)}", progress=lambda *_: None, **settings)
        assert out is not None
        return media.probe(out)

    def test_presets(self):
        sizes = []
        for preset in ("low", "mid", "high"):
            info = self.compressed("big.mp4", preset=preset)
            media.check(info.path)
            self.assertEqual((info.video, info.audio), ("h264", "aac"))
            self.assertAlmostEqual(info.duration or 0, 3.0, delta=0.1)
            sizes.append(info.size)
        self.assertLessEqual(media.probe(self.folder / "0.mp4").fps or 99, 30)
        self.assertLess(sizes[0], sizes[1])
        self.assertLess(sizes[1], sizes[2])
        self.assertLess(sizes[2], sample("big.mp4").stat().st_size)

    def test_resolution(self):
        self.assertEqual((self.compressed("big.mp4", resolution=240).width,), (240,))
        self.assertEqual(self.compressed("big.mp4", resolution=240, preset="low").height, 426)
        self.assertEqual(self.compressed("big.mp4", resolution=2000).width, 360)

    def test_stripped(self):
        info = self.compressed("meta.mp4", quality=40, audio_bitrate=32_000)
        self.assertIsNone(info.created)
        self.assertIsNone(info.location)
        self.assertNotIn("title", info.tags)
        self.assertNotIn("encoder", info.tags)
        self.assertEqual((info.width, info.height), (48, 64))
        listing = subprocess.run([_ffmpeg.exe(), "-hide_banner", "-i", str(info.path)], capture_output=True,
                                 text=True).stderr
        self.assertNotIn("Subtitle", listing)
        self.assertNotIn("Chapter", listing)
        self.assertNotIn("Lavc", listing)
        self.assertNotIn("Lavf", listing)

    def test_strip_false(self):
        info = self.compressed("meta.mp4", quality=40, audio_bitrate=32_000, strip=False)
        self.assertEqual(info.created, datetime(2024, 5, 1, 10, 0, tzinfo=timezone.utc))
        self.assertEqual(info.location, (48.8577, 2.295))

    def test_same_format(self):
        self.assertEqual(self.compressed("vp9.webm", quality=20, audio_bitrate=32_000).video, "vp9")
        self.assertEqual(self.compressed("av.mkv", quality=40, audio_bitrate=32_000).format, "mkv")
        self.assertEqual(self.compressed("tone.mp3", audio_bitrate=64_000).audio, "mp3")
        path = fresh("x.flv")
        out = media.compress(path, quality=40, audio_bitrate=32_000, progress=lambda *_: None)
        self.assertEqual(out, path.with_suffix(".mp4"))
        self.assertFalse(path.exists())

    def test_codec_and_crf(self):
        built = []
        with record(built):
            for codec, extension in (("h264", ".mp4"), ("h265", ".mp4"), ("vp9", ".webm")):
                media.compress(sample("big.mp4"), dest=self.folder / f"crf{codec}{extension}", quality=80,
                               codec=codec)
        crfs = [args[args.index("-crf") + 1] for args in built]
        self.assertEqual(crfs, ["23", "27", "34"])
        self.assertIn("hvc1", built[1])
        self.assertTrue(all("image2" not in " ".join(args) for args in built))

    def test_settings_replace_the_preset(self):
        built = []
        with record(built):
            for preset in ("low", "mid"):  # the x264 speed is the one thing only a preset sets
                media.compress(sample("big.mp4"), dest=self.folder / f"{preset}.mp4", preset=preset, quality=60,
                               resolution=1080, fps=30, audio_bitrate=96_000)
        self.assertEqual(built[0][:-1], built[1][:-1])

    def test_h265(self):
        self.assertEqual(self.compressed("big.mp4", codec="h265", preset="low").video, "hevc")

    def test_lossless_audio_left_alone(self):
        self.assertIsNone(media.compress(sample("tone.flac")))
        out = media.compress(fresh("tone.flac"), format="opus", progress=lambda *_: None)
        assert out is not None
        self.assertEqual((out.suffix, media.probe(out).audio), (".opus", "opus"))

    def test_progress_grows(self):
        seen = []
        media.compress(sample("big.mp4"), dest=self.folder / "p.mp4", preset="low",
                       progress=lambda done, total: seen.append((done, total)))
        self.assertTrue(seen)
        self.assertEqual([done for done, _ in seen], sorted(done for done, _ in seen))
        self.assertAlmostEqual(seen[-1][0], 3.0, delta=0.2)

    def test_default_progress_bar(self):
        media.compress(sample("big.mp4"), dest=self.folder / "bar.mp4", preset="low")
        self.assertIn("big.mp4", self.screen.out.getvalue())


@unittest.skipIf(sys.platform == "win32", "the fake ffmpeg is a shell script")
class FakeFfmpegTest(unittest.TestCase):
    """A fake ffmpeg through IMAGEIO_FFMPEG_EXE: the stall timer, the encoder check, the version check."""

    def setUp(self):
        self.folder = Path(tempfile.mkdtemp(dir=FOLDER))
        _ffmpeg.found.cache_clear()
        _ffmpeg.encoders.cache_clear()
        self.addCleanup(_ffmpeg.found.cache_clear)
        self.addCleanup(_ffmpeg.encoders.cache_clear)

    def fake(self, body: str, version: str = "6.1", encoders: str = " V....D libx264  H.264\n A....D aac  AAC") -> Path:
        script = self.folder / "ffmpeg"
        script.write_text(f"#!/bin/sh\ncase \"$*\" in\n*-version*) echo 'ffmpeg version {version} Copyright';;\n"
                          f"*-encoders*) printf '%s\\n' '{encoders}';;\n*) {body};;\nesac\n")
        script.chmod(0o755)
        patcher = mock.patch.dict(os.environ, {"IMAGEIO_FFMPEG_EXE": str(script)})
        patcher.start()
        self.addCleanup(patcher.stop)
        return script

    def test_stall_kills_ffmpeg(self):
        self.fake("echo out_time_us=1000; sleep 30")
        part = self.folder / ".x.part"
        with mock.patch.object(_ffmpeg, "STALL", 0.5), self.assertRaises(TimeoutError):
            _ffmpeg.encode(["-i", "x", str(part)], name="x.mp4", duration=1.0, progress=None)

    def test_missing_encoder(self):
        self.fake("exit 1", encoders=" A....D aac  AAC")
        with self.assertRaisesRegex(RuntimeError, "no libx264 encoder"):
            _ffmpeg.need("libx264")

    def test_too_old(self):
        self.fake("exit 1", version="4.2.7")
        with self.assertRaisesRegex(RuntimeError, "4.2 is too old"):
            _ffmpeg.exe()

    def test_imageio_ffmpeg_honours_its_variable(self):
        script = self.fake("exit 0")
        self.assertEqual(_ffmpeg.exe(), str(script))

    @needs_pillow
    def test_no_ffmpeg_at_all(self):
        code = ("import sys; sys.modules['imageio_ffmpeg'] = None\nfrom shared import media\n"
                f"print(media.probe({str(sample('srgb.jpg'))!r}).kind)\n"
                f"try:\n    media.probe({str(sample('v.mp4'))!r})\nexcept RuntimeError as error:\n    print(error)")
        lines = run_python(code, env={"PATH": str(self.folder)}).stdout.strip().splitlines()
        self.assertEqual(lines[0], "image")
        self.assertTrue(lines[1].startswith("shared.media needs ffmpeg for videos and audio"), lines)

    def test_path_ffmpeg_when_imageio_ffmpeg_is_missing(self):
        script = self.fake("exit 0")
        code = ("import sys; sys.modules['imageio_ffmpeg'] = None\nfrom shared import _ffmpeg\nprint(_ffmpeg.exe())")
        self.assertEqual(run_python(code, env={"PATH": str(self.folder)}).stdout.strip(), str(script))


@needs_ffmpeg()
class CheckTest(unittest.TestCase):
    def setUp(self):
        self.folder = Path(tempfile.mkdtemp(dir=FOLDER))

    def test_good_files_pass(self):
        for name in ("v.mp4", "a.m4a", "av.mkv", "tone.flac", "exif.jpg", "anim.gif", "tiny.heic"):
            with self.subTest(name=name):
                media.check(sample(name))

    def test_truncated(self):
        for name, keep in (("big.jpg", 0.5), ("v.mp4", 0.7)):
            with self.subTest(name=name):
                data = sample(name).read_bytes()
                cut = write(self.folder, name, data[:int(len(data) * keep)])
                with self.assertRaisesRegex(media.BrokenError, name):
                    media.check(cut)

    def test_not_media(self):
        for name, data in (("r.mp4", os.urandom(3000)), ("r.jpg", os.urandom(3000)),
                           ("page.mp4", b"<html>x</html>"), ("page.jpg", b"<html>x</html>")):
            with self.subTest(name=name), self.assertRaises(media.BrokenError):
                media.check(write(self.folder, name, data))


@needs_ffmpeg()
class SafetyTest(unittest.TestCase):
    def setUp(self):
        self.folder = Path(tempfile.mkdtemp(dir=FOLDER))
        self.secret = write(self.folder, "secret.txt", b"top secret\n")

    def playlist(self, name: str) -> Path:
        text = f"#EXTM3U\n#EXT-X-MEDIA-SEQUENCE:0\n#EXTINF:1.0,\nfile:{self.secret}\n#EXT-X-ENDLIST\n"
        return write(self.folder, name, text.encode())

    def test_hls_playlist_is_refused(self):
        with self.assertRaises(media.UnsafeError):
            media.probe(self.playlist("list.m3u8"))
        with self.assertRaises(media.BrokenError):
            media.probe(self.playlist("clip.mp4"))
        out = self.folder / "out.mkv"
        with self.assertRaises(ValueError):
            media.convert(self.playlist("list2.m3u8"), dest=out)
        self.assertFalse(out.exists())

    def test_concat_list_and_dash_are_refused(self):
        concat = write(self.folder, "list.mp4", f"ffconcat version 1.0\nfile '{sample('v.mp4')}'\n".encode())
        with self.assertRaises(media.UnsafeError):
            media.probe(concat)
        dash = write(self.folder, "x.mpd", b'<?xml version="1.0"?><MPD xmlns="urn:mpeg:dash:schema:mpd:2011"/>')
        with self.assertRaises(ValueError):
            media.probe(dash)

    def test_names_that_look_like_options_or_protocols(self):
        for name in ("-y.mp4", "http:x.mp4", "concat:a.mp4|b.mp4"):
            with self.subTest(name=name):
                path = self.folder / name
                path.write_bytes(sample("v.mp4").read_bytes())
                self.assertEqual(media.probe(path).video, "mpeg4")

    def test_ffreport_writes_nothing(self):
        with mock.patch.dict(os.environ, {"FFREPORT": f"file={self.folder}/report.log"}):
            media.probe(sample("v.mp4"))
        self.assertFalse((self.folder / "report.log").exists())

    @needs_pillow
    def test_eps_is_refused_and_ghostscript_never_runs(self):
        marker = self.folder / "gs-ran"
        gs = write(self.folder, "gs", f"#!/bin/sh\ntouch {marker}\n".encode())
        gs.chmod(0o755)
        eps = write(self.folder, "x.eps", b"%!PS-Adobe-3.0 EPSF-3.0\n%%BoundingBox: 0 0 10 10\nshowpage\n")
        with mock.patch.dict(os.environ, {"PATH": f"{self.folder}{os.pathsep}{os.environ['PATH']}"}):
            with self.assertRaises(media.UnsafeError):
                media.probe(eps)
            with self.assertRaises(media.UnsafeError):
                media.convert(eps, dest=self.folder / "x.png")
        self.assertFalse(marker.exists())

    @needs_pillow
    def test_bombs(self):
        import struct
        import zlib

        def chunk(kind: bytes, data: bytes) -> bytes:
            return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))

        header = struct.pack(">IIBBBBB", 20_000, 20_000, 8, 2, 0, 0, 0)
        png = b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", header) + chunk(b"IDAT", zlib.compress(b"\0" * 100)) + chunk(
            b"IEND", b"")
        with self.assertRaises(media.UnsafeError):
            media.probe(write(self.folder, "bomb.png", png))
        frames = [Image.new("P", (4000, 4000), index) for index in range(14)]
        frames[0].save(self.folder / "bomb.gif", save_all=True, append_images=frames[1:])
        with self.assertRaises(media.UnsafeError):
            media.probe(self.folder / "bomb.gif")

    @needs_pillow
    def test_truncated_images_stay_refused(self):
        from PIL import ImageFile

        media.probe(sample("exif.jpg"))
        media.convert(sample("exif.jpg"), dest=self.folder / "x.png")
        self.assertFalse(ImageFile.LOAD_TRUNCATED_IMAGES)

    def test_interrupt_leaves_no_part_and_no_child(self):
        def interrupt(*args, **kwargs):
            raise KeyboardInterrupt

        dest = self.folder / "out.mp4"
        with mock.patch.object(_ffmpeg, "encode", side_effect=interrupt), self.assertRaises(KeyboardInterrupt):
            media.compress(sample("v.mp4"), dest=dest, progress=lambda *_: None)
        self.assertEqual(parts(self.folder), [])
        self.assertFalse(dest.exists())

    def test_interrupt_during_a_real_encode(self):
        dest = self.folder / "real.mp4"
        calls = []

        def progress(done, total):
            calls.append(done)
            raise KeyboardInterrupt

        with self.assertRaises(KeyboardInterrupt):
            media.compress(sample("big.mp4"), dest=dest, quality=10, progress=progress)
        self.assertEqual(parts(self.folder), [])
        children = subprocess.run(["pgrep", "-P", str(os.getpid())], capture_output=True, text=True).stdout.split()
        self.assertEqual(children, [])


if __name__ == "__main__":
    unittest.main()
