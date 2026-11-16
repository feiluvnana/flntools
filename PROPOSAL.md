# PROPOSAL: flns, fln slideshow

Status: proposal, 2026-11-16. Nothing is built. It follows PLAN.md §1 (API rules) and reuses §2 (`shared`).

The user, 2026-11-16: "I can generate slideshow with different animation from cli. Maybe learn some cli
could do this, mix with ffmpeg. … It can do like slideshow, create album, create a photobooth card,…"

## 1. Goal and scope

flns turns flnd's media (`.flnd/media/<source>/<target>/…`, dated by mtime) into things to watch,
print or share. It is a library first, with a thin CLI. Like flnc, it imports `shared`, the stdlib and
flnd's public API only.

| product | what it makes | output | phase |
| --- | --- | --- | --- |
| `slideshow` | photos (and short video clips) with transitions, Ken Burns motion, title, music | `.mp4` (H.264/HEVC/AV1) | **MVP** |
| `card` | photobooth strip (1×3, 1×4), 2×2 grid, polaroid, with date captions | `.jpg`/`.png`, print sizes at 300 dpi | **MVP** |
| `album` | justified-grid pages with dates and captions | `.pdf`, `.jpg` pages | **MVP** |
| `collage` | one image: mosaic or grid of N photos | `.jpg`/`.png` | **MVP** (shares `card`'s layout code) |
| `loop` | a short looping animation from photos or one video | `.gif`, `.webp` | later |
| `album --html` | one static HTML page plus a thumbnails folder, with a lightbox | `.html` | later |
| `reel` | highlight reel: best moments of videos (scene detection), cut to music | `.mp4` | later |
| `recap` | "year in review": title cards per month + slideshow + collage cover | `.mp4` + `.jpg` | later (composes the others) |
| smart crop | keep faces/subjects inside the crop | option on all of the above | later |

Out of scope: a timeline editor, beat detection, effects beyond transitions and motion, uploading.

## 2. Research: which tools, and why

Checked on this machine: Homebrew `ffmpeg 9.0.2` has `xfade` (57 named transitions), `zoompan`,
`xstack`, `tile`, `afade`, `acrossfade`, `amix`, `loudnorm`, `sidechaincompress`, `scdet`,
`thumbnail`, and the `libx264`, `libsvtav1`, `h264_videotoolbox`, `hevc_videotoolbox` encoders.
It has **no `drawtext`** (no libfreetype) and **no `libwebp`** encoder. So text cannot rely on
`drawtext`, and WebP output cannot rely on ffmpeg.

| tool | what it does well | verdict |
| --- | --- | --- |
| **ffmpeg** | `xfade` transitions, `zoompan` Ken Burns, concat demuxer with stream copy, audio fades/mix, every encoder, `-progress pipe:1` | **required**; the only video engine |
| **Pillow** | in-process decode with `Image.draft()` (JPEG DCT downscale, 2–8× faster for thumbnails), `ImageOps.exif_transpose`, `ImageDraw` text with any TTF/OTF, multi-page PDF, animated WebP/GIF | **required** (Python dependency, no subprocess); does all stills, all text, PDF |
| ImageMagick 7 (`magick montage -tile 2x2 -geometry +10+10`, `-border`, `-annotate`) | montage, frames, text in one command | **not used**: Pillow covers it in-process; one more binary, a `policy.xml` that blocks PDF on some distros, and a subprocess per page |
| libvips (`vips thumbnail in.jpg out.jpg 1080 --smartcrop attention`, `vips arrayjoin "a b c d" out.jpg --across 2 --shim 10`) | fastest thumbnails, low memory, built-in `attention` smart crop | **optional, later**: used for `crop="smart"` and huge albums when `pyvips` imports; Pillow is the fallback |
| OpenCV YuNet (`cv2.FaceDetectorYN`, ~340 KB ONNX model) | fast, accurate face boxes | **optional, later** (`crop="face"`): `opencv-python-headless` is ~40 MB, so it is an extra |
| smartcrop.js / smartcrop.py | saliency crop | not used: vips `attention` does the same natively |
| ffmpeg-gl-transition | ~90 GLSL gl-transitions inside ffmpeg | **rejected**: needs a patched, OpenGL-enabled ffmpeg build tied to one ffmpeg version |
| editly / ffmpeg-concat (Node) | JSON5 specs, gl-transitions, Ken Burns (`zoomDirection in/out`) | **rejected**: Node runtime, `headless-gl` is hard to build on macOS ARM; good reference for spec shape and defaults |
| MLT `melt` | XML timelines, many effects | **rejected**: a second video engine for features ffmpeg already has |
| dvd-slideshow | bash slideshow scripts | **rejected**: unmaintained, DVD-era |
| Remotion / Motion Canvas | React/TS animated video, CLI render | **rejected**: Node + headless Chromium per render; far heavier than the need |

**Minimal set: ffmpeg + Pillow.** vips and OpenCV are optional extras for smart crop. Text is drawn
by Pillow into a transparent PNG and placed with ffmpeg's `overlay`, so no ffmpeg build needs
libfreetype.

## 3. Library API

```python
import flns

ns = flns.Flns(".flnd")                    # store: .flnd/metadata/flns.db, cache: .flnd/cache/flns/

# Selection: a Query[Photo] over flnd's files (flnd.Flnd.files), photos and videos alike.
photos = ns.photos.filter(target="ins:example_user", kind="image").filter(
    lambda p: p.date.year == 2026).sort("date").limit(60).get()
ns.photos.filter(target="twt:example_handle").count

# Slideshow: the look is a record; at most 4 keyword options per call.
show = flns.Show(transition="random", duration=3.0, fade=0.75, motion="kenburns", size="1080p")
r = ns.slideshow(photos, dest="show.mp4", style=show, music="song.mp3")
r.path, r.duration, r.skipped                # skipped: same inputs and options as last time

# Cards, albums, collages.
ns.card(photos[:4], dest="strip.jpg", layout="strip-4", theme=flns.themes.film)
ns.card(photos[0], dest="polaroid.jpg", layout="polaroid", caption="{date:%d %b %Y}")
ns.album(photos, dest="2026.pdf", page=flns.Page(size="a4", row_height=320), title="2026")
ns.collage(photos[:9], dest="grid.jpg", layout="grid-3x3")

# Plans, history, tools.
ns.preview(photos, style=show)               # Plan: slides, total seconds, pieces cached/to render
ns.outputs.filter(kind="slideshow").sort("at", desc=True).get()   # Query[Output]
ns.forget("show.mp4")                        # make it again next time
ns.tools                                     # Query[Tool]: ffmpeg, pyvips, cv2: path, version, missing
```

Records (frozen `msgspec.Struct`s, PLAN §1.2):

```python
class Show(msgspec.Struct, frozen=True):
    size: Size = "1080p"          # 1080p 1920×1080 | 4k | square 1080×1080 | story 1080×1920 | "W×H"
    fps: int = 30
    duration: float = 3.0         # seconds each photo is on screen, transitions included
    fade: float = 0.75            # transition length; 0 = hard cut
    transition: str = "fade"      # an xfade name, "cut" or "random"
    motion: Motion = Motion.NONE  # NONE | KENBURNS | ZOOM_IN | ZOOM_OUT | PAN_LEFT | … | RANDOM
    fit: Fit = Fit.BLUR           # FILL (crop) | FIT (pad) | BLUR (fit over a blurred fill)
    crop: Crop = Crop.CENTER      # CENTER | SMART | FACE (later)
    codec: Codec = Codec.H264     # H264 | HEVC | AV1
    title: str | None = None      # a 3 s title card; "{target}", "{first:%Y}" are filled in
    clip: float = 4.0             # max seconds of each video item
    seed: int | None = None       # None: derived from the inputs, so reruns match

class Theme(msgspec.Struct, frozen=True):     # flns.themes.plain | film | polaroid | dark | pastel
    background: str; frame: str; text: str; font: Path | None; caption: str = "{date:%Y-%m-%d}"
```

- `Photo` is `(path, target, date, kind, width, height, rotation)`; `date` is the mtime flnd set.
- Each verb returns `Result(path, skipped, seconds, …)`, never a bool that changes type.
- Progress: every verb takes the sink from `ui.live` through the `Flns(progress=…)` object, not an
  extra option, so the 4-option rule holds.
- Errors: `flns.Error` under `shared.BrokenError`; `flns.MissingError` names the tool and the fix
  (`"ffmpeg: not found; brew install ffmpeg"`).

## 4. CLI

```
flns slideshow <paths|targets…> [-o show.mp4] [--transition fade|wipeleft|…|random|cut]
               [--duration 3] [--fade 0.75] [--motion none|kenburns|zoom-in|…|random]
               [--size 1080p|4k|square|story|WxH] [--fps 30] [--fit blur|fill|fit]
               [--music a.mp3] [--title TEXT] [--codec h264|hevc|av1] [--hw] [--seed N]
flns card <paths|targets…> [-o card.jpg] --layout strip-3|strip-4|grid-4|polaroid|postcard
               [--theme film|plain|dark|pastel|polaroid] [--caption FMT] [--title TEXT] [--dpi 300]
flns album <paths|targets…> [-o album.pdf] [--page a4|letter|square] [--per-page N] [--title TEXT]
flns collage <paths|targets…> [-o grid.jpg] [--layout grid-3x3|mosaic] [--size WxH]
flns transitions            # the names this ffmpeg build offers, from `ffmpeg -h filter=xfade`
flns outputs [--kind]       # what flns made, with inputs and options
flns forget <outputs…>; flns tools
Selection (every maker): --from-target ins:example_user  --time 2026-01-01,2026-12-31
                         --kind image|video  --limit 60  --order date|random  --dry
Global: --ui plain|inline|full, -v, --home
```

- A positional is a file, a folder, or a flnd target (`ins:example_user`). Folders are walked with
  `fs.walk`; targets resolve through `flnd.Flnd.files`.
- `--time` uses flnd's syntax (`start,end`, either side open), against the file's date.
- `--dry` prints the plan (`ns.preview`): slides, length, transitions picked, pieces cached.
- Examples:
  ```
  flns slideshow --from-target ins:example_user --time 2026-01-01, --transition random \
       --motion kenburns --music song.mp3 --title "{target} · 2026" -o 2026.mp4
  flns card .flnd/media/ins/example_user/posts --order random --limit 4 --layout strip-4 --theme film
  flns album --from-target twt:example_handle --time 2026-06-01,2026-08-31 -o summer.pdf
  ```

## 5. Animations

### 5.1 Transitions (`xfade`, all with `duration=fade`, `offset=` computed)

| group | names (one spelling: the xfade name) | in `random` |
| --- | --- | --- |
| fades | `fade`, `fadefast`, `fadeslow`, `fadeblack`, `fadewhite`, `fadegrays`, `dissolve` | fade, fadeblack, dissolve |
| wipes | `wipeleft`, `wiperight`, `wipeup`, `wipedown`, `wipetl`, `wipetr`, `wipebl`, `wipebr` | wipeleft, wiperight |
| slides | `slideleft`, `slideright`, `slideup`, `slidedown` | all |
| smooth | `smoothleft`, `smoothright`, `smoothup`, `smoothdown` | all |
| cover / reveal | `coverleft`, `coverright`, `coverup`, `coverdown`, `revealleft`, `revealright`, `revealup`, `revealdown` | coverleft, revealright |
| shapes | `circleopen`, `circleclose`, `circlecrop`, `rectcrop`, `radial`, `vertopen`, `vertclose`, `horzopen`, `horzclose` | circleopen, radial, horzopen |
| diagonals | `diagtl`, `diagtr`, `diagbl`, `diagbr` | diagtl, diagbr |
| slices | `hlslice`, `hrslice`, `vuslice`, `vdslice` | — |
| wind | `hlwind`, `hrwind`, `vuwind`, `vdwind` | — |
| effects | `distance`, `pixelize`, `hblur`, `squeezeh`, `squeezev`, `zoomin` | zoomin |
| special | `cut` (no xfade, concat only), `random` (seeded pick from the pool, never the same twice in a row) | — |

- The offered names are read once from `ffmpeg -h filter=xfade` (cached with `sh.Tool.version`),
  so an older build that lacks `cover*`/`reveal*`/`*wind` just doesn't list them.
- `custom` (an `expr`) and gl-transitions are not offered; a few gl favourites (`heart`, `doorway`)
  could come later as `xfade=transition=custom:expr=…` if the user wants them.

### 5.2 Motion (`zoompan`, d = frames on screen, output `s=W×H`, `fps=`)

| motion | `z` | `x`, `y` |
| --- | --- | --- |
| `zoom-in` | `1+0.12*on/d` | centre: `iw/2-(iw/zoom/2)`, `ih/2-(ih/zoom/2)` |
| `zoom-out` | `1.12-0.12*on/d` | centre |
| `pan-left` / `pan-right` | `1.1` | `x=(iw-iw/zoom)*(1-on/d)` / `*(on/d)`; y centre |
| `pan-up` / `pan-down` | `1.1` | `y=(ih-ih/zoom)*…`; x centre |
| `kenburns` | zoom-in or zoom-out, alternating | toward the image's point of interest (centre, or the smart/face box) |
| `random` | any of the above, seeded | — |

- `zoompan` jitters because it rounds x/y to whole pixels. The fix: scale the normalized frame to 2×
  the output size before `zoompan`, then let `s=W×H` scale down. 2× costs about 4× the pixels per
  frame, so motion is opt-in, not the default.
- Defaults: `duration 3 s`, `fade 0.75 s`, `fps 30`, zoom 12%, title card 3 s, last slide holds
  `+1 s`, music fades in 1 s and out 2 s.

### 5.3 Text and audio

- Title cards and captions: Pillow draws text into a transparent PNG at output size;
  ffmpeg `overlay`s it with `fade=t=in:alpha=1`. No `drawtext` needed (missing from Homebrew 9.0).
- Music: `-stream_loop -1 -i song.mp3`, `atrim=0:T`, `afade=t=in:d=1`, `afade=t=out:st=T-2:d=2`,
  `loudnorm=I=-16`. Several tracks: `acrossfade=d=2` between them. No beat sync (out of scope).
- Video items keep their sound, ducked under music with `sidechaincompress`, mixed with `amix`
  (`--mute-clips` to drop it). Without music, a silent `anullsrc` track keeps players happy.

## 6. Pipeline and performance

```
select ─▶ probe ─▶ normalize (Pool, cached) ─▶ pieces (Pool, cached) ─▶ concat (copy) ─▶ mux audio (copy video)
```

1. **Select**: a `Query[Photo]`; `media.probe` per file (cached per path+mtime, in-process for images).
2. **Normalize** each photo to one frame at output size, cached as
   `.flnd/cache/flns/frames/<digest>.png`, keyed by `(path, mtime, size, fit, crop)`:
   EXIF rotation (`ImageOps.exif_transpose`), `draft()` downscale, then fill/fit/blur-pad and crop.
   This is pure Pillow, so it runs on `worker.Pool(jobs="cpu")`. Every frame is then the same size,
   SAR 1, `yuv420p`, `fps`, which `xfade` requires of both inputs.
3. **Pieces, not one graph.** One `filter_complex` with N `-loop 1` inputs chained through N−1
   `xfade`s works, but it holds N decoders open, it is single-process, it can't resume, and the
   offsets must be recomputed for the whole chain on any change. Past ~30 slides it gets slow and
   memory-heavy. So flns cuts the show into independent pieces:
   - a **hold** piece for each slide (`duration − fade` seconds, with its motion segment),
   - a **transition** piece for each pair (`fade` seconds: `xfade` of the tail of slide i's motion
     and the head of slide i+1's, from the same zoompan expressions offset by frame number `on`).

   Each piece is one small ffmpeg run on `worker.Pool(jobs=cpu//2)` with `-threads 2`, encoded with
   identical parameters (codec, profile, `-g`, `fps`, `yuv420p`), cached as
   `.flnd/cache/flns/pieces/<digest>.mp4` keyed by `(frames, motion, transition, Show)`. Adding one
   photo re-renders three pieces, not the show.
4. **Concat** with the concat demuxer and `-c copy` (pieces share parameters, so no re-encode):
   `ffmpeg -f concat -safe 0 -i list.txt -c copy`. The filter-graph `concat` filter is not used,
   since it re-encodes.
5. **Mux audio** in the last pass: `-c:v copy`, audio filters as §5.3, `-c:a aac -b:a 192k`,
   `-movflags +faststart`, into `fs.stage(dest)`; `fs.touch` sets the output's mtime to the newest
   input's date.

| encoder | when | settings |
| --- | --- | --- |
| `libx264` | default, plays everywhere | `-preset medium -crf 20 -pix_fmt yuv420p -profile:v high` |
| `h264_videotoolbox` / `hevc_videotoolbox` | `--hw` on macOS | `-q:v 65` (`-tag:v hvc1` for HEVC so QuickTime plays it) |
| `h264_nvenc`, `h264_qsv`, `h264_vaapi` | `--hw` on Linux/Windows when listed by `media.encoders` | `-cq 23` / `-global_quality 23` / `-qp 23` |
| `libsvtav1` | `--codec av1` | `-preset 8 -crf 32 -pix_fmt yuv420p10le` |

- **Progress**: each piece runs with `-progress pipe:1 -nostats`; `media.render` turns `frame=` /
  `out_time_us=` lines into `row.advance(Δframes)`. The row's total is the sum of all pieces'
  frames, known before rendering, so one `ui` row shows the whole show; cached pieces advance at
  once. `progress=end` closes a piece.
- **Stills** (card, album, collage) are Pillow only: a pure `layout()` returns boxes, then `draw()`
  pastes thumbnails. Album pages render on the pool; the PDF is written with `save_all=True`.
- **Cache hygiene**: `fs.sweep(".flnd/cache/flns", keep=30 days)` at start; `flns forget` drops rows.
- **Targets** (8-core Mac): 100 photos, 1080p, fade, no motion, cold < 60 s with VideoToolbox;
  a rerun with one photo added < 5 s; `flns --help` < 150 ms (no Pillow or numpy at import).

## 7. `shared` modules

| module | use | new, general (not flns-specific) |
| --- | --- | --- |
| `media` | `probe`, `encoders` | `probe` adds `rotation` (EXIF/display matrix). `encoders` lists hardware video encoders. **`media.render(*args, seconds=, progress=)`**: run ffmpeg with `-progress pipe:1`, feed the sink, raise `media.Error` with the last stderr lines; flnc's video path uses it too. **`media.concat(parts, dest)`**: concat demuxer, stream copy, staged. **`media.fit(src, size, *, mode=, focus=)`** → `PIL.Image`: EXIF transpose, `draft` downscale, fill/fit/blur, crop toward a focus box. **`media.font(name=None)`** → `Path`: a font by family on macOS/Linux/Windows, or the bundled one. |
| `fs` | `walk`, `stage`, `touch`, `sweep` | — |
| `sh` | `find("ffmpeg")`, `Tool.stream`, `version`, `fake` | — |
| `worker` | `Pool` for frames, pieces, album pages | — |
| `store` | `Row` for outputs | — |
| `crypto` | `digest` for cache keys | — |
| `ui`, `cli`, `clock`, `text` | `live` rows, `App`, `trap`, `text.duration` | — |
| `Query` | `ns.photos`, `ns.outputs` | — |

`media.montage` is not proposed: layouts differ per product, and a general grid helper would grow
options past 4. If a second user appears, `layout()` moves to `media` then.

## 8. Tools and install

| tool | required | found by | notes |
| --- | --- | --- | --- |
| ffmpeg + ffprobe ≥ 6.1 (7.0+ for every transition) | yes | `sh.find("ffmpeg")` on PATH, as PLAN §2.14 | macOS `brew install ffmpeg`; Linux distro package; Windows gyan.dev or BtbN builds (`winget install Gyan.FFmpeg`) |
| Pillow | yes | Python dependency, extra `media` | wheels on every OS; lazy-imported |
| pyvips + libvips | no (smart crop, huge albums) | `import pyvips`, extra `vips` | `brew install vips`; Windows needs the libvips DLL bundle |
| opencv-python-headless + YuNet model | no (face crop) | extra `face`; model fetched by `http.download`, sha256 pinned | ~40 MB wheel, 340 KB model |
| a font | yes for text | `media.font` | bundle one OFL font (Inter or Noto Sans, ~300 KB); system fonts by name otherwise |

- No pinned `sh.find` specs are needed for the MVP: ffmpeg comes from PATH (PLAN's rule), the rest
  are Python wheels. `flns tools` reports what is missing and how to get it.
- Licensing: GPL ffmpeg builds (libx264) are fine for a private tool; OFL fonts may be bundled.
- Windows: paths in the concat list are written with forward slashes and `'` escaped; `-safe 0`.

## 9. Store

`.flnd/metadata/flns.db` (WAL, `store.Row`):

```python
class Output(store.Row):          # key: dest relative to the home, or absolute outside it
    table = "outputs"
    kind: str                     # slideshow | card | album | collage
    inputs: str                   # digest of [(path, mtime, size), …] in order
    options: str                  # msgspec JSON of the Show/Theme/Page record and the layout
    tools: str                    # "ffmpeg 9.0.2; Pillow 11.3"
    count: int; seconds: float; size: int; mtime: int; at: float
```

- A rerun is skipped when the row's `inputs`, `options` and `tools` match and `dest` still has the
  row's `size` and `mtime`; `--again` forces it. `Result.skipped` says so.
- Pieces and frames have no rows: their digest is their file name, and a file that exists is a hit.
- `ns.outputs` is a `Query[Output]`; `flns outputs` prints it with what made each one.

## 10. Open questions (each with a recommended default)

1. **Name of the verb**: `slideshow` or `show`? → `slideshow` (the user's word).
2. **Default look**: plain fade, or `random` transitions + Ken Burns? → fade, 3 s, no motion;
   `--transition random --motion kenburns` is one flag pair away and costs 4× more to render.
3. **Default codec**: H.264 (plays everywhere) or AV1 (smaller, like flnc)? → H.264; `--codec av1`.
4. **Hardware encode by default on macOS?** → no; `--hw` opts in (VideoToolbox is fast but larger
   files at equal quality).
5. **Videos inside slideshows** in the MVP? → yes, trimmed to `clip=4 s`, sound ducked under music.
6. **Smart crop** in the MVP? → no; `CENTER` plus `BLUR` fit keeps every face in frame. vips
   `attention` next, YuNet faces after.
7. **Bundle a font** (Inter, OFL) in the wheel? → yes, one font, so cards look the same everywhere.
8. **Album HTML** in the MVP, or PDF only? → PDF and JPG pages first; HTML later.
9. **Output folder default**: the current folder, or `.flnd/made/`? → `<home>/made/<kind>/` with a
   name from the selection (`ins_example_user_2026.mp4`), `-o` to override.
10. **Package layout**: a new top-level `flns/` like `flnc/` → yes: `__init__` (`Flns`), `cli`,
    `select`, `slideshow`, `stills` (card, album, collage), `themes`.

## 11. Build steps

Each step is one commit as `feiluvnana` with `check` green, `api.txt` regenerated and read. Tests
use `sh.fake` (an ffmpeg handler that records argv and writes a stub file), `clock.Fake`, and pure
layout functions; nothing is rendered for real and no image is encoded.

1. `docs:add the flns plan` — this proposal folded into PLAN.md as §5 (after the user's answers).
2. `feat:add media.render and media.concat` — `-progress` parsing into a sink, concat list writing
   and escaping. Tests: canned progress output → sink counts; argv snapshot; error carries stderr.
3. `feat:add media.fit, media.font and probe rotation` — tests on tiny in-memory Pillow images (a
   4×2 px image fills to 2×2): this is arithmetic, not rendering.
4. `feat:add flns selection and store` — `Flns`, `photos` over `flnd.Flnd.files` and folders,
   `Output` rows, skip logic. Tests: a fake flnd home with dated stub files.
5. `feat:add flns slideshow pieces and transitions` — `Show`, transition list from a faked
   `ffmpeg -h filter=xfade`, seeded `random`, hold/transition pieces, offsets, filter strings.
   Tests: golden filtergraph strings per transition and motion; same seed, same plan.
6. `feat:render, cache and concat slideshow pieces` — `worker.Pool`, piece cache, concat, audio mux,
   encoder choice. Tests: `sh.fake` checks argv, that cached pieces skip ffmpeg, and that progress
   totals equal frames.
7. `feat:add flns cli for slideshow` — `cli.App`, selection flags, `--dry`, `ui.live` row; README.
8. `feat:add flns cards and collages` — `layout()` per layout (strip-3/4, grid-4, polaroid,
   postcard, grid-NxM, mosaic) returning boxes; themes; captions. Tests: box geometry and text
   positions at 300 dpi, no pixels compared.
9. `feat:add flns album pdf` — justified rows (Flickr-style, by aspect ratio), pages, title page.
   Tests: row breaking and page counts.
10. Later, one commit each: motion (`zoompan` with 2× prescale), video clips and ducking, `loop`
    (GIF via `palettegen`/`paletteuse`, WebP via Pillow), album HTML, smart crop (vips), face crop
    (YuNet), `reel` (`scdet` + `thumbnail`), `recap`.

Manual check (by hand, never in tests): on a copy of `.flnd/media` from the authorized test
accounts, render a 20-photo show with `--transition random --motion kenburns --music`, add one
photo and rerun (only three pieces re-render), print a `strip-4` card at 300 dpi.
