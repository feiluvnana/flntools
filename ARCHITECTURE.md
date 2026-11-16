# flnd's architecture

This is the map of flnd as it is being rebuilt: what the parts are, how a download flows through them, and how to write a source. It is meant to be read top to bottom in about twenty minutes.

[PLAN.md](PLAN.md) holds the rest: the measurements behind each choice, the dependencies, and the order of the work. Where the two differ, this file describes the design, and PLAN.md is updated to match.

For how the code on this branch works today, read [docs/architecture.md](docs/architecture.md). That page stays after the rebuild; this file is deleted.

## 1. Goals
- **Simple.** Few ideas, each with one name and one way to use it. A newcomer should be able to say what every module does after reading this page.
- **Feature-rich.** The engine does the hard parts once: following, syncing, resuming, retrying, pacing, deduping, compressing, unpacking, progress and history. Every source gets all of it for free.
- **A source is easy to write.** A source only answers one question: *what posts does this target have?* Ten lines are enough for a simple site.
- **Fast where it matters.** flnd's time goes to waiting on networks and browsers, not to Python. So the design overlaps the waits and never sleeps without a reason.

## 2. What we took from similar tools
flnd was compared with four tools that solve the same problem: gallery-dl and yt-dlp (many sites each), instaloader (Instagram) and twscrape (Twitter/X). Their code was read at their latest commits, in November 2026.

| question | gallery-dl | yt-dlp | instaloader, twscrape | flnd |
| --- | --- | --- | --- | --- |
| How is a site written? | An `Extractor` class per kind of URL (an Instagram user, a post, the reels…), each with a `pattern` regex and an `example` URL | An `InfoExtractor` per site, with `_VALID_URL` and `_TESTS` | One package for one site | **One `Source` class per site.** Each kind of URL is a decorated method (a *route*), so a site stays in one class |
| What does a site produce? | Message tuples: `Directory`, `Url`, and `Queue` (hand a URL to another extractor) | Info dicts with standard fields | Python objects (`Post`, `Profile`, `Tweet`) | **Typed objects:** `Post`, `File`, and `Target` to hand a link to another source |
| The parts of a profile | A "user" extractor dispatches to sub-extractors, chosen with the `include` option | — | — | **Categories** under a route, chosen with `--only` |
| Stopping at old posts | `skip = "abort:N"` stops after N files in a row already saved | `--break-on-existing` | `--fast-update` | **The engine** stops after N saved in a row on a sync (5, configurable per source) |
| What was downloaded | A separate SQLite archive of keys | A text file of IDs | A timestamps file | **`flnd.db`**: files, posts, targets and runs, with real columns |
| After a file lands | Post-processor modules on events (metadata, exec, mtime, zip, hash…) | A post-processor chain (ffmpeg merge, metadata, exec) | — | **A fixed step chain**: dedupe, unpack, compress, date, sidecar, exec |
| Loading only what is used | Imports extractor modules in order until a pattern matches | A generated `lazy_extractors.py` holds every pattern | — | **A generated manifest** of routes, like yt-dlp's |
| Plugins | Extra module folders in the config | A `yt_dlp_plugins` package in the user's config folder, or from pip | — | **The user's plugin folder, or an entry point** from pip, like yt-dlp |
| HTTP | `requests`, with browser-like headers and ciphers | Pluggable handlers; curl_cffi for `--impersonate` | httpx; twscrape is async with a pool of accounts | **curl_cffi, async**, impersonating Chrome |
| Rate limits | `sleep-request`, `sleep-429`, retries | Retries | instaloader tracks Instagram's limits in sliding windows per query type | **A token bucket per source**, `Retry-After`, and halving on a 429 |
| Concurrency | One file at a time; some extractors list on a second thread (`AsynchronousMixin`, a queue of 5) | Fragments of one video | twscrape: async, rotating accounts as each hits its limit | **Listing and downloading overlap**, with adaptive concurrency per host |
| Errors | `StopExtraction` and friends | `ExtractorError(expected=True)` prints no traceback | — | **One hierarchy**; a user error prints no traceback |
| Sites that need a browser | Cookies only, no browser | No browser | No browser | **Playwright**, only for the sites and pages that need it |
| Tests | Live result files per extractor | Live `_TESTS` per extractor | — | **Offline fixtures** (`flnd.testing`), and live runs by hand |

