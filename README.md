# flntools

Dead simple media downloader for **TikTok**, **Facebook**, **Instagram**, and **Twitter/X**. The command is `flnd`.

`cli`, `log`, and `prompt` for this repo are specified in [PLAN.md](PLAN.md). They are not implemented yet.

---

## Installation

Requires Python 3.10 or newer. This checkout uses a local virtualenv on Python 3.14.

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
flnd [targets...] [options]
```

Or via python module:

```bash
python -m flnd [targets...] [options]
```

### Target Format

`<source>:<want to download>`

Supported sources:

- `tik`: TikTok
- `fbk`: Facebook
- `ins`: Instagram
- `twt`: Twitter / X

### Examples

```bash
# Single target
flnd ins:example_user
flnd tik:example_creator
flnd twt:example_handle
flnd fbk:example.page

# Multiple targets with download limit
flnd ins:example_user twt:example_handle --limit 5

# See browser during execution
flnd ins:example_user --no-headless
```

---

## Cookies

Place your Netscape `cookies.txt` (or `.json`) under `.flnd/cookies/`:

- `.flnd/cookies/tik.txt`
- `.flnd/cookies/fbk.txt`
- `.flnd/cookies/ins.txt`
- `.flnd/cookies/twt.txt`

If cookies are missing when a site requires authentication, `flnd` will display an actionable instruction detailing the exact location to place your cookie file.

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
