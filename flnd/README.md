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
# From the repository root: puts `flnd` on your PATH, Telegram support included.
uv tool install -e ./flnd
```

From a checkout, `make setup` creates `.venv`, and then `uv run flnd …` runs it. On Windows,
install make with `winget install ezwinports.make`, or type the `uv` command each Makefile
target runs (see [Development](#development)).

Outside programs, each needed only for some files:

- **Google Chrome**, for the sites that open a browser. When it is missing and flnd runs in a
  terminal, it asks once whether to download Chromium for itself (about 200 MB, kept in
  Playwright's cache). Declined, or off a terminal (cron, `--json`), the run stops with
  "Google Chrome is needed" and a link to install it.
- **ffmpeg**, for videos: joining a video with its separate sound track.
- **7-Zip** (`7zz`, or the older `7z`), for 7z, RAR and encrypted zip archives. zip and tar are
  read by Python.

flnd downloads neither of the last two: it looks for each on your PATH. A missing one is a
`ToolError` whose hint says what to install, and it costs only the files that needed it: a video
that needs joining fails (and is retried on later runs), an archive is kept but not extracted.

## Quick start
```bash
flnd auth login ins chrome                   # use your Chrome login for Instagram
flnd get ins:example_user                    # its own content: posts, reels, stories, …
flnd get ins:example_user/reels --when 2026
flnd get                                     # what's new for every target you have followed
flnd target list                             # what this home knows: files, size, last run
flnd status                                  # the last run, its failures, accounts, the disk
```

## Commands
```text
flnd get [targets…] [--when WHEN] [--limit N] [--dry] [--again] [--exec CMD]
flnd target list                       the targets this home knows: followed or not, files, size
flnd target clean <targets…> [--media] forget targets; --media also deletes their files
flnd auth list                         every account: origin, default, status
flnd auth login <plugin> [method] [path]  log an account in
flnd auth logout <plugin> [account]    log an account out
flnd plugin list                       every plugin: built in or installed, and why one is disabled
flnd plugin install <file|folder>      add a plugin; plugin uninstall <code> removes it
flnd extension install                 connect Chrome (extension uninstall removes it)
flnd status                            the last run, its failures, accounts that need care, the disk
flnd config                            every setting: its value, default and where it comes from

global flags: --home DIR · -v/--verbose · --json · -V/--version
```

`flnd --help` lists every command and every form of target each site reads, and
`flnd <command> --help` a command's flags. [src/docs/cli.md](src/docs/cli.md) covers each in full.

- **A target** is `<plugin>:<name, id or link>`, `<plugin>?<words>` for a search, or just the
  link: `ins:example_user` and `https://www.instagram.com/example_user/` are the same target,
  and `mht?romance` is a search. Lines on standard input are added to the targets, one
  per line; with none at all, `flnd get` syncs every followed target.
