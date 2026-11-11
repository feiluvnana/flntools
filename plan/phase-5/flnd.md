# flnd on media

Phase 5, after [media](media.md). This file covers how flnd's engine and sources use `media`, the `pyproject` and packaging changes, the size budget, and what is decided and still open. It builds on the Phase 4 [engine](../phase-4/engine.md): `session.save`, the download pool, and the history.

What changes for someone using flnd:
1. **Saved files get the right extension.** A WebP served for a `.jpg` name is saved as `.webp`, and an HTML error page is never saved as a photo.
2. **Facebook and Instagram videos are saved at their best quality.** Those sites serve it as separate video and audio streams (DASH), which flnd now downloads and joins with `media.merge`.
3. **Downloads whose size couldn't be verified are checked** with `media.check`.
4. **`--compress-image` and `--compress-video`** (each `small|balanced|high`) make photos and videos smaller after they are saved, with `media.compress`, and **replace the original**.

No new command. ffmpeg arrives with `pip install`, because `imageio-ffmpeg` is a required dependency of flnd ([ffmpeg](ffmpeg.md)), so there is no `flnd install-ffmpeg`. flnd needs Python 3.11 or later.

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
2. sets the row's caption to `merging`, and runs `media.merge(video_part, audio=audio_part, dest=temp_dest)`. `merge` reads every packet, so a truncated part fails here;
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

Two new [download options](../phase-4/engine.md#download-options), for every source. Each is off by default, and they can be used together or alone:

| Option | Meaning |
| --- | --- |
| `--compress-image small\|balanced\|high` | after saving each photo, run `media.compress(path, preset=…)`, which **replaces the original** when the result is smaller |
| `--compress-video small\|balanced\|high` | the same for each video |

**Both always replace the original; flnd never keeps both files.** Compression is lossy, and the original's URL may stop working, so a replaced original can't be recovered. Anyone who wants the originals runs without these flags.

**The values are `media`'s [presets](media.md#presets),** with nothing added by flnd:

| Value | Photos | Videos (H.264 + AAC, as `.mp4`) |
| --- | --- | --- |
| `small` | quality 70, shorter side at most 1536 (a 4000×3000 photo becomes 2048×1536) | CRF 28, at most 720p, audio 64 kb/s |
| `balanced` | quality 80, shorter side at most 2304 (3072×2304) | CRF 23, at most 1080p, audio 128 kb/s |
| `high` | quality 90, no cap | CRF 19, no cap, audio 160 kb/s |

flnd always uses H.264, because it plays everywhere ([open question 2](#open-questions)).

**What changes, as `media.compress` decides it:**
- **Photos.** JPEG and WebP stay in their own format. HEIC and AVIF become `.jpg`. PNG, GIF and animated photos are left alone (`compress` returns `None`).
- **Videos** always become `.mp4`, so a `.webm` changes extension. A DASH video is compressed after its merge.

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

**Only new downloads are compressed.** Files saved by earlier runs are skipped as usual, and aren't compressed after the fact ([open question 3](#open-questions)).

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
| `flnd/cli.py` | `--compress-image`, `--compress-video` | +6 |
| `flnd/sources/fbk.py`, `ins.py` | choose DASH when there is a manifest | +8 each |

The 35-line sniffer the first draft put in `net.py` is now `media.sniff`, and the preset table lives only in `media`.

## Budget

Counted as in the [ceiling](../README.md#position): lines of code, without docstrings, comments or blank lines.

| | Lines of code |
| --- | --- |
| `shared` after Phase 4 | about 3,600 |
| `media` (`_media.py`, `_ffmpeg.py`, `_pillow.py`) | about 460 ([why past 400](media.md#files-and-budget)) |
| **`shared` after Phase 5** | **about 4,060**, under the 4,500 ceiling, with about 440 to spare |

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
  - a `.webm` becomes `.mp4` and a HEIC becomes `.jpg`, the old files are gone, and the rows' `file` names the new ones;
  - `small` caps a 1080×1920 video at 720×1280 and a 4000×3000 photo at 2048×1536;
  - a file that doesn't shrink is kept, recorded with `"none"`, and isn't tried again;
  - PNG, GIF and animated WebP are untouched;
  - `--compress-image` alone leaves videos untouched, and `--compress-video` alone leaves photos untouched;
  - a merged DASH video is compressed after the merge;
  - two videos queued at once are compressed one after the other;
  - the target, and `--zip`, wait for the queue;
  - a `BrokenError` keeps the original;
  - a missing `libx264` warns once and leaves videos as they are, while photos are still compressed;
  - Ctrl-C during an encode leaves the original, no `.part`, and no row marked compressed.
- **Checks:**
  - a chunked response without `Content-Length` whose body is a truncated JPEG is retried;
  - a download with a known size isn't decoded;
  - without ffmpeg, video checks are skipped.
- **Packaging** (skipped without PyInstaller): the built program runs `media.probe` on a generated photo and video, and the bundle has `licenses/ffmpeg-LICENSE.txt` and an `ffmpeg-SOURCE.txt` whose version matches the bundled binary.

## Decided

These were open questions, and the user has decided them (2026-11-11):
- **One `media` module**, for probing, converting and compressing photos and videos, with no public `which_ffmpeg()` ([media › Why one module](media.md#why-one-module)).
- **ffmpeg is always installed:** `imageio-ffmpeg` is a required dependency of flnd.
- **A shared bundle ships ffmpeg's GPL text and a source offer** in `licenses/` ([Packaging](#packaging)).
- **Compressing replaces the original;** flnd never keeps both. The history records the result ([Compressing](#compressing)).
- **DASH merging is automatic.**
- **Two compress options,** `--compress-image` and `--compress-video`, and no `--jpg`. HEIC and AVIF photos become JPEG only when they are compressed. `media.convert` stays available to scripts.
- **Python 3.11 is the floor.**

## Open questions

Still the user's call:
1. **GPS in compressed photos** is kept, for an archive that stays as it was posted. If you would rather share compressed photos without location, flnd can drop GPS whenever it re-encodes, with a `strip_location=True` on `media.compress`.
2. **The codec for `--compress-video`.** flnd always uses H.264, which plays everywhere. H.265 makes files 30–50% smaller, but many browsers and older Windows machines can't play it. If you want it, it could be a fourth value (`small-hevc`) or a `--hevc` flag. `media.compress(codec="h265")` already exists for scripts.
3. **Commands for files you already have.** The options only touch new downloads. Do you want `flnd compress <folder> --preset small` (and maybe `flnd info <file>`, printing `media.probe`)? Each is about 20 lines in `flnd/cli.py`, because `media` does the work.
