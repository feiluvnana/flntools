"""`Media.labels` (shared/_classify.py): scoring, frames, thresholds, and the questions before the model and the extra.

The model is never downloaded: `FakeRuntime` stands in for `onnxruntime`, with a session whose
text embeddings are one axis per label and whose image embedding is read from the picture's colour,
so a red photo is "food". `_http.download`, the installer's `subprocess.run`, `prompt.confirm` and
the cache folder are replaced where a test reaches them, and the screen is a fake one, interactive
or not. Pillow and NumPy run for real; the tests are skipped without the `classify` extra.
"""

import shutil
import tempfile
import tomllib
import unittest
from io import StringIO
from pathlib import Path
from unittest import mock

from media_samples import Image, needs_ffmpeg, needs_pillow, sample
from shared import _classify, _http, media, prompt, theme
from shared._theme import Screen

try:
    import numpy
except ImportError:
    numpy = None

ROOT = Path(__file__).resolve().parent.parent
NAMES = list(_classify.PROMPTS)
# The colour a fake picture is filled with, and the labels its fake embedding points at.
COLOURS = {(255, 0, 0): {"food": 1.0}, (0, 255, 0): {"nature": 1.0}, (0, 0, 255): {"pet": 0.6, "animal": 0.8},
           (255, 255, 255): {"document": 0.5, "nsfw": 0.5}}


class FakeSession:
    """`onnxruntime.InferenceSession` for a two-tower CLIP graph, recording every run's inputs."""

    def __init__(self, path, providers=None):
        self.path, self.runs = Path(path), []
        assert self.path.is_file(), "the model is loaded from the cache folder"

    def run(self, outputs, feeds):
        self.runs.append((outputs, feeds))
        if outputs == ["text_embeds"]:
            # One axis per label, in PROMPTS' order: a label's embedding is its own axis.
            count = feeds["input_ids"].shape[0]
            return [numpy.eye(count, len(NAMES), dtype=numpy.float32)]
        rows = []
        for picture in feeds["pixel_values"]:
            # Back from normalized pixels to the colour of the middle pixel.
            pixel = picture[:, 112, 112] * numpy.array(_classify.STD) + numpy.array(_classify.MEAN)
            # The nearest of COLOURS, so a JPEG's few levels of drift don't matter; any other is a selfie.
            colour = min(COLOURS, key=lambda known: sum((a - b * 255) ** 2 for a, b in zip(known, pixel)))
            if sum((a - b * 255) ** 2 for a, b in zip(colour, pixel)) > 30 ** 2:
                colour = None
            row = numpy.zeros(len(NAMES), numpy.float32)
            for name, weight in COLOURS.get(colour, {"selfie": 1.0}).items():
                row[NAMES.index(name)] = weight
            rows.append(row)
        return [numpy.stack(rows)]


class FakeRuntime:
    """The `onnxruntime` module, as far as `_classify` uses it."""

    def __init__(self):
        self.sessions = []

    def InferenceSession(self, path, providers=None):  # noqa: N802 - onnxruntime's name
        session = FakeSession(path, providers)
        self.sessions.append(session)
        return session


def photo(folder, colour, name="a.jpg", size=(320, 240)):
    path = Path(folder) / name
    Image.new("RGB", size, colour).save(path, quality=95)
    return path


@needs_pillow
@unittest.skipIf(numpy is None, "the classify extra is not installed")
class ClassifyCase(unittest.TestCase):
    def setUp(self):
        self.folder = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.folder, True)
        self.cache = self.folder / "cache"
        self.runtime = FakeRuntime()
        self.model = b"a fake model file"
        pinned = _classify.Model("Fake CLIP", "https://models.example/clip.onnx",
                                 _classify.hashlib.sha256(self.model).hexdigest(), len(self.model), "clip.onnx")
        self.patches = [
            mock.patch.object(_classify, "_loaded", None), mock.patch.object(_classify, "_failed", None),
            mock.patch.object(_classify, "_declined", set()), mock.patch.object(_classify, "MODEL", pinned),
            mock.patch.object(_classify, "_folder", lambda: self.cache),
            mock.patch.object(_classify, "_imported", lambda: (numpy, self.runtime)),
        ]
        for patcher in self.patches:
            patcher.start()
            self.addCleanup(patcher.stop)
        self.terminal(False)

    def terminal(self, interactive):
        screen = Screen(dest=StringIO(), inp=StringIO(""), width=200, tty=False, interactive=interactive)
        scope = theme.scope(screen=screen, color="none", unicode=True)
        scope.__enter__()
        self.addCleanup(scope.__exit__, None, None, None)

    def cached(self, data=None):
        """Put a model file in the cache folder, the pinned one unless `data` is given."""
        self.cache.mkdir(parents=True, exist_ok=True)
        (self.cache / "clip.onnx").write_bytes(self.model if data is None else data)


