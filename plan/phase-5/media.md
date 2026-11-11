# media

Phase 5. `shared.media` reads, converts, compresses and edits **photos, videos and audio**. It replaces the separate `image` and `video` modules of the first draft.

```python
from shared import media

# Any file: media decides what it holds from its content
info = media.probe("IMG_0042.heic")
info.kind, info.format, info.width, info.height, info.duration, info.fps
info.created, info.location, info.tags["Model"]
media.convert("IMG_0042.heic", dest="IMG_0042.png")              # still → still
media.convert("clip.webm", dest="clip.mp4", fps=30)              # moving → moving: video, GIF, animated WebP
media.convert("song.flac", dest="song.mp3")                      # audio → audio
media.compress("clip.mp4", preset="small")                       # in place: smaller, stripped, same format
media.check("clip.mp4")                                          # raises media.BrokenError when truncated or corrupt
media.sniff("download.part")                                     # ".webp", from the first bytes

# One kind: the namespaces
media.image.resize("a.jpg", width=1200, dest="a.small.jpg")
media.image.crop("a.jpg", box=(0, 0, 1080, 1080), dest="a.square.jpg")
media.video.trim("clip.mp4", start=12.5, end=30, dest="part.mp4")
media.video.frames("clip.mp4", every=1.0, dest_dir="frames/")    # video → photos
media.video.merge(["a.jpg", "b.jpg", "c.jpg"], dest="slides.mp4", duration=2.0)   # photos → video
media.video.merge(["v.mp4"], audio="a.m4a", dest="out.mp4")      # DASH video + audio, no re-encode
media.video.merge(["part1.mp4", "part2.mp4"], dest="whole.mp4")  # clips end to end
media.audio.extract("clip.mp4", dest="clip.mp3")                 # video → audio
```

Photos go through **Pillow** (with **pi-heif** for HEIC), in process. Videos and audio go through the **ffmpeg** program, out of process. The caller never chooses, and never sees ffmpeg: there is no `which_ffmpeg()` and no `run(*args)`.

## The layout

**Two rules decide where a function lives:**
1. **Top level: works on any kind of file.** `probe`, `convert`, `compress`, `check` and `sniff` take a photo, a video or an audio file, and do the matching thing.
2. **`media.image`, `media.video`, `media.audio`: one kind only, or one kind turned into another.** Resizing only means something for a photo; trimming only for something with a timeline.

**Conversions follow the same split:**

| From → to | Function |
| --- | --- |
| still photo → still photo | `media.convert` |
| moving picture → moving picture (video, GIF, animated WebP, animated PNG) | `media.convert` |
| audio → audio | `media.convert` |
| video → photos | `media.video.frames` |
| photos → video or animated image | `media.video.merge` |
| video → audio | `media.audio.extract` |

`convert` keeps a file's kind. A conversion that changes the kind is named for what it does, so `convert("clip.mp4", dest="clip.jpg")` raises `ValueError("clip.mp4 is a video; use media.video.frames to get photos")`, naming the right function.

**Arguments.** The file (or the list of files, for `merge`) is the one positional argument; everything else is named, as Naming rule 7 asks. `dest=`, `audio=` and every setting are written out.

**Where results go.** One rule for every function that writes one file:
- `convert`, `merge` and `extract` **need `dest=`**: their result is a different file by nature.
- `compress`, `image.resize`, `image.crop` and `video.trim` **replace the source when `dest=` is left out**, and write a copy when it is given.

### How the namespaces are built

`media.image`, `media.video` and `media.audio` are the private modules `shared/_media_image.py`, `_media_video.py` and `_media_audio.py`, set as attributes of `shared.media`. They load on first use, like `shared`'s modules: `from shared import media` neither imports Pillow nor looks for ffmpeg. `media.Info`, `media.BrokenError` and `media.UnsafeError` are the same objects everywhere.

## Formats

