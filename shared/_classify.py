"""What `Media.labels` does: name what a photo or video shows, with a CLIP model run by ONNX Runtime. Private: callers use `media`.

How it works: a CLIP model scores a picture against short texts, so the labels are words
(`media.LABELS`), not classes trained in. `labels` reads the picture (one photo, or three frames of
a video or animation, at 10%, 50% and 90% of its length), shrinks each to the model's 224x224
input, and runs the image tower once for them all. Each frame's embedding is compared with every
label's text embedding; the softmax over the labels asked for gives scores that add up to 1, and
a video's frames are averaged. The labels at or above the threshold come back, best first.

The model is OpenCLIP's ViT-B/32 trained on LAION-2B, as an int8 ONNX file of both towers
(plan/phase-6/classify.md says why this one and not MobileCLIP). It is downloaded on first use,
after asking, into the user's cache folder, and checked against the SHA-256 pinned here. The
labels' texts are kept here already tokenized, so no tokenizer is needed: when the model loads,
one run of its text tower turns them into embeddings, kept for the process. The text input the
image runs need is the shortest the graph takes, a start and an end token.

ONNX Runtime and NumPy are the `classify` extra. When they are missing, a terminal is asked once
whether to install them into the running Python; a bundled flnd carries them. This module wraps
ONNX Runtime and NumPy, and has no `.raw`, since `labels` returns plain pairs.

    from shared import media
    media.open("a.jpg").labels()                    # [("food", 0.71)]
    media.open("clip.mp4").labels(top=2)            # [("pet", 0.64), ("nature", 0.22)]

Threads: one model session for the process, loaded on first use under a lock, so two threads never
ask or download twice; runs share it one at a time, since ONNX Runtime already uses every core for one.
"""

from __future__ import annotations

import hashlib
import importlib
import os
import shutil
import subprocess
import sys
import threading
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from shared import _media, _pillow, prompt
from shared._log import log
from shared._theme import theme

# Private: what `Media.labels` calls.
__all__ = ["labels", "prepare"]


@dataclass(frozen=True)
class Model:
    """The model file `labels` downloads: where from, its checks, and the name it is kept under."""

    # Said in the download question.
    name: str
    # Pinned to one revision of the repository, so the file behind it can't change.
    url: str
    # The file's SHA-256, checked after every download and at every load.
    sha256: str
    # Bytes, for the question and the progress total.
    size: int
    # Its file name in the cache folder.
    file: str


# onnx-community's export of laion/CLIP-ViT-B-32-laion2B-s34B-b79K (MIT), dynamically quantized to
# int8, both towers in one graph: inputs `input_ids`, `attention_mask` (text, [batch, tokens], int64)
# and `pixel_values` ([batch, 3, 224, 224], float32); outputs `image_embeds` and `text_embeds`
# ([batch, 512], already L2-normalized). Checked by hand on 2026-11-12 (plan/phase-6/classify.md).
MODEL = Model(
    name="OpenCLIP ViT-B/32",
    url=("https://huggingface.co/onnx-community/CLIP-ViT-B-32-laion2B-s34B-b79K-ONNX/resolve/"
         "de693d0c5a5b263ae94ece9bf751e0cda14305d8/onnx/model_quantized.onnx"),
    sha256="2ac8410fe198fbd003cd97460bdd68653d99c33f53311881cd306190067aee9b",
    size=152_708_043,
    file="clip-vit-b-32-laion2b-int8.onnx",
)

