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
`lint`, `type`, `check`, `baseline`, `api`, `flnd`, `dist`, `try`) is in `scripts/dev.py`, so on Windows, which has no make,
run `uv run --no-project scripts/dev.py <task>`, or install make with `winget install ezwinports.make`.
`uv tool install --editable .` puts `flnd` on your PATH instead.

`make try ARGS="ins:example_user --from reels --time 2026-01-01,"` is for working on a source: it runs
the listing for real with the saved login in `.flnd`, downloads and records nothing, and prints each post
with its planned file paths and URLs (without their signed query).

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
              [--shrink [off]] [-v]
flnd sync [targets...]
flnd login
flnd logout <sources...> | all
```

Or via python module:

```bash
python -m flnd download <targets...>
```

Files go to `.flnd/media/`, and the browser window is visible while a source runs.
A file already saved, even under another name (a feed's dated post and a tab's undated photo of
the same media), is not downloaded again; each category says how many it skipped, and `-v` lists
each skipped file with the name that holds it.
flnd uses the `HTTPS_PROXY` and `ALL_PROXY` environment variables (httpx reads them).

### Target Format

`<source>:<name, id or URL>`, or just the URL: `ins:example_user`, `ins:@example_user` and
`https://www.instagram.com/example_user/` are the same target. `flnd --help` lists every
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

# Smaller files, each in its own format; the originals are replaced, and this is saved for the
# target, so later downloads and syncs do the same
flnd download ins:example_user --shrink

