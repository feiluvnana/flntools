# flntools

Dead simple media downloader for **TikTok**, **Facebook**, **Instagram**, **Twitter/X**, and **LOIBUS**. The command is `flnd`.

The plan for the shared helpers flnd is built on is in [plan/README.md](plan/README.md).

---

## Installation

Requires Python 3.11 or newer. This checkout uses a local virtualenv on Python 3.14.

```bash
uv venv .venv --python 3.14
source .venv/bin/activate
uv pip install -e .
flnd install-browser
```

`.venv` stays in this folder. Activate it again with `source .venv/bin/activate` before running `flnd`.
With `uv`, `uv tool install --editable .` puts `flnd` on your PATH instead.

`flnd install-browser` downloads Chromium (about 150 MB) once, into Playwright's folder in your user cache,
for the sources that open a browser. Your installed Chrome is used instead when there is one.

### A folder that runs without Python

```bash
make setup
make dist            # dist/flnd/flnd (flnd.exe on Windows), for the OS you build on
```

Copy the whole `dist/flnd/` folder; it holds `flnd`, `_internal/` and `licenses/`, the third-party licences.
Then run `flnd install-browser` once. The bundled `flnd` still writes `.flnd/` in the folder you run it from.

---

## Usage

```bash
flnd download <targets...> [--from CATEGORIES] [--since DATE] [--until DATE] [--unpack] [--zip]
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
```

### Options

Every source takes the same options:

- `--from`: which categories to take, such as `posts,reels`, or `all` (`flnd --help` lists them).
  Without it, each source's own content; other people's (tagged, reposts) only when named;
- `--since`, `--until`: only items dated in that range (YYYY-MM-DD);
- `--unpack`: unpack each saved archive into a folder named after it, including one saved by an
  earlier run;
- `--zip`: zip each target's folder when it ends;
- `--locale`, `--timezone`: the browser's language and timezone, such as `fr-FR` and `Europe/Paris`.
  Match where your connection really comes from.

`flnd download` never asks questions. `flnd` alone prints the help.

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