What flnd adopts:
- yt-dlp's **generated manifest**, so `flnd --help` imports no site.
- yt-dlp's **plugin locations**: the user's own folder and pip, never the current folder.
- gallery-dl's **`Queue`**, as `Target`: a post that links to another site hands the link on, and the right source downloads it.
- gallery-dl's **job types**, as consumers of one event stream. Download, `--dry` and `--json` read the same listing and differ only in what they do with it.
- gallery-dl's **skip-and-abort** idea, owned by the engine, so no source repeats it.
- yt-dlp's **"expected" errors**: an error the user can fix prints one line, never a traceback.
- instaloader's lesson that **some sites limit by window, not by rate.** The pacer can take windows later, for the source that needs them.

Where flnd differs on purpose:
- **One class per site, not one per URL kind.** gallery-dl's Instagram is 17 extractor classes; flnd's is one class with routes and categories. A site's shared state (its API client, its user cache) lives in one place.
- **History is a database with columns**, not a list of keys, because `sync`, `ls`, `check` and `status` all ask questions of it.
- **Concurrency is built in.** gallery-dl downloads one file at a time, and yt-dlp one video at a time (in parallel fragments). flnd follows whole profiles, where overlapping the list and the downloads is most of the speed.
- **A browser when a site demands it.** Some of flnd's sites only show their data to a logged-in page, so a source can scroll a real tab and capture the site's own API responses.

Why flnd doesn't reuse gallery-dl's or yt-dlp's site code:
- **gallery-dl is GPL-2.0**, so importing its extractors would bind flnd's license. Its extractors are also synchronous and built on `requests`, while flnd's engine is one async loop.
- **yt-dlp is public domain and could be reused**, but it is about one video per URL, while flnd follows whole accounts with categories, stories and logins.
- The cost is real: four of flnd's sites (Instagram, X, TikTok, Facebook) are maintained by large communities in those tools, and by one person here. Section 12 lists an optional yt-dlp fallback as a decision.

## 3. The big picture
```
             you
              │
   ┌──────────┴───────────┐
   │  flnd/cli.py         │  argparse; turns flags into library calls,
   │  (the command line)  │  and events into progress lines
   └──────────┬───────────┘
              │ imports only `flnd`
   ┌──────────┴───────────┐
   │  flnd                │  Flnd, Run, events, errors: the public library
   │  (the library)       │
   └──────────┬───────────┘
              │
   ┌──────────┴─────────────────────────────────────────────────┐
   │  the engine (flnd/_engine.py, _job.py, _download.py)       │
   │  resolves targets, runs the listings, downloads, runs the  │
   │  steps after each file, records history, emits events      │
   └───┬──────────────┬──────────────┬──────────────┬───────────┘
       │ calls        │ uses         │ uses         │ uses
┌──────┴──────┐ ┌─────┴──────┐ ┌─────┴──────┐ ┌─────┴──────────┐
│ sources     │ │ the Site   │ │ flnd.db    │ │ the steps      │
│ (one class  │ │ fetch,     │ │ history,   │ │ unpack,        │
│ per site,   │ │ pages,     │ │ targets,   │ │ compress,      │
│ plugins)    │ │ browser    │ │ runs       │ │ sidecars, exec │
└─────────────┘ └─────┬──────┘ └────────────┘ └────────────────┘
                      │
          ┌───────────┴────────────┐
          │ curl_cffi  ·  Playwright │  on one asyncio loop
          └────────────────────────┘
```

There are three public modules, and everything else is private (its name starts with `_`):

| module | for whom | what it holds |
| --- | --- | --- |
| `flnd` | scripts and the CLI | `Flnd`, `Run`, the events, the errors, `Record`, `Target`, `Login` |
| `flnd.source` | source authors | `Source`, `target`, `Site`, `Post`, `File`, `Target`, `cookies`, `Login`, `dig` |
| `flnd.testing` | source authors' tests | `FakeSite`, `collect`, `check`, a fake clock |