# Each label's prompt, as CLIP's BPE tokenizer (the model repository's tokenizer.json) reads it,
# 49406 and 49407 being its start and end tokens. The text is in the comment; a prompt changed
# here must be tokenized again with that tokenizer. The keys are `media.LABELS`, in its order.
PROMPTS: dict[str, tuple[int, ...]] = {
    "selfie": (49406, 320, 3666, 49407),                                       # a selfie
    "portrait": (49406, 320, 5352, 1125, 539, 320, 2533, 49407),               # a portrait photo of a person
    "group": (49406, 320, 1771, 1125, 539, 1047, 49407),                       # a group photo of people
    "food": (49406, 320, 1125, 539, 1559, 49407),                              # a photo of food
    "pet": (49406, 320, 1125, 539, 320, 3703, 2368, 541, 1929, 49407),         # a photo of a pet cat or dog
    "animal": (49406, 320, 1125, 539, 320, 3220, 4668, 49407),                 # a photo of a wild animal
    "nature": (49406, 320, 1125, 539, 2625, 537, 5727, 49407),                 # a photo of nature and landscape
    "city": (49406, 320, 1125, 539, 320, 1305, 2012, 537, 8866, 49407),        # a photo of a city street and buildings
    "travel": (49406, 320, 1949, 1125, 539, 320, 4096, 15208, 49407),          # a travel photo of a famous landmark
    "screenshot": (49406, 320, 12646, 539, 320, 1951, 541, 11639, 652, 3750, 49407),  # a screenshot of a phone or computer screen
    "document": (49406, 320, 1125, 539, 320, 15121, 593, 4160, 49407),         # a photo of a document with text
    "meme": (49406, 320, 9169, 593, 320, 11327, 49407),                        # a meme with a caption
    "art": (49406, 320, 3610, 541, 320, 3086, 49407),                          # a drawing or a painting
    "fashion": (49406, 320, 2444, 1125, 539, 550, 6525, 49407),                # a fashion photo of an outfit
    "sport": (49406, 320, 1125, 539, 1047, 1629, 2364, 49407),                 # a photo of people playing sport
    "car": (49406, 320, 1125, 539, 320, 1615, 49407),                          # a photo of a car
    "nsfw": (49406, 320, 16630, 541, 23032, 33228, 1125, 49407),               # a nude or sexually explicit photo
}
# The text the image runs feed the graph, which wants one: the empty prompt, start and end.
EMPTY_TEXT = (49406, 49407)
# The model's input side, and the mean and spread of each channel it was trained with (its
# preprocessor_config.json): resize the short side to 224 with bicubic, crop the middle.
SIZE = 224
MEAN = (0.48145466, 0.4578275, 0.40821073)
STD = (0.26862954, 0.26130258, 0.27577711)
# CLIP's learned temperature, exp(logit_scale), which LAION's training clamps at 100: the model's
# own `logits_per_image` is 100 times the cosine.
SCALE = 100.0
# Where in a video or animation the frames are taken, as fractions of its length: three, so one
# that is mostly one thing isn't labelled by its first second.
FRAMES_AT = (0.1, 0.5, 0.9)
# A CLIP model spots nudity reasonably but not reliably, so `nsfw` needs this score at least,
# whatever the threshold, and a beach photo isn't marked by a weak one (decided 2026-11-12).
NSFW, NSFW_THRESHOLD = "nsfw", 0.5
# What comes back when no label reaches the threshold.
OTHER = "other"

# The `classify` extra's packages, as pyproject.toml lists them; what the install offer installs.
PACKAGES = ("onnxruntime>=1.20", "numpy>=2")
MISSING = "Media.labels needs onnxruntime and numpy: pip install 'flntools[classify]'"
INSTALL_QUESTION = "Media.labels needs onnxruntime and numpy (about 30 MB). Install them now?"
MODEL_QUESTION = "Media.labels needs its model ({name}, about {size} MB). Download it now?"
NO_MODEL = "Media.labels needs its model; run it once in a terminal to download it"
DECLINED = "Media.labels needs its model, and its download was declined"

# The loaded model, and what was answered or failed: kept for the process, so a run that labels
# many files asks once. `_loading` makes the import, the questions, the download and the load one
# step: two threads finding no model at once would otherwise both ask and both download.
_loaded: _Session | None = None
_loading = threading.Lock()
_declined: set[str] = set()
_failed: BaseException | None = None


