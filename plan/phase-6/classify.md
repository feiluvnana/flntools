# classify

Phase 6, step 7, after [scripts](scripts.md) and [dates](dates.md). The user asked on 2026-11-12 for a classifier: `media.classify(path)` tells what kind of photo or video a file is, using a small but accurate model, and flnd groups the files by category in link folders, without renaming them.

```python
from shared import media

media.classify("2026-01-02_ABC123.jpg")           # [("food", 0.71)]
media.classify("clip.mp4", top=2)                  # [("pet", 0.64), ("nature", 0.22)]
```

```
flnd download ins:example_user --classify
  posts/2026-01-02_ABC123.jpg                  the files keep their names and places
  posts/2026-01-05_DEF456_0.jpg
  labels/food/2026-01-02_ABC123.jpg            links to them, grouped by label
  labels/pet/2026-01-05_DEF456_0.jpg
  labels/nature/2026-01-05_DEF456_0.jpg        a file with two labels is in both
  labels/other/2026-01-07_GHI789.mp4
```

## The model

**Zero-shot image–text matching (CLIP style), image tower only, run with ONNX Runtime.** Why this kind:
- **Its categories are words.** A CLIP model scores an image against text, so the category list is a list of labels, not a fixed set trained in. Changing the list needs no new model.
- **It is small enough to run on a laptop's CPU.** Only the image tower runs at classify time. The labels' text embeddings are computed once, when the list is written, and shipped as a 25 KB file next to the code. So there is no tokenizer, no text model and no PyTorch at run time.
- **ONNX Runtime** is one wheel of about 15 MB for every platform flnd supports, with NumPy. It needs no GPU, and uses one thread per core.

**The model (decided 2026-11-12): MobileCLIP-S0, or OpenCLIP ViT-B/32 if MobileCLIP's licence doesn't allow flnd's use.** The licence was checked before building, on 2026-11-12, and **it is OpenCLIP ViT-B/32**: see [Licence and model file](#licence-and-model-file-checked-2026-11-12). The table as it was planned:

| Model | Image tower | ImageNet zero-shot | Licence |
| --- | --- | --- | --- |
| **MobileCLIP-S0** (Apple, 2024) | about 11 M parameters; about 45 MB as fp32 ONNX, about 12 MB int8 | about 68% | to check: Apple's model licence may restrict use |
| OpenCLIP ViT-B/32 (LAION-2B) | about 88 M parameters; about 90 MB int8 | about 66% | MIT code, weights openly released |

MobileCLIP-S0 is both smaller and more accurate, so it was the pick if its licence allowed flnd's use. It doesn't, so it's ViT-B/32. The numbers above are from the models' papers and cards and are rechecked before choosing. Either way it's about 10–50 ms per photo on a recent laptop CPU, which is small next to the download itself.

### Licence and model file (checked 2026-11-12)

