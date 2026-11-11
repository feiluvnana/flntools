# media

Phase 5. `shared.media` is one module for photos, videos and audio: **read what a file is, convert it, compress it**, plus the two jobs flnd's downloads need, merging DASH streams and checking a download is whole. It replaces the separate `image` and `video` modules of the first draft.

```python
from shared import media

info = media.probe("IMG_0042.heic")         # works the same for photos, videos and audio
info.kind, info.format                       # "image", "heif"
info.width, info.height                      # 3024, 4032: as displayed, rotation applied
info.created, info.location                  # datetime(2026, 1, 2, 9, 30, tzinfo=…), (35.6895, 139.6917)
info.tags["Model"]                           # "iPhone 15 Pro": every metadata field, by name

media.convert("IMG_0042.heic", dest="IMG_0042.jpg")          # the format comes from dest's extension
media.convert("clip.webm", dest="clip.mp4")                  # copies the streams when they fit, re-encodes when not
media.convert("clip.mp4", dest="clip.m4a")                   # the audio only

media.compress("IMG_0042.jpg")                               # in place, "balanced": replaces the file when the result is smaller
media.compress("IMG_0042.jpg", quality=75, resolution=1080)  # exact settings instead of a preset's
media.compress("clip.mp4", dest="small.mp4", preset="small", resolution=480)   # a preset, one setting changed; the source stays

media.merge("v.mp4", audio="a.m4a", dest="out.mp4")          # DASH video + audio, without re-encoding
media.check("clip.mp4")                                      # raises media.BrokenError when truncated or corrupt
media.sniff("download.part")                                 # ".webp", from the first bytes; no library needed
```

**The file is the one positional argument; everything else is named** (Naming rule 7). `convert(a, b)` doesn't say which file is read and which is written, and `merge(a, b, c)` is worse, so `dest=` and `audio=` are always written out, and so is every setting.

Every function takes a path to a file **of any kind** and decides what it holds from its **content**, never its name. Photos go through **Pillow** (with **pi-heif** for HEIC), in process. Videos and audio go through the **ffmpeg** program, out of process. The caller never chooses, and never sees ffmpeg's path or arguments: there is no `which_ffmpeg()` and no `run(*args)`.

## Why one module

- **The caller thinks in files, not libraries.** "Make this smaller", "what is this", "turn it into a JPEG" are the same questions for a photo and a video. With two modules, every caller first sniffs the file and picks one: flnd's engine would have `if kind == "photo": image.compress(…) else: video.compress(…)` in four places.
- **One `Info`** for both. A photo and a video have the same questions: size, date taken, where, what camera.
- **Shared code.** Content sniffing, the temp-file-then-rename output, the "didn't get smaller" rule, the presets, and the errors are written once.
- **It has a precedent.** `archive` already wraps `zipfile`, `tarfile`, `pyzipper`, `py7zr` and the `unrar` program behind one `zip` / `unzip`, and chooses by content. `media` does the same with Pillow and ffmpeg. Rule 1 ("wrap one library") is about not rebuilding a library, which `media` doesn't: it is a thin layer over two.

## Files and budget

Rule 3 counts a module as all of its files, in lines of code (no docstrings, comments or blanks):

| File | Holds | Lines |
| --- | --- | --- |
| `shared/_media.py` | the public functions, `Info`, the errors, content sniffing, presets, the output rule | 130 |
| `shared/_ffmpeg.py` | finding ffmpeg, the version and encoder checks, the safe runner with its timeouts, parsing ffmpeg's output, and the commands | 210 |
| `shared/_pillow.py` | the safe open, EXIF to `tags` / `created` / `location`, the write pipeline, decoding for `check` | 120 |
| **`media`** | | **about 460** |

**This is past Rule 3's 400.** The reasons:
- it wraps two engines, not one, so it is really two ~200-line modules sharing one front;
- it is **smaller than what it replaces**: the first draft's `image` (170) and `video` (295), plus flnd's 35-line sniffer, came to 500;
- splitting it again brings back the two-module API this redesign removes.

`shared` goes from about 3,600 to about **4,060** of the 4,500 ceiling.

## probe

`probe(path) -> Info`

