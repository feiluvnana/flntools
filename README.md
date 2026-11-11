# flntools

Dead simple media downloader for **TikTok**, **Facebook**, **Instagram**, and **Twitter/X**. The command is `flnd`.

The plan for the shared helpers flnd is built on is in [plan/README.md](plan/README.md).

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
flnd download <targets...>
flnd auth set-cookie --path <cookies.txt>
```

Or via python module:

```bash
python -m flnd download <targets...>
```

Files go to `.flnd/downloads/`, and the browser window is visible while a source runs.

### Target Format

`<source>:<want to download>`

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
```

### Prompt mode

Run `flnd` with no arguments in a terminal and it asks what to do: download (it asks for the source and the username one target at a time) or set a cookie file. Before it runs, it prints the matching command line, so you can type it directly next time. Without a terminal, `flnd` alone prints the help.

---

## Cookies

Most sources need your login cookies. Export them from your browser as a Netscape `cookies.txt` or a JSON file, then:

```bash
flnd auth set-cookie --path ~/Downloads/cookies.txt   # a file
flnd auth set-cookie --content "$(pbpaste)"           # the raw text
pbpaste | flnd auth set-cookie                        # the raw text from stdin
```

Each cookie is stored under the source its domain belongs to, in `.flnd/cookies/<source>.txt`. A full browser export sets every matching source at once; cookies for other sites are skipped.

If a source runs without cookies in a terminal, `flnd` offers to set a cookie file right there.

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