# Stop shrinking this target's new files from now on
flnd download ins:example_user --shrink off
```

### Options

Every source takes the same options:

- `--from`: which categories to take, such as `posts,reels` (`flnd --help` lists them). Without it,
  every category, other people's content (tagged, reposts) included. It applies to profiles and
  chats only: with a single post's link among the targets, it is an error;
- `--time <since>,<until>`: only items dated in that range, both days included (YYYY-MM-DD). Leave a
  side empty for no limit: `--time 2026-01-01,` or `--time ,2026-06-30`. `*` works too, quoted
  (`--time 2026-01-01,'*'`), since zsh stops at a bare `*` with "no matches found";
- `--shrink`: compress each new photo, video and audio file after it is saved, to a balanced
  quality (photos up to 2,304 px, videos up to 1080p, quality 80) **in its own format**: a JPEG
  stays a JPEG, an MP4 stays an MP4 in its own codec, so every file opens where it opened before.
  It **replaces the original** when the result is smaller, and strips its metadata (date taken,
  camera, GPS); the post's date stays in the file name. Videos are encoded one at a time.

`--shrink` is **saved for each target** in `.flnd/metadata/targets.db`, and used by every later
`download` and `sync` of it; a run that uses it says so, as `ins:example_user: --shrink (saved)`.
The `download` that turns it on shrinks the target's files already saved first, then the new ones.
`--shrink off` turns it off and touches no file. `--from` and `--time` are never saved. To convert
to another format (WebP, H.265), use any converter afterwards.

A saved photo, video or audio file gets the extension its content calls for (a WebP is saved as
`.webp`, not `.jpg`), a web page served instead of a photo is retried rather than saved, and a
download whose size the server didn't give is checked to be complete. Archives are saved as the site
sends them; unpack them with any archive tool. There is no download limit: a run ends when the
sources have nothing more, or on Ctrl-C.

A dropped connection resumes where it stopped instead of starting the file again, and so does the
next run after a Ctrl-C or a crash: the unfinished file waits as a hidden `.part` with its
`.part.json` beside it. A link that expired meanwhile is asked for afresh, once, and the file
resumes from it. The **first Ctrl-C** stops listing and lets the files downloading now finish (the
rows say `stopping: 2 files finishing (Ctrl-C again to stop now)`); the **second** stops at once,
keeping the unfinished files to resume. Either way flnd exits 130.

`flnd download` asks no questions, except whether to download Chromium when Google Chrome is
missing, which is asked before any download starts. `flnd` alone prints the help.

---

## Keeping downloads up to date

```bash
flnd sync                                             # every target downloaded so far
flnd sync ins:example_user tik:example_creator        # only these
```

`flnd sync` takes no options and changes nothing about how a target is downloaded: each target runs
with its saved `--shrink`, and each source's default categories. To change a
setting, run `flnd download` with it. Folders downloaded before flnd saved settings are synced
without any. For each target:

1. **repair**: a missing, empty or broken file (cut off, or a web page) is downloaded again, and
   leftover `.part` files are removed, except an unfinished download from the last 7 days, which
   the fetch resumes;
2. **fetch**: new posts are downloaded, as `flnd download` would;
3. **summary**: one line per target, `ins:example_user: 120 files, 85.2 MB; 2 repaired, 1 missing`
   (missing: files the site no longer lists), then the totals.

`sync` never deletes a file whose post was deleted, asks no questions but that same Chromium one
when Google Chrome is missing, and exits 1 when a target failed.

---

## Run it on a schedule

`flnd sync` suits a daily job: it asks nothing, and its exit status (1 when a target failed) tells
the scheduler whether it went well. Run it from the repo folder, because `.flnd/` is taken from where
flnd runs, and install Google Chrome first, since a scheduled run has no one to answer the Chromium
question. Cron, with uv's full path (`which uv` shows it), every day at 03:15:

```bash
15 3 * * * cd /path/to/flntools && /full/path/to/uv run flnd sync
```

Windows, with Task Scheduler's `schtasks`, the same daily run:

```bat
schtasks /create /tn "flnd sync" /sc daily /st 03:15 /tr "cmd /c cd /d C:\path\to\flntools && uv run flnd sync"
```

---

## Logging in

Most sources work better logged in. `flnd login` reads your login cookies from your browser, or from a cookie file you exported.
It takes no arguments and asks everything, in a terminal: the source (those already logged in say so),
then from your browser or a cookie file, then which browser (Chrome, Firefox, Edge, Brave or Chromium)
and profile, the default one preselected, or the file's path (Netscape cookies.txt or JSON).
For Telegram the methods are its own: your browser's Telegram Web login, a QR code, or your phone
number and the code Telegram sends.

```bash
flnd login                  # asks the source, the method, then the browser or the file
flnd logout ins tik         # removes those logins
flnd logout all             # removes every login
```

A full browser export works: only the source's own cookies are kept, encrypted in `.flnd/metadata/secrets.db`
with every other login; the file you give is read once and never copied. A login saved by an earlier
version as `.flnd/cookies/<source>.txt` moves into it on the next run, so there's no need to log in again.
The key is `.flnd/metadata/secret.key`, so keep `.flnd/` as private as the logins in it.
A source that isn't logged in still downloads with public access, and says so. LOIBUS without a
login gives a post's info and preview images only; its archives need a VIP login,
and count against the account's daily quota (`--from photo` leaves them out).

---

## The `.flnd/` folder

```
.flnd/
├── media/       # every downloaded file, laid out as below
├── metadata/    # history.db, targets.db, sources.db, secrets.db and secret.key
└── log/         # one log per download or sync, kept 30 days
```

Each run of `flnd download` or `flnd sync` writes `.flnd/log/<date>_<time>_<command>.log`: every
line `-v` would show, plain text with the time of each, whatever the screen showed. URLs are
written without their query strings and no login ever reaches it, so a log can be read or shared
to understand a run. Logs older than 30 days are deleted when a run starts.

A `.flnd/` from before this layout isn't moved: flnd reads and writes only the folders above. To
keep an old one, move it by hand while flnd isn't running: `downloads/` becomes `media/`, and
`history.db`, `targets.db`, `sources.db`, `secrets.db` and `secret.key` go into `metadata/`.

## Media Layout (`.flnd/media/`)

Files are saved with platform-optimized subfolder layouts and automatic deduplication:

```
.flnd/media/
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
