# flntools

Dead simple media downloader for **TikTok**, **Facebook**, **Instagram**, **Twitter/X**, **LOIBUS**, and **Telegram**. The command is `flnd`.

The plan for the shared helpers flnd is built on is in [plan/README.md](plan/README.md).

---

## Installation

Requires Python 3.11 or newer, and [uv](https://docs.astral.sh/uv/). From this folder:

```bash
uv sync                          # creates .venv from uv.lock; run it again after a pull
uv run flnd download ins:example_user
```

Or `make setup`, then `make flnd ARGS="download ins:example_user"`. Every task (`setup`, `test`,
`lint`, `type`, `check`, `flnd`, `dist`) is in `scripts/dev.py`, so on Windows, which has no make,
run `uv run --no-project scripts/dev.py <task>`, or install make with `winget install ezwinports.make`.
`uv tool install --editable .` puts `flnd` on your PATH instead.

ffmpeg comes with `uv sync`, as the `imageio-ffmpeg` dependency; flnd uses it to join Facebook and
Instagram videos with their sound, and to compress videos and audio. Where that package has no wheel (musl Linux,
32-bit ARM), install ffmpeg on your PATH instead.

flnd needs [Google Chrome](https://www.google.com/chrome/), installed the usual way, for the sources
that open a browser. When Chrome is missing, flnd asks once, in a terminal, whether to download Chromium
(about 200 MB) instead, into Playwright's folder in your user cache; later runs use it without asking.

### A folder that runs without Python

```bash
make setup
make dist            # dist/flnd/flnd (flnd.exe on Windows), for the OS you build on
```

Copy the whole `dist/flnd/` folder; it holds `flnd`, `_internal/` and `licenses/`, the third-party licences.
The bundle includes ffmpeg, a GPL program: `licenses/ffmpeg.txt` has its licence and the offer of its
source, and `licenses/ffmpeg-source.txt` the exact build.
It uses Google Chrome too, or offers the same Chromium download. The bundled `flnd` still writes `.flnd/`
in the folder you run it from.

---

## Usage

```bash
flnd download <targets...> [--from CATEGORIES] [--time SINCE,UNTIL]
              [--image FORMAT,LEVEL] [--video FORMAT,LEVEL] [--audio FORMAT,LEVEL]
flnd sync [targets...]
flnd login
flnd logout <sources...> | all
```

Or via python module:

```bash
python -m flnd download <targets...>
```

Files go to `.flnd/downloads/`, and the browser window is visible while a source runs.

### Target Format

`<source>:<name, id or URL>`, or just the URL: `ins:example_user`, `ins:@example_user` and
`https://www.instagram.com/example_user/` are the same target. `flnd download --help` lists every
accepted form.

Sources:

- `tik`: TikTok
- `fbk`: Facebook
- `ins`: Instagram
- `twt`: Twitter/X
- `loi`: LOIBUS (loix.cc), one post by its id or URL
- `tlg`: Telegram, a chat, group or channel the account can see

Facebook and Instagram serve their best videos as separate video and sound streams (DASH);
flnd downloads both and joins them, so you get the highest quality the site has, up to 1080p
or more where a progressive file stops at 720p. Twitter/X and TikTok videos already come whole.

### Examples

```bash
# Single target
flnd download ins:example_user
flnd download tik:example_creator
flnd download twt:example_handle
flnd download fbk:example.page
flnd download loi:5930

# Several targets
flnd download ins:example_user twt:example_handle

# One target per line from a file (blank lines and # comments are skipped)
cat list.txt | flnd download

# Reels and tagged posts from this year
flnd download ins:example_user --from reels,tagged --time 2026-01-01,

# The first half of 2026, and everything up to the end of June
flnd download twt:example_handle --time 2026-01-01,2026-06-30
flnd download twt:example_handle --time ,2026-06-30

# Without --from, everything: other people's content too (tagged, reposts, replies, liked)
flnd download twt:example_handle tik:example_creator

# Only the profile pictures
flnd download fbk:example.page ins:example_user --from profile

# One TikTok video, one Instagram post or reel, one tweet; a post's link is downloaded whole, and
# --from with one is an error, since a post has no categories
flnd download https://www.tiktok.com/@example_creator/video/7301234567890123456
flnd download https://www.instagram.com/p/C1abcDEFghi/
flnd download https://x.com/example_handle/status/1790000000000000000

# A LOIBUS post with its VIP archives; the unzip password is in the post's info.md
flnd download https://loix.cc/lois/5930

# Only its info and preview images, without using the daily VIP quota
flnd download https://loix.cc/lois/5930 --from photo

# Smaller files: photos as WebP, videos at most 720p; the originals are replaced, and this is
# saved for the target, so later downloads and syncs do the same
flnd download ins:example_user --image webp,mid --video low

# Videos as H.265, 30-50% smaller again, but many browsers can't play it
flnd download tik:example_creator --video h265,mid

# Stop compressing photos for this target from now on
flnd download ins:example_user --image off
```

### Options

Every source takes the same options:

- `--from`: which categories to take, such as `posts,reels` (`flnd --help` lists them). Without it,
  every category, other people's content (tagged, reposts) included. It applies to profiles and
  chats only: with a single post's link among the targets, it is an error;
- `--time <since>,<until>`: only items dated in that range, both days included (YYYY-MM-DD). Leave a
  side empty for no limit: `--time 2026-01-01,` or `--time ,2026-06-30`. `*` works too, quoted
  (`--time 2026-01-01,'*'`), since zsh stops at a bare `*` with "no matches found";
- `--image`, `--video`, `--audio`: compress each new photo, video or audio file after it is saved.
  Each takes a format, a level, or both, in either order: `--image webp,mid`, `--image mid` (each
  file's own format), `--image avif` (level `high`). It **replaces the original** when the result is
  smaller, and strips its metadata (date taken, camera, GPS); the post's date stays in the file name;

  | Level | Means |
  | --- | --- |
  | `best` | no visible loss: quality 95, full resolution |
  | `high` | quality 90, full resolution |
  | `mid` | quality 80, photos up to 2,304 px, videos up to 1080p |
  | `low` | quality 70, photos up to 1,536 px, videos up to 720p and 30 fps |

  The formats are, for `--image`: `jpg`, `webp`, `avif`, `png`, `gif`, `tif`, `bmp`; for `--video`: a
  container (`mp4`, `mov`, `mkv`, `webm`, `avi`, `ts`, `3gp`, `gif`), a codec (`h264`, `h265`, `vp9`),
  or both as `mkv:h265`; for `--audio`: `mp3`, `m4a`, `aac`, `opus`, `ogg`, `flac`.

`--image`, `--video` and `--audio` are **saved for each target** in `.flnd/targets.db`, each on its
own, and used by every later `download` and `sync` of it; a run that uses saved ones says so, as
`ins:example_user: --image webp,mid (saved)`. Giving one again changes it, and that `download`
compresses the target's files already saved with the new setting first, then the new ones. `off`
turns one off (`--image off`). `--from` and `--time` are never saved.

A saved photo, video or audio file gets the extension its content calls for (a WebP is saved as
`.webp`, not `.jpg`), a web page served instead of a photo is retried rather than saved, and a
download whose size the server didn't give is checked to be complete. Archives are saved as the site
sends them; unpack them with any archive tool. There is no download limit: a run ends when the
sources have nothing more, or on Ctrl-C.

`flnd download` asks no questions, except the one above, whether to download Chromium when Google
Chrome is missing. `flnd` alone prints the help.

---

## Keeping downloads up to date

```bash
flnd sync                                             # every target downloaded so far
flnd sync ins:example_user tik:example_creator        # only these
```

`flnd sync` takes no options and changes nothing about how a target is downloaded: each target runs
with its saved `--image`, `--video` and `--audio`, and each source's default categories. To change a
setting, run `flnd download` with it. Folders downloaded before flnd saved settings are synced
without any. For each target:

1. **repair**: a missing, empty or broken file (cut off, or a web page) is downloaded again, and
   leftover `.part` files are removed;
2. **fetch**: new posts are downloaded, as `flnd download` would;
3. **summary**: one line per target, `ins:example_user: 120 files, 85.2 MB; 2 repaired, 1 missing`
   (missing: files the site no longer lists), then the totals.

`sync` never deletes a file whose post was deleted, asks no questions but that same Chromium one
when Google Chrome is missing, and exits 1 when a target failed.

---

## Logging in

Most sources work better logged in. `flnd login` reads your login cookies from your browser, or from a cookie file you exported.
It takes no arguments and asks everything, in a terminal: the source (those already logged in say so),
then from your browser or a cookie file, then which browser (Chrome, Firefox, Edge, Brave or Chromium)
and profile, the default one preselected, or the file's path (Netscape cookies.txt or JSON).

```bash
flnd login                  # asks the source, the method, then the browser or the file
flnd logout ins tik         # removes those logins
flnd logout all             # removes every login
```

A full browser export works: only the source's own cookies are kept, in `.flnd/cookies/<source>.txt`.
A source that isn't logged in still downloads with public access, and says so. LOIBUS without a
login gives a post's info and preview images only; its archives need a VIP login,
and count against the account's daily quota (`--from photo` leaves them out).

---

## Download Layout (`.flnd/downloads/`)

Files are saved with platform-optimized subfolder layouts and automatic deduplication:

```
.flnd/downloads/
├── ins/<username>/
│   ├── posts/                  # Feed photos and every carousel item
│   ├── reels/                  # Reels not already saved from the grid
│   ├── tagged/                 # Tagged posts
│   ├── reposts/                # Reposts
│   ├── stories/                # Active 24h stories
│   ├── highlights/<title>/     # Story highlights
│   └── profile/                # HD profile picture
│
├── fbk/<username_or_id>/
│   ├── posts/                  # Timeline posts
│   ├── photos/                 # Uploaded photos
│   ├── tagged/                 # Tagged photos
│   ├── albums/<album_title>/   # Photo albums
│   ├── reels/                  # Facebook reels
│   ├── videos/                 # HD videos
│   ├── stories/                # Active stories
│   └── profile/                # Profile and cover photos
│
├── tik/<username>/
│   ├── videos/                 # TikTok videos
│   ├── images/                 # Photo mode slides
│   ├── stories/                # Active stories
│   ├── albums/<playlist>/      # Series/playlists
│   ├── reposts/, liked/, favorites/   # Liked and favorites only when public
│   └── profile/                # Profile picture
│
├── twt/<username>/
│   ├── posts/                  # Media from the user's own tweets
│   ├── images/, videos/        # Media tab items not already in posts/
│   ├── highlights/             # Highlights tab
│   ├── reposts/                # Media of retweets
│   ├── replies/                # Media of the user's replies
│   └── profile/                # Avatar and banner
│
├── loi/<post id>/<author>/<post id> <title>/
│   ├── info.md                 # The post's fields, download links and unzip password
│   ├── images/                 # Preview images
│   └── <archive>.7z            # VIP archives, saved as the site sends them
│
└── <source>/<post id>/         # A single post, reel, tweet or video given by its link
```
