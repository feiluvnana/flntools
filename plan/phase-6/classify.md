# classify

Phase 6, step 7, after [scripts](scripts.md) and [dates](dates.md). The user asked on 2026-11-12 for a classifier: `media.classify(path)` tells what kind of photo or video a file is, using a small but accurate model, and flnd puts the categories at the start of the file name.

```python
from shared import media

media.classify("2026-01-02_ABC123.jpg")           # [("food", 0.71)]
media.classify("clip.mp4", top=2)                  # [("pet", 0.64), ("nature", 0.22)]
```

```
flnd download ins:example_user --classify
  posts/food_2026-01-02_ABC123.jpg
  posts/pet+nature_2026-01-05_DEF456_0.jpg
  reels/other_2026-01-07_GHI789.mp4
```

## The model

**Zero-shot image–text matching (CLIP style), image tower only, run with ONNX Runtime.** Why this kind:
- **Its categories are words.** A CLIP model scores an image against text, so the category list is a list of labels, not a fixed set trained in. Changing the list needs no new model.
- **It is small enough to run on a laptop's CPU.** Only the image tower runs at classify time. The labels' text embeddings are computed once, when the list is written, and shipped as a 25 KB file next to the code. So there is no tokenizer, no text model and no PyTorch at run time.
- **ONNX Runtime** is one wheel of about 15 MB for every platform flnd supports, with NumPy. It needs no GPU, and uses one thread per core.

**The model (decided 2026-11-12): MobileCLIP-S0, or OpenCLIP ViT-B/32 if MobileCLIP's licence doesn't allow flnd's use.** The licence is checked before building:

| Model | Image tower | ImageNet zero-shot | Licence |
| --- | --- | --- | --- |
| **MobileCLIP-S0** (Apple, 2024) | about 11 M parameters; about 45 MB as fp32 ONNX, about 12 MB int8 | about 68% | to check: Apple's model licence may restrict use |
| OpenCLIP ViT-B/32 (LAION-2B) | about 88 M parameters; about 90 MB int8 | about 66% | MIT code, weights openly released |

MobileCLIP-S0 is both smaller and more accurate, so it's the pick if its licence allows flnd's use. Otherwise it's ViT-B/32. The numbers above are from the models' papers and cards and are rechecked before choosing. Either way it's about 10–50 ms per photo on a recent laptop CPU, which is small next to the download itself.

