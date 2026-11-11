# flntools

Dead simple media downloader for **TikTok**, **Facebook**, **Instagram**, and **Twitter/X**. The command is `flnd`.

The plan for the shared helpers flnd is built on is in [plan/README.md](plan/README.md).

---

## Installation

Requires Python 3.11 or newer. This checkout uses a local virtualenv on Python 3.14.

```bash
uv venv .venv --python 3.14
source .venv/bin/activate
uv pip install -e .
playwright install chromium
```

`.venv` stays in this folder. Activate it again with `source .venv/bin/activate` before running `flnd`.

---

## Usage

```bash
flnd download <targets...> [--from CATEGORIES] [--since DATE] [--until DATE] [--unpack] [--zip]
flnd login <source>
flnd logout <source>
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

### Examples

```bash
# Single target
flnd download ins:example_user
flnd download tik:example_creator
flnd download twt:example_handle
flnd download fbk:example.page

# Several targets
flnd download ins:example_user twt:example_handle

# One target per line from a file (blank lines and # comments are skipped)
cat list.txt | flnd download

# Reels and tagged posts from this year, and one single TikTok video by URL
flnd download ins:example_user --from reels,tagged --since 2026-01-01
flnd download https://www.tiktok.com/@example_creator/video/7301234567890123456
```

### Options

Every source takes the same options:

- `--from`: which categories to take, such as `posts,reels`, or `all` (`flnd --help` lists them).
  Without it, each source's own content; other people's (tagged, reposts) only when named;
- `--since`, `--until`: only items dated in that range (YYYY-MM-DD);
- `--unpack`: unpack each saved archive into a folder named after it;
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
A source that isn't logged in still downloads with public access, and says so.

---

## Download Layout (`.flnd/downloads/`)

Files are saved with platform-optimized subfolder layouts and automatic deduplication:

```
.flnd/downloads/
├── ins/<username>/
│   ├── posts/                  # Feed photos and multi-photo carousels
│   ├── reels/                  # Video reels
│   ├── reposts/                # User reposts
│   ├── stories/                # Active 24h stories
│   ├── highlights/<title>/     # Grouped story highlights
│   └── tagged/                 # Tagged posts
│
├── fbk/<username_or_id>/
│   ├── posts/                  # Timeline posts
│   ├── photos/                 # Uploaded photos
│   ├── reels/                  # Facebook reels
│   ├── videos/                 # HD videos
│   ├── stories/                # Active stories
│   ├── highlights/<title>/     # Profile highlights
│   └── albums/<album_title>/   # Photo albums
│
├── tik/<username>/
│   ├── videos/                 # TikTok videos
│   ├── images/                 # Photo mode slides
│   ├── stories/                # Active stories
│   └── albums/<playlist>/      # Series/playlists
│
└── twt/<username>/
    ├── posts/                  # Media from user tweets / posts timeline
    ├── images/                 # Original resolution photos from media tab
    └── videos/                 # Best bitrate MP4 videos from media tab
```