class LabelsTest(ClassifyCase):
    def setUp(self):
        super().setUp()
        self.cached()

    def test_a_photo_gets_its_best_label(self):
        self.assertEqual(media.open(photo(self.folder, (255, 0, 0))).labels(), [("food", 1.0)])
        self.assertEqual(media.open(photo(self.folder, (0, 255, 0), "b.png")).labels(), [("nature", 1.0)])

    def test_preprocessing_is_224_square_normalized_and_one_batch(self):
        media.open(photo(self.folder, (255, 0, 0), size=(1000, 300))).labels()
        [session] = self.runtime.sessions
        text, image = session.runs
        # The labels' texts once, at load; then the empty prompt with the picture.
        self.assertEqual(text[1]["input_ids"].shape[0], len(NAMES))
        self.assertEqual(text[1]["input_ids"][NAMES.index("food")].tolist()[:6], list(_classify.PROMPTS["food"]))
        self.assertEqual(text[1]["attention_mask"][NAMES.index("selfie")].tolist()[:5], [1, 1, 1, 1, 0])
        pixels = image[1]["pixel_values"]
        self.assertEqual((pixels.shape, pixels.dtype), ((1, 3, 224, 224), numpy.float32))
        self.assertEqual(image[1]["input_ids"].tolist(), [list(_classify.EMPTY_TEXT)])
        red = (1.0 - _classify.MEAN[0]) / _classify.STD[0]
        self.assertAlmostEqual(float(pixels[0, 0].mean()), red, places=1)
        self.assertAlmostEqual(float(pixels[0, 1].mean()), -_classify.MEAN[1] / _classify.STD[1], places=1)

    def test_the_model_loads_once_for_every_file(self):
        for colour in ((255, 0, 0), (0, 255, 0)):
            media.open(photo(self.folder, colour)).labels()
        self.assertEqual(len(self.runtime.sessions), 1)

    def test_top_threshold_and_the_scores_over_the_choices(self):
        blue = media.open(photo(self.folder, (0, 0, 255)))
        with mock.patch.object(_classify, "SCALE", 5.0):
            found = blue.labels(top=2, threshold=0.0, choices=("pet", "animal"))
            self.assertEqual([name for name, _ in found], ["animal", "pet"])
            self.assertAlmostEqual(sum(score for _, score in found), 1.0, places=3)
            self.assertEqual(len(blue.labels(top=1, threshold=0.0)), 1)
            # Nothing reaches 0.9: "other", with the best label's score.
            [(name, score)] = blue.labels(threshold=0.9, choices=("pet", "animal"))
            self.assertEqual(name, "other")
            self.assertAlmostEqual(score, found[0][1], places=3)

    def test_nsfw_needs_its_own_higher_threshold(self):
        white = media.open(photo(self.folder, (255, 255, 255)))
        with mock.patch.object(_classify, "SCALE", 1.0):
            # Two even labels of two: 0.5 each, which nsfw's 0.5 lets through.
            self.assertEqual({name for name, _ in white.labels(threshold=0.1, choices=("document", "nsfw"))},
                             {"document", "nsfw"})
            # Among every label, nsfw's score is far below 0.5, even with a threshold of 0.01.
            names = [name for name, _ in white.labels(top=17, threshold=0.01)]
            self.assertIn("document", names)
            self.assertNotIn("nsfw", names)

    @needs_ffmpeg("mpeg4")
    def test_a_video_is_scored_on_three_frames_averaged(self):
        found = media.open(sample("v.mp4")).labels()
        [session] = self.runtime.sessions
        self.assertEqual(session.runs[-1][1]["pixel_values"].shape[0], 3)
        self.assertEqual(len(found), 1)

    def test_an_animation_is_scored_on_three_frames(self):
        media.open(sample("anim.gif")).labels()
        self.assertEqual(self.runtime.sessions[0].runs[-1][1]["pixel_values"].shape[0], 3)

    def test_bad_arguments_and_audio(self):
        clip = media.open(photo(self.folder, (255, 0, 0)))
        for options, message in (({"choices": ("food", "beach")}, "unknown label 'beach'"), ({"choices": ()}, "at least one"),
                                 ({"top": 0}, "top="), ({"threshold": 1.5}, "threshold must be from 0 to 1")):
            with self.subTest(options=options), self.assertRaisesRegex(ValueError, message):
                clip.labels(**options)
        with mock.patch.object(media.Media, "kind", "audio"), self.assertRaisesRegex(media.KindError, "labels is for photos and videos"):
            media.open(photo(self.folder, (0, 0, 0), "c.jpg")).labels()

    def test_the_labels_and_the_extra_match_their_other_homes(self):
        self.assertEqual(tuple(_classify.PROMPTS), media.LABELS)
        for tokens in _classify.PROMPTS.values():
            self.assertEqual((tokens[0], tokens[-1]), _classify.EMPTY_TEXT)
        extras = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]["optional-dependencies"]
        self.assertEqual(tuple(extras["classify"]), _classify.PACKAGES)


