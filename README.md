# flnd

flnd downloads photos, videos and files from the sites you follow and keeps them up to date. It shrinks them without visible loss and unpacks the archives it saves. It is a Python library with a thin command line on top, and new sites can be added as plugins.

> **Status.** This branch (`next`) holds [PLAN.md](PLAN.md), the plan for rebuilding flnd as one fast package, and [ARCHITECTURE.md](ARCHITECTURE.md), which explains the design. This README describes flnd **as planned**. Until the rebuild lands (PLAN.md §15), the code here still behaves as on `main`: run `git show main:README.md` to read that version. The `docs/` pages linked below are written during the rebuild.

Sites: **Instagram** (`ins`), **TikTok** (`tik`), **Twitter/X** (`twt`), **Facebook** (`fbk`), **Telegram** (`tlg`), **LOIBUS** (`loi`) and **Mimihentai** (`mht`), plus any site a plugin adds.

---

## Install
flnd needs [uv](https://docs.astral.sh/uv/) and Python 3.11 or newer. [Google Chrome](https://www.google.com/chrome/) is needed for the sites that open a browser; when it's missing, flnd offers to download Chromium once.

```bash
git clone https://github.com/feiluvnana/flntools && cd flntools
uv tool install .                        # puts `flnd` on your PATH
uv tool install '.[telegram]'            # with Telegram
```

From a checkout, `make setup` creates `.venv`, and then `uv run flnd …` runs it. On Windows, install make first with `winget install ezwinports.make`, or type the `uv` command each Makefile target runs (see [Development](#development)).

Photos are compressed in-process, with no tool to install. ffmpeg is used for videos (joining video and sound, compressing) and 7zz for archives. flnd finds each on your PATH, or offers to download it the first time it's needed.

## Quick start
```bash
flnd login ins chrome                        # use your Chrome login for Instagram
flnd download ins:example_user               # every category of example_user, once
flnd download ins:example_user --only reels --when 2026
flnd follow ins:example_user                 # follow it, and download what's there now
flnd sync                                    # new posts of every followed target
flnd ls                                      # what you follow: files, size, last sync
```

## Commands
```
flnd download <targets…> [--only CATS] [--when WHEN] [--limit N] [--compress] [--dry] [--exec CMD]
flnd follow <targets…> [--only CATS] [--when WHEN] [--compress]   follow, and download what's there now
flnd sync [targets…] [--compress] [--exec CMD]                    what's new for followed targets
flnd unfollow <targets…>             stop following; files and history stay
flnd ls [targets…] [--json]          followed targets: files, size, last sync, failures
flnd status [--json]                 the last run: what failed and why
flnd retry                           retry the last run's retryable failures
flnd check [--fix]                   compare the history with the files on disk, and repair it
flnd login [source] [how]            how: chrome | firefox | safari | cookies.txt | qr | phone; alone, lists your logins
flnd logout <source>
flnd sources                         built-in and plugin sources

global options: --home DIR · --ui full|inline|plain · -v · --json
```

- **A target** is `<source>:<name, id or link>`, or just the link: `ins:example_user`, `ins:@example_user` and `https://www.instagram.com/example_user/` are the same target. A search is `<source>?<words>`, as in `mht?romance`. `flnd sources` lists every accepted form.
- **`--only`** picks categories: `posts`, `reels`, `stories`, `highlights`, `tagged`, `reposts` and so on (`flnd sources` lists each site's). Without it, flnd takes every category, other people's content included (tagged posts, reposts, likes). A single post has no categories, so `--only` with a post's link is an error.
- **`--when`** takes `2026`, `2026-06`, `2026-06-15`, `2026-01..2026-03` or `30d` (the last 30 days).
- **`--dry`** shows what would be downloaded (new, already saved, gone) and writes nothing.
- **`--exec CMD`** runs a command for each saved file, such as `--exec 'notify-send {path}'`. It never goes through a shell, so a file name can't inject a command.
- **`--json`** prints one JSON event per line, for scripts.
- **Exit codes:** 0 ok, 1 some files failed, 2 a usage error, 3 a login is needed, 4 flnd stopped on an error (a full disk, a missing tool, a crash), 130 stopped with Ctrl-C.

### Following and syncing
`download` takes what's there once. `flnd follow` remembers a target with its options and downloads it. `flnd sync` then fetches what's new for every followed target, stopping early once it reaches posts it already has. `flnd unfollow` stops following a target; its files and history stay. A scheduled sync suits cron, launchd or Task Scheduler, because it asks no questions and its exit code says whether it went well:

```bash
15 3 * * * /path/to/flnd --home /path/to/media sync
```

### When something fails
- **Network errors and rate limits** are retried with backoff, and a file that still fails is listed at the end of the run:
  ```
  ✗ 3 files failed: 2 network (retry: flnd retry), 1 gone. Details: flnd status
  ```
- **Logins:** a login that expired stops that site only, with the command that fixes it.
- **Ctrl-C** works in three stages:
  1. the first stops listing and lets the files in progress finish;
  2. the second stops at once and keeps the unfinished files, which resume on the next run;
  3. the third exits immediately.
- Nothing is ever left half-written.

### Compressing and unpacking
- **`--compress`** makes each file as small as it can without visible loss, as it lands: photos to AVIF or optimized JPEG, videos to AV1 (or HEVC, with `[compress] video = "hevc"`, for devices that can't play AV1). Each result is checked before it replaces the original, and a file that wouldn't get smaller is left alone. `flnd sync --compress` also shrinks files saved earlier.
- **Archives unpack automatically** into a folder beside them. A site that knows the password (LOIBUS) passes it on. Unpacking refuses archive tricks: paths that escape the folder, links, and zip bombs.

## Logging in
`flnd login <source> [how]` reads your login from your browser (Chrome, Firefox, Safari, Edge, Brave), or from a cookies file you exported. Telegram logs in with Telegram Web, a QR code or your phone number. Logins are stored encrypted in `<home>/secrets.db`, with the key in `<home>/secret.key`, so keep the home as private as the logins in it. `[vault] key = "keychain"` in `config.toml` moves the key to your system keychain, though a scheduled sync may not be able to reach it there. `flnd login` alone shows each login and when it expires. A site without a login still downloads what's public, and says so.

## The home folder
Everything flnd keeps lives in one folder: `--home`, else `$FLND_HOME`, else `./.flnd`.
```
<home>/
  media/<source>/<target>/…   downloaded files, by category (posts/, reels/, stories/, …)
  flnd.db                     what was saved, followed targets, run history and failures
  secrets.db                  logins, encrypted
  secret.key                  the key to secrets.db
  config.toml                 defaults and per-site settings (pace, proxy, scroll style)
  logs/                       each run's log and crash reports, kept 30 days
```
An existing `.flnd` from an older flnd is converted on first use, so nothing is downloaded again. The old files are only read, so an older flnd still works on it.

## Use it from Python
```python
import flnd

with flnd.open("~/Media/flnd") as fl:
    for event in fl.download("ins:example_user", only=["reels"], when="2026", compress=True):
        if isinstance(event, flnd.Saved):
            print(event.path, event.post.date)

    report = fl.sync().wait()                            # runs it, and returns the counts and failures
    photos = fl.files(target="ins:example_user", kind="image")   # what's saved, from flnd.db
```
[docs/library.md](docs/library.md) covers every call, its options and its events.

## Add a site
A source is a small Python class: it says which links it accepts and how to list a target's posts, and flnd does the rest (downloading, resuming, dating, deduping, progress).
```python
from flnd.source import Source, Site, Post, File, target, cookies

class Example(Source):
    """example.com: a user's posts."""
    code = "exm"                     # targets look like exm:example_user
    domains = ("example.com",)       # links, cookies and pacing
    api = 1                          # the source API this was written for
    login = cookies("session")       # logged in when this cookie is there

    @target("exm:{user}", "example.com/{user}")
    def profile(self, site: Site, user: str) -> dict:
        return site.fetch(f"https://api.example.com/users/{user}").json

    @profile.category(newest_first=True)
    def posts(self, site: Site, profile: dict):
        for page in site.pages(f"https://api.example.com/users/{profile['id']}/posts", cursor="next"):
            for p in page["items"]:
                yield Post(p["id"], [File(p["url"])], date=p["time"])
```
Save it as `exm.py` in your plugin folder (`~/.config/flnd/sources/`, or `$FLND_SOURCES`), publish it as a package with a `flnd.sources` entry point, or call `fl.add(Example)`. A plugin runs with your logins, so install only code you trust; flnd never loads one from a home folder. [docs/writing-a-source.md](docs/writing-a-source.md) walks through it, with the `flnd.testing` kit for its tests.

## Documentation
[ARCHITECTURE.md](ARCHITECTURE.md) explains how flnd is built and how to write a source. [docs/](docs/README.md) covers:
- the library and the CLI;
- writing a source;
- errors, storage and performance;
- a page per site.

The code itself is commented for people to read: every module starts with what it does and how it fits.

## Development
| make | what it runs |
| --- | --- |
| `make setup` | `uv sync --all-extras`: the environment from `uv.lock` |
| `make check` | lint (ruff) and every test |
| `make test ARGS=…` | the tests, or some of them |
| `make bench` | the offline benchmarks; `ARGS=--gate` compares with the targets |
| `make api` | regenerates `tests/data/api.txt`, the public API snapshot |
| `make docs` | builds the API reference into `build/docs/` |
| `make type` | checks the public API's type hints with pyright (informational, not part of `check`) |
| `make try ARGS=…` | lists, live, the files one target would save, downloading nothing |
| `make dist` | a single-file `flnd` binary for this OS (PyInstaller) |

Each target is one `uv` command, listed in the `Makefile`, so it can also be typed by hand on a machine without make. Contributors and coding agents: read [AGENTS.md](AGENTS.md) first.
