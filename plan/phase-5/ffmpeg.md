# ffmpeg

Phase 5, cross-cutting. This file decides how the ffmpeg program gets onto the user's machine, and how [media](media.md) runs it safely. It adds no module of its own, and no public function: finding ffmpeg is private to `media`.

**The decision:**
- **ffmpeg comes from a pip wheel.** `imageio-ffmpeg` ships a static ffmpeg inside its platform wheels, so `pip install` / `uv tool install` puts it on the machine. pip and uv already download reliably: they pin it, check its hash, retry, cache and show progress.
- **It is a required dependency of flnd**, so every flnd install has it. `shared` lists it in the `media` extra.
- **The fallback is the system `ffmpeg` on `PATH`**, for platforms that have no wheel (musl, 32-bit ARM) and for scripts that use `shared` without the `media` extra.
- **No downloader of our own, no `sh` module, no `flnd install-ffmpeg`, and no public `which_ffmpeg()`.** A caller asks `media` for a result, never for ffmpeg.

## What flnkit did

flnkit had **no image or video module**. Its proposal listed "media, image, video, audio" as out of scope, with the advice "use a CLI: `sh.tool("ffmpeg", …)`". `sh` was 2,103 lines in 14 files: `run`, `stream`, `start`, `supervise`, `ssh`, `ps`, `kill`, `open`, and `tool`.

`flnkit/sh/_tool.py` (163 lines) was the "tool API" the user remembers:
- a `Tool` had a pinned `version`;
- a `builds` table mapped each platform to `(url, sha256)`;
- files lived in `~/.cache/flnkit/tools/<name>/<version>/`;
- `system=True` preferred a `PATH` copy, and `ensure()` downloaded and checked the hash.

**Kept from it:**
- the idea of a pinned, hash-checked ffmpeg, which we get from pip and `uv.lock`;
- the `system=` fallback, which becomes the `PATH` fallback.

**Dropped:**
- **`sh` itself.** flnd runs exactly one outside program for media, and `archive` already runs `unrar` privately the same way.
- **The per-platform URL table.** Someone has to keep it up to date by hand.
- **The downloader.** It had bugs nobody noticed, because nothing called it correctly:
  - `ensure()` wrote the downloaded *archive's* bytes out as the binary, never extracting it, although `Build["bin"]` existed;
  - it read the whole file into memory and wrote it non-atomically;
  - flnkit's flnd called `sh.tool("ffmpeg", "-y", "-i", path, "-c", "copy", out)`, which only builds a `Tool` and never runs ffmpeg. So its `shrink` feature never ran. It was also a stream copy, which can't shrink anything.