def labels(info: _media.Info, *, top: int, threshold: float, choices: Sequence[str]) -> list[tuple[str, float]]:
    """The labels from `choices` that best describe `info`'s photo, video or animation, best first.

    Args:
        info: the probe of the photo, video or animation to score.
        top: the most labels to return, at least 1.
        threshold: the score, from 0 to 1, a label needs to be returned; `nsfw` needs at least 0.5.
        choices: the labels to score against, each one of `media.LABELS`, with no repeats kept.

    Returns:
        `(label, score)` pairs, best first, scores rounded to 4 places; `[("other", score)]` when none
        reaches the threshold.

    Raises:
        ValueError: a label isn't in `PROMPTS`, `choices` is empty, `top` is below 1, or `threshold`
            is outside 0 to 1.
        BrokenError: the picture, or a video's frames, can't be read.
        ImportError, RuntimeError: the model or its packages are missing, and see `_session`.

    Blocks until the model has loaded, which on first use may ask a question in a terminal and
    download about 150 MB; the scoring itself runs under the session's lock.
    """
    names = list(dict.fromkeys(choices))
    if unknown := [name for name in names if name not in PROMPTS]:
        raise ValueError(f"unknown label {unknown[0]!r}; labels are {', '.join(PROMPTS)}")
    if not names or top < 1:
        raise ValueError("labels needs at least one choice, and top= of 1 or more")
    if not 0.0 <= threshold <= 1.0:
        raise ValueError(f"threshold must be from 0 to 1, not {threshold}")
    # The pictures first: a file that can't be read fails before any question about the model.
    pictures = _pictures(info)
    session = _session()
    scores = session.scores(pictures, names)
    ranked = sorted(zip(names, scores), key=lambda pair: -pair[1])
    kept = [(name, round(score, 4)) for name, score in ranked
            if score >= (max(threshold, NSFW_THRESHOLD) if name == NSFW else threshold)][:top]
    return kept or [(OTHER, round(ranked[0][1], 4))]


def prepare() -> None:
    """Import, ask, install, download and load what `labels` needs, now; see `media.prepare_labels`."""
    _session()


def _pictures(info: _media.Info) -> list[Any]:
    """The RGB Pillow images to score: the photo upright, or a video's or animation's frames at `FRAMES_AT`."""
    if info.kind == "image" and not info.animated:
        photo = _pillow.open(info.path)
        if photo is None:
            raise _media.BrokenError(f"{info.path.name}: not a photo")
        if photo.format == "JPEG":
            # Decoding a 12 MP JPEG at a quarter or an eighth of its size is several times faster,
            # and still far above the 224 pixels the model sees.
            photo.draft("RGB", (SIZE * 2, SIZE * 2))
        return [_pillow.upright(photo, keep=False).convert("RGB")]
    from shared import _media_video

    duration = info.duration or 0.0
    # A stream that doesn't say its length gives its first frame only.
    times = [round(duration * at, 3) for at in FRAMES_AT] if duration > 0 else [0.0]
    pictures = [picture for _, picture in _media_video._pictures(info, times, None, info.fps or 30.0)]
    if not pictures:
        raise _media.BrokenError(f"{info.path.name}: no frame could be read")
    return pictures


