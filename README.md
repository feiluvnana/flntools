# flntools

Dead simple media downloader for **TikTok**, **Facebook**, **Instagram**, **Twitter/X**, **LOIBUS**, and **Telegram**. The command is `flnd`.

The plan for the shared helpers flnd is built on is in [plan/README.md](plan/README.md).

---

## Installation

Requires Python 3.11 or newer, and [uv](https://docs.astral.sh/uv/). From this folder:

```bash
uv sync                          # creates .venv from uv.lock; run it again after a pull
uv run flnd install-browser
uv run flnd download ins:example_user
```

Or `make setup`, then `make flnd ARGS="download ins:example_user"`. Every task (`setup`, `test`,
`lint`, `type`, `check`, `flnd`, `dist`) is in `scripts/dev.py`, so on Windows, which has no make,
run `uv run --no-project scripts/dev.py <task>`, or install make with `winget install ezwinports.make`.
`uv tool install --editable .` puts `flnd` on your PATH instead.

ffmpeg comes with `uv sync`, as the `imageio-ffmpeg` dependency; flnd uses it to join Facebook and
Instagram videos with their sound, and to compress videos. Where that package has no wheel (musl Linux,
32-bit ARM), install ffmpeg on your PATH instead.

`flnd install-browser` downloads Chromium (about 200 MB) once, into Playwright's folder in your user cache,
for the sources that open a browser. Your installed Chrome is used instead when there is one.

### A folder that runs without Python

```bash
make setup
make dist            # dist/flnd/flnd (flnd.exe on Windows), for the OS you build on
```

Copy the whole `dist/flnd/` folder; it holds `flnd`, `_internal/` and `licenses/`, the third-party licences.
The bundle includes ffmpeg, a GPL program: `licenses/ffmpeg.txt` has its licence and the offer of its
source, and `licenses/ffmpeg-source.txt` the exact build.
Then run `flnd install-browser` once. The bundled `flnd` still writes `.flnd/` in the folder you run it from.

---

## Usage

```bash
flnd download <targets...> [--from CATEGORIES] [--since DATE] [--until DATE] [--unpack] [--zip]
              [--compress-image PRESET] [--compress-video PRESET] [--format-image FORMAT] [--format-video FORMAT]
flnd sync [targets...] [--only STEPS] [any download option but --since and --until]
flnd login <source>
flnd logout <source>
flnd install-browser
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
flnd download ins:example_user --from reels,tagged --since 2026-01-01

# Everything, other people's content too: reposts, replies, liked
flnd download twt:example_handle tik:example_creator --from all

# Only the profile pictures
flnd download fbk:example.page ins:example_user --from profile

# One TikTok video, one Instagram post or reel, one tweet
flnd download https://www.tiktok.com/@example_creator/video/7301234567890123456
flnd download https://www.instagram.com/p/C1abcDEFghi/
flnd download https://x.com/example_handle/status/1790000000000000000

# A LOIBUS post with its VIP archives, unpacked with the post's password
flnd download https://loix.cc/lois/5930 --from all --unpack

# Smaller files: photos' shorter side at most 1536, videos at most 720p; the originals are replaced
flnd download ins:example_user --compress-image small --compress-video small

# Videos as H.265, 30-50% smaller again, but many browsers can't play it
flnd download tik:example_creator --compress-video balanced --format-video h265
```

### Options

Every source takes the same options:

- `--from`: which categories to take, such as `posts,reels`, or `all` (`flnd --help` lists them).
  Without it, each source's own content; other people's (tagged, reposts) only when named;
- `--since`, `--until`: only items dated in that range (YYYY-MM-DD);
- `--unpack`: unpack each saved archive into a folder named after it, including one saved by an
  earlier run;
- `--zip`: zip each target's folder when it ends;
- `--compress-image`, `--compress-video` (`small`, `balanced` or `high`): make each new photo or video
  smaller after it is saved, in the same format. It **replaces the original**, and strips its
  metadata (date taken, camera, GPS); the post's date stays in the file name. A file that wouldn't
  get smaller is kept as it is;

  | Preset | Photos | Videos |
  | --- | --- | --- |
  | `small` | quality 70, shorter side at most 1536 | H.264 CRF 28, at most 720p and 30 fps, audio 64 kb/s |
  | `balanced` | quality 80, shorter side at most 2304 | CRF 23, at most 1080p and 60 fps, audio 128 kb/s |
  | `high` | quality 90, full size | CRF 19, full size, audio 160 kb/s |

- `--format-image`: write compressed photos as `jpg`, `webp`, `avif`, `png`, `gif`, `tif` or `bmp`;
- `--format-video`: write compressed videos in a container (`mp4`, `mov`, `mkv`, `webm`, `avi`, `ts`,
  `3gp`, `gif`), with a codec (`h264`, `h265`, `vp9`), or both as `mkv:h265`. Each `--format-*` needs
  its `--compress-*`;
- `--locale`, `--timezone`: the browser's language and timezone, such as `fr-FR` and `Europe/Paris`.
  Match where your connection really comes from.

A saved photo or video gets the extension its content calls for (a WebP is saved as `.webp`, not
`.jpg`), a web page served instead of a photo is retried rather than saved, and a download whose
size the server didn't give is checked to be complete.

`flnd download` never asks questions. `flnd` alone prints the help.

---

## Keeping downloads up to date

```bash
flnd sync                                             # every target downloaded so far
flnd sync ins:example_user tik:example_creator        # only these
flnd sync --compress-video small --format-video h265  # and compress their videos from now on
flnd sync --compress-video off                        # stop compressing videos
flnd sync --only info                                 # just the summary; nothing is fetched or changed
```

Every `flnd download` records its target and options in `.flnd/targets.db`, and `flnd sync` runs
each target again with them. Options given to `sync` replace the recorded ones from then on.
Folders downloaded before flnd recorded options are synced with the defaults. For each target:

1. **repair**: a file whose extension doesn't match its content is renamed; a broken one (cut
   off, or a web page) is deleted and downloaded again; a missing one is downloaded again;