The private modules, by job:

| job | modules |
| --- | --- |
| running a download | `_engine` (a run), `_job` (one target), `_download` (the file queue) |
| reaching sites | `_net` (curl_cffi: pace, retry, fingerprint), `_browser` (Playwright), `_cookies` (reading browsers' cookies), `_vault` (encrypted logins) |
| files and history | `_files` (parts, renames, walks), `_db` (`flnd.db`) |
| after a file lands | `_unpack` (archives), `_shrink` (compression), `_media` (ffmpeg probes and runs), `_tools` (finding ffmpeg and 7zz) |
| plumbing | `_loop` (the asyncio thread), `_plugins` (finding sources), `_html` (selectolax), `_ui` (progress), `_text`, `_time` |
| sites | `flnd/sources/<code>.py`, one per site, and `_manifest.py` |

The CLI imports only `flnd`, so anything the command line can do, a script can do too. A source imports only `flnd.source` and the standard library, so a source can't reach into the engine.

## 4. Words
| word | meaning | example |
| --- | --- | --- |
| **source** | the code for one site | `ins` (Instagram) |
| **target** | something to download, written `<source>:<what>`, or a link | `ins:example_user`, `https://www.instagram.com/example_user/` |
| **route** | a method of a source that handles one kind of target | `profile` handles `ins:{user}` |
| **category** | one part of a target, chosen with `--only` | `posts`, `reels`, `stories` |
| **post** | one item a site lists, with a date and one or more files | a post with three photos |
| **file** | one thing to download in a post | a photo's URL |
| **record** | a saved file's row in `flnd.db` | its path, size, date and hash |
| **run** | one `download`, `follow`, `sync` or `retry` | |
| **event** | what a run reports as it goes | `Saved`, `Skipped`, `Failed`, `Progress` |
| **home** | the folder flnd keeps everything in | `./.flnd` |

## 5. One download, start to finish
`flnd download ins:example_user --only reels`:

1. **The CLI** parses the flags and calls `fl.download("ins:example_user", only=["reels"])`. It has imported nothing heavy yet.
2. **Resolving the target.** The manifest maps `ins:` to `flnd/sources/ins.py`, so only that module is imported. Its route `ins:{user}` matches, with `user="example_user"`. `--only reels` is checked against the route's categories before anything is fetched.
3. **Listing.** The route runs on a listing thread. It returns the profile, and then the `reels` category yields posts, one at a time, as the site's pages arrive.
4. **Planning each post.** The engine checks each post as it arrives:
   - a post already in `flnd.db` is skipped;
   - a post outside `--when` is skipped;
   - after 5 saved posts in a row on a sync, or 5 too old for `--when`, the listing stops early;
   - the rest become file jobs in the download queue.
5. **Downloading.** The download queue runs on the asyncio loop. Each file streams into `<name>.part`, which is also its lock, while SHA-256 is computed on the way. Up to 32 files download at once per host, adapting to how the host answers. When the queue is full, the listing waits, so memory stays flat.
6. **The steps** run on each finished file, in order: dedupe, unpack, compress, set the date, rename into place, record, sidecar, exec (section 8).
7. **Recording.** When a post's last file lands, its rows commit in one transaction.
8. **Events.** Each file produces an event. The CLI draws progress from them; a script iterates them; `--json` prints them.
9. **The end.** The run's report is printed: counts, sizes and failures, with the command that retries them.

## 6. Writing a source
A source answers one question: what posts does this target have? flnd does everything else.

### 6.1 The smallest source
```python
from flnd.source import Source, Post, File, target


class Example(Source):
    """example.com: a user's photos."""

    code = "exm"                    # targets start with exm:
    domains = ("example.com",)      # links from these domains belong to this source
    api = 1                         # the source API this was written for

    @target("exm:{user}", "example.com/{user}", newest_first=True)
    def photos(self, site, user):
        """A user's photos, newest first."""
        for page in site.pages(f"https://api.example.com/users/{user}/photos", cursor="next"):
            for item in page["items"]:
                yield Post(item["id"], [File(item["url"])], date=item["time"])
```
That is a whole source. With it:
- `flnd download exm:example_user` and `flnd download https://example.com/example_user` both work;
- files land in `<home>/media/exm/example_user/`, named after the post;
- a second run skips what it already has. Because the route says `newest_first=True`, a sync also stops early once it reaches old posts. Without it, a sync reads the whole listing: slower, but never wrong, so flnd never guesses the order;
- requests are paced (0.5 s apart by default), retried, and resumed after Ctrl-C;
- `--when`, `--limit`, `--dry`, `--compress`, `--json` and `follow` all work, without a line of source code.

### 6.2 A target with categories
A route that **returns** a value, instead of yielding posts, has categories. Each category receives what the route returned.
```python
    @target("exm:{user}", "example.com/{user}")
    def profile(self, site, user):
        """A profile: what every category below receives."""
        return site.fetch(f"https://api.example.com/users/{user}").json

    @profile.category
    def posts(self, site, profile):
        """The profile's posts."""
        ...

    @profile.category
    def stories(self, site, profile):
        """The profile's stories, while they last."""
        ...
```
`flnd download exm:example_user` takes every category. `--only stories` takes one. `flnd sources` lists each source's categories.

### 6.3 Posts and files
```python
Post(id, files, date=None, **fields)
File(url, name=None, audio=None, password=None)
```
- **`files`** is a list, or a function that returns one. A function runs only if the post is new, so a source never fetches the pages of a post flnd already has.
- **`date`** takes a `datetime`, Unix seconds or an ISO 8601 string. The engine parses it once, and sets each file's modified time from it.
- **`fields`** are anything else worth keeping: `caption`, `author`, `title`, `tags`. They feed the folder and file names, the sidecars and search.
- **`File(url, audio=…)`** downloads a video and its separate audio track at once, then joins them with ffmpeg without re-encoding.
- **`File(url, password=…)`** marks an archive, which is unpacked after download.

### 6.4 Where files go
```python
    folder = "{target}/{category}"     # the default
    filename = "{post}_{num}"          # the default; the extension comes from the file
```
A template can use `{source}`, `{target}`, `{category}`, `{post}`, `{num}`, `{date}` and any field of the post. A template that names an unknown field fails when the class is made, not halfway through a download.

### 6.5 Logging in
```python
from flnd.source import cookies

    login = cookies("sessionid")       # logged in when this cookie is there
```
`flnd login exm chrome` then reads the cookie from Chrome, and stores it encrypted. A site with its own login flow (Telegram's QR code) assigns a `Login` subclass instead. Either way, `login` is the one attribute that says how a source logs in.

### 6.6 What `Site` offers
| call | what it does |
| --- | --- |
| `site.fetch(url, params=…)` | one request, paced, retried and sent with Chrome's fingerprint; `.json`, `.text`, `.html` |
| `site.pages(url, cursor=…, send=…)` | a paged listing; flnd remembers the cursor and resumes an interrupted download from it |
| `site.open(url)`, `site.scroll()`, `site.feed(match)` | the browser: open a page, scroll it like a person, and capture the site's own API responses while scrolling |
| `site.document()`, `site.evaluate(js)`, `site.click(css)`, `site.wait(css=…)` | read and drive the open page |
| `site.html(text)`, `dig(data, "a.b[].c")` | parse HTML; walk JSON |
| `site.state` | a small key-value store per source, kept between runs |
| `site.warn(msg)`, `site.pause(seconds, reason)` | tell the user something; wait with a reason shown |

A source that needs the browser just calls these. flnd starts one browser per run, the first time a source asks, and shares it.

### 6.7 Searches, and links to other sites
A route can yield targets instead of posts. Each target is downloaded as if it had been typed:
```python
    @target("exm?{words}")
    def search(self, site, words):
        """A search: each user it finds is downloaded."""
        for page in site.pages("https://api.example.com/search", params={"q": words}, cursor="next"):
            for user in page["users"]:
                yield Target(f"exm:{user['name']}")
```
A target may belong to another source: `yield Target("https://www.tiktok.com/@example_user")` hands the link to TikTok's source. This is gallery-dl's `Queue`, and it is how a post that links elsewhere gets followed.

### 6.8 What a source never does
A source never touches the disk, the history, retries, concurrency or progress. If it needs one of those, that is a missing feature in `Site`, and it is added there once for every source.

The engine owns:
- the early stops, and spotting a login wall or a block (a `Site` call raises `LoginError` or `Blocked`);
- `--when`, `--only`, `--limit` and `--dry`;
- dedupe, file names, dates, history, and resuming;
- pacing, retries, the browser's lifetime and concurrency.

### 6.9 Mistakes fail early
When the class is made, flnd checks it: the `code`, the patterns, the templates, duplicate routes, a category on a route that yields posts. A mistake raises `SourceError` with the attribute and the fix:
```
exm.folder: unknown field {usr}; fields are source, target, category, post, num, date, user
```

### 6.10 Sync or async
A route or category may be `def` or `async def`. Plain `def` runs on its own listing thread, which is the easy choice. `async def` runs on the loop, which suits a site with an async client, such as Telegram's. An async route may also use `site.tab`, the raw Playwright page. That is outside the source API's version promise, because Playwright's API isn't flnd's to keep stable.

### 6.11 Testing a source
```python
from flnd.testing import FakeSite, collect

site = FakeSite(json={"https://api.example.com/users/example_user/photos": PAGE})
saved = collect(Example, "exm:example_user", site)
assert saved[0].path == "exm/example_user/123_1.jpg"
```
Tests never touch the network. `check(Example)` validates the class and resolves every example in its route docstrings. Live runs against real sites are done by hand, on test accounts.

### 6.12 Shipping it
- **A file:** save it as `exm.py` in `~/.config/flnd/sources/` (or `$FLND_SOURCES`). The file name is the source's code.
- **A package:** declare `[project.entry-points."flnd.sources"] exm = "flnd_example:Example"` and publish it.
- **In a script:** `fl.add(Example)`.

flnd never loads code from a home folder, since a home is often the folder you happen to be in. A plugin runs with your logins, so install only plugins you trust. A broken plugin disables only itself, with a warning naming the file and line.

## 7. The engine: threads, the loop and queues
```
 listing threads (one per source)      the asyncio loop thread            worker pool
 ───────────────────────────────       ──────────────────────────         ─────────────
 route → category → Post ──────▶ plan (seen? --when? stop?)
                                        │ file jobs (bounded queue)
                                        ▼
                                 download: curl_cffi, per-host limits ──▶ ffmpeg, Pillow
                                        │ finished files                  (compress, unpack)
                                        ▼                                       │
                                 steps ◀────────────────────────────────────────┘
                                        │
                                        ▼
                                 flnd.db (one transaction per post) ──▶ events ──▶ CLI / script
```
- **One asyncio loop** runs every network call, the browser and Telegram. Nothing waits by blocking a thread.
- **Each source's listing has its own thread**, so a source is plain, top-to-bottom code. Its calls to `Site` cross to the loop (about 40 µs each, small against a network round trip).
- **Bounded queues** connect the stages. When downloads fall behind, the listing waits, so memory stays flat on a 100,000-post account.
- **Heavy work leaves Python's thread:** ffmpeg runs as a subprocess, and Pillow's encoders run in C on a small pool.
- **Pacing** is per source: a token bucket for the site's own pages, set by `pace`, with `Retry-After` honoured and the rate halved after a 429. CDNs get no pace, only adaptive concurrency per host.

## 8. After a file lands: the steps
Every finished file goes through the same fixed chain. Each step is on or off by config or flag:

| step | when | what it does |
| --- | --- | --- |
| dedupe | `[defaults] dedupe = "link"` | the same bytes already saved elsewhere become a hard link |
| unpack | an archive | extracts beside the archive, refusing escaping paths, links and bombs |
| compress | `--compress` | a smaller file that still looks the same, checked before it replaces the original |
| date | always | the post's date becomes the file's modified time |
| place | always | the part is renamed into place, atomically |
| record | always | the row is written, in the post's transaction |
| sidecar | `[defaults] sidecars = true` | the post's fields as `<file>.json` |
| exec | `--exec CMD` | runs a command with the file's path, never through a shell |

This is gallery-dl's post-processor idea with one fixed order, so steps can't be combined in an order that breaks. It isn't a plugin API yet: the steps are flnd's own until a real need for a custom one appears.

## 9. The library, the CLI and events
```python
import flnd

with flnd.open("~/Media/flnd") as fl:
    for event in fl.download("ins:example_user", only=["reels"]):
        if isinstance(event, flnd.Saved):
            print(event.path)
```
- **A `Run`** is what `download`, `follow`, `sync` and `retry` return. It starts when iterated or waited on (`run.wait()` returns the report), and async code can `async for` it.
- **Events are the only output.** The CLI's progress, `--json`, and a script's loop are three readers of the same stream, the way gallery-dl's job types read one extractor. There are no callbacks.
- **Live stats are events too.** Once a second a listened-to run yields a `Stats` snapshot (browser tabs and RAM, flnd's RAM and CPU, downloads in flight and per-host caps, waits and their reasons, free disk), and `run.stats` holds the latest. The full-screen UI, the default on a terminal, draws from those snapshots and the other events, so a script sees everything the screen shows.
- **The CLI is thin.** Each command turns flags into one library call, and events into lines on screen:

| command | what it does |
| --- | --- |
| `download <targets…>` | take what's there now, once |
| `follow <targets…>` / `unfollow` | start or stop following; `follow` also downloads |
| `sync [targets…]` | what's new for followed targets |
| `ls` · `status` · `retry` · `check` | what you follow; the last run; retry its failures; history against disk |
| `login [source] [how]` · `logout` · `sources` | logins, and the sources with their categories |

## 10. Storage
```
<home>/
  flnd.db       files, posts, targets, cursors, runs, failures
  secrets.db    logins, encrypted (the key is secret.key beside it)
  config.toml   defaults and per-source settings
  media/        <source>/<target>/<category>/…
  logs/         each run's log and crash reports, kept 30 days
  cache/        ffmpeg probes and other things safe to delete
```
- **`flnd.db`** is SQLite in WAL mode, with real columns and indexes, so `sync`, `ls`, `check` and `status` are queries, not file walks.
- **A file is visible only once whole**: it is written as a `.part` and renamed. Its row is written after the rename. A crash in between leaves a whole file with no row, which the next sync adopts.
- **Two runs can share a home**, because each part is locked by the run writing it.

## 11. Errors and recovery
All errors are `flnd.Error`: `UsageError`, `LoginError`, `SiteError` (with `Blocked`, `NotFound`, `Changed`), `NetworkError`, `DiskError`, `ToolError` and `SourceError`. Each has a one-line message and a `hint` saying what to do.
- **Retried by flnd:** timeouts, resets, 5xx, and rate limits (after the site's wait).
- **Recorded, not retried:** deleted or private posts, which never get asked for again.
- **Stops one source, not the run:** a login that expired.
- **Stops the run cleanly:** a full disk.
- **Ctrl-C** works in three stages (finish what's in flight; stop now, keeping parts; quit). Every stage can be resumed.
- **A user error never prints a traceback**, and a crash writes one to a file and says where.

## 12. What's not in flnd, and why
- **Slideshows, cards and albums.** Removed on 2026-11-16, at the user's request. flnd downloads, keeps and compresses; making things from the files is a different tool's job.
- **Reusing gallery-dl's or yt-dlp's site code** (section 2). One option to decide: an optional `flnd-ytdlp` plugin that hands single video links no source knows to yt-dlp. It would be outside the core, so flnd's dependencies and license stay as they are.
- **Process isolation for plugins.** A plugin needs a logged-in `Site`, so a separate process wouldn't protect the logins. The safeguard is where plugins load from.
- **A plugin API for steps**, a full-screen UI, and free-threaded Python come later, if ever. PLAN.md §12 ranks them.

## 13. Decisions to confirm
PLAN.md §16 lists every open decision with a recommended answer. Questions 16 to 18 are this file's: one class per site, a fixed chain of steps, and the yt-dlp fallback.