class _Session:
    """The loaded model: its ONNX Runtime session and the labels' text embeddings, run one call at a time."""

    def __init__(self, numpy: Any, runtime: Any, path: Path) -> None:
        self.np = numpy
        self.session = runtime.InferenceSession(str(path), providers=["CPUExecutionProvider"])
        # Every run goes through this: ONNX Runtime uses every core for one, so two at once only contend.
        self.lock = threading.Lock()
        ids, mask = self._tokens(list(PROMPTS.values()))
        blank = numpy.zeros((1, 3, SIZE, SIZE), numpy.float32)
        (embeds,) = self.session.run(["text_embeds"], {"input_ids": ids, "attention_mask": mask,
                                                       "pixel_values": blank})
        self.text = dict(zip(PROMPTS, self._unit(embeds)))

    def scores(self, pictures: Sequence[Any], names: Sequence[str]) -> list[float]:
        """Each of `names`' score for the pictures: the softmax over `names`, averaged over the pictures."""
        np = self.np
        batch = np.stack([self._pixels(picture) for picture in pictures])
        ids, mask = self._tokens([EMPTY_TEXT])
        with self.lock:
            (embeds,) = self.session.run(["image_embeds"], {"input_ids": ids, "attention_mask": mask,
                                                            "pixel_values": batch})
        logits = SCALE * self._unit(embeds) @ np.stack([self.text[name] for name in names]).T
        # Softmax per picture, shifted by its largest logit so exp never overflows.
        weights = np.exp(logits - logits.max(axis=1, keepdims=True))
        return [float(score) for score in (weights / weights.sum(axis=1, keepdims=True)).mean(axis=0)]

    def _pixels(self, picture: Any) -> Any:
        """One picture as the model's input: short side 224 (bicubic), middle 224x224, scaled and normalized, CHW."""
        np = self.np
        from PIL import Image

        width, height = picture.size
        ratio = SIZE / min(width, height)
        resized = picture.resize((max(SIZE, round(width * ratio)), max(SIZE, round(height * ratio))),
                                 Image.Resampling.BICUBIC)
        left, upper = (resized.width - SIZE) // 2, (resized.height - SIZE) // 2
        pixels = np.asarray(resized.crop((left, upper, left + SIZE, upper + SIZE)), dtype=np.float32) / 255.0
        normalized = (pixels - np.array(MEAN, np.float32)) / np.array(STD, np.float32)
        return normalized.transpose(2, 0, 1).astype(np.float32)

    def _tokens(self, prompts: Sequence[Sequence[int]]) -> tuple[Any, Any]:
        """Token rows padded with 0 to the longest, and the mask of the real tokens, as the graph's int64 inputs."""
        np = self.np
        ids = np.zeros((len(prompts), max(map(len, prompts))), np.int64)
        mask = np.zeros_like(ids)
        for row, tokens in enumerate(prompts):
            ids[row, :len(tokens)] = tokens
            mask[row, :len(tokens)] = 1
        return ids, mask

    def _unit(self, embeds: Any) -> Any:
        """Rows scaled to length 1: the graph's already are, and a cosine needs them to be."""
        np = self.np
        embeds = np.asarray(embeds, np.float32)
        return embeds / np.maximum(np.linalg.norm(embeds, axis=1, keepdims=True), 1e-12)


def _session() -> _Session:
    """The process's model session, importing, asking, installing and downloading what it needs on first use.

    Raises:
        ImportError: ONNX Runtime or NumPy is missing, and their install was declined, couldn't
            be asked, or failed.
        RuntimeError: the model isn't downloaded, and its download was declined, couldn't be
            asked (no terminal), or failed (with the URL); or the model file doesn't load.
    """
    global _loaded, _failed
    if _loaded is not None:
        return _loaded
    with _loading:
        # Looked at again under the lock: a thread that waited here finds what the first one did.
        if _loaded is not None:
            return _loaded
        if _failed is not None:
            # A failed install or download would most likely fail again, after asking again.
            raise _failed
        numpy, runtime = _runtime()
        path = _model()
        try:
            _loaded = _Session(numpy, runtime, path)
        except Exception as error:
            # ONNX Runtime raises its own classes for a file it can't load; the hash has already
            # passed, so this is a runtime too old for the graph, or no memory.
            _failed = RuntimeError(f"Media.labels couldn't load its model {path}: {error}")
            raise _failed from error
        return _loaded


def _runtime() -> tuple[Any, Any]:
    """NumPy and ONNX Runtime, offering once, in a terminal, to install them when they are missing."""
    try:
        return _imported()
    except ImportError:
        pass
    # A bundled flnd can't install packages into itself, and carries them anyway.
    if getattr(sys, "frozen", False) or "install" in _declined or not theme.screen.interactive:
        raise ImportError(MISSING) from None
    # confirm opens log's question, which holds other threads' live rows until it's answered.
    if not prompt.confirm(INSTALL_QUESTION, default=False):
        _declined.add("install")
        raise ImportError(MISSING) from None
    global _failed
    try:
        _install()
    except ImportError as error:
        _failed = error
        raise
    importlib.invalidate_caches()
    try:
        return _imported()
    except ImportError as error:
        raise ImportError(f"{MISSING} (the install finished, but importing failed: {error})") from None


