# flnd on media

Phase 5, after [media](media.md). This file covers how flnd's engine and sources use `media`, the `pyproject` and packaging changes, the size budget, and what is decided and still open. It builds on the Phase 4 [engine](../phase-4/engine.md): `session.save`, the download pool, and the history.

What changes for someone using flnd:
1. **Saved files get the right extension.** A WebP served for a `.jpg` name is saved as `.webp`, and an HTML error page is never saved as a photo.
2. **Facebook and Instagram videos are saved at their best quality.** Those sites serve it as separate video and audio streams (DASH), which flnd now downloads and joins with `media.video.merge`.
3. **Downloads whose size couldn't be verified are checked** with `media.check`.
4. **`--compress-image` and `--compress-video`** (each `small|balanced|high`) make photos and videos smaller after they are saved, with `media.compress`: re-encoded, capped in resolution, stripped of metadata, in the **same format** unless `--format-image` / `--format-video` names another. They **replace the original**.
5. **`flnd sync`** brings every download folder up to date in one command: it repairs what is broken, fetches new posts, compresses what isn't compressed yet, and prints a summary.

One new command, [`flnd sync`](#flnd-sync). ffmpeg arrives with `pip install`, because `imageio-ffmpeg` is a required dependency of flnd ([ffmpeg](ffmpeg.md)), so there is no `flnd install-ffmpeg`. flnd needs Python 3.11 or later.

## Content decides the extension

In `flnd/net.py`, after a photo or video download finishes and before the `.part` file is renamed, the engine calls `media.sniff(part)` ([the table](media.md#sniff)). It reads 64 bytes and needs no library. Today every source hard-codes `.jpg` or `.mp4`, whatever the CDN sent.

| `sniff` gives | The engine |
| --- | --- |
| a photo or video extension | saves the file with it: `2026-01-02_123.jpg` becomes `2026-01-02_123.webp` when the content is WebP |
| `.html` or `.json` | fails the download with `ValueError("got a web page instead of a photo")`, retried like any failed download |
| `None` | keeps the source's name |

- It runs only for `kind="photo"` and `kind="video"`. Files and archives keep their names; `archive` sniffs for itself.
- **The history row records the saved file name.** The skip check reads the row (key → file) and tests that file, so a `.jpg` saved as `.webp` is skipped on the next run, not downloaded again. The key itself is still `folder/name`, as in Phase 4.

## DASH video and audio

**`session.save(…, audio=None)`.** `audio` is the URL, or list of mirror URLs, of a separate audio stream, for `kind="video"` only. Passing it for another kind is a programming error (`RuntimeError`). When it is given, the engine, in one download-pool task:
1. downloads `source` and `audio` one after the other, to `.name.<random>.video.part` and `.audio.part`, on one progress row sized by both `Content-Length`s;
2. sets the row's caption to `merging`, and runs `media.video.merge([video_part], audio=audio_part, dest=temp_dest)`. `merge` reads every packet, so a truncated part fails here;
3. renames the result into place, deletes both parts, and writes the history row.

`BrokenError` from `merge` counts as a failed download, retried like any other. `RuntimeError` (no ffmpeg, or a codec the container can't hold) fails the file at once, with its message.

**DASH is used automatically**, whenever a manifest is found. There is no flag: flnd always takes the best quality, as it already takes Twitter/X's highest bitrate. A source does:

```python
if streams := dash.best(node.get("dash_manifest")):
    session.save(streams.video, name + streams.extension, kind="video", date=posted, audio=streams.audio)
else:
    session.save(progressive_url, name + ".mp4", kind="video", date=posted)
```

**There is no `can_merge` check.** `imageio-ffmpeg` is a required dependency, so ffmpeg is there on every platform with a wheel. On one without (musl Linux, 32-bit ARM) and with no ffmpeg on `PATH`, `merge` raises its `RuntimeError`. The engine then prints one hint per run, `! fbk: ffmpeg not found; install ffmpeg on your PATH to save Facebook and Instagram videos`, and fails those videos with that message.

**`flnd/dash.py`** (about 40 lines). `best(mpd_xml) -> Streams | None` reads a DASH manifest:
- **Parsing.** It uses the stdlib's `xml.etree.ElementTree`, with the MPD namespace. ElementTree never fetches external entities, and the expat in every supported Python (3.11 and later, expat 2.4.1 or newer) refuses billion-laughs expansion.
- **Choosing.** It picks the video `Representation` with the greatest height, breaking ties by `bandwidth`, and the audio `Representation` with the greatest `bandwidth`.
- **The result.** It returns their `BaseURL`s and an `extension`: `.webm` when both are WebM, otherwise `.mp4`. Opus and VP9 go into MP4 since ffmpeg 4.3.
- **When it gives up.** Only single-file representations are used, which is what Facebook and Instagram serve. A manifest that uses `SegmentTemplate` or `SegmentList`, or that lacks video or audio, returns `None`, and the source falls back to progressive.

`dash.best` is used by the sources, not the engine, because each site hides its manifest in a different place:

| Source | Where the manifest is | Today |
| --- | --- | --- |
| fbk | the video node's DASH manifest, next to `browser_native_hd_url` / `playable_url_quality_hd` | the progressive HD or SD URL, which is sometimes lower than the manifest's best |
| ins | `video_dash_manifest` on video items, next to `video_versions` | `video_versions[0]`, a progressive file |
| twt, tik, tlg, loi | — | no change: Twitter/X and TikTok give progressive MP4s with audio, and Telegram and loi give whole files |

The exact JSON paths are confirmed against saved, anonymized fixtures when each source is changed. No live request is part of the plan or the tests.

## Compressing

Four new [download options](../phase-4/engine.md#download-options), for every source. All are off by default:

| Option | Meaning |
| --- | --- |
| `--compress-image small\|balanced\|high` | after saving each photo, run `media.compress(path, preset=…)`: smaller, metadata stripped, same format. It **replaces the original** when the result is smaller |
| `--compress-video small\|balanced\|high` | the same for each video |
| `--format-image <format>` | write compressed photos in this format instead of their own: `media.compress(…, format=…)` |
| `--format-video <format>` | the same for videos: a container, a codec, or both ([below](#video-formats-and-codecs)) |

`--format-image` takes any still or animated format `media` [writes](media.md#write): `jpg`, `webp`, `avif`, `png`, `gif`, ….

#### Video formats and codecs

`--format-video` takes one of three forms:

| Value | Container | Codec |
| --- | --- | --- |
| a container: `mp4`, `mov`, `mkv`, `webm`, `avi`, `ts`, `3gp`, `gif` | that one | the container's default: H.264, or VP9 for WebM |
| a codec: `h264`, `h265`, `vp9` | the file's own, when it can hold that codec; otherwise MP4 for `h264`/`h265`, WebM for `vp9` | that one |
| both, as `container:codec`: `mkv:h265`, `mp4:h265` | that one | that one; a pair that can't go together (`webm:h265`) is a usage error |

- **H.265** (`--format-video h265`) gives files 30–50% smaller than H.264 at the same quality, but Chrome and Firefox on many machines, and older Windows, can't play them. It is written with the `hvc1` tag, so Apple's players open it. flnd never picks it on its own.
- flnd turns the value into `media.compress(…, format=…, codec=…)`.

- **`--format-image` needs `--compress-image`**, and `--format-video` needs `--compress-video`. Alone, each raises `cli.UsageError("--format-image needs --compress-image")`. flnd has no conversion without compression, as decided when `--jpg` was dropped.
- **Both compress options always replace the original; flnd never keeps both files.** Compression is lossy and strips metadata, and the original's URL may stop working, so a replaced original can't be recovered. Anyone who wants the originals runs without these flags.

**The values are `media`'s [presets](media.md#presets),** with nothing added by flnd:

| Value | Photos | Videos |
| --- | --- | --- |
| `small` | quality 70, shorter side at most 1536 (a 4000×3000 photo becomes 2048×1536) | quality 70 (H.264 CRF 28), at most 720p and 30 fps, audio 64 kb/s |
| `balanced` | quality 80, shorter side at most 2304 (3072×2304) | quality 80 (CRF 23), at most 1080p and 60 fps, audio 128 kb/s |
| `high` | quality 90, no cap | quality 90 (CRF 19), no cap, audio 160 kb/s |

**What happens to each file, as `media.compress` decides it:**
- **The format stays the same** unless `--format-*` is given. JPEG, WebP, AVIF and PNG photos stay in their format (PNG losslessly, only resized and stripped). MP4, MOV and MKV videos stay in their container, re-encoded as H.264 + AAC; WebM stays WebM, as VP9 + Opus. A DASH video is compressed after its merge.
- **Metadata is stripped:** EXIF (date taken, camera, **GPS**), XMP, the embedded thumbnail, and every video tag, chapter and extra stream ([media › What is stripped](media.md#what-is-stripped)). The picture is kept upright, and a wide-gamut color profile is kept. The post's date stays in the file name and the history.
- **Animated photos** (GIF, animated WebP) are compressed as animations: frames resized, frame rate capped.
- **Left alone:** HEIC without `--format-image` (`media` can't write HEIC). With `--format-image jpg`, a HEIC becomes a `.jpg`.

**The sequence for one file:**
1. The file is saved at `name.ext` and passes the usual checks.
2. `media.compress(saved, preset=…)` writes the result next to it and, **only if it is smaller**, renames it into place, deleting the original when the extension changed. It returns the new path, or `None` when nothing changed.
3. The history row is written last.

**What the history records**, in the row keyed by `folder/name` as in Phase 4:

| Field | After a replacement | When `compress` returned `None` |
| --- | --- | --- |
| `file` | the compressed file's name, such as `2026-01-02_123.mp4`, even when the original was `2026-01-02_123.webm` | the original's name |
| `compressed` | the preset, such as `"small"` | `"none"` |
| `size` | the compressed file's size, in bytes | the original's size |
| `original_size` | what was downloaded, in bytes | the same as `size` |

The skip check reads `file`. So:
- a compressed file is skipped on the next run, never downloaded or compressed again, **whatever preset the next run uses**;
- a file recorded with `"none"` isn't tried again.

If someone deletes the file itself, the next run downloads it again, and compresses it with that run's flags.

**`flnd download` compresses only new downloads.** Files saved by earlier runs are skipped as usual. [`flnd sync`](#flnd-sync) compresses the ones already there.

**Where it runs.**
- **Photos** compress on the download pool's thread, right after the save. A photo takes about 0.1–0.3 s.
- **Videos go to one compression queue per run**, a `worker.pool(concurrency=1)`, because x264 already uses every CPU core, and six encodes at once would only thrash. Downloads keep going meanwhile.
  - The file's row shows `compressing`, with the encode's progress in seconds of video, through `progress=row.update`.
  - A target ends, and `--zip` runs, only after its queued compressions finish.

**Failures never lose a file.** The original is only replaced, by an atomic rename, after the compressed file is complete.

| What happens | Result |
| --- | --- |
| `BrokenError` | the original is kept and recorded with `"none"`; one warning for that file |
| `FileExistsError` (`a.heic` would become `a.jpg`, and `a.jpg` already exists) | the original is kept and recorded with `"none"`; one warning |
| `RuntimeError` (no ffmpeg, or no `libx264` in a `PATH` ffmpeg) | one warning, and video compression stops for this run. Photos continue |
| Ctrl-C during an encode | the `.part` is removed, the original stays, and no row says it was compressed |

## flnd sync

`flnd sync` brings **everything already downloaded** up to date, so one command keeps every account current:

```sh
flnd sync                                   # every target ever downloaded, with the settings each was downloaded with
flnd sync ins:example_user tik:example_creator   # only these
flnd sync --compress-video small --format-video h265   # and change their settings from now on
flnd sync --only info                       # just the summary; nothing is fetched or changed
```

### Which targets, with which settings

- **Every `flnd download` records its target and its settings** in `.flnd/targets.db`, a `kv` table keyed by `<code>:<value>`: `kind`, `compress_image`, `compress_video`, `format_image`, `format_video`, `unpack` and `zip`, and when it last ran. `--since` and `--until` aren't recorded, because they describe one run, not the target.
- **`flnd sync` with no targets** runs every recorded target. Folders under `.flnd/downloads/<code>/<value>/` with no record (downloaded before Phase 5) are found too, and run with the default settings; their first sync records them.
- **`flnd sync` with targets** runs only those, resolved as in `flnd download`.
- **Options given to `sync` override the recorded ones and are saved**, so `flnd sync --compress-video small` turns compression on for every target from then on. `--no-compress-image` and `--no-compress-video` turn it off. `sync` takes every [download option](../phase-4/engine.md#download-options) except `--since` and `--until`.

### The steps

For each target, in this order, under the target's progress row:

| Step | What it does |
| --- | --- |
| **1. repair** | Walks the target's history rows and its folder. **Wrong extension:** `media.sniff` disagrees with the name (a WebP saved as `.jpg` before Phase 5), so the file is renamed and its row updated; a name already taken is reported and left. **Broken:** `media.check` fails, so the file is deleted and its row forgotten. **Missing:** a row whose file is gone. Broken and missing files are then downloaded again by step 2 |
| **2. fetch** | The same run as `flnd download <target>` with the target's settings: new posts are downloaded, and so are the broken and missing files from step 1, because their rows no longer say they were saved. New files are compressed as they arrive, as in `flnd download` |
| **3. compress** | When the target has a compress setting: every file in the target's folder whose history row has no `compressed` field yet goes through `media.compress` with the target's preset and format, exactly as a new download would. Videos use the one-at-a-time queue. Rows that say `compressed` (a preset or `"none"`) are skipped, so a second `sync` does nothing here |
| **4. info** | One summary line per target, and a total |

**The summary**, aligned columns printed with `log.info`, sizes through `fmt.size`:

```
target                  files   photos  videos   size     saved     repaired  missing
ins:example_user       1,204   1,010   194      3.1 GB   4.8 GB    2         0
tik:example_creator   88      0       88       610 MB   1.2 GB    0         1
total                   1,292   1,010   282      3.7 GB   6.0 GB    2         1
```

- `saved` is the sum of `original_size − size` over compressed rows. `missing` counts files step 2 couldn't get back, because the post is gone, or lies outside what the source still lists.
- `--only` chooses steps: `--only info` changes nothing and fetches nothing; `--only repair,compress` works offline. The default is all four.
- **Repair never deletes a file it can't replace without saying so.** A broken file whose post is gone is still deleted, because a broken file is worth nothing, and it is listed by name in the summary's warnings.
- Files in a target's folder that aren't in its history (put there by hand) are counted in `files` and otherwise left alone: not checked, not compressed.

### Things it doesn't do

- **Recompress.** A file compressed once is skipped, whatever settings the next sync has. Changing `--format-video` to `h265` applies to files compressed from then on. A `--recompress` would re-encode an already lossy file and lose quality twice.
- **Delete files whose posts were deleted.** An archive keeps what it saved.
- **Ask questions.** Like `flnd download`, it never prompts; a source that needs a login gets the usual hint and its targets are skipped.

## Checking downloads

The engine already compares the bytes it received with `Content-Length`. Some downloads have no size to compare with: a chunked response without `Content-Length`, or a Telegram `fetch` with `size=None`. For those it runs `media.check` on photos and videos, before renaming the `.part` file. A `BrokenError` is a failed download, retried like any other.
- Downloads with a known size aren't checked again; their size already proves they are complete.
- When ffmpeg is missing, video checks are skipped, logged at `debug`.

## Files

| File | Change | Lines |
| --- | --- | --- |
| `flnd/net.py` | `sniff`, the `audio=` path, checks | +45 |
| `flnd/session.py`, `flnd/engine.py` | `audio=`, the hint, both compress options with the video queue, and the history fields | +70 |
| `flnd/dash.py` | new | 40 |
| `flnd/cli.py` | `--compress-image`, `--compress-video`, `--format-image`, `--format-video` (with the `container:codec` parsing), the check that a format comes with its compress option, and the `sync` command | +40 |
| `flnd/sync.py` | new: the repair, compress and summary steps of `flnd sync`, and the saved target settings | 140 |
| `flnd/sources/fbk.py`, `ins.py` | choose DASH when there is a manifest | +8 each |

The 35-line sniffer the first draft put in `net.py` is now `media.sniff`, and the preset table lives only in `media`.

## Budget

Counted as in the [ceiling](../README.md#position): lines of code, without docstrings, comments or blank lines.

| | Lines of code |
| --- | --- |
| `shared` after Phase 4 | about 3,600 |
| `media` (six files) | about 780 ([why past 400](media.md#files-and-budget)) |
| **`shared` after Phase 5** | **about 4,380**, under the raised ceiling of about 5,000 |

## pyproject

`requires-python` is already `>=3.11` (committed in Phase 4).

```toml
[project]
dependencies = [
    # … Phase 4's list, plus:
    "pillow>=11.3",            # 11.3 is the first with AVIF in its wheels; on 3.11+ this resolves to 13.x
    "pi-heif>=1.0",            # HEIC, decode only, no GPL encoder
    "imageio-ffmpeg>=0.6",     # required: the ffmpeg binary, 20–68 MB wheels
]

[project.optional-dependencies]
media = ["pillow>=11.3", "pi-heif>=1.0", "imageio-ffmpeg>=0.6"]   # for shared.media used without flnd
```

One extra, named for the module, because `shared`'s missing-extra messages name it. The Makefile's `setup` installs `'.[archive,selector,browser,bundler,media]'`.

## Packaging

`make dist` is unchanged as a command. `bundler.build` adds `imageio_ffmpeg`, `PIL` and `pi_heif` to its hidden imports when they are installed, because `shared` loads them lazily and PyInstaller can't see that.
- **ffmpeg's binary** is collected by `pyinstaller-hooks-contrib`'s `hook-imageio_ffmpeg.py`, as `imageio_ffmpeg/binaries/ffmpeg-<platform>-v9.0`. hooks-contrib is installed with PyInstaller.
- **Pillow** has hooks in PyInstaller itself.
- **pi-heif** is a C extension whose libraries PyInstaller finds by dependency analysis.
- **Size.** The bundle grows by the ffmpeg binary: 52 MB on macOS arm64, 101 MB on Windows x64, and 165 MB on Linux x86_64. Pillow and pi-heif add about 15 MB. Chromium is still not bundled.
- **The executable bit.** A data file collected into the bundle could lose it. The bundle test runs a probe from inside the built program, so a lost bit fails the test instead of a user's download.
- **The GPL.** Every bundle includes ffmpeg, because it is a required dependency. The binary is a GPL build ([ffmpeg › Licensing](ffmpeg.md#licensing)), so `make dist` ships a **`licenses/` folder** with it, through `bundler.build(data=("licenses",))`. It holds:
  - `ffmpeg-LICENSE.txt`: the GPL text, from FFmpeg's `COPYING.GPLv3`;
  - `ffmpeg-SOURCE.txt`: the **source offer**. It gives the exact ffmpeg version and `configure` line (read from `ffmpeg -version` at build time), links to the matching FFmpeg release tarball and to imageio-ffmpeg's build scripts, and makes a written offer to provide that source on request for three years;
  - the licenses of the other bundled native code: Pillow (MIT-CMU), pi-heif (BSD-3) with libheif and libde265 (LGPL), and Playwright (Apache-2.0).

  `flnd/licenses/` is a checked-in folder. Only `ffmpeg-SOURCE.txt` is generated, by the `dist` target, so its version always matches the binary in the bundle. The bundle test checks that both ffmpeg files exist, and that the version in the source offer equals the bundled binary's.

## Tests

- **Extensions:**
  - a WebP body for a `.jpg` name is saved as `.webp`; an MP4 body keeps `.mp4`;
  - an HTML body for a photo is retried, then fails, and nothing is saved;
  - a `.jpg` saved as `.webp` is skipped on the second run (the history row);
  - archives are never renamed.
- **DASH**, with `httpx.MockTransport` serving the generated `v.mp4` and `a.m4a` from [media › Tests](media.md#tests); skipped without ffmpeg:
  - the saved file has both streams;
  - both parts are deleted;
  - a truncated audio part is retried;
  - a container mismatch fails at once;
  - `audio=` for a photo raises `RuntimeError`;
  - with ffmpeg made unavailable, the video fails with the hint printed once per run.
- **`dash.best`**, on anonymized manifest fixtures:
  - the highest video and audio are chosen;
  - `.webm` versus `.mp4`;
  - `SegmentTemplate` gives `None`;
  - a manifest without audio gives `None`;
  - an XML entity bomb is refused.
- **Compressing**, using the generated `big.mp4` and the large JPEG from the media tests; video cases are skipped without ffmpeg or `libx264`:
  - for each value of each option, the saved file is **smaller than what was served**, still passes `check`, and replaced the original; only one file is in the folder;
  - the history row has `file`, `compressed`, `size` and `original_size` as in the table, and a second run with a different preset skips the file;
  - with `--format-video mp4` a `.webm` becomes `.mp4`, the old file is gone, and the row's `file` names the new one;
  - `small` caps a 1080×1920 video at 720×1280 and a 4000×3000 photo at 2048×1536;
  - a file that doesn't shrink is kept, recorded with `"none"`, and isn't tried again;
  - an animated GIF stays animated, with smaller frames;
  - `--compress-image` alone leaves videos untouched, and `--compress-video` alone leaves photos untouched;
  - without `--format-*`, a JPEG stays `.jpg`, a WebP `.webp`, an MP4 `.mp4` and a WebM `.webm`; with `--format-image jpg` a WebP becomes `.jpg`, and with `--format-video mp4` a WebM becomes `.mp4`;
  - a saved photo has no EXIF or GPS left, and a saved video no `creation_time` or location;
  - a HEIC is left alone without `--format-image`, and becomes `.jpg` with `--format-image jpg`;
  - `--format-image jpg` without `--compress-image` is a usage error, and nothing is downloaded;
  - a merged DASH video is compressed after the merge;
  - two videos queued at once are compressed one after the other;
  - the target, and `--zip`, wait for the queue;
  - a `BrokenError` keeps the original;
  - a missing `libx264` warns once and leaves videos as they are, while photos are still compressed;
  - Ctrl-C during an encode leaves the original, no `.part`, and no row marked compressed.
- **`flnd sync`**, on a temp `.flnd/` with fake sources (`httpx.MockTransport`), never a real site:
  - with no targets, it runs every recorded target, and a folder without a record with the defaults, then records it;
  - options given to `sync` override the recorded ones and are saved; `--no-compress-video` turns it off;
  - **repair:** a WebP named `.jpg` is renamed and its row updated; a truncated JPEG is deleted, re-downloaded by the fetch step, and counted in `repaired`; a missing file is re-downloaded; a file the source no longer lists is counted in `missing` and named in a warning;
  - **compress:** existing uncompressed files are compressed and recorded; a second `sync` compresses nothing; files compressed with `"none"` are skipped; a file not in the history is left alone;
  - **`--format-video`:** `h265` keeps `.mp4` and gives `hevc`; on a WebM it gives `.mp4`; `mkv:h265` gives `.mkv` + `hevc`; `webm:h265` is a usage error;
  - **`--only info`** makes no request and changes no file; `--only repair,compress` makes no request;
  - the summary's numbers match the history: files, kinds, size, saved, repaired, missing.
- **Checks:**
  - a chunked response without `Content-Length` whose body is a truncated JPEG is retried;
  - a download with a known size isn't decoded;
  - without ffmpeg, video checks are skipped.
- **Packaging** (skipped without PyInstaller): the built program runs `media.probe` on a generated photo and video, and the bundle has `licenses/ffmpeg-LICENSE.txt` and an `ffmpeg-SOURCE.txt` whose version matches the bundled binary.

## Decided

These were open questions, and the user has decided them (2026-11-11):
- **One `media` module**, for probing, converting and compressing photos and videos, with no public `which_ffmpeg()` ([media › The layout](media.md#the-layout)).
- **ffmpeg is always installed:** `imageio-ffmpeg` is a required dependency of flnd.
- **A shared bundle ships ffmpeg's GPL text and a source offer** in `licenses/` ([Packaging](#packaging)).
- **Compressing replaces the original;** flnd never keeps both. The history records the result ([Compressing](#compressing)).
- **DASH merging is automatic.**
- **Two compress options,** `--compress-image` and `--compress-video`, and no `--jpg`. They strip metadata, GPS included, and keep the format unless `--format-image` / `--format-video` names another. `media.convert` stays available to scripts.
- **A setting given directly replaces the preset's value for that setting** (`media.compress(p, quality=75)`); the preset fills in the rest.
- **Python 3.11 is the floor.**
- **H.265 only when asked,** through `--format-video h265` or `container:h265` ([Video formats and codecs](#video-formats-and-codecs)).
- **`flnd sync`** repairs, fetches, compresses and summarizes every target ([flnd sync](#flnd-sync)).
- **`media` has top-level functions for any file and `media.image` / `media.video` / `media.audio` namespaces**, reads every format Pillow and ffmpeg can read safely, and adds trim, frame extraction, photos-to-video and frame-rate caps ([media](media.md)).
- **`shared`'s ceiling rises to about 5,000 lines**, so `media` (about 780) fits; the README change waits until Phase 4 is done with it.

## Open questions

None right now.