class ModelTest(ClassifyCase):
    def fake_download(self, data):
        """An `_http.download` that writes `data` to a part and passes it through `check`, as the real one does."""
        def download(url, dest, *, size=None, check=None, label=None, **options):
            self.downloads.append((url, dest, size))
            part = Path(dest).with_name(".part")
            part.write_bytes(data)
            try:
                check(part)
            except BaseException:
                part.unlink()
                raise
            part.replace(dest)
            return Path(dest)

        self.downloads = []
        return mock.patch.object(_http, "download", download)

    def test_without_a_terminal_nothing_is_asked_and_it_says_how(self):
        with mock.patch.object(prompt, "confirm") as confirm, self.fake_download(self.model):
            with self.assertRaisesRegex(RuntimeError, "run it once in a terminal to download it"):
                media.open(photo(self.folder, (255, 0, 0))).labels()
        confirm.assert_not_called()
        self.assertEqual(self.downloads, [])

    def test_the_download_is_asked_first_and_only_once(self):
        self.terminal(True)
        with mock.patch.object(prompt, "confirm", return_value=True) as confirm, self.fake_download(self.model):
            self.assertEqual(media.open(photo(self.folder, (255, 0, 0))).labels(), [("food", 1.0)])
            media.open(photo(self.folder, (0, 255, 0))).labels()
        confirm.assert_called_once()
        self.assertIn("Fake CLIP", confirm.call_args.args[0])
        [(url, dest, size)] = self.downloads
        self.assertEqual((url, Path(dest), size), ("https://models.example/clip.onnx", self.cache / "clip.onnx", len(self.model)))

    def test_prepare_asks_up_front_and_labels_then_asks_nothing(self):
        self.terminal(True)
        with mock.patch.object(prompt, "confirm", return_value=True) as confirm, self.fake_download(self.model):
            media.prepare_labels()
            confirm.assert_called_once()
            self.assertEqual(len(self.runtime.sessions), 1)
            self.assertEqual(media.open(photo(self.folder, (255, 0, 0))).labels(), [("food", 1.0)])
        confirm.assert_called_once()
        self.assertEqual(len(self.downloads), 1)

    def test_prepare_without_a_terminal_raises_and_asks_nothing(self):
        with mock.patch.object(prompt, "confirm") as confirm, self.assertRaisesRegex(RuntimeError, "in a terminal"):
            media.prepare_labels()
        confirm.assert_not_called()

    def test_a_no_is_kept_for_the_process(self):
        self.terminal(True)
        with mock.patch.object(prompt, "confirm", return_value=False) as confirm, self.fake_download(self.model):
            for _ in range(2):
                with self.assertRaisesRegex(RuntimeError, "download was declined"):
                    media.open(photo(self.folder, (255, 0, 0))).labels()
        confirm.assert_called_once()
        self.assertEqual(self.downloads, [])

    def test_a_sha256_mismatch_is_refused_and_leaves_no_file(self):
        self.terminal(True)
        with mock.patch.object(prompt, "confirm", return_value=True), self.fake_download(b"someone else's model"):
            with self.assertRaisesRegex(RuntimeError, "has SHA-256 [0-9a-f]{64}, not"):
                media.open(photo(self.folder, (255, 0, 0))).labels()
            # The failure is kept: a second call doesn't download the same wrong file again.
            with self.assertRaisesRegex(RuntimeError, "SHA-256"):
                media.open(photo(self.folder, (255, 0, 0))).labels()
        self.assertEqual(list(self.cache.iterdir()), [])
        self.assertEqual(len(self.downloads), 1)
        self.assertEqual(self.runtime.sessions, [])

    def test_a_cached_file_that_changed_is_never_loaded(self):
        self.cached(b"changed on disk")
        with self.assertRaisesRegex(RuntimeError, "run it once in a terminal"):
            media.open(photo(self.folder, (255, 0, 0))).labels()
        self.assertFalse((self.cache / "clip.onnx").exists())
        self.assertEqual(self.runtime.sessions, [])

    def test_a_failed_download_names_the_url(self):
        self.terminal(True)

        def refused(url, dest, **options):
            raise _http.RefusedError("GET https://models.example/clip.onnx: HTTP 404")

        with mock.patch.object(prompt, "confirm", return_value=True), mock.patch.object(_http, "download", refused):
            with self.assertRaisesRegex(RuntimeError, r"wasn't downloaded from https://models\.example/clip\.onnx"):
                media.open(photo(self.folder, (255, 0, 0))).labels()


