"""Sample photos, videos and audio for the media tests, generated on first use into one temp folder.

Videos and audio are made by calling ffmpeg directly, with lavfi sources that media's whitelist
rightly refuses. Each sample is made once per test run and shared by every media test file.
"""

import atexit
import functools
import hashlib
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

FOLDER = Path(tempfile.mkdtemp(prefix="flntools-media-"))
atexit.register(shutil.rmtree, FOLDER, True)
HEIC = Path(__file__).resolve().parent / "data" / "tiny.heic"
HEIC_SHA256 = "d478e3a322a7a0307ad423ce893405820898c3e890d21ece8f5ec3d0e70f460a"

try:
    from PIL import Image, ImageCms
except ImportError:
    Image = ImageCms = None


@functools.cache
def ffmpeg() -> str | None:
    try:
        import imageio_ffmpeg

        return imageio_ffmpeg.get_ffmpeg_exe()
    except (ImportError, RuntimeError):
        return shutil.which("ffmpeg")


@functools.cache
def encoders() -> frozenset[str]:
    if not ffmpeg():
        return frozenset()
    text = subprocess.run([ffmpeg(), "-hide_banner", "-encoders"], capture_output=True, text=True).stdout
    return frozenset(line.split()[1] for line in text.splitlines() if line.startswith(" ") and len(line.split()) > 1)


def needs_pillow(case):
    return unittest.skipIf(Image is None, "Pillow is not installed")(case)


def needs_ffmpeg(*wanted: str):
    missing = [name for name in wanted if name not in encoders()]
    return unittest.skipIf(not ffmpeg() or missing, f"ffmpeg or its {', '.join(missing)} encoder is missing")


def fresh(name: str) -> Path:
    """A copy of the sample `name` in a new folder, for a test that changes or replaces it."""
    folder = Path(tempfile.mkdtemp(dir=FOLDER))
    return Path(shutil.copy(sample(name), folder / name))


def run(*args: str) -> None:
    assert ffmpeg(), "ffmpeg is needed for this sample"
    subprocess.run([ffmpeg(), "-hide_banner", "-v", "error", "-y", *args], check=True, capture_output=True)


TONE = ("-f", "lavfi", "-i", "sine=frequency=440:sample_rate=44100:duration=1")
PICTURE = ("-f", "lavfi", "-i", "testsrc=size=64x48:rate=10:duration=1")


@functools.cache
def sample(name: str) -> Path:
    """The path of a generated sample: see the table in plan/phase-5/media.md › Tests."""
    path = FOLDER / name
    maker = MAKERS.get(name) or (lambda target: _other_container(target))
    maker(path)
    return path


@functools.cache
def srgb_profile() -> bytes:
    return ImageCms.ImageCmsProfile(ImageCms.createProfile("sRGB")).tobytes()


@functools.cache
def wide_profile() -> bytes:
    """An ICC profile that isn't sRGB, standing in for a phone's Display P3."""
    return srgb_profile().replace("sRGB".encode("utf-16-be"), "P3D6".encode("utf-16-be"))


def _exif_jpeg(path: Path) -> None:
    image = Image.new("RGB", (80, 40), "red")
    image.paste((0, 0, 255), (0, 0, 40, 40))
    exif = Image.Exif()
    exif[0x0112] = 6
    exif[0x0110] = "Pixel 99"
    exif[0x0132] = "2024:01:01 00:00:00"
    block = exif.get_ifd(0x8769)
    block[0x9003] = "2024:05:01 10:30:00"
    block[0x9011] = "+09:00"
    block[0x927C] = b"maker secrets"
    gps = exif.get_ifd(0x8825)
    gps.update({1: "N", 2: (48.0, 51.0, 29.52), 3: "E", 4: (2.0, 17.0, 40.2)})
    image.save(path, "JPEG", exif=exif.tobytes(), icc_profile=wide_profile(), quality=95,
               xmp=b"<x:xmpmeta xmlns:x='adobe:ns:meta/'>secret</x:xmpmeta>")


def _zero_jpeg(path: Path) -> None:
    exif = Image.Exif()
    exif.get_ifd(0x8769)[0x9003] = "0000:00:00 00:00:00"
    exif.get_ifd(0x8825).update({1: "N", 2: (0.0, 0.0, 0.0), 3: "E", 4: (0.0, 0.0, 0.0)})
    Image.new("RGB", (8, 8)).save(path, "JPEG", exif=exif.tobytes())


def _big_jpeg(path: Path) -> None:
    noise = Image.effect_noise((4000, 3000), 40).convert("RGB")
    noise.save(path, "JPEG", quality=95)


def _animation(path: Path) -> None:
    frames = [Image.new("RGBA", (40, 30), color) for color in ("red", "green", "blue", "white")]
    frames[0].save(path, save_all=True, append_images=frames[1:], duration=[100, 200, 100, 100], loop=0)