def _imported() -> tuple[Any, Any]:
    """The `numpy` and `onnxruntime` modules; ImportError when either is missing."""
    import numpy
    import onnxruntime

    return numpy, onnxruntime


def _install() -> None:
    """Install the `classify` extra's packages into the running Python: with uv when it's on PATH, else pip.

    Only the packages are named, at the versions pyproject.toml asks for; never `flntools[classify]`,
    which would fetch whatever a package index holds under that name.

    Raises:
        ImportError: the installer failed, with its last line of output.
    """
    uv = shutil.which("uv")
    command = ([uv, "pip", "install", "--python", sys.executable, *PACKAGES] if uv
               else [sys.executable, "-m", "pip", "install", *PACKAGES])
    with log.progress("Installing onnxruntime and numpy"):
        done = subprocess.run(command, capture_output=True, text=True, encoding="utf-8", errors="replace")
    if done.returncode:
        last = next((line for line in reversed((done.stderr + done.stdout).splitlines()) if line.strip()), "no output")
        raise ImportError(f"{MISSING} (the install failed: {last.strip()})")


def _model() -> Path:
    """The model file in the cache folder, checked, downloading it after asking when it's missing or fails its check.

    Raises:
        RuntimeError: the download was declined or couldn't be asked, or failed.
    """
    global _failed
    path = _folder() / MODEL.file
    if path.is_file():
        if _sha256(path) == MODEL.sha256:
            return path
        # Changed since it was checked (a cut-off copy, a disk error, an edit): never loaded.
        log.warn("{name}: the model file doesn't match its SHA-256, so it is downloaded again", name=path.name)
        path.unlink()
    if "model" in _declined or not theme.screen.interactive:
        raise RuntimeError(DECLINED if "model" in _declined else NO_MODEL)
    if not prompt.confirm(MODEL_QUESTION.format(name=MODEL.name, size=round(MODEL.size / 1e6)), default=False):
        _declined.add("model")
        raise RuntimeError(DECLINED)
    try:
        _download(path)
    except RuntimeError as error:
        _failed = error
        raise
    return path


def _download(path: Path) -> None:
    """Download `MODEL` onto `path` under a progress row, keeping it only when its SHA-256 matches.

    Raises:
        RuntimeError: the download failed (with the URL), or the file isn't the pinned one, which
            is then deleted.
    """
    from shared import _http

    def check(part: Path) -> None:
        found = _sha256(part)
        if found != MODEL.sha256:
            # Not a `BrokenError`, which `download` would retry: the pinned file can't change, so a
            # second try would bring the same wrong bytes.
            raise RuntimeError(f"Media.labels' model from {MODEL.url} has SHA-256 {found}, not {MODEL.sha256}; "
                               "it was deleted")

    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        # Resumable: a Ctrl-C or a dropped connection keeps the part, and the next run's download
        # goes on from it. A part whose check fails is deleted by `download` itself, and only a
        # checked file is renamed onto `path`.
        _http.download(MODEL.url, path, size=MODEL.size, check=check, label=f"Downloading {MODEL.name}")
    except _http.Error as error:
        raise RuntimeError(f"Media.labels' model wasn't downloaded from {MODEL.url}: {error}") from None


def _sha256(path: Path) -> str:
    """The file's SHA-256 in hex, read in 1 MB chunks, so a 150 MB model never sits in memory twice."""
    digest = hashlib.sha256()
    with path.open("rb") as file:
        while chunk := file.read(1 << 20):
            digest.update(chunk)
    return digest.hexdigest()


def _folder() -> Path:
    """Where models are kept: `%LOCALAPPDATA%\\flntools\\models` on Windows, else `$XDG_CACHE_HOME` or `~/.cache`."""
    if sys.platform == "win32" and os.environ.get("LOCALAPPDATA"):
        return Path(os.environ["LOCALAPPDATA"]) / "flntools" / "models"
    return Path(os.environ.get("XDG_CACHE_HOME") or Path.home() / ".cache") / "flntools" / "models"