**The model file is downloaded on first use, after asking**, the same way `browser.connect` asks before downloading Chromium ([cli › Chrome only](cli.md#chrome-only)):
- In a terminal, the first `media.classify` asks once, with `prompt.confirm`: `media.classify needs its model (MobileCLIP-S0, about 12 MB). Download it now? [y/N]`.
- On yes, it downloads into the user's cache folder (`~/.cache/flntools/models/`, or `%LOCALAPPDATA%\flntools\models` on Windows), from a URL pinned to one revision. The file is checked against a SHA-256 kept in the code, under a `log.progress` row. Later runs find it and don't ask.
- On no, or without a terminal, it raises `RuntimeError("media.classify needs its model; run once in a terminal to download it")`. flnd then stops classifying for the run, with one warning, and keeps every file saved.
- Both downloads share one helper in `shared`, a private `_fetch_once(name, url, sha256, size, question)` (about 30 lines): ask, download with progress, check, and move into place. So there is one way to fetch a large file on first use.
- So `uv sync` stays quick ([setup](setup.md)), and a run that never classifies downloads nothing.

## `media.classify`

```python
def classify(path: PathLike, *, labels: Sequence[str] = LABELS, top: int = 2,
             threshold: float = 0.25) -> list[tuple[str, float]]:
    """The labels that best describe a photo or video, best first, with their scores."""
```

- **Photos** are decoded with Pillow, as `media` already does, resized to the model's input size, and scored once.
- **Videos and animated images** are scored on three frames, at 10%, 50% and 90% of their length (`media.video.frames`), and the three scores are averaged. So a video that is mostly one thing isn't labelled by its first second.
- **Scores** are the softmax over the labels, so they add up to 1. Up to `top` labels at or above `threshold` are returned. When none reaches it, `[("other", score)]` is returned.
- **`labels`** must be names in `LABELS`, whose embeddings are shipped. A label that isn't raises `ValueError`. Free-text labels need the text tower and a tokenizer, so they are [deferred](#deferred).
- **Errors:**
  - a file that isn't a photo or video raises `media.UnsafeError`, as `media.check` does;
  - a missing `classify` extra raises `ImportError("shared.media.classify needs onnxruntime: pip install 'flntools[classify]'")`;
  - a model download that's declined, or that fails, raises `RuntimeError`, with the URL when it failed.
- **Threads:** one model session is loaded on first use and shared, behind a lock. ONNX Runtime already uses every core for one image, so classifying files one at a time is the fastest way.

### `LABELS`

The labels a downloader's user most often wants to sort by, each with the prompt its embedding was made from (`"a photo of food"`):

`selfie`, `portrait`, `group`, `food`, `pet`, `animal`, `nature`, `city`, `travel`, `screenshot`, `document`, `meme`, `art`, `fashion`, `sport`, `car`, `nsfw`.

**`nsfw` is a label too** (decided 2026-11-12). A CLIP model spots it reasonably but not reliably, so in a file name it is a hint, not a filter. It uses a higher `threshold` of its own (0.5), so a beach photo isn't marked by a weak score.

`scripts/dev.py labels` rebuilds `shared/_labels.npy` from `LABELS` with the model's text tower. It is the only time the text tower runs, on a developer's machine.

### Files and budget

- `shared/_classify.py`, about 130 lines of code: model download and check, preprocessing, frames and scoring. It's part of the `media` module and loads lazily like `media.image`.
- `shared/_labels.npy`: the label embeddings, about 25 KB.
- The `classify` extra is `onnxruntime>=1.20` and `numpy>=2`.
- `media.video.frames` stays: `classify` is its caller ([api › media](api.md#media-1098-lines-in-six-files)).

## flnd

**`--classify`**, a sixth option on `download` and `sync` ([cli](cli.md#download)):
- After each photo or video is saved (and compressed, when asked), the engine classifies it and renames it to `<labels>_<YYYY-MM-DD>_<name>`, with the labels joined by `+`, such as `pet+nature_`.
- **The history key doesn't change**, so a renamed file is never downloaded again. The row records the labels.
- Classifying runs in its own one-at-a-time pool next to the video encodes, so downloads never wait for it.
- A `download` that turns `--classify` on for a target classifies its files already saved, too; `flnd sync` classifies only the new ones ([cli › sync](cli.md#sync)).
- `--classify` is saved with the target, like `--image`, and `--classify off` turns it off ([cli › Saved settings](cli.md#saved-settings)).
- A missing extra or a failed model download stops classifying for the rest of the run, with one warning (`classify stops for this run: …`). Files stay saved and unlabelled.

**`classify` is an extra, offered when it's first needed** (decided 2026-11-12), the same way as Chrome and the model:
- In a terminal, the first `media.classify` without ONNX Runtime and NumPy asks once: `media.classify needs onnxruntime and numpy (about 30 MB). Install them now? [y/N]`.
- On yes, it installs `flntools[classify]` into the running Python: `uv pip install --python <sys.executable>` when uv is on `PATH`, else `<sys.executable> -m pip install`. Then it imports them and goes on, to the model question.
- On no, or without a terminal, it raises the usual missing-extra `ImportError` (`pip install 'flntools[classify]'`), and flnd stops classifying for the run with one warning.
- **A bundled flnd** (`make dist`) can't install packages into itself, so the bundle is built with the `classify` extra included. Only the model is downloaded, after asking.
- `setup` installs the extra for development, so its tests run.

## Deferred

- **Free-text labels** (`--classify selfie,beach,my cat`): need the text tower and CLIP's tokenizer at run time, about 60 MB more and a tokenizer of about 80 lines.
- **Sorting into folders by label** instead of name prefixes.
- **Faces and people by name.**

## Tests

- `tests/test_classify.py` runs a tiny ONNX model built in the test (a fixed linear layer, written with `onnx`'s helpers in a dev-only fixture), so the tests check the plumbing, not the real model's judgement:
  - preprocessing size and normalization;
  - three frames for a video, averaged;
  - `top` and `threshold`, with `other` when nothing reaches it;
  - an unknown label raises `ValueError`;
  - a SHA-256 mismatch on the model file raises `RuntimeError` and deletes the file.
- They are skipped without the `classify` extra.
- flnd: `--classify` renames a saved photo to `food_<day>_<name>`, keeps its history key, and records the labels. A failed classify leaves the file saved with one warning.
- Once, live and by hand: a folder of about 50 real photos from the test account, labelled, with the results looked over by the user.