2. **fetch**: new posts are downloaded, as `flnd download` would;
3. **compress**: with a compress option, files not compressed yet are compressed. A file is
   compressed once, whatever later options say;
4. **info**: one line per target and a total: files, photos, videos, size, space saved by
   compressing, repaired, and missing (files the site no longer lists).

`--only repair,compress` works offline. `sync` never deletes a file whose post was deleted, and
never asks questions.

---

## Logging in

Most sources work better logged in. `flnd login` reads your login cookies from your browser, or from a cookie file you exported:

```bash
flnd login ins                                              # asks: from your browser, or a cookie file?
flnd login ins --method browser --browser firefox           # Chrome, Firefox, Edge, Brave or Chromium
flnd login twt --method file --path ~/Downloads/cookies.txt # Netscape cookies.txt or JSON
flnd logout ins                                             # or: flnd logout --all
```

A full browser export works: only the source's own cookies are kept, in `.flnd/cookies/<source>.txt`.
A source that isn't logged in still downloads with public access, and says so. LOIBUS without a
login gives a post's info and preview images only; its archives (`--from archive`) need a VIP login,
and count against the account's daily quota.

---

## Download Layout (`.flnd/downloads/`)

Files are saved with platform-optimized subfolder layouts and automatic deduplication:

```
.flnd/downloads/
├── ins/<username>/
│   ├── posts/                  # Feed photos and every carousel item
│   ├── reels/                  # Reels not already saved from the grid
│   ├── tagged/                 # Tagged posts (opt-in)
│   ├── reposts/                # Reposts (opt-in)
│   ├── stories/                # Active 24h stories
│   ├── highlights/<title>/     # Story highlights
│   └── profile/                # HD profile picture
│
├── fbk/<username_or_id>/
│   ├── posts/                  # Timeline posts
│   ├── photos/                 # Uploaded photos
│   ├── tagged/                 # Tagged photos (opt-in)
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
│   ├── reposts/, liked/, favorites/   # Opt-in; liked and favorites only when public
│   └── profile/                # Profile picture
│
├── twt/<username>/
│   ├── posts/                  # Media from the user's own tweets
│   ├── images/, videos/        # Media tab items not already in posts/
│   ├── highlights/             # Highlights tab
│   ├── reposts/                # Media of retweets (opt-in)
│   ├── replies/                # Media of the user's replies (opt-in)
│   └── profile/                # Avatar and banner
│
├── loi/<post id>/<author>/<post id> <title>/
│   ├── info.md                 # The post's fields, download links and unzip password
│   ├── images/                 # Preview images
│   └── <archive>.7z, <archive>/   # VIP archives (--from archive), unpacked with --unpack
│
└── <source>/<post id>/         # A single post, reel, tweet or video given by its link
```
