# flnd

flnd downloads photos, videos and files from the sites you follow, and keeps them up to date.
It unpacks the archives it saves, and hard-links what it has saved before. It is a Python
library with a thin command line on top, and new sites can be added as plugins.

Sites: **Instagram** (`ins`), **TikTok** (`tik`), **Twitter/X** (`twt`), **Facebook** (`fbk`),
**Telegram** (`tlg`), **LOIBUS** (`loi`) and **mimihentai** (`mht`), plus any site a plugin adds.

---

## Install
flnd needs [uv](https://docs.astral.sh/uv/) and Python 3.11 or newer.

```bash
git clone https://github.com/feiluvnana/flntools
cd flntools
uv tool install .                  # puts `flnd` on your PATH, Telegram support included
```

From a checkout, `make setup` creates `.venv`, and then `uv run flnd …` runs it. On Windows,
install make with `winget install ezwinports.make`, or type the `uv` command each Makefile
target runs (see [Development](#development)).

Outside programs, each needed only for some files:

- **Google Chrome**, for the sites that open a browser. When it is missing and flnd runs in a
  terminal, it asks once whether to download Chromium for itself (about 200 MB, kept in
  Playwright's cache). Declined, or off a terminal (cron, `--json`), the run stops with
  "flnd needs Google Chrome" and a link to install it.
- **ffmpeg**, for videos: joining a video with its separate sound track.
- **7-Zip** (`7zz`, or the older `7z`), for 7z, RAR and encrypted zip archives. zip and tar are
  read by Python.

flnd downloads neither of the last two: it looks for each on your PATH. A missing one is a
`ToolError` whose hint says what to install, and it costs only the files that needed it: a video
that needs joining fails (and is retried on later runs), an archive is kept but not extracted.

## Quick start
```bash
flnd login ins chrome                        # use your Chrome login for Instagram
flnd download ins:example_user               # every category of example_user, once
flnd download ins:example_user --only reels --when 2026
flnd follow ins:example_user                 # follow it, and download what's there now
flnd sync                                    # what's new for every followed target
flnd ls                                      # what this home knows: files, size, last sync
```

## Commands
```text
flnd download [targets…] [--only CATS] [--when WHEN] [--limit N] [--dry] [--again] [--exec CMD]
flnd follow [targets…] [--only CATS] [--when WHEN] [--again]
flnd sync [targets…] [--exec CMD]
flnd unfollow <targets…>      stop following; files and history stay
flnd ls [targets…]            the targets this home knows: followed or not, files, size, last run
flnd status                   the last run: what it saved, each failure, why, and what to do
flnd retry                    download again every file that failed
flnd check [--fix]            compare the history with the files on disk, and repair it
flnd login [source] [how]     log in; alone, lists every source's login
flnd logout <source>          remove a saved login
flnd sources                  every source: built in, from a package or the plugin folder

global flags: --home DIR · -v/--verbose · --json · -V/--version
```

`flnd --help` lists every command and every form of target each site reads, and
`flnd <command> --help` a command's flags. [flnd/docs/cli.md](flnd/docs/cli.md) covers each in full.

- **A target** is `<source>:<name, id or link>`, `<source>?<words>` for a search, or just the
  link: `ins:example_user` and `https://www.instagram.com/example_user/` are the same target,
  and `mht?romance` is a search. With no targets, `download` and `follow` read them from
  standard input, one per line.
- **`--only`** picks categories, such as `posts`, `reels`, `stories`, `highlights`, `tagged` or
  `reposts` (`flnd --help` lists each site's). Without it, flnd takes every category. With
  targets of several sites, `--only tik:videos --only twt:media` names each site's. A single
  post has no categories, so `--only` with a post's link is a usage error.
- **`--when`** takes `2026`, `2026-06`, `2026-06-15`, a range `2026-01..2026-03` (either end may
  be left out), or a span back from today: `30d`, `8w`, `1y`.
- **`--limit N`** takes at most N posts per target, or N targets from a search.
- **`--dry`** lists what would be downloaded (each file `new` or already `saved`), and
  downloads and writes nothing.
- **`--again`** lists every post again, past the posts already saved, and asks again for files
  the site said were gone (404 or 410).
- **`--exec CMD`** runs a command for each saved file once it is final, such as
  `--exec 'notify-send {path}'`; `{target}` and `{post}` work too. It never goes through a
  shell, so a file name from a site can't inject a command.
- **`--json`** prints one JSON object per line: every event, then the run's report; `ls`,
  `status`, `check`, `login` and `sources` print their rows the same way. It never asks a
  question, which suits scripts and cron.
- **Exit codes:** 0 everything worked, 1 some files or targets failed, 2 a usage error, 3 a login
  is needed, 4 flnd stopped on an error (a full disk, a missing browser, a broken source, a bug),
  130 stopped with Ctrl-C.

### Following and syncing
`download` takes what's there, once. `flnd follow` remembers a target, with its `--only`, and
downloads it. `flnd sync` then fetches what's new for every followed target (or just the ones
named). `flnd unfollow` stops following a target; its files and history stay. A sync asks no
questions, and its exit code says how it went, so it suits cron, launchd or Task Scheduler:

```bash
15 3 * * * /path/to/flnd --home /path/to/media sync
```

A sync is smart about what it has:

- **Nothing new costs little.** A category that lists newest first remembers its newest post,
  and a sync stops as soon as it gets there: a category with nothing new reads one page, which
  is one request on a site that pages by cursor.
- **An interrupted sync leaves no gap.** A sync cut short (Ctrl-C, an error, `--limit`) keeps
  where it stopped. The next sync takes the new posts on top, then jumps to that point and goes
  on from there, so nothing between them is missed.
- **Failed files come back by themselves.** Every download and sync of a target first retries
  its files that failed on earlier runs, each up to 3 times, listing the post again for fresh
  links. `flnd retry` retries every failed file at once, however many runs failed it. A file
  the site said is gone (404 or 410) is left alone; `download --again` asks for it again.

### When something fails
- **Network errors and rate limits** are retried with backoff, and a wait a site asks for
  (`Retry-After`) is kept. A file that still fails is listed at the end of the run:
  ```text
  ✗ 3 files failed: 2 network (retry: flnd retry), 1 gone. Details: flnd status
  ```
  `flnd status` shows each failure with its message, how many runs failed it and what to do.
- **Logins:** a login that is missing or expired stops that site only, with the command that
  fixes it. A site whose login is optional downloads what's public, and says so.
- **Mistakes** you can fix (usage, login, disk, a tool) print `error: …` and what to do, never a
  traceback. A bug writes its traceback to `<home>/log/crash-<time>.txt` (`-v` prints it).
- **Ctrl-C** works in three stages:
  1. the first stops listing and lets the files in progress finish;
  2. the second stops at once. Unfinished files stay as `<name>.part` and resume on the next
     run;
  3. the third quits at once, without waiting for the browser to close or the last rows to be
     written.

  A file is written under its real name only once it is whole, so nothing is ever left
  half-written.

### Unpacking archives
Archives unpack by themselves (`.zip`, `.7z`, `.rar`, `.tar*`) into a folder of their name
beside them. A site that knows the password (LOIBUS) passes it on. Unpacking refuses archive
tricks: paths that escape the folder, links of any kind, and zip bombs. The archive file stays
on disk after extraction.

### Dedupe and sidecars
- **Dedupe:** a file whose bytes are already saved (a repost, a photo in two albums) becomes a
  hard link to the saved one, so it takes no more space. `dedupe = "off"` under `[defaults]` in
  the home's `config.toml` keeps both copies.
- **Sidecars:** with `sidecars = true` under `[defaults]`, each saved file gets `<file>.json`
  beside it, with its post's date, link, caption, author and tags.

### Checking a home
`flnd check` compares the history in `flnd.db` with the files on disk, and prints each file
that is missing, unknown or of the wrong size. It fetches nothing, and exits 1 when they
disagree. `flnd check --fix` makes the history match the disk; it never deletes, moves or
downloads a file.

## Logging in
`flnd login <source> <how>` reads your login for that site from a browser (`chrome`, `firefox`,
`edge`, `brave`, `chromium`, or `safari` on macOS), or from a `cookies.txt` or JSON cookie file
you exported. Reading Safari needs Full Disk Access for your terminal (System Settings › Privacy
& Security › Full Disk Access); without it, flnd says so and how to allow it. It keeps only the
site's cookies, and refuses a login that lacks the site's login cookie. Telegram logs in its own
way: `browser` (Telegram Web's session), `qr` or `phone`. Left out in a terminal, `how` is asked
with numbered choices.

```bash
flnd login ins chrome
flnd login twt ~/Downloads/cookies.txt
flnd login tlg qr
flnd login                                   # each source's login, and what expires soon
flnd logout ins
```

Logins and cookies are stored in `<home>/metadata/secret.db`, a file only your account can
read — keep the home as private as the logins in it.

## The home folder
Everything flnd keeps lives in one folder: `--home`, else `$FLND_HOME`, else `./.flnd`.
```text
<home>/
  media/<code>/<target>/…     downloaded files, by category (posts/, reels/, stories/, …)
  metadata/
    flnd.db                   what was saved, the targets, run history and failures (SQLite)
    secret.db                 logins and cookies, readable only by your account
  config.toml                 optional settings (below)
  log/                        crash reports and samples of changed site answers, kept 30 days
```
[flnd/docs/storage.md](flnd/docs/storage.md) describes every file and table.

### Settings
`<home>/config.toml` holds defaults for every run and settings per site. A flag given for one run
wins over it, and it wins over flnd's default:

```toml
[defaults]
when = "1y"                     # as if every download, follow and sync had --when 1y

[sources.ins]
pace = 1.0                      # seconds between page requests
scroll = "fast"                 # or "human", the default
limits = { "cdninstagram.com" = 8 }   # files at once from that host
proxy = "socks5://127.0.0.1:1080"     # HTTP requests only: the browser isn't proxied
stop_after = 5                  # saved posts in a row that end a sync
```

`--when ""` undoes the default for one run. A wrong value stops flnd with the file, the key and
what it takes; an unknown key is a warning.
[flnd/docs/storage.md](flnd/docs/storage.md#settings-configtoml) lists every key.

## Use it from Python
`flnd.open(home)` gives a `Flnd`, whose methods are the commands. A download returns a run that
yields events as it goes: `Saved`, `Skipped`, `Failed`, `Progress`, and `Planned`
about once a second.

<!-- docs-test: skip: it downloads from Instagram, which needs the network and a login -->
```python
import flnd

with flnd.open("~/Media/flnd") as fl:
    for event in fl.download("ins:example_user", only=["reels"], when="2026"):
        if isinstance(event, flnd.Saved):
            print(event.path, event.date, event.post)     # event.post is the post's id
        elif isinstance(event, flnd.Failed):
            print("failed:", event.error, event.error.hint)

    report = fl.sync().wait()                             # runs it to the end: the counts
    print(report.saved, report.skipped, report.ok)
    photos = fl.files(target="ins:example_user", kind="image")   # what's saved, from flnd.db
```
[flnd/docs/library.md](flnd/docs/library.md) covers every call, its options and its events, with examples
that run offline.

## Add a site
A source is a small Python class: it says which targets and links it reads and how to list a
target's posts, and flnd does the rest (downloading, resuming, dating, deduping, retrying,
progress).

```python
from flnd.source import File, Post, Site, Source, cookies, target


class Example(Source):
    """example.com: a user's posts."""
    code = "exm"                     # targets look like exm:example_user
    domains = ("example.com",)       # links, cookies and pacing
    api = 1                          # the source API this was written for
    login = cookies("session")       # logged in when this cookie is there

    @target("exm:{user}", "example.com/{user}")
    def profile(self, site: Site, user: str) -> dict:
        """A user's profile: `exm:example_user`, or `https://example.com/example_user`."""
        return site.fetch(f"https://api.example.com/users/{user}").json

    @profile.category(newest_first=True)
    def posts(self, site: Site, profile: dict):
        url = f"https://api.example.com/users/{profile['id']}/posts"
        for page in site.pages(url, cursor="next"):
            for p in page["items"]:
                yield Post(p["id"], [File(p["url"])], date=p["time"])
```

`flnd.testing` runs a source against fixtures, with no network and no home:

```python
from flnd.testing import check, collect

API = "https://api.example.com"
saved = collect(Example, "exm:example_user", json={
    f"{API}/users/example_user": {"id": "42"},
    f"{API}/users/42/posts": {
        "items": [{"id": "p1", "time": "2026-01-02", "url": "https://cdn.example.com/p1.jpg"}],
        "next": None,
    },
})
assert [(file.path, str(file.date)) for file in saved] == [("posts/p1_0.jpg", "2026-01-02")]
assert [str(found) for found in check(Example)] == ["exm:example_user", "exm:example_user"]
```

To make flnd know it, either:

- save it as `exm.py` in your plugin folder: `$FLND_SOURCES`, else `~/.config/flnd/sources/`
  (`%APPDATA%\flnd\sources\` on Windows);
- publish it as a package with an entry point in the `flnd.sources` group, named by its code;
- or, in a script, call `fl.add(Example)` on a `Flnd`.

`flnd sources` then lists it. A plugin runs with your logins, so install only code you trust;
flnd never loads one from a home folder. A broken plugin disables itself alone, with a warning
naming its file and line. [flnd/docs/writing-a-source.md](flnd/docs/writing-a-source.md) walks through it
all.

## Documentation
[flnd/docs/](flnd/docs/README.md) is the guide: the CLI, the library, writing a source, errors, storage,
performance, a glossary and a page per site.
[flnd/docs/architecture.md](flnd/docs/architecture.md) explains how flnd is built, how it compares with
similar tools, and the decisions behind it.

## Development
| make | what it runs |
| --- | --- |
| `make setup` | `uv sync --all-extras`: the environment from `uv.lock`, with the dev tools |
| `make check` | lint (ruff), then every test; green before every commit. `PY=3.11` runs it on the oldest Python flnd supports |
| `make test ARGS=…` | the tests, or some of them (`ARGS="-k fetch"`) |
| `make lint` | ruff alone |
| `make api` | regenerates `tests/data/api.txt`, the public API snapshot |
| `make manifest` | regenerates `flnd/sources/_manifest.py`, what `--help` and target resolution read; run it after changing a source's domains, routes or categories |
| `make bench` | the offline benchmarks; `ARGS=--gate` compares them with the targets |
| `make cdn-real ARGS=…` | a hand check: throughput from one real CDN host at several caps |
| `make docs` | builds the API reference into `build/docs/` |
| `make try ARGS=…` | lists, live, what one target would save, in a scratch home `.try`, downloading nothing |
| `make dist` | a single-file `flnd` binary for this OS (PyInstaller) |
| `make clean` | removes `build/`, `dist/` and `.try` |

Each target is one `uv` command, listed in the `Makefile`, so it can also be typed by hand on a
machine without make. Contributors and coding agents: read [AGENTS.md](AGENTS.md) first.