def _noise_animation(path: Path) -> None:
    frames = [Image.effect_noise((200, 150), 60).convert("RGB") for _ in range(4)]
    frames[0].save(path, save_all=True, append_images=frames[1:], duration=100, loop=0)


def _heic(path: Path) -> None:
    assert hashlib.sha256(HEIC.read_bytes()).hexdigest() == HEIC_SHA256, "tests/data/tiny.heic changed"
    shutil.copy(HEIC, path)


def _meta(path: Path) -> None:
    chapters = FOLDER / "chapters.txt"
    chapters.write_text(";FFMETADATA1\n[CHAPTER]\nTIMEBASE=1/1000\nSTART=0\nEND=500\ntitle=one\n")
    subtitles = FOLDER / "subtitles.srt"
    subtitles.write_text("1\n00:00:00,000 --> 00:00:00,900\nhello\n")
    plain = FOLDER / "meta-plain.mp4"
    run(*PICTURE, *TONE, "-i", str(chapters), "-i", str(subtitles), "-map", "0", "-map", "1", "-map", "3",
        "-map_chapters", "2", "-c:v", "mpeg4", "-c:a", "aac", "-c:s", "mov_text", str(plain))
    # ffmpeg drops creation_time when it copies metadata, so it is set again on the second pass.
    run("-display_rotation", "90", "-i", str(plain), "-map", "0", "-c", "copy", "-metadata", "title=hi\nStream #0:5: "
        "Video: h264, 9999x9999", "-metadata", "creation_time=2024-05-01T10:00:00.000000Z", "-metadata",
        "location=+48.8577+002.2950/", "-metadata", "encoder=Sample Encoder", str(path))


def _other_container(path: Path) -> None:
    audio_codec = {".avi": "libmp3lame", ".flv": "aac", ".wmv": "wmav2", ".ogv": "flac", ".mpg": "mp2",
                   ".ts": "aac"}[path.suffix]
    video_codec = {".flv": "flv", ".wmv": "wmv2", ".ogv": "libtheora", ".mpg": "mpeg2video"}.get(path.suffix, "mpeg4")
    if audio_codec not in encoders():
        audio_codec = "mp2"
    run(*PICTURE, *TONE, "-c:v", video_codec, "-r", "25", "-c:a", audio_codec, str(path))


def _html(path: Path) -> None:
    path.write_bytes(b"\xef\xbb\xbf  <!doctype html><title>not found</title>")


MAKERS = {
    "exif.jpg": _exif_jpeg,
    "zero.jpg": _zero_jpeg,
    "srgb.jpg": lambda path: Image.new("RGB", (32, 32), "green").save(path, "JPEG", icc_profile=srgb_profile()),
    "rgba.png": lambda path: Image.new("RGBA", (32, 32), (255, 0, 0, 0)).save(path),
    "small.png": lambda path: Image.new("RGB", (100, 50), "blue").save(path, optimize=True),
    "noise.gif": lambda path: _noise_animation(path),
    "big.jpg": _big_jpeg,
    "anim.webp": _animation, "anim.gif": _animation, "anim.png": _animation,
    "tiny.heic": _heic,
    "page.html": _html,
    "v.mp4": lambda path: run(*PICTURE, "-c:v", "mpeg4", str(path)),
    "a.m4a": lambda path: run(*TONE, "-c:a", "aac", str(path)),
    "av.mkv": lambda path: run(*PICTURE, *TONE, "-c:v", "mpeg4", "-c:a", "aac", str(path)),
    "av.mp4": lambda path: run(*PICTURE, *TONE, "-c:v", "mpeg4", "-c:a", "aac", str(path)),
    "meta.mp4": _meta,
    "big.mp4": lambda path: run("-f", "lavfi", "-i", "testsrc2=size=360x640:rate=60:duration=3", "-f", "lavfi", "-i",
                                "sine=frequency=300:duration=3", "-c:v", "mpeg4", "-q:v", "2", "-c:a", "aac",
                                "-b:a", "192k", str(path)),
    "wide.mp4": lambda path: run("-f", "lavfi", "-i", "testsrc=size=96x48:rate=10:duration=1", "-c:v", "mpeg4",
                                 str(path)),
    "vp9.webm": lambda path: run(*PICTURE, *TONE, "-c:v", "libvpx-vp9", "-c:a", "libvorbis", str(path)),
    "tone.flac": lambda path: run(*TONE, "-c:a", "flac", str(path)),
    "tone.wav": lambda path: run(*TONE, str(path)),
    "tone.aiff": lambda path: run(*TONE, str(path)),
    "tone.mp3": lambda path: run(*TONE, "-c:a", "libmp3lame", "-b:a", "192k", "-metadata", "title=Song",
                                 "-metadata", "artist=Someone", str(path)),
    "tone.ogg": lambda path: run(*TONE, "-c:a", "libvorbis", str(path)),
}