**MobileCLIP-S0 can't be used.** Apple's [ml-mobileclip](https://github.com/apple/ml-mobileclip) releases its code under MIT ([LICENSE](https://github.com/apple/ml-mobileclip/blob/main/LICENSE)), but the weights under the **Apple Machine Learning Research Model License Agreement** ([LICENSE_MODELS](https://github.com/apple/ml-mobileclip/blob/main/LICENSE_MODELS)), and its data under CC-BY-NC-ND ([LICENSE_DATA](https://github.com/apple/ml-mobileclip/blob/main/LICENSE_DATA)). The model licence grants use "exclusively for Research Purposes", defined as "non-commercial scientific research and academic development activities". Sorting one's own downloads in a personal CLI is non-commercial but isn't research, so it isn't covered, and an ONNX export of those weights is a Model Derivative under the same terms. Bundling or offering it would be unclear at best, so the plan's fallback applies.

**OpenCLIP ViT-B/32, trained on LAION-2B (`laion2b_s34b_b79k`), is used.** Its licences:
- the code that trained it, [open_clip](https://github.com/mlfoundations/open_clip): MIT ([LICENSE](https://github.com/mlfoundations/open_clip/blob/main/LICENSE));
- the weights, [laion/CLIP-ViT-B-32-laion2B-s34B-b79K](https://huggingface.co/laion/CLIP-ViT-B-32-laion2B-s34B-b79K): MIT, as its model card declares;
- the ONNX export used, [onnx-community/CLIP-ViT-B-32-laion2B-s34B-b79K-ONNX](https://huggingface.co/onnx-community/CLIP-ViT-B-32-laion2B-s34B-b79K-ONNX): MIT, as its card declares, made by Hugging Face's ONNX conversion space from the weights above.

MIT lets flnd download and run it, and lets the bundle carry the `classify` extra (ONNX Runtime is MIT, NumPy BSD; their licences are in `licenses/`). The model is never bundled: it is downloaded from its own repository. LAION's model card, like OpenAI's, calls any deployed use "out of scope" without testing on a fixed class taxonomy: a recommendation, not a licence term, and flnd's labels are such a fixed list, used as hints for folders, never to decide anything about a person.

**The file**, pinned in `shared/_classify.py` (`MODEL`):
- URL, at one revision: `https://huggingface.co/onnx-community/CLIP-ViT-B-32-laion2B-s34B-b79K-ONNX/resolve/de693d0c5a5b263ae94ece9bf751e0cda14305d8/onnx/model_quantized.onnx`
- SHA-256 `2ac8410fe198fbd003cd97460bdd68653d99c33f53311881cd306190067aee9b`, 152,708,043 bytes (int8, dynamically quantized). It is kept as `clip-vit-b-32-laion2b-int8.onnx` in the cache folder.
- The graph, checked by hand once: inputs `input_ids` and `attention_mask` (int64, [batch, tokens]) and `pixel_values` (float32, [batch, 3, 224, 224]); outputs `image_embeds` and `text_embeds` ([batch, 512], already L2-normalized), with `logits_per_image` 100 times their cosine. A generated page of black text on white came out `document` 0.39, `screenshot` 0.38; a test pattern video `screenshot` 0.997; a photo takes about 12 ms, loading about 0.9 s with the hash check.
- Why this file: no vision-only int8 export of the LAION weights exists from a known publisher. Immich's [vision-only export](https://huggingface.co/immich-app/ViT-B-32__laion2b-s34b-b79k) is fp32, 352 MB. This one holds both towers in 153 MB, and the image runs feed its text side the empty prompt (two tokens), which costs nothing measurable. The text side also makes the label embeddings at load (below), so no second file is needed.

**What changed from the plan because of it:**
- **No `_labels.npy` and no `scripts/dev.py labels`.** Each label's prompt is kept in `_classify.PROMPTS` already tokenized, with the text beside it in a comment (made once with the repository's `tokenizer.json`); when the model loads, one run of its text tower turns them into embeddings for the process. So no binary file ships, nothing can drift from the model, and no tokenizer is needed at run time.
- The download is about 150 MB, not 12 MB, so the question says so.

**The model file is downloaded on first use, after asking**, the same way `browser.connect` asks before downloading Chromium ([cli › Chrome only](cli.md#chrome-only)):
- In a terminal, the first `Media.labels` asks once, with `prompt.confirm`: `Media.labels needs its model (OpenCLIP ViT-B/32, about 153 MB). Download it now?`.
- On yes, it downloads into the user's cache folder (`$XDG_CACHE_HOME` or `~/.cache`, then `flntools/models/`, or `%LOCALAPPDATA%\flntools\models` on Windows), from the URL pinned to one revision, through `http.download` (resumable, under its progress row). The file is checked against the SHA-256 kept in the code before it is renamed into place; a mismatch deletes it and raises. It is checked again at each process's first load, and a cached file that changed is deleted and asked for again. Later runs find it and don't ask.
- On no, or without a terminal (nothing is asked then), it raises `RuntimeError`: "Media.labels needs its model; run it once in a terminal to download it", or "…its download was declined". A no and a failure are kept for the process. flnd then stops classifying for the run, with one warning, and keeps every file saved.
- The download is written inside `_classify.py` ([simplify](simplify.md) §6: `_fetch_once` had one caller).
- So `uv sync` stays quick ([setup](setup.md)), and a run that never classifies downloads nothing.

## `media.classify`

Built as **`Media.labels(*, top=3, threshold=0.25, choices=media.LABELS)`** ([redesign › media](redesign.md#media-a-fluent-media-object)): a read, so a noun, on the opened file. The signature below is the plan's first form; the rules under it hold, with `choices` for `labels` and `top=3`. An audio file raises `media.KindError`. The code is `shared/_classify.py`, loaded on first use.

```python
def classify(path: PathLike, *, labels: Sequence[str] = LABELS, top: int = 2,
             threshold: float = 0.25) -> list[tuple[str, float]]:
    """The labels that best describe a photo or video, best first, with their scores."""
```

- **Photos** are decoded with Pillow, as `media` already does, resized to the model's input size, and scored once.
- **Videos and animated images** are scored on three frames, at 10%, 50% and 90% of their length (`media.video.frames`), and the three scores are averaged. So a video that is mostly one thing isn't labelled by its first second.
- **Scores** are the softmax over the labels, so they add up to 1. Up to `top` labels at or above `threshold` are returned. When none reaches it, `[("other", score)]` is returned.
- **`labels`** (`choices`) must be names in `LABELS`, whose prompts are shipped. A label that isn't raises `ValueError`. Free-text labels need the text tower and a tokenizer, so they are [deferred](#deferred).
- **Errors:**
  - a file that isn't a photo or video raises `media.UnsafeError`, as `media.check` does;
  - a missing `classify` extra raises `ImportError("Media.labels needs onnxruntime and numpy: pip install 'flntools[classify]'")`;
  - a model download that's declined, or that fails, raises `RuntimeError`, with the URL when it failed.
- **Threads:** one model session is loaded on first use and shared, behind a lock. ONNX Runtime already uses every core for one image, so classifying files one at a time is the fastest way.

### `LABELS`

The labels a downloader's user most often wants to sort by, each with the prompt its embedding was made from (`"a photo of food"`):

`selfie`, `portrait`, `group`, `food`, `pet`, `animal`, `nature`, `city`, `travel`, `screenshot`, `document`, `meme`, `art`, `fashion`, `sport`, `car`, `nsfw`.

**`nsfw` is a label too** (decided 2026-11-12). A CLIP model spots it reasonably but not reliably, so in a file name it is a hint, not a filter. It uses a higher `threshold` of its own (0.5), so a beach photo isn't marked by a weak score.

The prompts are tokenized in `_classify.PROMPTS` (see [Licence and model file](#licence-and-model-file-checked-2026-11-12)); the text tower turns them into embeddings once per process, at load.

### Files and budget

- `shared/_classify.py`, about 130 lines of code: model download and check, preprocessing, frames and scoring. It's part of the `media` module and loads lazily like `media.image`.
- No label file: the prompts' tokens are in `_classify.py`.
- The `classify` extra is `onnxruntime>=1.20` and `numpy>=2`.
- `media.video.frames` stays: `classify` is its caller ([api › media](api.md#media-1098-lines-in-six-files)).

## flnd

**`--classify`**, a sixth option on `download`, saved for the target and used by `sync` ([cli](cli.md#download)):
- After each photo or video is saved (and compressed, when asked), the engine classifies it and **links it into `labels/<label>/`** under the target's folder, once per label. **The file itself is never renamed or moved** (decided 2026-11-12), so its name, its place and its history row stay as they are. The row records the labels.
- **The link is a hard link**: the same file under a second name. It takes no extra space, opens in any app, and needs no rights on Windows. Where a hard link can't be made (another volume), it is a **symlink**, relative to the link's folder, so the target folder can be moved as a whole. Where neither works (FAT and exFAT have neither), the views are skipped with one warning per run, and the labels are still recorded.
- **The views are rebuilt, never trusted.** `flnd sync` re-creates any link that's missing from the recorded labels, and removes links whose file is gone. Deleting `labels/` costs nothing, and `--classify off` removes it.
- **A link is never the last copy deleted** (coordinator review, 2026-11-12). A hard link is the file itself, so a file whose own name was deleted by hand survives only in `labels/`. Before anything is removed (`sync`, before its repair; every rebuild; `--classify off`), `labels.restore` moves such a link back to the path its history row names, folders made as needed, and the row stays; its other links are then kept or remade as usual. A file counts as held only in `labels/` when its link count equals its names there. Only symlinks whose file is gone, and hard links whose file still has a name elsewhere, are removed. An entry holding a last copy that no row names is kept.
- Two files with the same name in different category folders (`posts/` and `tagged/`) can meet in one label folder. The second gets its category in front: `tagged_2026-01-02_ABC123.jpg`.
- **Copying a target folder elsewhere** copies hard-linked files twice, since most copy tools don't keep hard links. That is noted in README.
- Classifying runs as the `classify` postprocessor, on the thread that runs the file's postprocessing (a download thread, or the encode pool's for a compressed video). Runs share the one model one at a time; a photo takes about 12 ms, small next to its download, so no pool of its own was built (2026-11-12, step 11).
- A `download` given `--classify` classifies the target's files already saved that have no labels yet, then rebuilds its links; given again when it is already on, it labels what an earlier run couldn't (no model then). `flnd sync` classifies only the new ones and rebuilds the links ([cli › sync](cli.md#sync)). A `download` that changes `--image` or `--video` of a classified target rebuilds its links after compressing, since a file compressed into another format has a new name.
- `--classify` is saved with the target, like `--image` (`targets.db`'s `classify`, true or false), and `--classify off` turns it off and deletes `labels/` ([cli › Saved settings](cli.md#saved-settings)). On the command line the word is optional (`shared.cli.opt(choices=("on", "off"), const="on")`): `--classify` takes the next argument only when it is exactly `on` or `off`, so `flnd download --classify ins:example_user` and `flnd download ins:example_user --classify off` both work (coordinator review, 2026-11-12).
- **The model is asked about up front** (coordinator review, 2026-11-12), as Chromium is: when a `download` or `sync` runs any target with `--classify` (given or saved), the engine calls `media.prepare_labels()` before any work. It asks in a terminal, once, with the live rows held, to install the extra and to download the model when either is missing, and loads the model. A no, no terminal, or a failure warns once (`classify is off for this run: …`): the run downloads without classifying, and the saved setting stays, so the next run asks again. The job's mid-run stop stays as a safety net.
- The history row records the labels as `labels: ["food", "pet"]`, best first, `["other"]` when none fits. A file that couldn't be labelled has no `labels` field.
- `labels/` isn't counted in `sync`'s summary: it holds second names of the same files.
- The code is `flnd/common/labels.py` (`Labeller`, `apply`, `rebuild`, `remove`) and `post.classify`.
- A missing extra or a failed model download stops classifying for the rest of the run, with one warning (`classify stops for this run: …`). Files stay saved and unlabelled.

**`classify` is an extra, offered when it's first needed** (decided 2026-11-12), the same way as Chrome and the model:
- In a terminal, the first `media.classify` without ONNX Runtime and NumPy asks once: `media.classify needs onnxruntime and numpy (about 30 MB). Install them now? [y/N]`.
- On yes, it installs the extra's packages into the running Python: `uv pip install --python <sys.executable> "onnxruntime>=1.20" "numpy>=2"` when uv is on `PATH`, else `<sys.executable> -m pip install …`. It names the packages, never `flntools[classify]`, which would install whatever a package index holds under that name (built 2026-11-12; a test keeps the list equal to pyproject's extra). Then it imports them and goes on, to the model question.
- On no, or without a terminal, it raises the usual missing-extra `ImportError` (`pip install 'flntools[classify]'`), and flnd stops classifying for the run with one warning.
- **A bundled flnd** (`make dist`) can't install packages into itself, so the bundle is built with the `classify` extra included. Only the model is downloaded, after asking.
- `setup` installs the extra for development, so its tests run.

## Deferred

- **Free-text labels** (`--classify selfie,beach,my cat`): need the text tower and CLIP's tokenizer at run time, about 60 MB more and a tokenizer of about 80 lines.
- **Tags inside files or the filesystem** (XMP keywords, Finder tags): considered and not wanted (2026-11-12); the link folders are the grouping.
- **Faces and people by name.**

## Tests

- `tests/test_classify.py` fakes ONNX Runtime's session instead (built 2026-11-12: no `onnx` dev dependency), with one embedding axis per label and an image embedding read from the picture's colour, so the tests check the plumbing, not the real model's judgement:
  - preprocessing size and normalization;
  - three frames for a video, averaged;
  - `top` and `threshold`, with `other` when nothing reaches it;
  - an unknown label raises `ValueError`;
  - a SHA-256 mismatch on the model file raises `RuntimeError` and deletes the file.
- They are skipped without the `classify` extra.
- flnd:
  - `--classify` links a saved photo into `labels/food/` with a hard link (same inode), leaves the original's name and place alone, and records the labels;
  - a hard link that fails becomes a relative symlink;
  - on a file system without either, the views are skipped with one warning;
  - `flnd sync` restores a deleted link;
  - `--classify off` removes `labels/`;
  - a failed classify leaves the file saved with one warning.
- Once, live and by hand: a folder of about 50 real photos from the test account, labelled, with the results looked over by the user.