It reads what the file is and its metadata, **without decoding** the picture or the video. A photo opens in Pillow and reads its header and EXIF. A video or audio file is one ffmpeg run of a fraction of a second ([how](#how-a-video-is-probed)). A file that isn't a photo, video or audio file raises `BrokenError`.

`Info` is a frozen dataclass. It is a value, like `os.stat_result`, not a wrapper, so it has no `.raw`; `tags` holds everything that was read.

| Field | Photo | Video / audio |
| --- | --- | --- |
| `path` | the file | the file |
| `kind` | `"image"` | `"video"`, or `"audio"` when there is no video stream |
| `format` | `"jpeg"`, `"png"`, `"gif"`, `"webp"`, `"heif"`, `"avif"` | `"mp4"`, `"mov"`, `"webm"`, `"mkv"`, `"m4a"` (from the content, by the [sniffing table](#sniff)) |
| `size` | bytes on disk | bytes on disk |
| `width`, `height` | **as displayed**: EXIF orientation 5–8 swaps them | **as displayed**: a 90° rotation in the display matrix swaps them. `None` for audio |
| `duration` | `None` | seconds, or `None` when ffmpeg prints `N/A` (some WebM) |
| `frames` | 1 for a still, more for an animated GIF or WebP | `None` |
| `fps` | `None` | of the first video stream, or `None` |
| `bitrate` | `None` | the whole file's, in bits per second, or `None` |
| `video` | `None` | the first video stream's codec: `"h264"`, `"hevc"`, `"vp9"`, `"av1"`. `None` when there is none |
| `audio` | `None` | the first audio stream's codec: `"aac"`, `"opus"`. **A DASH video stream has `audio=None`** |
| `created` | EXIF `DateTimeOriginal`, with `OffsetTimeOriginal` when present; else EXIF `DateTime` | the container's `creation_time` |
| `location` | `(latitude, longitude)` from the EXIF GPS block | from the ISO 6709 `location` or `com.apple.quicktime.location.ISO6709` tag |
| `tags` | every EXIF field, the Exif and GPS blocks included, named by Pillow's `ExifTags` (`"Make"`, `"Model"`, `"DateTimeOriginal"`, `"GPSLatitude"`), as strings. Binary fields (`MakerNote`, thumbnails) are left out | the file's metadata (`title`, `comment`, `encoder`, `location`, …), as strings |

- `created` is a `datetime`: timezone-aware when the file says the offset (`OffsetTimeOriginal`, or a `Z` in `creation_time`), naive otherwise. A date that doesn't parse (`0000:00:00 00:00:00`, which cameras write) gives `None`, and stays in `tags` as written.
- `location` is `None` when absent, and also when it is `0, 0`, which some apps write meaning "unknown".
- **Nothing in `Info` decodes pixels or frames,** so probing a folder of a thousand files takes seconds.

### How a video is probed

One ffmpeg run gives both the stream summary and the metadata:

```sh
ffmpeg -hide_banner -nostdin -v info -protocol_whitelist file -format_whitelist mov,matroska \
  -i file:/abs/clip.mp4 -f ffmetadata pipe:1
```

- **stdout** is the metadata in ffmpeg's `ffmetadata` format: `;FFMETADATA1`, then `key=value` lines in which `=`, `;`, `#`, `\` and newline are escaped with `\`. So a value can't fake another key. That gives `tags`.
- **stderr** is the `Input #0` summary, read up to `Stream mapping:`:
  - `Duration: HH:MM:SS.ss, start: …, bitrate: N kb/s` gives `duration` and `bitrate`;
  - lines matching exactly `^  Stream #0:\d+.*?: (Video|Audio): (\w+)` give the codecs. For video, the first `, (\d+)x(\d+)[ ,]` after the codec gives the stored size, and `(\d+(?:\.\d+)?) fps` the frame rate;
  - `displaymatrix: rotation of (-?\d+\.\d+) degrees` under that stream gives the rotation;
  - `^    creation_time   : (\S+)$`, at exactly that indent before `Duration:`, gives `created`. ffmpeg's `ffmetadata` writer leaves `creation_time` out, which is why it is read here.
- **Lines are matched at their exact indent.** A metadata value with a newline is printed on continuation lines that start with spaces and `: `, so a hostile title can't add a fake `Stream` line. Checked on ffmpeg 9.0.2: a title of `hi\nStream #0:5: Video: h264, 9999x9999` stays a title, in both outputs.
- It exits 0 only when the container opens, so `probe` also proves the file is a readable video.
- The timeout is 30 s.

## convert

`convert(src, *, dest, quality=90) -> Path`

It writes `src` as the format **`dest`'s extension** names, and returns `dest`. `src` is never changed or deleted.

| `src` | `dest` extension | What happens |
| --- | --- | --- |
| a photo | `.jpg` / `.jpeg`, `.png`, `.webp` | decoded and written by Pillow: orientation applied (and the tag reset), EXIF and the ICC profile kept when the format holds them, transparency flattened onto white for JPEG, modes JPEG can't hold made RGB. `quality` (1–100) sets JPEG and WebP quality |
| a video | `.mp4` / `.m4v`, `.mov`, `.mkv` | **the streams are copied** when the container can hold their codecs: fast, no quality lost. When it can't (VP9 + Vorbis into `.mp4`), they are **re-encoded** to H.264 + AAC with the `"high"` [preset](#presets) |
| a video | `.webm` | streams copied when they are VP8/VP9/AV1 + Opus/Vorbis. Otherwise `ValueError`: encoding VP9 is too slow to do quietly |
| a video or audio file | `.m4a` | the first audio stream: copied when it is AAC, else encoded to AAC 160 kb/s |
| a video or audio file | `.mp3` | the first audio stream, encoded with `libmp3lame` at 192 kb/s |

Refused before anything is read, with `ValueError`:
- any other extension;
- a photo to a video or audio format, or a video to a photo format (`"clip.mp4 is a video; .jpg is a photo format"`). Frame grabs and GIF-to-MP4 are [rejected](#rejected) for now;
- an **animated** photo to any format (`"a.webp is animated; .jpg can't hold animation"`), because writing only the first frame would silently drop the rest;
- a video or audio file with no audio, to `.m4a` or `.mp3`.

Which codecs each container holds, for the copy decision: MP4 and MOV hold H.264, HEVC, AV1, VP9 and MPEG-4 video, and AAC, Opus, MP3 and ALAC audio. Matroska holds all of them. WebM holds VP8, VP9 and AV1, and Opus and Vorbis. HEVC copied into MP4 or MOV is tagged `hvc1`, so Apple's players open it. MP4 and MOV get `-movflags +faststart`, so they preview before they are fully read.

## compress

`compress(src, *, dest=None, preset="balanced", quality=None, resolution=None, codec="h264", audio_bitrate=None, progress=None) -> Path | None`

It re-encodes `src` smaller. **The result is kept only if it is smaller than `src`.** It returns the path it wrote, or `None` when nothing changed.

**Where the result goes:**
- **No `dest=`** (the default) **replaces `src`**. The result is written next to `src`, then renamed onto it. When the format changes (`a.heic` → `a.jpg`, `clip.webm` → `clip.mp4`), the new name is used and `src` is deleted after the rename. If that new name already exists, `FileExistsError` is raised before anything is encoded.
- **`dest=` given** writes a copy there, and `src` stays. The format comes from `dest`'s extension, as in `convert`.

So `media.compress(path)` is "make this file smaller", and a loop over a folder is safe:
```python
for path in folder.iterdir():
    media.compress(path, preset="small")   # photos and videos shrink; everything else returns None
```

**`None` is returned, with nothing changed,** when:
- the result isn't smaller. A file that is already well compressed often can't shrink at the same quality. `log.info("a.jpg: compressing saved nothing; kept the original")`;
- the file is one `compress` doesn't touch: PNG and GIF (lossless; mostly screenshots and animations, where lossy compression hurts), animated photos, and audio-only files. `log.debug("a.png: png isn't compressed")`. To shrink a PNG photo, `convert` it to `.jpg`.

A file that isn't media at all raises `BrokenError`, as everywhere else.

**What it writes:**

| Source | Result |
| --- | --- |
| JPEG | JPEG, `optimize=True`, `progressive=True` |
| WebP (still) | WebP |
| HEIC, AVIF | JPEG (`.jpg`): `media` doesn't write HEIC or AVIF |
| any video | MP4 (`.mp4`), H.264 + AAC, or H.265 + AAC with `codec="h265"` |

Photos keep their orientation (applied), EXIF and ICC profile, as in `convert`. Videos keep **only the first video stream and the first audio stream**: subtitles and extra tracks are dropped, and a 10-bit HDR source becomes 8-bit `yuv420p`, because that plays everywhere. The docstring says so.

### Presets

A preset is a name, never raw encoder settings. One table for both kinds:

| `preset` | photo quality | video CRF, H.264 / H.265 | x264/x265 speed | audio (AAC) | `resolution` cap |
| --- | --- | --- | --- | --- | --- |
| `"small"` | 70 | 28 / 32 | `medium` | 64 kb/s | 720 videos, 1536 photos |
| `"balanced"` (default) | 80 | 23 / 28 | `medium` | 128 kb/s | 1080 videos, 2304 photos |
| `"high"` | 90 | 19 / 24 | `slow` | 160 kb/s | none |

**Every setting can be given directly**, and a given setting wins over the preset's. The preset fills in only what isn't given:

| Argument | Means | Range |
| --- | --- | --- |
| `quality` | one scale for photos and videos, higher is better: JPEG/WebP quality for a photo; for a video it becomes the CRF, `round(59.5 - 0.45 × quality)` for H.264, 4 more for H.265, so 70, 80 and 90 give the presets' 28, 23 and 19 | 1–100 |
| `resolution` | the shorter side's cap, in pixels (below) | ≥ 16, or `None` for the preset's |
| `audio_bitrate` | AAC bitrate for a video, in bits per second (`96_000`) | 32,000–320,000 |
| `codec` | `"h264"` or `"h265"`, videos only | — |

So `compress(p, quality=75)` is "balanced, but quality 75", and `compress(p, preset="high", resolution=720)` is "high quality, at most 720p". A value outside its range raises `ValueError` before anything is read. Photos ignore `codec` and `audio_bitrate`.

- **CRF** keeps the quality constant and lets the size follow, which is what "smaller without ruining it" means, with no bitrate guessing. x265's CRF scale runs about 5 higher than x264's for the same quality.
- **`resolution`** is one rule for both kinds: **the shorter side is at most this many pixels**, the way people say "720p". A portrait 1080×1920 video becomes 720×1280; a 4000×3000 photo with 1536 becomes 2048×1536. It is measured **as displayed**, after rotation. Nothing is ever scaled up.
  - Video: `scale='if(gte(iw,ih),-2,min(iw,R))':'if(gte(iw,ih),min(ih,R),-2)',setsar=1`. `-2` keeps both sides even, which H.264 needs.
  - Photo: `Image.thumbnail` with Lanczos filtering.
- **`codec`**: `"h264"` (the default) plays on every browser, phone, TV and editor. `"h265"` is 30–50% smaller, but Chrome and Firefox on many machines, and older Windows, can't play it. Photos ignore it. An unknown value raises `ValueError`.
- **`progress`** is a `(done, total)` callback in seconds of video, matching `log.progress`'s `bar.update(done, total=None)`. When it is `None`, a video gets its own `log.progress(src.name, total=duration)` bar, so a script sees progress without asking. Photos take 0.1–0.3 s and report nothing.

### How a video is compressed

```sh
ffmpeg -hide_banner -nostdin -v error -protocol_whitelist file -format_whitelist mov,matroska -i file:/abs/clip.mp4 \
  -map 0:v:0 -map 0:a:0? -vf <scale>,setsar=1 -c:v libx264 -crf 23 -preset medium -pix_fmt yuv420p \
  -c:a aac -b:a 128k -movflags +faststart -progress pipe:1 -nostats -f mp4 -n file:/abs/.clip.mp4.k3x9.part
```

- **Progress.** The duration comes from a `probe` first. `-progress pipe:1` writes `out_time_us=…` about twice a second; each line calls `progress`. stderr goes to a temp file, so a chatty ffmpeg can't fill a pipe and deadlock.
- **A stall timer, not a total timeout.** A long transcode legitimately takes many minutes. ffmpeg is killed when no progress line has arrived for **60 s**, raising `TimeoutError`.
- **Encoders.** `ffmpeg -encoders` is read once. A `PATH` ffmpeg without `libx264` (Fedora's `ffmpeg-free` and other LGPL builds) raises `RuntimeError("this ffmpeg has no libx264 encoder; …")`. Every imageio-ffmpeg build checked (0.8.1, macOS arm64, Linux x86_64, Windows x64) has libx264 and libx265.

## merge

`merge(video, *, audio, dest) -> Path`

It joins a video-only file and an audio-only file into `dest` **without re-encoding**, which is what DASH needs: Facebook and Instagram serve their best quality as separate tracks ([flnd › DASH](flnd.md#dash-video-and-audio)).

```sh
ffmpeg -hide_banner -nostdin -v error -xerror \
  -protocol_whitelist file -format_whitelist mov,matroska -i file:/abs/v.mp4 \
  -protocol_whitelist file -format_whitelist mov,matroska -i file:/abs/a.m4a \
  -map 0:v:0 -map 1:a:0 -c copy -movflags +faststart -f mp4 -n file:/abs/.out.mp4.k3x9.part
```

- The container comes from `dest`'s extension (`.mp4`/`.m4v`, `.mov`, `.mkv`, `.webm`); any other raises `ValueError`.
- The first video stream of `video` and the first audio stream of `audio` are used. An `audio` without audio raises `BrokenError("a.m4a has no audio stream")`.
- Codecs the container can't hold raise `RuntimeError` with ffmpeg's last line: the caller picked the wrong `dest`. Nothing is re-encoded to fit.
- It reads every packet, so a truncated part fails here. No progress: a stream copy runs at disk speed. The timeout is 600 s. The inputs are never changed or deleted.

## check

`check(path) -> None` raises `BrokenError` when the file is truncated, corrupt, or not media at all, naming the file and the first error: `BrokenError("clip.mp4: corrupt input packet in stream 0")`.
- **A photo** is fully decoded (`img.load()`), with `LOAD_TRUNCATED_IMAGES` left off. About 50 ms for 12 MP. A truncated JPEG raises `"a.jpg: Truncated File Read"`, checked on Pillow 13.
- **A video or audio file** has every packet of every stream read, **without decoding**: `-map 0 -c copy -f null -`, with `-xerror`. That catches what a download leaves behind (a missing `moov` index, a cut-off last packet), checked on ffmpeg 9.0. It doesn't catch damage *inside* frames, which only decoding finds at about 50 times the cost; the docstring says so. The timeout is 600 s.

## sniff

`sniff(path) -> str | None` reads the first 64 bytes and returns the extension the content calls for. It needs neither Pillow nor ffmpeg, and costs one small read, so flnd runs it on every download. It is the same table `probe` uses for `format`.

| First bytes | Returns |
| --- | --- |
| `FF D8 FF` | `.jpg` |
| `89 50 4E 47 0D 0A 1A 0A` | `.png` |
| `GIF87a` / `GIF89a` | `.gif` |
| `RIFF` … `WEBP` | `.webp` |
| `ftyp` at offset 4, brand `avif`/`avis`, or `mif1` with `avif` among the compatible brands | `.avif` |
| `ftyp`, brand `heic`/`heix`/`heim`/`heis`/`hevc`/`hevx`, or `mif1`/`msf1` | `.heic` |
| `ftyp`, brand `qt  ` | `.mov` |
| `ftyp`, brand `M4A ` | `.m4a` |
| `ftyp`, any other brand (`isom`, `iso2`–`iso9`, `mp41`, `mp42`, `avc1`, `dash`, `M4V `) | `.mp4` |
| `1A 45 DF A3`, DocType `webm` | `.webm` (otherwise `.mkv`) |
| `<` after optional whitespace or a BOM | `.html`: an error page, not media |
| `{` or `[` after optional whitespace or a BOM | `.json` |
| anything else | `None` |

"Sniffing" is the usual word for deciding a type from a file's first bytes (browsers' MIME sniffing). It returns an extension, ready to put on a file name. `.html` and `.json` are returned rather than `None` because "the server sent a web page" is exactly what a downloader needs to know.

## Finding ffmpeg

Private, done once on the first video or audio call:
1. `imageio_ffmpeg.get_ffmpeg_exe()` when imageio-ffmpeg is installed. It honors the **`IMAGEIO_FFMPEG_EXE`** environment variable, so a user can point `media` at any ffmpeg without a setting of ours.
2. Otherwise `shutil.which("ffmpeg")`.
3. Otherwise a video call raises `RuntimeError("shared.media needs ffmpeg for videos: pip install 'flntools[media]', or install ffmpeg on your PATH")`. Photos still work.

`ffmpeg -version` is read once. Older than **4.4** raises `RuntimeError`; an unparseable version (a git build) is accepted. Details and the reasons for the wheel are in [ffmpeg](ffmpeg.md).

**Pillow** is imported on the first photo call. A missing Pillow raises the usual missing-extra `ImportError("shared.media needs Pillow for photos: pip install 'flntools[media]'")`; videos still work. `pi_heif.register_heif_opener()` runs at the first photo call, not on import. `sniff` needs neither.

## Output

Every function that writes (`convert`, `compress`, `merge`):
- writes to a fresh `.<name>.<random>.part` **next to the final file**, and `os.replace`s it into place only when complete;
- refuses an existing `dest` with `FileExistsError` before reading anything. The one exception is `compress(src)` without `dest=`, whose whole job is to replace `src`;
- deletes the `.part` on any failure, timeout or Ctrl-C, and kills ffmpeg if it is running;
- returns the final path.

## Safety

Media comes from the network, so it is untrusted.

| Threat | Rule | Error |
| --- | --- | --- |
| **ffmpeg reading something other than the file**: a playlist saved as `.mp4` pulling in `/etc/passwd`, an `ffconcat` list, an image pattern, a network URL, a name read as an option (`-y.mp4`) or a protocol (`concat:a\|b`) | every input is `file:` + the absolute path, with `-protocol_whitelist file -format_whitelist mov,matroska`. Only MP4/MOV/M4A and Matroska/WebM are opened at all. `enable_drefs` stays off. Full rules in [ffmpeg › Running ffmpeg safely](ffmpeg.md#running-ffmpeg-safely) | `UnsafeError` |
| **a Pillow decoder nobody needs**: EPS starts Ghostscript | every open passes `formats=("JPEG", "PNG", "GIF", "WEBP", "HEIF", "AVIF")`; nothing else is tried | `UnsafeError` |
| **a decompression bomb**: a tiny file declaring 100,000 × 100,000 pixels | Pillow's own check, from the header, past `2 × MAX_IMAGE_PIXELS` (about 179 MP). Its 89 MP warning is silenced, so 108 MP and 200 MP phone photos open | `UnsafeError` |
| **truncated data** | never padded: `LOAD_TRUNCATED_IMAGES` stays `False`, and ffmpeg runs with `-xerror` where it reads packets | `BrokenError` |
| **a crash on hostile media** | in ffmpeg, it stays in ffmpeg's process and becomes `BrokenError` with its last line. In Pillow's C decoders (libjpeg, libwebp, libheif, libavif) it can't be contained; they are fuzzed through OSS-Fuzz and pip keeps them current. The docstring says so | `BrokenError` |
| **hangs** | 30 s for `probe`, 600 s for `merge` and `check`, a 60 s stall timer for video `compress` and `convert` re-encodes | `TimeoutError` |
| **stolen keystrokes, stray report files** | `-nostdin`; `FFREPORT` removed from ffmpeg's environment; `AV_LOG_FORCE_NOCOLOR=1` | — |

## Errors

| Error | Base | When |
| --- | --- | --- |
| `media.BrokenError` | `ValueError` | the file is truncated, corrupt, not media, or lacks a stream the call needs |
| `media.UnsafeError` | `ValueError` | the file is a format `media` refuses to open, or a decompression bomb |
| `ValueError` | — | an unknown `preset` or `codec`, a `dest` extension that isn't allowed, a photo-to-video conversion, an animated photo |
| `RuntimeError` | — | ffmpeg missing, too old, or without an encoder the call needs; codecs `merge`'s container can't hold |
| `ImportError` | — | Pillow missing, for a photo call |
| `FileExistsError`, `TimeoutError` | builtins | `dest` exists; a timeout ran out or an encode stalled |

`UnsafeError` covers both the "bomb" and the "format not allowed" cases, because a caller does the same with both: skip the file. That is one error fewer than the first draft's `BombError`.

## pyproject

```toml
[project.optional-dependencies]
media = ["pillow>=11.3", "pi-heif>=1.0", "imageio-ffmpeg>=0.6"]
```

- `pillow>=11.3`: the first with AVIF in its wheels. On Python 3.11 and later this resolves to Pillow 13.
- `pi-heif`: HEIC, **decode only**. It leaves out the x265 encoder, which is GPL, so photos carry no GPL code.
- `imageio-ffmpeg`: the ffmpeg binary, 20–68 MB wheels ([ffmpeg](ffmpeg.md)).

One extra, because `media` is one module. flnd lists all three as plain dependencies ([flnd › pyproject](flnd.md#pyproject)). `media` joins the lazily loaded modules in `shared/__init__.py`: `from shared import log` never imports Pillow or looks for ffmpeg.

## Rejected

| Idea | Why not |
| --- | --- |
| **separate `image` and `video` modules** | the first draft. Every caller would sniff and choose, and `Info`, the presets, the output rule and the errors would be written twice ([Why one module](#why-one-module)) |
| **a public `which_ffmpeg()` or `run(*args)`** | finding ffmpeg is `media`'s job. A `run` couldn't apply the input rules to arguments it doesn't understand, and would be a subprocess helper ([Out of scope](../README.md#out-of-scope)). A script that needs a rarer ffmpeg command runs `imageio_ffmpeg.get_ffmpeg_exe()` itself |
| **`ffprobe` JSON for `probe`** | the wheel ships no `ffprobe`. ffmpeg's own summary plus `ffmetadata` give the same fields in one run |
| **PyAV** | it has dropped Python 3.11 in its latest release, breaks its API every few months, and a decoder crash would take flnd down ([ffmpeg](ffmpeg.md#why-the-wheel-and-not-pyav)) |
| **ffmpeg for photos** | it ignores EXIF orientation for stills and drops EXIF. Pillow is in process and about 50 ms per photo |
| **writing HEIC or AVIF** | nothing asks for it, and HEIC writing needs x265 (GPL) |
| **AV1 in `compress`** | the smallest files, but very slow on Windows, whose wheel has only `libaom`; older phones and TVs can't decode it. A third `codec` when the wheels carry SVT-AV1 everywhere |
| **hardware encoders** (VideoToolbox, NVENC, QSV) | lower quality per byte, and different on every machine, so a preset would mean different things |
| **raw CRF and x264 `-preset` arguments; a target file size** | `quality`, `resolution`, `audio_bitrate` and `codec` cover what people choose; CRF and encoder speed are ffmpeg's vocabulary, and `quality` maps onto CRF. A target size means several encodes per file |
| **frame grabs, thumbnails, GIF previews, GIF-to-MP4** | no caller yet. GIF-to-MP4 would need `gif` added to ffmpeg's allowed formats, which is a one-word change when a caller appears |
| **writing or stripping metadata** (`media.set_tags`, `strip_location=`) | no caller yet; whether flnd drops GPS when it compresses is an [open question](flnd.md#open-questions). Through Pillow it is one line: `exif.pop(0x8825)` |
| **HLS / m3u8 downloads** | ffmpeg would fetch from the network itself, bypassing the engine's cookies, retries and progress. No source needs it |
| **near-duplicate detection** (v8's frame hashes) | it scanned every stored file on every save; exact duplicates are caught by the engine's history |
| **RAW camera formats, TIFF, BMP, PSD, SVG** | no source serves them, and each widens the attack surface |
| **async variants** | every `shared` API is synchronous; flnd calls `media` from its download pool's threads |

## Tests

`tests/test_media.py`. **Fixtures are generated** in `setUpClass`, into a temp folder:
- photos by Pillow: JPEG (with EXIF orientation 6, `DateTimeOriginal`, `OffsetTimeOriginal`, `Model`, a GPS block, and an ICC profile), PNG, RGBA PNG, WebP, animated WebP, GIF, AVIF, and a 4000×3000 quality-95 noise JPEG that really shrinks;
- HEIC is the one exception, because pi-heif can't write it: `tests/data/tiny.heic`, a committed 16×16 file of about 1 KB made with macOS `sips -s format heic`, its SHA-256 listed in the test;
- videos by calling ffmpeg directly (not through `media`, whose whitelist rightly refuses `lavfi`), with only built-in encoders so any build can make them: `v.mp4` (video only), `a.m4a` (audio only), `av.mkv` (both), `meta.mp4` with `creation_time`, an ISO 6709 `location`, and a rotation of 90°, and `big.mp4`, 3 s of portrait 360×640 at `mpeg4 -q:v 2` plus 192 kb/s AAC, about 1.5 MB.

Video tests are skipped without ffmpeg; tests needing `libx264`, `libx265` or `libmp3lame` are skipped when the ffmpeg lacks it.

- **`probe`:**
  - each photo format gives the right `kind`, `format`, `frames` and size; the orientation-6 JPEG reports its width and height swapped;
  - the EXIF JPEG gives an aware `created`, its `location` to 4 decimals, and `tags["Model"]`; `MakerNote` isn't in `tags`;
  - a PNG renamed `.jpg` reports `"png"`;
  - `v.mp4` gives `duration` ≈ 1.0, 64×48, `video="mpeg4"`, `audio=None`, an `fps` of 10; `a.m4a` gives `kind="audio"`; `av.mkv` gives `"mkv"` and both codecs;
  - `meta.mp4` gives `created`, `location`, and its rotated size;
  - a `0000:00:00 00:00:00` date and a `0, 0` GPS give `None`.
- **The ffmpeg output parser**, without ffmpeg, on text captured from ffmpeg 4.4, 6.1 and 9.0: `Duration: N/A`; `(default)` and side data; a display matrix; a title with a newline and a fake `Stream #0:5: Video: h264, 9999x9999` line, which isn't counted in either output; `ffmetadata` escapes.
- **`convert`:**
  - HEIC, WebP, AVIF and PNG to `.jpg`; WebP to `.png`; orientation applied and reset; EXIF and ICC kept; RGBA flattened onto white; `quality` changes the size;
  - `av.mkv` (mpeg4 + AAC) to `.mp4` is a **copy**: the output's codecs equal the input's, and it is fast; a VP9 + Vorbis WebM to `.mp4` is re-encoded to H.264 + AAC;
  - `av.mkv` to `.m4a` and `.mp3`; `v.mp4` to `.m4a` raises `ValueError`;
  - `.bmp`, photo-to-`.mp4`, video-to-`.jpg`, an animated WebP, and an H.264 file to `.webm` raise `ValueError`;
  - an existing `dest` raises `FileExistsError`; a failure leaves no `.part`.
- **`compress`:**
  - each preset, on the big JPEG and `big.mp4`: the result is **smaller**, passes `check`, and `"small"` < `"balanced"` < `"high"` in size. The video probes as `h264` + `aac`, duration ≈ 3.0;
  - **in place:** `compress(path)` leaves one file; `a.heic` → `a.jpg` and `clip.webm` → `clip.mp4` with the old file gone; an existing `a.jpg` next to `a.heic` raises `FileExistsError` and nothing is encoded;
  - **with `dest`:** `src` is unchanged;
  - `resolution` on a portrait 360×640 video at 240 gives 240×426, a landscape 640×360 gives 426×240, and 2000 doesn't upscale. `"small"` takes a 4000×3000 photo to 2048×1536;
  - the photo stays upright and keeps `DateTimeOriginal` and GPS;
  - `codec="h265"` gives `hevc` with the `hvc1` tag;
  - **not smaller:** compressing a `"small"` output again with `"high"` returns `None`, leaves the file as it was, and logs the info line;
  - **untouched:** PNG, GIF, animated WebP and `a.m4a` return `None` and are unchanged;
  - **progress:** `done` only grows, ending ≈ 3.0, `total` ≈ 3.0; with `progress=None` a `log.progress` bar is shown (a fake `theme.screen`);
  - **stall:** a fake `ffmpeg` that prints one progress line and sleeps is killed by a shortened stall timer; `TimeoutError`, and no `.part`;
  - a fake `ffmpeg -encoders` without `libx264` gives the `RuntimeError`;
  - **settings win over the preset:** `preset="small", quality=90` is larger than plain `"small"`; `quality=80` on a video runs CRF 23 (H.264) and 27 (H.265), checked from the command line built; `audio_bitrate=64_000` gives about 64 kb/s;
  - `preset="tiny"`, `codec="av1"`, `quality=0`, `quality=101` and `audio_bitrate=8_000` raise `ValueError`.
- **`merge`:** `v.mp4` + `a.m4a` into `.mp4` and `.mkv` has both streams and duration ≈ 1.0; the inputs are unchanged; an audio input without audio raises `BrokenError`; `.avi` raises `ValueError`; a codec the container can't hold raises `RuntimeError` and leaves no file.
- **`check`:** every fixture passes; a JPEG cut in half and `v.mp4` cut to 70% raise `BrokenError` naming the file; random bytes and an HTML page named `.mp4` or `.jpg` raise `BrokenError`.
- **`sniff`:** every row of the table from byte prefixes, `mif1` + `avif`, an HTML body with a BOM, and an empty file (`None`).
- **Safety:**
  - an HLS playlist pointing at a local secret file, named `list.m3u8` and `clip.mp4`, and an `ffconcat` file: refused, `UnsafeError` for the `.m3u8`, and no output;
  - inputs named `-y.mp4`, `http:x.mp4` and `concat:a.mp4|b.mp4` are read as plain local files;
  - with `FFREPORT=file=report.log` set, no report file appears;
  - an EPS and a TIFF raise `UnsafeError`, and a fake `gs` on `PATH` that fails the test never runs;
  - a PNG header declaring 20,000 × 20,000 pixels raises `UnsafeError` without allocating them;
  - `LOAD_TRUNCATED_IMAGES` is still `False` after every call;
  - a `KeyboardInterrupt` during an ffmpeg run leaves no `.part` and no child process.
- **Finding ffmpeg:** imageio-ffmpeg's binary; `IMAGEIO_FFMPEG_EXE` pointing at a fake; imageio-ffmpeg blocked and a fake on a temp `PATH`; neither gives the install message on a video call while photos still work; version `4.2` refused, `4.4.2-0ubuntu0.22.04.1`, `n7.1` and `N-12345-gabc` accepted.
- **Lazy import:** `from shared import log` imports neither PIL nor `shared._media`; `sniff` works with PIL and imageio-ffmpeg blocked; with PIL blocked, a photo call raises the missing-extra `ImportError`.