- **Categories** go in the target, after its last `/`: `ins:example_user/reels,posts`, or
  `/all` for every one (`flnd get --help` lists each site's). Without them, a target flnd
  knows takes the ones it was followed with, and a new one the site's own content. A search
  passes them on to each target it finds (`mht?romance/cover`). A link takes the defaults, and
  a single post has no categories, so naming one for it is a usage error.
- **`--when`** takes `2026`, `2026-06`, `2026-06-15`, a range `2026-01..2026-03` (either end may
  be left out), or a span back from today: `30d`, `8w`, `1y`.
- **`--limit N`** takes at most N posts per target, or N targets from a search.
- **`--dry`** lists what would be saved (each file `new` or already `saved`), and
  saves and writes nothing.
- **`--again`** lists every post again, past the posts already saved, and asks again for files
  the site said were gone (404 or 410).
- **`--exec CMD`** runs a command for each saved file once it is final, such as
  `--exec 'notify-send {path}'`; `{target}` and `{post}` work too. It never goes through a
  shell, so a file name from a site can't inject a command.
- **`--json`** prints one JSON object per line: every event, then the run's report; the
  lists and `status` print their rows the same way. It never asks a
  question, which suits scripts and cron.
- **Exit codes:** 0 everything worked, 1 some files or targets failed, 2 a usage error, 3 a login
  is needed, 4 flnd stopped on an error (a full disk, a missing browser, a broken plugin, a bug),
  130 stopped with Ctrl-C.

### Following and syncing
`flnd get` takes what's there, and remembers a target (unless it is a single post) with its
categories. `flnd get` alone then fetches what's new for every followed target. `flnd target
clean` forgets a target; its files stay unless `--media` is given. A get asks no questions, and
its exit code says how it went, so it suits cron, launchd or Task Scheduler:

```bash
15 3 * * * /path/to/flnd --home /path/to/media get
```

A sync is smart about what it has:

- **Nothing new costs little.** A category that lists newest first remembers its newest post,
  and a sync stops as soon as it gets there: a category with nothing new reads one page, which
  is one request on a site that pages by cursor.
- **An interrupted sync leaves no gap.** A sync cut short (Ctrl-C, an error, `--limit`) keeps
  where it stopped. The next sync takes the new posts on top, then jumps to that point and goes
  on from there, so nothing between them is missed.
- **Failed files come back by themselves.** Every get of a target first retries its files that
  failed on earlier runs, each up to 3 times, listing the post again for fresh links. After
  that, `r` in `flnd target list` makes them retryable again. A file the site said is gone (404
  or 410) is left alone; `get --again` asks for it again.

### When something fails
- **Network errors and rate limits** are retried with backoff, and a wait a site asks for
  (`Retry-After`) is kept. A file that still fails is listed at the end of the run:
  ```text
  ✗ 3 files failed: 2 network (retry: flnd get), 1 gone. Details: flnd status
  ```
  `flnd status` shows each failure with its message, how many runs failed it and what to do.
- **Logins:** a login that is missing or expired stops that site only, with the command that
  fixes it. A site whose login is optional downloads what's public, and says so.
- **Mistakes** you can fix (usage, login, disk, a tool) print `✗ …`, then `hint  …` with what
  to do, never a traceback. A bug prints `✗ flnd hit a bug: …`, and writes its traceback to
  `<home>/log/crash-<time>.txt`, which its hint names (`-v` prints it).
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
  hard link to the saved one, so it takes no more space. `dedupe = false` under `[defaults]` in
  the home's `config.toml` keeps both copies.
- **Sidecars:** with `sidecars = true` under `[defaults]`, each saved file gets `<file>.json`
  beside it, with its post's date, link, caption, author and tags.

### Checking a home
`flnd status` compares the history in `flnd.db` with the files on disk, and says which are
missing, unknown or of the wrong size. It fetches nothing, and exits 1 when they disagree. A
repair makes the history match the disk; it never deletes, moves or downloads a file.

## Logging in
`flnd auth login <plugin> <method>` reads your login for that site from a browser (`chrome`, `firefox`,
`edge`, `brave`, `chromium`, or `safari` on macOS), or from a `cookies.txt` or JSON cookie file
you exported. Reading Safari needs Full Disk Access for your terminal (System Settings › Privacy
& Security › Full Disk Access); without it, flnd says so and how to allow it. It keeps only the
site's cookies, and refuses a login that lacks the site's login cookie. Telegram logs in its own
way: `browser` (Telegram Web's session), `qr` or `phone`. Left out in a terminal, the method is asked.

```bash
flnd auth login ins chrome
flnd auth login twt file ~/Downloads/cookies.txt
flnd auth login tlg qr
flnd auth list                               # each account, and what expires soon
flnd auth logout ins
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
[src/docs/storage.md](src/docs/storage.md) describes every file and table.

### Settings
`<home>/config.toml` holds defaults for every run and settings per site. A flag given for one run
wins over it, and it wins over flnd's default:

```toml
[defaults]
when = "1y"                     # as if every download, follow and sync had --when 1y

[plugins.ins]
pace = 1.0                      # seconds between page requests
folder = "{category}"           # where its files go, over its own template
limits = { "cdninstagram.com" = 8 }   # files at once from that host
proxy = "socks5://127.0.0.1:1080"     # HTTP requests only: the browser isn't proxied
stop_after = 5                  # saved posts in a row that end a sync
```

`--when ""` undoes the default for one run. A wrong value stops flnd with the file, the key and
what it takes; an unknown key is a warning.
[src/docs/storage.md](src/docs/storage.md#settings-configtoml) lists every key.

## Use it from Python
`flnd.open(home)` gives a `Flnd`, whose methods and groups are the commands. `get` returns a run that
yields events as it goes: `Saved`, `Skipped`, `Failed`, and `Planned` for a dry run.

<!-- docs-test: skip: it downloads from Instagram, which needs the network and a login -->
```python
import flnd

with flnd.open("~/Media/flnd") as fl:
    for event in fl.get("ins:example_user/reels", when="2026"):

        if isinstance(event, flnd.Saved):
            print(event.path, event.date, event.post)     # event.post is the post's id
        elif isinstance(event, flnd.Failed):
            print("failed:", event.error, event.error.hint)

    report = fl.get().wait()                              # runs it to the end: the counts
    print(report.saved, report.skipped, report.ok)
    photos = fl.files(target="ins:example_user", kind="image")   # what's saved, from flnd.db
```
[src/docs/library.md](src/docs/library.md) covers every call, its options and its events, with examples
that run offline.

## Add a site
A plugin is a small Python class: it says which targets and links it reads and how to list a
target's posts, and flnd does the rest (downloading, resuming, dating, deduping, retrying,
progress).

```python
from flnd.plugin import Ctx, File, Plugin, Post, cookies, route

API = "https://api.example.com"


class Example(Plugin):
    """example.com: a user's posts."""
    code = "exm"                     # targets look like exm:example_user
    name = "Example"                 # the site's name, shown in lists and help
    domains = ("example.com",)       # links, cookies and pacing
    api = 2                          # the plugin API this was written for
    login = cookies("session", url="https://www.example.com/login")   # logged in with it

    @route("exm:{user}", "example.com/{user}")
    async def user(self, ctx: Ctx, user: str) -> dict:
        """A user's profile: `exm:example_user`, or `https://example.com/example_user`."""
        return (await ctx.fetch(f"{API}/users/{user}")).json

    @user.category
    async def posts(self, ctx: Ctx, profile: dict):
        """The profile's posts, newest first, page by page."""
        async for page in ctx.pages(f"{API}/users/{profile['id']}/posts", cursor="next"):
            for item in page.json["items"]:
                yield Post(item["id"], [File(url) for url in item["urls"]], date=item["time"])
```

`flnd.testing` runs a plugin against fixtures, with no network and no home:

```python
from flnd.testing import FakeCtx, Reply, collect

ctx = FakeCtx(Example, routes={
    f"{API}/users/example_user": Reply({"id": "42"}),
    f"{API}/users/42/posts": Reply({"items": [
        {"id": "p1", "time": "2026-01-02", "urls": ["https://cdn.example.com/p1.jpg"]}]}),
})
saved = collect(Example, "exm:example_user", ctx=ctx)
assert [str(file.date) for file in saved] == ["2026-01-02"]
```

To make flnd know it, either:

- save it as `exm.py` in your plugin folder: `$FLND_PLUGINS`, else `~/.config/flnd/plugins/`
  (`%APPDATA%\flnd\plugins\` on Windows);
- publish it as a package with an entry point in the `flnd.sources` group, named by its code;
- or, in a script, call `fl.add(Example)` on a `Flnd`.

`flnd plugin list` then lists it. A plugin runs with your logins, so install only code you trust;
flnd never loads one from a home folder. A broken plugin disables itself alone, with a warning
naming its file and line. [src/docs/writing-a-plugin.md](src/docs/writing-a-plugin.md) walks
through it all.

## Documentation
[src/docs/](src/docs/README.md) is the guide: the CLI, the library, writing a plugin, errors,
storage, performance, a glossary and a page per built-in plugin.
[src/docs/architecture.md](src/docs/architecture.md) explains how flnd is built, how it compares with
similar tools, and the decisions behind it.

## Development
| make | what it runs |
| --- | --- |
| `make setup` | `uv sync --all-extras`: the environment from `uv.lock`, with the dev tools |
| `make check` | lint (ruff), then every test; green before every commit. `PY=3.11` runs it on the oldest Python flnd supports |
| `make test ARGS=…` | the tests, or some of them (`ARGS="-k fetch"`) |
| `make lint` | ruff alone |
| `make api` | regenerates `tests/data/api.txt`, the public API snapshot |
| `make manifest` | regenerates `src/plugins/_manifest.py`, what `--help` and target resolution read; run it after changing a built-in plugin's domains, routes or categories |
| `make bench` | the offline benchmarks; `ARGS=--gate` compares them with the targets |
| `make cdn-real ARGS=…` | a hand check: throughput from one real CDN host at several caps |
| `make docs` | builds the API reference into `build/docs/` |
| `make try ARGS=…` | lists, live, what one target would save, in a scratch home `.try`, downloading nothing |
| `make dist` | a single-file `flnd` binary for this OS (PyInstaller) |
| `make clean` | removes `build/`, `dist/` and `.try` |

Each target is one `uv` command, listed in the `Makefile`, so it can also be typed by hand on a
machine without make. Contributors and coding agents: read [AGENTS.md](AGENTS.md) first.