The flnd before flnkit (v8) re-encoded videos to save disk (`shrink`), and found near-duplicates with ffmpeg frame hashes.
- **Re-encoding comes back** as [`media.compress`](media.md#compress), with named presets, and with the "keep the original if it didn't shrink" rule v8 had.
- **Near-duplicates stay out:** see [media › Rejected](media.md#rejected).

## The options compared

Facts from PyPI on 2026-11-11.

| Path | Size | Python 3.10–3.14 | Platforms | License of what ships | PyInstaller | Maintenance | Verdict |
| --- | --- | --- | --- | --- | --- | --- | --- |
| **`imageio-ffmpeg`** (wheel with an ffmpeg 9.0 binary) | wheel 20–68 MB. The binary unpacked is 52 MB on macOS arm64, 101 MB on Windows x64, and 165 MB on Linux x86_64 | yes: `py3-none` wheels, `>=3.10` | macOS x86_64 (10.13+) and arm64 (12+), manylinux_2_28 x86_64 and aarch64, Windows x64, arm64 and win32. No musl, no armv7 | wrapper BSD-2. The binary is a **GPL** build (`--enable-gpl`, x264, x265) | `pyinstaller-hooks-contrib` has `hook-imageio_ffmpeg.py`, which collects the binary | 0.8.1 on 2026-11-03; 0.7.0 and 0.8.0 earlier this month | **primary** |
| system `ffmpeg` on `PATH` | 0 | any | wherever the user installs it (brew, apt, winget) | whatever they installed; we ship nothing | nothing to bundle | the distro's. Ubuntu 22.04 has 4.4, 24.04 has 6.1 | **fallback** |
| PyAV (`av`, libav bundled in the wheel) | 18–46 MB | **no**: 19.x needs `>=3.12`, 18.x `>=3.11`. The last release for 3.10 is 17.1 (2026-06) | wide, including musl and armv7 | libav LGPL-3, but the wheels also carry x264 and x265 (GPL) | `hook-av.py` exists | a breaking major release every few months (17→19 in 2026) | rejected |
| `static-ffmpeg` (downloads binaries on first use) | tiny wheel, then about 80 MB fetched from one GitHub repo | yes | the five usual platforms | GPL binaries | poor: it downloads into `site-packages`, which a bundle can't write to | one maintainer; 3.0 on 2026-01-16 | rejected |
| our own downloader ("tool API") | about 80 MB fetched per platform | yes | as many as we keep URLs for | GPL or LGPL, our choice of build | the binary lives in the user cache, outside the bundle | **ours, forever** | rejected |

### Why the wheel and not PyAV

- **Python versions.** PyAV 19 has already dropped 3.11, flnd's floor. flnd would be stuck on `av<19`, which gets no fixes.
- **Out of process is safer.** ffmpeg runs as a separate program, so a crash on hostile media kills ffmpeg, not flnd. A hang can be timed out and killed. Inside PyAV, a segfault in a decoder takes the whole downloader down, and a stuck `demux()` can't be interrupted.
- **Less code.** Everything `media` does with video and audio (copies, merges, trims, frames, encodes, probing) is one ffmpeg command line each. With PyAV, a merge is a packet loop with timestamp rescaling, and a re-encode is a decode, scale and encode loop with its own audio resampling. We would write and test both ourselves.
- **API churn.** PyAV broke its API three times in 2026. ffmpeg's command line has been stable for a decade.

### Why the wheel and not our own downloader

What `flnd install-browser` shows: Chromium isn't on PyPI, so Playwright ships its own installer, and flnd only calls it. ffmpeg *is* on PyPI, inside imageio-ffmpeg's wheels.

A downloader of our own would need all of this, and keep it working:
- a table of URLs and SHA-256s for five platforms;
- three different hosts, because no single host builds every platform:
  - BtbN for Linux and Windows, which prunes old builds;
  - Martin Riedl or osxexperts for macOS;
  - gyan.dev, also for Windows;
- three archive layouts;
- extraction (through `archive`);
- an install folder in the user cache, plus a version file;
- progress, retries, and an upgrade path.

That is about 150 lines in flnd, plus a table someone edits every release. pip already does all of it, with hashes recorded in `uv.lock`.

### What ffmpeg's wheel lacks, and how we cope

- **No `ffprobe`.** imageio-ffmpeg ships only `ffmpeg`. So `media.probe` reads the stream summary that ffmpeg prints when it opens a file, plus its `ffmetadata` output for the tags, in one run ([media › How a video is probed](media.md#how-a-video-is-probed)). The summary is the same text imageio-ffmpeg itself parses, and its layout has been stable since FFmpeg 2.x. One way only: we never call `ffprobe`, even when it is installed.
- **No musl or armv7 wheel.** Alpine and Raspberry Pi 32-bit use the `PATH` fallback.

## Finding ffmpeg

Private to `media` (`shared/_ffmpeg.py`), done once on the first video or audio call:

1. **imageio-ffmpeg, when it is installed:** `imageio_ffmpeg.get_ffmpeg_exe()`. It already honors the `IMAGEIO_FFMPEG_EXE` environment variable, so a user can point flnd at any ffmpeg without a new setting of ours.
2. **Otherwise, `shutil.which("ffmpeg")`.**
3. **Otherwise, the call raises** the missing-ffmpeg `RuntimeError` below. Photo calls never look for ffmpeg.

The wheel's binary comes first because its version is pinned and known. A `PATH` ffmpeg can be anything from 4.x to a nightly build.

- **Version check.** On first use, `ffmpeg -version` is read once.
  - Older than **4.4** raises `RuntimeError("ffmpeg 4.2 is too old; shared.media needs 4.4 or later")`. 4.4 is the oldest version a current LTS distro ships, and Opus in MP4 stopped being experimental in 4.3.
  - A version string that doesn't parse, such as a git build `N-12345-g…`, is accepted.
- **Missing ffmpeg.** A video or audio call raises `RuntimeError("shared.media needs ffmpeg for videos: pip install 'flntools[media]', or install ffmpeg on your PATH")`. This replaces the usual missing-extra `ImportError`, because a `PATH` ffmpeg is enough.

## Running ffmpeg safely

`media` builds every command line itself, from fixed options and the caller's paths. **There is no public "run ffmpeg with these arguments" function.** A script that needs something rarer runs `subprocess.run([imageio_ffmpeg.get_ffmpeg_exe(), …])` itself, and owns the safety of what it passes.

Every run gets:

| Threat | Rule |
| --- | --- |
| a **file name read as an option** (`-y.mp4`) or **as a protocol** (`http:x.mp4`, `concat:a\|b`, `subfile:…`) | every input and output is passed as `file:` + the **absolute** path. It can't start with `-`, and ffmpeg can't read it as another protocol |
| **network access**, or an input that pulls in other files: an HLS playlist saved as `.mp4`, an `ffconcat` list, or an `image2` pattern. This is the known ffmpeg hole where a "video" playlist reads `/etc/passwd` into the output | each input gets `-protocol_whitelist file` and a `-format_whitelist` naming only readers that never open another file or URL: `mov,matroska,avi,mpegts,mpeg,flv,asf,ogg,mp3,aac,flac,wav,aiff,caf,amr,gif` ([media › Read](media.md#read)). `hls`, `dash`, `concat`, `image2` and every `*_pipe` reader are left out. ffmpeg refuses anything else before it reads past the header, with `Format not on whitelist`. That refusal raises `media.UnsafeError`. Recent ffmpeg also limits which HLS segment extensions it will open, but the 4.4 that Ubuntu 22.04 ships does not, so the whitelist is what protects a `PATH` ffmpeg. External references in MOV (`enable_drefs`) are off by default in ffmpeg, and we never turn them on |
| ffmpeg **reading the terminal**: it waits on `q` and steals keystrokes from `log`'s live area | `-nostdin` |
| **overwriting** something | output goes to a fresh temp name `.<name>.<random>.part` next to `dest`, with `-n` (never overwrite). It is renamed onto `dest` with `os.replace` only after ffmpeg exits 0. Any failure, timeout or Ctrl-C deletes the temp file. This matters: when muxing fails, ffmpeg has already created a partial output file. A `dest` that already exists raises `FileExistsError` before ffmpeg starts |
| **hangs** | every call has a `timeout` in seconds: `probe` 30, stream copies and `check` 600. `subprocess.run` kills ffmpeg when the time runs out, and on any exception, Ctrl-C included. That raises the builtin `TimeoutError`, naming the file. Every encode has a **stall timeout** instead: 60 s without a progress line, because a long transcode is legitimately slow |
| **the environment** | `FFREPORT` is removed, because it makes ffmpeg write a log file into the working directory. `AV_LOG_FORCE_NOCOLOR=1` is set, so the output we parse has no color codes |
| **stray console windows** | none: flnd is a console program, and ffmpeg inherits its console |
| **a crash on hostile media** | it stays inside ffmpeg's process and becomes `media.BrokenError` with ffmpeg's last error line |

The command is always a list, never a shell string.

## Licensing

### Encoders

Checked by reading the `configure` line built into each imageio-ffmpeg 0.8.1 binary (the binaries were not run):

| Build | GPL | H.264 `libx264` | H.265 `libx265` | AV1 `libsvtav1` | AV1 `libaom` | VP9 `libvpx` | Opus |
| --- | --- | --- | --- | --- | --- | --- | --- |
| macOS arm64 | yes | yes | yes | yes | yes | yes | yes |
| Linux x86_64 | yes | yes | yes | yes | yes | yes | yes |
| Windows x64 | yes | yes | yes | **no** | yes | yes | yes |

AAC is ffmpeg's own encoder, so it is in every build.

**Which codec to default to.**
- **H.264 through `libx264` plays everywhere, so it is the default.**
- **Choosing H.265 or AV1 avoids no GPL obligation.** x264 and x265 are both GPL. The binary is already one GPL program, whichever encoder we invoke. And running a GPL program as a separate process puts no license on flnd itself.
- **The only LGPL-friendly encoders are AV1** (SVT-AV1 is BSD, libaom is BSD) **and VP9**. They would matter only for a bundle built around an LGPL ffmpeg, which no wheel offers.

So licensing doesn't decide the codec. Playback and encode speed do:
- **H.264** is the default;
- **H.265** is offered for smaller files;
- **AV1** waits until every wheel has SVT-AV1 ([media › Rejected](media.md#rejected)).

A `PATH` ffmpeg may be an LGPL build without x264 (Fedora's `ffmpeg-free`). `compress` and `convert` check `-encoders` and say so ([media › How a video is encoded](media.md#how-a-video-is-encoded)).

### Distributing

Every bundled path ships GPL code. imageio-ffmpeg's binary is a GPL build, and PyAV's wheels carry x264 and x265. Installing from PyPI on your own machine raises nothing. **Handing a `make dist` folder to someone else** does: it then distributes a GPL ffmpeg binary next to flnd, as a separate program, which is aggregation, not linking. The GPL then asks that ffmpeg's license and an offer of its source travel with it. **Decided:** every `make dist` bundle ships a `licenses/` folder with ffmpeg's GPL text and a source offer ([flnd › Packaging](flnd.md#packaging)).

## Tests

In `tests/test_media.py`: finding ffmpeg, the version and encoder checks, the safety rules, timeouts and cleanup ([media › Tests](media.md#tests)).