**Every format Pillow and ffmpeg can read safely from a local file.** What is refused is what reads *other* files, runs *other* programs, or talks to the network ([Safety](#safety)).

### Read

| Kind | Formats |
| --- | --- |
| photo (Pillow) | JPEG (and MPO), PNG and APNG, GIF, WebP, HEIC/HEIF, AVIF, TIFF, BMP, ICO, TGA, PPM/PGM/PBM, JPEG 2000, QOI, PSD (the flattened image), DDS. Checked on Pillow 13 with pi-heif |
| video (ffmpeg) | MP4, M4V, MOV, 3GP (all ffmpeg's `mov` reader); MKV and WebM (`matroska`); AVI; MPEG-TS (`.ts`, `.mts`, `.m2ts`); MPEG-PS (`.mpg`, `.vob`); FLV; WMV/ASF; OGV (`ogg`); GIF (ffmpeg's `gif` reader, when a GIF is used as a moving picture) |
| audio (ffmpeg) | MP3, M4A/AAC (`mov`, and raw ADTS `aac`), Opus and Ogg Vorbis (`ogg`), FLAC, WAV, AIFF, CAF, AMR, WMA (`asf`), MKA (`matroska`) |

Animated WebP, AVIF and APNG are read by Pillow and handed to ffmpeg as raw frames through a pipe when a video function needs them, because ffmpeg 4.4, the oldest supported, has no animated WebP reader.

### Write

| Kind | Formats, by `dest`'s extension |
| --- | --- |
| still photo | `.jpg`/`.jpeg`, `.png`, `.webp`, `.avif`, `.gif`, `.tif`/`.tiff`, `.bmp`, `.ico` (at most 256×256; larger is scaled down), `.tga`, `.ppm`, `.jp2`, `.qoi` |
| moving picture | video: `.mp4`/`.m4v`, `.mov`, `.mkv`, `.webm`, `.avi`, `.ts`, `.3gp`. Animated image: `.gif` (ffmpeg, with a generated palette), `.webp` and `.png` (APNG) (Pillow, from frames ffmpeg decodes) |
| audio | `.mp3`, `.m4a`, `.aac`, `.opus`, `.ogg`, `.flac`, `.wav`, `.aiff` |

**Not written:** HEIC, because Pillow has no HEIC writer and pi-heif is decode-only (checked: saving as `HEIF` fails), see [Rejected](#rejected); PSD and DDS, which are read only.

### Codecs chosen when writing

Streams are **copied** whenever the target container can hold them, which is fast and loses nothing. Otherwise they are encoded with:

| Container | Video | Audio |
| --- | --- | --- |
| MP4, MOV, MKV, 3GP, TS | H.264 (`libx264`), or H.265 with `codec="h265"` | AAC |
| WebM | VP9 (`libvpx-vp9`) | Opus |
| AVI | H.264 | MP3 |
| `.mp3` | — | MP3 (`libmp3lame`) |
| `.m4a`, `.aac` | — | AAC |
| `.opus` | — | Opus |
| `.ogg` | — | Vorbis (`libvorbis`), or Opus in Ogg when this ffmpeg has no `libvorbis` |
| `.flac`, `.wav`, `.aiff` | — | FLAC, PCM, PCM |

Which codecs a container holds, for the copy decision: MP4 and MOV hold H.264, HEVC, AV1, VP9 and MPEG-4 video, and AAC, Opus, MP3 and ALAC. Matroska holds all of them. WebM holds VP8, VP9 and AV1, and Opus and Vorbis. HEVC in MP4 or MOV is tagged `hvc1`, so Apple's players open it. MP4 and MOV always get `+faststart`.

## Files and budget

Rule 3 counts a module as all of its files, in lines of code (no docstrings, comments or blanks):

| File | Holds | Lines |
| --- | --- | --- |
| `shared/_media.py` | the top-level functions, `Info`, the errors, `sniff`, the presets, the output rule, the format tables | 170 |
| `shared/_ffmpeg.py` | finding ffmpeg, the version and encoder checks, the safe runner with its timeouts and stall timer, the raw-frame pipe, parsing ffmpeg's output | 190 |
| `shared/_pillow.py` | the safe open, EXIF to `tags` / `created` / `location`, the write pipeline, stripping, animated frames | 150 |
| `shared/_media_image.py` | `resize`, `crop` | 40 |
| `shared/_media_video.py` | `trim`, `frames`, `merge` | 200 |
| `shared/_media_audio.py` | `extract` | 30 |
| **`media`** | | **about 780** |

**This is far past Rule 3's 400, on purpose:** it wraps two engines and three kinds of file, and splitting it brings back the two-module API this design removes. `shared` goes from about 3,600 to about **4,380**, close to the 4,500 ceiling. **The user decided (2026-11-11) to raise the ceiling to about 5,000** so `media` has room. `plan/README.md`'s ceiling, and Rule 3 with a line naming `media` as the one module allowed past 400, change once Phase 4 is finished with that file.

## probe

`probe(path) -> Info`

It reads what the file is and its metadata, **without decoding** the picture or the video. A photo opens in Pillow and reads its header and EXIF. A video or audio file is one ffmpeg run of a fraction of a second ([how](#how-a-video-is-probed)). A file that isn't media raises `BrokenError`.

`Info` is a frozen dataclass. It is a value, like `os.stat_result`, not a wrapper, so it has no `.raw`; `tags` holds everything that was read.

| Field | Photo | Video / audio |
| --- | --- | --- |
| `path` | the file | the file |
| `kind` | `"image"` | `"video"`, or `"audio"` when there is no video stream |
| `animated` | `True` for an animated GIF, WebP, AVIF or APNG | `False` |
| `format` | `"jpeg"`, `"png"`, `"gif"`, `"webp"`, `"heif"`, `"avif"`, `"tiff"`, … (Pillow's name, lowercased) | `"mp4"`, `"mov"`, `"webm"`, `"mkv"`, `"avi"`, `"ts"`, `"mp3"`, `"flac"`, … (from [`sniff`](#sniff) where it knows, otherwise ffmpeg's reader name) |
| `size` | bytes on disk | bytes on disk |
| `width`, `height` | **as displayed**: EXIF orientation 5–8 swaps them | **as displayed**: a 90° rotation swaps them. `None` for audio |
| `duration` | `None` for a still; the sum of frame durations for an animation | seconds, or `None` when ffmpeg prints `N/A` |
| `frames` | 1 for a still, the frame count for an animation | `None` (counting needs a full decode) |
| `fps` | `None`, or for an animation, frames ÷ duration | of the first video stream, or `None` |
| `bitrate` | `None` | the whole file's, in bits per second, or `None` |
| `video` | `None` | the first video stream's codec: `"h264"`, `"hevc"`, `"vp9"`, `"av1"`, … `None` when there is none |
| `audio` | `None` | the first audio stream's codec: `"aac"`, `"opus"`, `"mp3"`, … **A DASH video stream has `audio=None`** |
| `sample_rate`, `channels` | `None` | of the first audio stream, or `None` |
| `created` | EXIF `DateTimeOriginal`, with `OffsetTimeOriginal` when present; else EXIF `DateTime` | the container's `creation_time` |
| `location` | `(latitude, longitude)` from the EXIF GPS block | from the ISO 6709 `location` or `com.apple.quicktime.location.ISO6709` tag |
| `tags` | every EXIF field, the Exif and GPS blocks included, named by Pillow's `ExifTags` (`"Make"`, `"Model"`, `"DateTimeOriginal"`, `"GPSLatitude"`), as strings. Binary fields (`MakerNote`, thumbnails) are left out | the file's metadata (`title`, `artist`, `album`, `comment`, `encoder`, `location`, …), as strings |

- `created` is a `datetime`: timezone-aware when the file says the offset, naive otherwise. A date that doesn't parse (`0000:00:00 00:00:00`, which cameras write) gives `None`, and stays in `tags` as written.
- `location` is `None` when absent, and also at `0, 0`, which some apps write meaning "unknown".
- **Nothing in `Info` decodes pixels or frames,** so probing a folder of a thousand files takes seconds.

### How a video is probed

One ffmpeg run gives both the stream summary and the metadata:

```sh
ffmpeg -hide_banner -nostdin -v info -protocol_whitelist file -format_whitelist <readers> \
  -i file:/abs/clip.mp4 -f ffmetadata pipe:1
```

- **stdout** is the metadata in ffmpeg's `ffmetadata` format: `;FFMETADATA1`, then `key=value` lines in which `=`, `;`, `#`, `\` and newline are escaped with `\`. So a value can't fake another key. That gives `tags`.
- **stderr** is the `Input #0` summary, read up to `Stream mapping:`:
  - `Duration: HH:MM:SS.ss, start: …, bitrate: N kb/s` gives `duration` and `bitrate`;
  - lines matching exactly `^  Stream #0:\d+.*?: (Video|Audio): (\w+)` give the codecs. For video, the first `, (\d+)x(\d+)[ ,]` after the codec gives the stored size, and `(\d+(?:\.\d+)?) fps` the frame rate. For audio, `(\d+) Hz` and the channel layout;
  - `displaymatrix: rotation of (-?\d+\.\d+) degrees` under that stream gives the rotation;
  - `^    creation_time   : (\S+)$`, at exactly that indent before `Duration:`, gives `created`. ffmpeg's `ffmetadata` writer leaves `creation_time` out, which is why it is read here.
- **Lines are matched at their exact indent.** A metadata value with a newline is printed on continuation lines that start with spaces and `: `, so a hostile title can't add a fake `Stream` line. Checked on ffmpeg 9.0.2: a title of `hi\nStream #0:5: Video: h264, 9999x9999` stays a title, in both outputs.
- It exits 0 only when the container opens, so `probe` also proves the file is readable. The timeout is 30 s.

## convert

`convert(src, *, dest, quality=90, fps=None, codec=None) -> Path`

It writes `src` as the format **`dest`'s extension** names, keeping its kind ([the layout](#the-layout)), and returns `dest`. `src` is never changed. Metadata is **kept** where the target can hold it; `compress` is the one that strips.

| `src` → `dest` | What happens |
| --- | --- |
| still → still | decoded and written by Pillow: orientation applied (and the tag reset), EXIF and the ICC profile kept when the format holds them, transparency flattened onto white for formats without it (JPEG, BMP), modes the format can't hold converted. `quality` (1–100) sets JPEG, WebP, AVIF and JPEG 2000 quality |
| animated → animated image (`.gif`, `.webp`, `.png`) | every frame and its duration kept, through Pillow's `save_all`. GIF is limited to 256 colors per frame |
| animated → still format | `ValueError("a.webp is animated; .jpg can't hold animation; use media.video.frames")` |
| video or animation → video | **streams copied** when the container holds them; otherwise encoded ([codecs](#codecs-chosen-when-writing)) at `quality` |
| video → animated image | `.gif` through ffmpeg's `palettegen` / `paletteuse`, at most 15 fps unless `fps=` says otherwise; `.webp` / `.png` from frames ffmpeg decodes into Pillow. A GIF of a long video is huge; the docstring says so |
| audio → audio | copied when the target holds the codec (AAC into `.m4a`), otherwise encoded: MP3 192 kb/s, AAC 160 kb/s, Opus 128 kb/s, Vorbis quality 5, FLAC and WAV lossless |

- **`fps`** caps the frame rate of a moving picture (`-vf fps=N`; frames are dropped, never invented). It forces a re-encode. A still or an audio file with `fps=` raises `ValueError`.
- **`codec`**: `"h264"`, `"h265"` or `"vp9"`, used when the video is encoded. Giving it forces an encode even when the streams could be copied.
- **Refused before anything is read**, with `ValueError`: an extension not in the [write table](#write); a conversion that changes kind (with the function to use instead).

## compress

`compress(src, *, dest=None, format=None, preset="balanced", quality=None, resolution=None, fps=None, codec=None, audio_bitrate=None, strip=True, progress=None) -> Path | None`

It makes `src` smaller: it re-encodes it, caps its resolution and frame rate, and **strips what a viewer doesn't need**. **The result is kept only if it is smaller than `src`.** It returns the path it wrote, or `None` when nothing changed.

**The format stays the same** unless `format=` (or `dest=`'s extension) asks for another, of the same kind. A JPEG stays JPEG, a WebM stays WebM, an MP3 stays MP3.

| Source | Written as, by default |
| --- | --- |
| JPEG / WebP / AVIF / JPEG 2000 | the same, at `quality`; JPEG with `optimize=True`, `progressive=True` |
| PNG, TIFF, BMP, TGA, PPM, QOI (lossless) | the same, lossless: resized, stripped, and PNG and TIFF at their best compression. `quality` doesn't apply. To shrink a lossless photo a lot, give `format="jpg"` or `"webp"` |
| HEIC | **left alone** (`None`), because `media` can't write HEIC. With `format=`, compressed to that |
| GIF, animated WebP, APNG | the same, with frames resized and `fps` applied |
| MP4, MOV, MKV, 3GP, TS, AVI | the same container, H.264 + AAC (MP3 in AVI) |
| WebM | WebM, VP9 + Opus |
| other video containers (FLV, WMV, MPEG-PS, OGV) | MP4, because they hold modern codecs poorly. The docstring says so |
| MP3, M4A/AAC, Opus, Ogg | the same codec, at `audio_bitrate` |
| FLAC, WAV, AIFF (lossless audio) | **left alone** (`None`); give `format="mp3"` or another lossy format to shrink them |

- `format` is a short name without a dot (`"jpg"`, `"mp4"`, `"opus"`), named after Pillow's `format`. A format of another kind raises `ValueError`. `format=` and `dest=` together must agree.
- **Where the result goes:** with no `dest=`, the result replaces `src`. When the format changes (`a.heic` → `a.jpg`), the new name is used and `src` is deleted after the rename. If that new name already exists, `FileExistsError` is raised before anything is encoded.

So `media.compress(path)` is "make this file smaller, keep its format", and a loop over a folder is safe:
```python
for path in folder.iterdir():
    media.compress(path, preset="small")   # photos, videos and audio shrink; anything it can't shrink returns None
```

**`None` is returned, with nothing changed,** when the result isn't smaller (`log.info("a.jpg: compressing saved nothing; kept the original")`), or the file is one `compress` doesn't touch: HEIC or lossless audio without `format=` (`log.debug`). A file that isn't media raises `BrokenError`.

### What is stripped

With `strip=True` (the default), the result holds the picture and the sound, and nothing else:

| | Kept | Dropped |
| --- | --- | --- |
| photo | the pixels, upright (orientation is applied to them, so the tag isn't needed); the ICC color profile **unless it is sRGB**, because a Display P3 phone photo looks wrong without its profile, and sRGB is what a viewer assumes anyway | all EXIF: camera, lens, settings, date taken, **GPS**, MakerNote, the embedded thumbnail; XMP; IPTC; comments |
| video | the first video stream, upright (ffmpeg applies the rotation while encoding), and the first audio stream | every other stream (subtitles, extra audio, data, cover art), chapters, and all container and stream metadata: `creation_time`, location, title, encoder and handler names (`-map_metadata -1 -map_chapters -1`, `-fflags +bitexact`, `-flags +bitexact` on each stream) |
| audio | the first audio stream | tags (title, artist, album), cover art, chapters |

- **The date taken is dropped with the rest.** flnd keeps the post's date in the file name and its history. A script that wants metadata kept passes `strip=False`: photos keep EXIF (orientation reset) and XMP; videos and audio keep their tags.
- A compressed photo is therefore safe to share: no location, no camera serial number.
- On a small photo, metadata is a real share of the file: a phone's EXIF with its thumbnail is 20–60 KB.

### Presets

A preset is a name, never raw encoder settings. One table for every kind:

| `preset` | `quality` | `resolution` (shorter side) | `fps` | x264/x265 speed | `audio_bitrate` |
| --- | --- | --- | --- | --- | --- |
| `"small"` | 70 | 720 videos, 1536 photos | 30 | `medium` | 64 kb/s |
| `"balanced"` (default) | 80 | 1080 videos, 2304 photos | 60 | `medium` | 128 kb/s |
| `"high"` | 90 | none | none | `slow` | 160 kb/s |

**A setting given directly replaces the preset's value for that setting.** The preset fills in only the settings that aren't given:
- `compress(p, quality=75)` is "balanced, but quality 75": the preset's 80 is ignored, and its resolution, fps and audio still apply;
- `compress(p, preset="high", resolution=720)` is "high quality, at most 720p";
- `compress(p, quality=60, resolution=1080, fps=30, audio_bitrate=96_000)` gives every value, so the preset is ignored entirely.

| Argument | Means | Range |
| --- | --- | --- |
| `quality` | one scale for every kind, higher is better. A photo's JPEG, WebP, AVIF or JPEG 2000 quality. For a video, the encoder's constant-quality value: CRF `round(59.5 − 0.45 × quality)` for H.264 (70, 80, 90 → 28, 23, 19), 4 more for H.265, and `round(82 − 0.6 × quality)` for VP9 (→ 40, 34, 28). Lossless formats ignore it | 1–100 |
| `resolution` | the shorter side's cap, in pixels: the way people say "720p". A portrait 1080×1920 video becomes 720×1280; a 4000×3000 photo with 1536 becomes 2048×1536. Measured as displayed. Never scales up | ≥ 16 |
| `fps` | the frame rate's cap, for videos and animations. Frames are dropped, never invented; a 24 fps video stays 24 under a cap of 30 | 1–240 |
| `audio_bitrate` | the audio's bitrate, in bits per second (`96_000`) | 32,000–320,000 |
| `codec` | the video codec: `"h264"` or `"h265"` for MP4, MOV, MKV, 3GP and TS; `"vp9"` for WebM. `None` (the default) picks by container ([codecs](#codecs-chosen-when-writing)). One the container can't hold raises `ValueError` | — |

A value outside its range raises `ValueError` before anything is read. Settings that don't apply to the file's kind are ignored (a photo ignores `codec`, `fps` and `audio_bitrate`).

- **Constant quality** (CRF) keeps the quality the same and lets the size follow, with no bitrate guessing.
- Video scaling is `scale='if(gte(iw,ih),-2,min(iw,R))':'if(gte(iw,ih),min(ih,R),-2)',setsar=1`; `-2` keeps both sides even, which H.264 needs. Photo scaling is `Image.thumbnail` with Lanczos filtering.
- **`codec`**: H.264 plays on every browser, phone, TV and editor. H.265 is 30–50% smaller, but Chrome and Firefox on many machines, and older Windows, can't play it. VP9 plays in every browser, and encodes 2–4 times slower than x264.
- A video is written as 8-bit `yuv420p`, because that plays everywhere; a 10-bit HDR source loses its HDR. The docstring says so.
- **`progress`** is a `(done, total)` callback in seconds of media, matching `log.progress`'s `bar.update(done, total=None)`. When it is `None`, a video or audio encode shows its own `log.progress(src.name, total=duration)` bar. Photos report nothing.

### How a video is encoded

`compress` builds one command, and `convert`, `trim` and `merge` build the same one when they must encode:

```sh
ffmpeg -hide_banner -nostdin -v error -protocol_whitelist file -format_whitelist <readers> -i file:/abs/clip.mp4 \
  -map 0:v:0 -map 0:a:0? -map_metadata -1 -map_chapters -1 -fflags +bitexact -flags:v +bitexact -flags:a +bitexact \
  -vf <scale>,fps=<cap>,setsar=1 -c:v libx264 -crf 23 -preset medium -pix_fmt yuv420p \
  -c:a aac -b:a 128k -movflags +faststart -progress pipe:1 -nostats -f mp4 -n file:/abs/.clip.mp4.k3x9.part
```

- The `fps` filter is added only when the source's frame rate is above the cap, so frames are never duplicated.
- WebM uses `-c:v libvpx-vp9 -crf 34 -b:v 0 -row-mt 1 -deadline good -cpu-used 2 -c:a libopus`.
- With `strip=False`, and in `convert`, `trim` and `merge`, the `-map_metadata -1 -map_chapters -1` and `bitexact` flags are left out.
- **Progress.** The duration comes from a `probe` first. `-progress pipe:1` writes `out_time_us=…` about twice a second; each line calls `progress`. stderr goes to a temp file, so a chatty ffmpeg can't fill a pipe and deadlock.
- **A stall timer, not a total timeout.** A long encode legitimately takes many minutes. ffmpeg is killed when no progress line has arrived for **60 s**, raising `TimeoutError`.
- **Encoders.** `ffmpeg -encoders` is read once. A `PATH` ffmpeg without an encoder a call needs (`libx264`, `libx265`, `libvpx-vp9`, `libopus`, `libmp3lame`; Fedora's `ffmpeg-free` lacks x264) raises `RuntimeError("this ffmpeg has no libx264 encoder; …")`. Every imageio-ffmpeg build checked (0.8.1, macOS arm64, Linux x86_64, Windows x64) is a GPL build with x264, x265, libvpx and Opus.

## media.image

Still photos only. An animated photo raises `ValueError` naming `media.convert` (which keeps animation) or `media.video.frames`.

### resize

`resize(src, *, dest=None, width=None, height=None, resolution=None, quality=90, strip=False) -> Path`

- `width` and/or `height` give a box the photo is fitted into, keeping its aspect ratio: `width=1200` alone sets the width; both fit it inside `width × height`. `resolution` caps the shorter side, as in `compress`. Exactly one way must be given, or `ValueError`.
- **It scales up too** when the box is larger, because asking for a size means wanting that size. `compress` never scales up: it only makes things smaller.
- The format stays the same unless `dest=` names another still format. Orientation is applied first, so sizes are as displayed. Lanczos filtering. Metadata is kept unless `strip=True`.
- No `dest=` replaces `src`.

### crop

`crop(src, *, box, dest=None, quality=90, strip=False) -> Path`

- `box` is `(left, top, right, bottom)` in pixels, **as displayed**, as in Pillow. A box outside the photo, or an empty one, raises `ValueError`.
- The same format, metadata and `dest=` rules as `resize`.

## media.video

Anything with a timeline: videos and, where it makes sense, animated photos and audio files.

### trim

`trim(src, *, start=0.0, end=None, dest=None, exact=True) -> Path`

It keeps the part from `start` to `end`, in seconds; `end=None` means to the end. It works on videos, audio files and animations.

- **`exact=True`** (the default) cuts at exactly those times, re-encoding the video at the `"high"` preset's quality (H.264 or VP9 by container), with the audio copied when it can be. Slower, but what people expect.
- **`exact=False`** copies the streams, so it is as fast as a file copy and loses nothing, but a video then starts at the **keyframe at or before `start`**, up to a few seconds early. Audio files cut exactly either way.
- `start >= end`, a negative time, or a `start` past the duration raises `ValueError`.
- Metadata is kept. No `dest=` replaces `src`. The [stall timer](#how-a-video-is-encoded) applies when encoding, a 600 s timeout when copying.

### frames

`frames(src, *, dest_dir, every=None, at=None, count=None, format="jpg", quality=90, limit=1000) -> list[Path]`

It saves frames of a video or animation as photos, and returns their paths in time order. At most one of these chooses the frames:

| Argument | Frames saved |
| --- | --- |
| `every=2.0` | one every 2 seconds, from 0 |
| `at=[0, 12.5, 30]` | the frames at those times |
| `count=9` | that many, evenly spread (each in the middle of its ninth of the video): thumbnails or a contact sheet |
| none of them | **every** frame |

- `format` is any still format from the [write table](#write); `quality` as in `convert`.
- **Names** are `<stem>_<seconds>.<ext>`, with the seconds zero-padded to three decimals so they sort in time order: `clip_00012.500.jpg`. Existing files with those names raise `FileExistsError` before anything is written. `dest_dir` is created when missing.
- **`limit`** guards the disk: a request that would write more than `limit` frames raises `ValueError` before writing any. A 10-minute 60 fps video has 36,000 frames. `limit=None` lifts it.
- Frames are taken **as displayed**: rotated, with square pixels.
- **How:** a video is decoded once by ffmpeg, with `-vf fps=1/every` (or `select` for `at=` and `count=`), written as numbered files into a temp folder next to `dest_dir`, then renamed. That output file pattern is our own path, so it isn't the input-side `image2` risk. An animation is read by Pillow, frame by frame.

### merge

`merge(sources, *, dest, audio=None, fps=None, duration=None, progress=None) -> Path`

It **combines several inputs into one** moving picture. What it does depends on what `sources` holds:

| `sources` | Result |
| --- | --- |
| **photos** (`["a.jpg", "b.png", …]`) | a slideshow or animation: each photo shown for `duration` seconds (default 1.0), or the photos played as frames at `fps` frames per second. Give one or the other. All are scaled and letterboxed to the first photo's size, as displayed |
| **videos** (`["part1.mp4", "part2.webm"]`) | the clips **end to end**. When they share codecs, size and frame rate, the streams are copied; otherwise everything is encoded to the first clip's size, with black bars where needed. A clip without audio gets silence, so the sound stays in sync |
| **one video** plus `audio=` | **the DASH case:** the video's first video stream and `audio`'s first audio stream, **copied** without re-encoding. Any audio already in the video is ignored |

- **`audio=`** adds a soundtrack to any of these. With photos or several clips, the result ends when the pictures end (`-shortest`).
- **`dest`** may be a video (`.mp4`, `.webm`, `.mkv`, …) or an animated image (`.gif`, `.webp`, `.png`). An animated image can't hold `audio=`, which raises `ValueError`.
- Mixing photos and videos in one `sources` raises `ValueError`, and so does an empty list. One photo is fine: one frame, shown for `duration`.
- **How photos reach ffmpeg:** Pillow decodes each photo, and its pixels are written to ffmpeg's stdin as raw frames (`-f rawvideo -pix_fmt rgb24 -s WxH -r <fps> -i pipe:0`). This avoids ffmpeg's `image2` pattern reader, which is refused on input ([Safety](#safety)). Checked on ffmpeg 9.0.2: `-nostdin` still allows `pipe:0` input.
- **How clips are joined:**
  - **when they must be encoded,** with ffmpeg's `concat` *filter*, each clip being its own whitelisted `-i file:` input;
  - **when they can be copied,** with the `concat` *reader* over a list file **that `media` writes itself** into a temp folder, after `probe` has accepted every clip: one `file 'file:/abs/part1.mp4'` line per clip, quotes escaped, read with `-safe 1 -protocol_whitelist file -format_whitelist concat,<readers>`. The `concat` reader is refused for any input that comes from outside ([Safety](#safety)), because an untrusted list can name any file.
- In the DASH case, the container comes from `dest`; codecs it can't hold raise `RuntimeError` with ffmpeg's last line; and it reads every packet, so a truncated part fails here. The timeout is 600 s. Every other case that encodes has the stall timer and `progress`.
- The inputs are never changed or deleted.

## media.audio

### extract

`extract(src, *, dest, bitrate=None) -> Path`

It saves the first audio stream of a video (or of any file with audio) as an audio file. The format comes from `dest`'s extension ([write table](#write)). The stream is **copied** when the format holds its codec (AAC into `.m4a`, Opus into `.opus`), and otherwise encoded at `bitrate`, or the format's default (MP3 192 kb/s, AAC 160, Opus 128). A file without audio raises `BrokenError("clip.mp4 has no audio stream")`. Title, artist and similar tags are kept.

Audio-to-audio conversion is `media.convert`. `media.audio` exists so that "video → audio" is easy to find, and as the home for audio-only functions later.

## check

`check(path) -> None` raises `BrokenError` when the file is truncated, corrupt, or not media, naming the file and the first error: `BrokenError("clip.mp4: corrupt input packet in stream 0")`.
- **A photo** is fully decoded (`img.load()`, every frame of an animation), with `LOAD_TRUNCATED_IMAGES` left off. About 50 ms for 12 MP.
- **A video or audio file** has every packet of every stream read, **without decoding**: `-map 0 -c copy -f null -`, with `-xerror`. That catches what a download leaves behind (a missing `moov` index, a cut-off last packet), checked on ffmpeg 9.0. It doesn't catch damage *inside* frames, which only decoding finds, at about 50 times the cost; the docstring says so. The timeout is 600 s.

## sniff

`sniff(path) -> str | None` reads the first 64 bytes and returns the extension the content calls for. It needs neither Pillow nor ffmpeg, so flnd runs it on every download. "Sniffing" is the usual word for this (browsers' MIME sniffing). `probe` uses the same table for `format`.

| First bytes | Returns |
| --- | --- |
| `FF D8 FF` | `.jpg` |
| `89 50 4E 47 0D 0A 1A 0A` | `.png` |
| `GIF87a` / `GIF89a` | `.gif` |
| `RIFF` … `WEBP` / `WAVE` / `AVI ` | `.webp` / `.wav` / `.avi` |
| `II*\0` / `MM\0*` | `.tif` |
| `BM` | `.bmp` |
| `00 00 01 00` | `.ico` |
| `ftyp` at offset 4, brand `avif`/`avis`, or `mif1` with `avif` among the compatible brands | `.avif` |
| `ftyp`, brand `heic`/`heix`/`heim`/`heis`/`hevc`/`hevx`, or `mif1`/`msf1` | `.heic` |
| `ftyp`, brand `qt  ` | `.mov` |
| `ftyp`, brand `M4A ` / `3gp4`–`3gp6` | `.m4a` / `.3gp` |
| `ftyp`, any other brand (`isom`, `iso2`–`iso9`, `mp41`, `mp42`, `avc1`, `dash`, `M4V `) | `.mp4` |
| `1A 45 DF A3`, DocType `webm` | `.webm` (otherwise `.mkv`) |
| `47` at bytes 0 and 188 | `.ts` |
| `FLV` | `.flv` |
| `OggS`, then `OpusHead` / `\x01vorbis` / `\x80theora` | `.opus` / `.ogg` / `.ogv` |
| `fLaC` | `.flac` |
| `ID3`, or an MPEG audio frame sync (`FF Ex` / `FF Fx`) | `.mp3` |
| `FORM` … `AIFF` | `.aiff` |
| `<` after optional whitespace or a BOM | `.html`: an error page, not media |
| `{` or `[` after optional whitespace or a BOM | `.json` |
| anything else | `None` |

It returns an extension, ready to put on a file name. `.html` and `.json` are returned rather than `None` because "the server sent a web page" is exactly what a downloader needs to know.

## Finding ffmpeg

Private, done once on the first video or audio call:
1. `imageio_ffmpeg.get_ffmpeg_exe()` when imageio-ffmpeg is installed. It honors the **`IMAGEIO_FFMPEG_EXE`** environment variable, so a user can point `media` at any ffmpeg without a setting of ours.
2. Otherwise `shutil.which("ffmpeg")`.
3. Otherwise a video or audio call raises `RuntimeError("shared.media needs ffmpeg for videos and audio: pip install 'flntools[media]', or install ffmpeg on your PATH")`. Photos still work.

`ffmpeg -version` is read once. Older than **4.4** raises `RuntimeError`; an unparseable version (a git build) is accepted. Details and the reasons for the wheel are in [ffmpeg](ffmpeg.md).

**Pillow** is imported on the first photo call. A missing Pillow raises the usual missing-extra `ImportError("shared.media needs Pillow for photos: pip install 'flntools[media]'")`; videos and audio still work. `pi_heif.register_heif_opener()` runs at the first photo call, not on import. `sniff` needs neither.

## Output

Every function that writes:
- writes to a fresh `.<name>.<random>.part` **next to the final file** (or a temp folder next to `dest_dir`, for `frames`), and `os.replace`s it into place only when complete;
- refuses an existing `dest` with `FileExistsError` before reading anything, except when `dest=` is left out and the function [replaces the source](#the-layout);
- deletes the `.part` on any failure, timeout or Ctrl-C, and kills ffmpeg if it is running;
- returns the final path, or paths.

## Safety

Media comes from the network, so it is untrusted.

| Threat | Rule | Error |
| --- | --- | --- |
| **ffmpeg reading something other than the file**: an HLS or DASH playlist saved as `.mp4` that pulls in `/etc/passwd`, an `ffconcat` list, an `image2` pattern, a network URL, a name read as an option (`-y.mp4`) or a protocol (`concat:a\|b`) | every input is `file:` + the absolute path, with `-protocol_whitelist file` and `-format_whitelist` naming **only the readers in the [read table](#read)**: `mov,matroska,avi,mpegts,mpeg,flv,asf,ogg,mp3,aac,flac,wav,aiff,caf,amr,gif`. None of them opens another file or URL. Refused, by not being listed: `hls`, `dash`, `concat`, `image2`, every `*_pipe` reader, `sdp`, `rtsp`, `lavfi`, `tee`, `data`, `subfile`, `live_flv`. The two exceptions are inputs `media` builds itself: the raw-frame pipe (`rawvideo` on `pipe:0`, bytes we write) and `merge`'s own concat list. `enable_drefs` stays off. Full rules in [ffmpeg › Running ffmpeg safely](ffmpeg.md#running-ffmpeg-safely) | `UnsafeError` |
| **a Pillow decoder that runs another program**: EPS starts Ghostscript | every open passes `formats=` naming only the [readable formats](#read); `EPS` is left out, as are `PDF`, `WMF`/`EMF` and the rarer plugins | `UnsafeError` |
| **a decompression bomb**: a tiny file declaring 100,000 × 100,000 pixels | Pillow's own check, from the header, past `2 × MAX_IMAGE_PIXELS` (about 179 MP). Its 89 MP warning is silenced, so 108 MP and 200 MP phone photos open. An animation's frames × pixels is capped the same way before decoding | `UnsafeError` |
| **a disk bomb from frames** | `frames(limit=1000)` checks the count before writing | `ValueError` |
| **truncated data** | never padded: `LOAD_TRUNCATED_IMAGES` stays `False`, and ffmpeg runs with `-xerror` where it reads packets | `BrokenError` |
| **a crash on hostile media** | in ffmpeg, it stays in ffmpeg's process and becomes `BrokenError` with its last line. In Pillow's C decoders (libjpeg, libpng, libwebp, libtiff, libheif, libavif, openjpeg) it can't be contained; they are fuzzed through OSS-Fuzz and pip keeps them current. **TIFF and JPEG 2000 are the riskiest of these**, and are allowed because the user chose every safe local format; the docstring says so | `BrokenError` |
| **hangs** | 30 s for `probe`; 600 s for stream copies and `check`; a 60 s stall timer for every encode | `TimeoutError` |
| **stolen keystrokes, stray report files** | `-nostdin`; `FFREPORT` removed from ffmpeg's environment; `AV_LOG_FORCE_NOCOLOR=1` | — |

## Errors

| Error | Base | When |
| --- | --- | --- |
| `media.BrokenError` | `ValueError` | the file is truncated, corrupt, not media, or lacks a stream the call needs |
| `media.UnsafeError` | `ValueError` | the file is a format `media` refuses to open, or a decompression bomb |
| `ValueError` | — | an unknown `preset`, `codec` or `format`; a `dest` extension that isn't allowed; a conversion that changes kind; an animated photo where a still is needed; bad times, boxes or sizes; too many frames |
| `RuntimeError` | — | ffmpeg missing, too old, or without an encoder the call needs; codecs `merge`'s container can't hold |
| `ImportError` | — | Pillow missing, for a photo call |
| `FileExistsError`, `TimeoutError` | builtins | `dest` exists; a timeout ran out or an encode stalled |

`UnsafeError` covers both "format not allowed" and "bomb", because a caller does the same with both: skip the file.

## pyproject

```toml
[project.optional-dependencies]
media = ["pillow>=11.3", "pi-heif>=1.0", "imageio-ffmpeg>=0.6"]
```

- `pillow>=11.3`: the first with AVIF in its wheels. On Python 3.11 and later this resolves to Pillow 13, which reads and writes AVIF (checked).
- `pi-heif`: HEIC, **decode only**. It leaves out the x265 encoder, which is GPL, so photos carry no GPL code.
- `imageio-ffmpeg`: the ffmpeg binary, 20–68 MB wheels ([ffmpeg](ffmpeg.md)).

One extra, because `media` is one module. flnd lists all three as plain dependencies ([flnd › pyproject](flnd.md#pyproject)).

## Rejected

| Idea | Why not |
| --- | --- |
| **separate `image` and `video` modules** | the first draft. Every caller would sniff and choose, and `Info`, the presets, the output rule and the errors would be written twice |
| **only flat functions** (`media.trim`, `media.resize`, …) | with a dozen functions, the kind each one works on is no longer obvious from its name. The namespaces say it |
| **a public `which_ffmpeg()` or `run(*args)`** | finding ffmpeg is `media`'s job. A `run` couldn't apply the input rules to arguments it doesn't understand, and would be a subprocess helper ([Out of scope](../README.md#out-of-scope)). A script that needs a rarer ffmpeg command runs `imageio_ffmpeg.get_ffmpeg_exe()` itself |
| **writing HEIC** | Pillow has no HEIC writer, and pi-heif is decode-only. The full pillow-heif writes HEIC, but its wheels link x265 (GPL) **into flnd's own process**, which would put flnd itself under the GPL when bundled, unlike ffmpeg, which is a separate program |
| **JPEG XL** | Pillow 13 has no JPEG XL support (checked); it needs a plugin with its own native library. Reconsider when Pillow adds it |
| **`ffprobe` JSON for `probe`** | the wheel ships no `ffprobe`. ffmpeg's own summary plus `ffmetadata` give the same fields in one run |
| **PyAV** | it has dropped Python 3.11 in its latest release, breaks its API every few months, and a decoder crash would take flnd down ([ffmpeg](ffmpeg.md#why-the-wheel-and-not-pyav)) |
| **ffmpeg for stills** | it ignores EXIF orientation for stills and drops EXIF. Pillow is in process and about 50 ms per photo |
| **AV1 encoding** | the smallest files, but very slow on Windows, whose wheel has only `libaom`; older phones and TVs can't decode it. A fourth `codec` when the wheels carry SVT-AV1 everywhere |
| **hardware encoders** (VideoToolbox, NVENC, QSV) | lower quality per byte, and different on every machine, so a preset would mean different things |
| **raw CRF and x264 `-preset` arguments; a target file size** | `quality`, `resolution`, `fps`, `audio_bitrate` and `codec` cover what people choose. A target size means several encodes per file |
| **filters and effects**: rotate, flip, watermark, color, speed, reverse, loudness normalization, burned-in subtitles | each is its own small API, with no caller yet. They would go in the namespaces (`media.image.rotate`, `media.audio.normalize`) when one appears |
| **writing or editing metadata** (`media.set_tags`), or stripping only some of it | no caller. `compress` strips everything a viewer doesn't need, or nothing with `strip=False` |
| **HLS / m3u8 downloads** | ffmpeg would fetch from the network itself, bypassing the engine's cookies, retries and progress. No source needs it |
| **near-duplicate detection** (v8's frame hashes) | it scanned every stored file on every save; exact duplicates are caught by the engine's history |
| **EPS, PDF, WMF/EMF, SVG** | EPS and PDF need Ghostscript, WMF/EMF only open on Windows, and Pillow can't read SVG |
| **async variants** | every `shared` API is synchronous; flnd calls `media` from its download pool's threads |

## Tests

`tests/test_media.py`, `test_media_image.py`, `test_media_video.py` and `test_media_audio.py`. **Fixtures are generated** in `setUpClass`, into a temp folder:
- **photos** by Pillow, in every writable format: a JPEG with EXIF orientation 6, `DateTimeOriginal`, `OffsetTimeOriginal`, `Model`, a GPS block and a Display P3 ICC profile; an sRGB JPEG; an RGBA PNG; animated WebP, GIF and APNG; a 4000×3000 quality-95 noise JPEG that really shrinks;
- **HEIC** is the one exception, because pi-heif can't write it: `tests/data/tiny.heic`, a committed 16×16 file of about 1 KB made with macOS `sips -s format heic`, its SHA-256 listed in the test;
- **videos and audio** by calling ffmpeg directly (not through `media`, whose whitelist rightly refuses `lavfi`), with only built-in encoders so any build can make them: `v.mp4` (video only), `a.m4a` (audio only), `av.mkv` (both), `meta.mp4` with `creation_time`, an ISO 6709 `location`, a title, a chapter, a subtitle stream and a rotation of 90°; `big.mp4`, 3 s of portrait 360×640 at 60 fps, `mpeg4 -q:v 2` plus 192 kb/s AAC, about 1.5 MB; and one short file in each other readable container (AVI, TS, MPG, FLV, WMV, OGV) and audio format (MP3 where `libmp3lame` exists, FLAC, WAV, AIFF, Ogg).

Video and audio tests are skipped without ffmpeg; tests needing `libx264`, `libx265`, `libvpx-vp9`, `libopus` or `libmp3lame` are skipped when the ffmpeg lacks it.

- **`probe`:**
  - every readable format gives the right `kind`, `format`, size and `animated`; an animated GIF gives `frames`, `duration` and `fps`;
  - the orientation-6 JPEG reports its width and height swapped; the EXIF JPEG gives an aware `created`, its `location` to 4 decimals, and `tags["Model"]`; `MakerNote` isn't in `tags`;
  - a PNG renamed `.jpg` reports `"png"`;
  - `v.mp4` gives `duration` ≈ 1.0, 64×48, `video="mpeg4"`, `audio=None`, `fps` 10; `a.m4a` gives `kind="audio"`, a `sample_rate` and `channels`; `meta.mp4` gives `created`, `location` and its rotated size;
  - a `0000:00:00 00:00:00` date and a `0, 0` GPS give `None`.
- **The ffmpeg output parser**, without ffmpeg, on text captured from ffmpeg 4.4, 6.1 and 9.0: `Duration: N/A`; `(default)` and side data; a display matrix; a title with a newline and a fake `Stream #0:5: Video: h264, 9999x9999` line, which isn't counted in either output; `ffmetadata` escapes.
- **`convert`:**
  - every readable still format to every writable still format, spot-checked by probing the output; orientation applied and reset; EXIF and ICC kept; RGBA flattened onto white for JPEG; `quality` changes the size; a large photo to `.ico` is scaled to 256;
  - animated WebP → GIF and GIF → APNG keep every frame and its duration; animated → `.jpg` raises `ValueError` naming `media.video.frames`;
  - `av.mkv` (mpeg4 + AAC) to `.mp4` is a **copy** (codecs unchanged, fast); a VP9 + Vorbis WebM to `.mp4` is encoded to H.264 + AAC; `big.mp4` to `.webm` gives VP9 + Opus, and to `.gif` at most 15 fps; GIF to `.mp4` works;
  - `fps=30` on `big.mp4` gives 30 fps; `fps=` on a photo raises `ValueError`;
  - FLAC → MP3, Opus, Ogg and WAV; AAC → `.m4a` is a copy;
  - a video to `.jpg` and a photo to `.mp4` raise `ValueError` naming the right function; `.xyz` raises `ValueError`;
  - an existing `dest` raises `FileExistsError`; a failure leaves no `.part`.
- **`compress`:**
  - each preset, on the big JPEG and `big.mp4`: smaller, passes `check`, and `"small"` < `"balanced"` < `"high"` in size. The video probes as `h264` + `aac`, duration ≈ 3.0, and at most 30 fps for `"small"`;
  - **same format:** JPEG, WebP, AVIF, PNG and GIF stay in their format; MP4, MOV and MKV stay H.264 in their container; WebM stays VP9 + Opus; MP3 stays MP3 at the lower bitrate; FLV becomes `.mp4`;
  - **`format=`:** `a.heic` with `format="jpg"` → `a.jpg`; `clip.webm` with `format="mp4"` → `clip.mp4`; FLAC with `format="opus"` → `.opus`; the old files are gone; a photo with `format="mp4"`, and `format="jpg"` with `dest="x.webp"`, raise `ValueError`;
  - **in place:** one file left; an existing `a.jpg` next to `a.heic` with `format="jpg"` raises `FileExistsError` and nothing is encoded. **With `dest=`**, `src` is unchanged;
  - **stripped:** the photo is upright with no EXIF, XMP or GPS, keeps the Display P3 profile and drops the sRGB one; `meta.mp4` loses `creation_time`, location, title, chapters, the subtitle stream and the encoder tag, keeps one video and one audio stream, and is upright; an MP3 loses its tags;
  - **`strip=False`** keeps `DateTimeOriginal`, GPS, and the video's `creation_time` and location;
  - `resolution` on a portrait 360×640 video at 240 gives 240×426, on a landscape 640×360 gives 426×240, and 2000 doesn't upscale; `"small"` takes a 4000×3000 photo to 2048×1536;
  - **settings win over the preset:** `preset="small", quality=90` is larger than plain `"small"`; `quality=80` builds CRF 23 (H.264), 27 (H.265) and 34 (VP9); giving every value builds the same command whatever `preset` is; `audio_bitrate=64_000` gives about 64 kb/s;
  - **not smaller** returns `None` and logs the info line; **untouched:** HEIC and FLAC without `format=` return `None`;
  - **progress** only grows, ending ≈ 3.0; with `progress=None` a `log.progress` bar is shown (a fake `theme.screen`);
  - **stall:** a fake `ffmpeg` that prints one progress line and sleeps is killed by a shortened stall timer: `TimeoutError`, and no `.part`;
  - a fake `ffmpeg -encoders` without `libx264` gives the `RuntimeError`;
  - `preset="tiny"`, `codec="av1"`, `codec="h265"` for WebM, `quality=0`, `quality=101`, `fps=0` and `audio_bitrate=8_000` raise `ValueError`.
- **`media.image`:** `resize(width=1200)` on the 4000×3000 JPEG gives 1200×900; `width` with `height` fits inside; a small photo is scaled up; two ways at once raise `ValueError`; `crop` uses the box as displayed on the orientation-6 photo; a box outside raises `ValueError`; an animated WebP raises `ValueError`; no `dest=` replaces the file.
- **`media.video.trim`:** `big.mp4` from 1.0 to 2.0 with `exact=True` lasts 1.0 ± one frame; with `exact=False` it is a copy (codecs unchanged) starting at or before 1.0; an MP3 trims exactly; `start >= end` raises `ValueError`.
- **`media.video.frames`:** `every=1.0` on `big.mp4` gives 3 files, `big_00000.000.jpg` first; `at=[0.5, 2.5]` gives 2; `count=4` gives 4 in time order; no selector on `v.mp4` gives every frame (10); an animated GIF gives its frames; `format="png"` works; `limit=5` on 180 frames raises `ValueError` and writes nothing; an existing frame name raises `FileExistsError`.
- **`media.video.merge`:**
  - photos with `duration=2.0` → `.mp4` lasting 6.0, at the first photo's size, the others letterboxed; with `fps=10` → 0.3 s; photos → `.gif` and `.webp` animations;
  - `v.mp4` + `audio="a.m4a"` → `.mp4` and `.mkv` by **copy**, both streams, duration ≈ 1.0; an audio input without audio raises `BrokenError`; a codec the container can't hold raises `RuntimeError` and leaves no file;
  - two clips of the same format join by copy, lasting the sum; an MP4 and a WebM of different sizes join by encoding at the first's size; a clip without audio gets silence and the sound stays in sync;
  - photos with `audio=` end with the pictures; `audio=` with `.gif` raises `ValueError`; photos mixed with videos raise `ValueError`;
  - the built commands never use the `image2` reader, and use the `concat` reader only on `media`'s own list file.
- **`media.audio.extract`:** `av.mkv` → `.m4a` is a copy; → `.mp3` is encoded at 192 kb/s, or at `bitrate=`; `v.mp4` raises `BrokenError`.
- **`check`:** every fixture passes; a JPEG cut in half and `v.mp4` cut to 70% raise `BrokenError` naming the file; random bytes and an HTML page named `.mp4` or `.jpg` raise `BrokenError`.
- **`sniff`:** every row of the table from byte prefixes, `mif1` + `avif`, an HTML body with a BOM, and an empty file (`None`).
- **Safety:**
  - an HLS playlist pointing at a local secret file, named `list.m3u8` and `clip.mp4`, an `ffconcat` file, and a DASH `.mpd`: refused, `UnsafeError` for the `.m3u8`, and no output;
  - inputs named `-y.mp4`, `http:x.mp4` and `concat:a.mp4|b.mp4` are read as plain local files;
  - with `FFREPORT=file=report.log` set, no report file appears;
  - an EPS raises `UnsafeError`, and a fake `gs` on `PATH` that fails the test never runs;
  - a PNG header declaring 20,000 × 20,000 pixels raises `UnsafeError` without allocating them, and so does a GIF whose frames × size passes the limit;
  - `LOAD_TRUNCATED_IMAGES` is still `False` after every call;
  - a `KeyboardInterrupt` during an ffmpeg run leaves no `.part` and no child process.
- **Finding ffmpeg:** imageio-ffmpeg's binary; `IMAGEIO_FFMPEG_EXE` pointing at a fake; imageio-ffmpeg blocked and a fake on a temp `PATH`; neither gives the install message on a video call while photos still work; version `4.2` refused, and `4.4.2-0ubuntu0.22.04.1`, `n7.1` and `N-12345-gabc` accepted.
- **Lazy import:** `from shared import log` imports neither PIL nor `shared._media`; `from shared import media` imports neither PIL nor `shared._media_video`; `sniff` works with PIL and imageio-ffmpeg blocked; with PIL blocked, a photo call raises the missing-extra `ImportError`.
