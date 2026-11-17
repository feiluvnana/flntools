# Compression and extraction

What happens to a file after flnd saves it: archives are extracted beside themselves, and with
`--compress`, photos and videos are replaced by smaller copies that look the same. The code is
`flnd/_steps.py` (the chain), `flnd/_unpack.py`, `flnd/_shrink.py` and `flnd/_media.py`.

## The steps after a file lands

Every saved file goes through one fixed chain of steps. Each is on or off by its flag, and the
order never changes, so no combination can compress a file before dedupe has linked it:

```
download (hashing) → dedupe → unpack → compress → date → place → record → sidecar → Saved → --exec
```

| step | when | what it does |
| --- | --- | --- |
| download | always | the file streams into `<name>.part` beside its final name, and its SHA-256 is computed from the same bytes |
| dedupe | on unless `[defaults] dedupe = "off"` | a file whose bytes are already saved becomes a hard link to them ([storage.md](storage.md#dedupe)) |
| unpack | an archive | extracts it into a folder of its name (below) |
| compress | `--compress` | a smaller file that still looks the same replaces it (below) |
| date | always | the post's day becomes the file's modified time |
| place | always | the part is renamed onto the final name |
| record | always | the file's history row is written |
| sidecar | `[defaults] sidecars = true` | the post's metadata as `<file>.json` beside it ([storage.md](storage.md#sidecars)) |
| Saved | always | the run's `flnd.Saved` event, with the file's final path and size |
| exec | `--exec CMD` (the CLI) | runs CMD for the `Saved` file, never through a shell ([cli.md](cli.md#running-a-command-per-file)) |

A file and the hard links dedupe made of it are compressed once: the result is linked at every
one of their paths, and their rows move together in one transaction.

Unpack and compress are slow (seconds for a video, minutes for a large archive), so they don't
run on the download's thread. Once a file's history row is written, it is handed to the run's
**chain**, which runs those steps on threads of its own and then places and records the file
again when it changed. A slow encode never holds up a download. Three lanes run side by side:

| lane | threads | why |
| --- | ---: | --- |
| archive | 1 | extraction is disk-bound |
| photo | 2 | libavif already spreads one encode over every core |
| video | 1 | ffmpeg uses every core for one encode |

At the end of the run, flnd waits for the lanes and prints one line, such as
`Media: 120 files compressed, 310.4 MB saved; 2 archives extracted`. After Ctrl-C, files not
yet started are left as they are (a later `flnd sync --compress` takes them); a second Ctrl-C
also stops a running ffmpeg.

## Archives

A saved `.zip`, `.7z`, `.rar` or `.tar` (plain, `.gz`, `.bz2`, `.xz`), or any file its source
gave a password, is extracted beside itself into a folder of its name: `loi-5930.7z` fills
`loi-5930/`. The format is read from the file's first bytes, since a name can lie.

Before anything is written, every entry is checked. The archive is refused when an entry:

- would land outside the folder (`../x`, `/x`, `C:x`);
- is a link of any kind (a symbolic link, a hard link, a Windows junction);
- passes the limits: 20 GB in all, 100,000 entries, or 200 bytes unpacked per archive byte
  (a zip bomb).

The archive is extracted into a hidden folder beside it, `.loi-5930.unpack`, and every file is
checked against the size the listing gave. Only then is it moved into place. A failure removes
the hidden folder, so there is never half a folder.

- **Tools.** zip and tar are read by Python. 7z, RAR and encrypted zips need 7-Zip's `7zz` (or
  the older `7z`) on your PATH; without it, those archives are left as they are and recorded as
  `failed`.
- **Passwords.** A source may give one (`File(url, password=…)`); loi does. A wrong or missing
  password keeps the archive and records `failed`.
- **The archive itself** is kept, unless `--compress` is on: then it is removed after a verified
  extract, and the extracted folder counts as the saved file, so a later run doesn't download
  it again.

## `--compress`

`flnd download … --compress` and `flnd sync --compress` compress what they save. `sync
--compress` then also compresses files these targets saved earlier that aren't done yet.

The originals these sites serve are already compressed, so re-encoding costs a generation of
quality and replaces the original. Every result is checked before it does.

### What is tried

| original | candidates, in order |
| --- | --- |
| JPEG | AVIF (quality 60); an optimized JPEG with its own quantization tables; WebP (quality 80) |
| PNG | oxipng (lossless); then lossless WebP for flat art (256 colours or fewer), or AVIF for a photo |
| GIF | lossless WebP, animated when the GIF is |
| video (`.mp4`, `.mov`, `.webm`, `.mkv`, `.m4v`) | SVT-AV1 10-bit, CRF 35, preset 8, with Opus at 96 kbit/s, `+faststart`, metadata copied |
| sound (`.m4a`, `.mp3`, `.aac`, `.wav`, `.flac`) | Opus at 96 kbit/s |

Photos are encoded in-process by Pillow and pyoxipng, which flnd installs: no tool is needed.
Videos and sound need ffmpeg on your PATH; without it they are recorded `failed`, and retried
once ffmpeg is installed.

**AV1 doesn't play everywhere.** Apple devices decode it in hardware only from 2023 (A17 Pro,
M3), and older iPhones and Macs may not play it in Photos or QuickTime. For those, videos can go
to HEVC (x265, CRF 28, with AAC sound and the `hvc1` tag QuickTime needs) instead: set it in the
home's `config.toml` ([storage.md](storage.md#settings-configtoml)):

```toml
[compress]
video = "hevc"
```

A file compressed or kept records the tools it was done with, so changing the codec doesn't redo
what is done; a file `kept` or `failed` is tried again with the new one. An ffmpeg without the
codec asked for uses the other (SVT-AV1 or x265), and one with neither records `failed`.

### The checks

A candidate replaces the original only when:

1. it decodes;
2. its dimensions and frame count match (a video's duration within 0.1 s, and every frame of
   it decodes);
3. a photo keeps an SSIM of 0.98 or more against the original. SSIM is a measure of how alike
   two pictures look, 1.0 for the same picture. flnd computes it in-process on the brightness
   plane over 8×8 blocks; it agrees with ffmpeg's `ssim` filter to about 0.001;
4. it saves at least 5%.

The first candidate that saves 40% or more wins at once; otherwise the smallest that passed.
When none passes, the original stays, recorded as `kept`.

### Placing the result

The result keeps the original's modified time (the post's day).

Each candidate is encoded into a part of the name it would land at (`a.avif.part`), like a
download, so a crash leaves only a part the next run's sweep removes.

- **Same suffix** (an optimized JPEG, an oxipng PNG, an AV1 `.mp4`): it replaces the original in
  one rename.
- **A new suffix** (`a.jpg` becomes `a.avif`): the new file is placed beside the original, the
  history row is moved to it, and only then is the original removed. A crash in between leaves
  both files and a row naming the new one. If a file already has the new name, the original is
  kept.

### Recorded in history

Each file's row records what the step did (see [storage.md](storage.md)): `shrink` is
`compressed`, `kept` or `failed`, and `tools` names the encoders and their versions. A
compressed file is never compressed again. A `kept` or `failed` file is tried again only when
the tools change (an upgrade of Pillow, libavif or ffmpeg), so a `sync --compress` over a large
home skips every done file without opening it.

### Kinds that don't shrink are left alone

A kind of file (JPEG, PNG, GIF, video, sound) whose median saving, over at least 20 files, is
under 15% isn't worth the encode time or the lost generation, so flnd stops compressing that
kind. The count starts from the results already in history.

The defaults above were chosen on synthetic media. Measuring them on real downloads is a hand
check, `make media-real` ([performance.md](performance.md#compression)), and its results are
recorded there.