class ExtraTest(ClassifyCase):
    def setUp(self):
        super().setUp()
        self.cached()
        self.installed = False

        def imported():
            if not self.installed:
                raise ImportError("No module named 'onnxruntime'")
            return numpy, self.runtime

        patcher = mock.patch.object(_classify, "_imported", imported)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_a_missing_extra_names_it_and_asks_nothing_without_a_terminal(self):
        with mock.patch.object(prompt, "confirm") as confirm, \
                self.assertRaisesRegex(ImportError, r"^Media.labels needs onnxruntime and numpy: pip install 'flntools\[classify\]'$"):
            media.open(photo(self.folder, (255, 0, 0))).labels()
        confirm.assert_not_called()

    def test_a_yes_installs_the_packages_not_a_package_named_flntools(self):
        self.terminal(True)

        def install(command, **options):
            self.installed = True
            self.commands.append(command)
            return mock.Mock(returncode=0, stdout="", stderr="")

        self.commands = []
        with mock.patch.object(prompt, "confirm", return_value=True) as confirm, \
                mock.patch.object(_classify.subprocess, "run", install), \
                mock.patch.object(_classify.shutil, "which", return_value="/bin/uv"):
            self.assertEqual(media.open(photo(self.folder, (255, 0, 0))).labels(), [("food", 1.0)])
        self.assertIn("onnxruntime and numpy", confirm.call_args.args[0])
        [command] = self.commands
        self.assertEqual(command[:4], ["/bin/uv", "pip", "install", "--python"])
        self.assertEqual(command[-2:], list(_classify.PACKAGES))
        self.assertFalse(any(part.startswith("flntools") for part in command))

    def test_pip_without_uv_and_a_failed_install(self):
        self.terminal(True)
        failed = mock.Mock(returncode=1, stdout="", stderr="ERROR: no matching distribution\n")
        with mock.patch.object(prompt, "confirm", return_value=True), \
                mock.patch.object(_classify.subprocess, "run", return_value=failed) as run, \
                mock.patch.object(_classify.shutil, "which", return_value=None):
            with self.assertRaisesRegex(ImportError, "the install failed: ERROR: no matching distribution"):
                media.open(photo(self.folder, (255, 0, 0))).labels()
        self.assertEqual(run.call_args.args[0][1:4], ["-m", "pip", "install"])

    def test_a_bundle_never_offers_to_install(self):
        self.terminal(True)
        with mock.patch.object(prompt, "confirm") as confirm, mock.patch.object(_classify.sys, "frozen", True, create=True), \
                self.assertRaisesRegex(ImportError, "flntools\\[classify\\]"):
            media.open(photo(self.folder, (255, 0, 0))).labels()
        confirm.assert_not_called()


if __name__ == "__main__":
    unittest.main()
