# How flnd fits together

This page is a map of flnd's code as it is today: its parts, what each one does, and the path
one download takes from the command line to the disk. Read it before changing the engine, or
when a module's docstring mentions a part you haven't met yet. The words it uses are defined in
the [glossary](glossary.md).

flnd is being rebuilt as one package, in small steps. Each step that changes a part described
here also changes this page, so the page always describes the code you have checked out.

## The parts

```
                 you
                  │
   flnd/cli.py ───┤  the command line (argparse): flags in, progress lines out
   flnd/__init__  │  the library: flnd.open, Flnd, Run, events (names load lazily)
   flnd/_library  │  Flnd's methods; flnd/_run.py: Run; flnd/_events.py: events, Report
                  ▼
   flnd/engine.py    Engine: resolves targets, checks options, runs each target in turn
        │
        ├── flnd/_home.py       the home: flnd.db (files, targets, runs, failures), the logins
        ├── flnd/options.py     Options: what one target downloads; RunShared: what a run shares
        ├── flnd/site.py        SourceRun and TargetSite: what a source acts through
        │       └── flnd/common/  net (HTTP and downloads), paths (what a file is)
        └── flnd/job.py         one target: list its posts, plan its files, download, record
                │
                └── flnd/sources/<code>.py   one class per site (flnd/source.py is their API)

   flnd/_*.py   the private toolkit the rest is built on: http, browser, db, files, loop, ui,
             cookies, vault, unpack, html, prompt, cli, text, time and a few more
```

| part | what it holds | who calls it |
| --- | --- | --- |
| `flnd/cli.py` | the commands: `download`, `follow`, `sync`, `ls`, `login`, `logout` and the rest | the `flnd` script |
| `flnd/__init__.py` | the public names, each imported from its module on first use | scripts |
| `flnd/_library.py` | `open` and `Flnd`: each command as a method, over the engine | scripts |
| `flnd/_run.py` | `Run`: a command on its own thread, read as a stream of events | `Flnd` |
| `flnd/_events.py` | the events (`Saved`, `Skipped`, `Failed`, `Progress`, `Planned`), `Report`, `Login`, `Record`, `Tracked` | the engine, the job, `Run` |
| `flnd/engine.py` | `Engine`: every command as a method, and target resolution | `Flnd` |
| `flnd/job.py` | one target's run: listing, planning, the download pool, history rows | the engine |
| `flnd/site.py` | `SourceRun` (a source's cookies, clients and browser for a run) and `TargetSite` (the `Site` a route sees) | the engine, the job |
| `flnd/source.py` | the source API: `Source`, `@target`, `Post`, `File`, `Site`, `Target`, errors | every source |
| `flnd/sources/` | the seven built-in sources and their shared helpers (`sources/common/`) | the engine, through `SOURCES` |
| `flnd/sources/_manifest.py` | each built-in source as plain data (code, domains, forms, categories), made by `make manifest` | `--help`, `SOURCES` (which imports a source only when used), link resolution |
| `flnd/_home.py` | the home: its layout, `flnd.db` (files, posts, targets, runs, failures, sources' state), the migration of an old home, and the logins in the vault | the engine, the job, the site, `Flnd` |
| `flnd/options.py` | `Options` and `RunShared`, and the checks of `--only` and folder templates before any work | the engine, the job |
| `flnd/common/` | `net` (the HTTP client and file downloads), `paths` (content sniffing) | the engine, the site, the job |
| `flnd/_*.py` | the private toolkit: HTTP over httpx (`_http`), the run's browser on async Playwright (`_browser`), a SQLite store (`_db`), sealed secrets (`_vault`), atomic files (`_files`), the run's asyncio loop and a thread pool (`_loop`), the terminal UI (`_ui`) | everything above |
| `flnd/_steps.py`, `_unpack.py`, `_shrink.py`, `_media.py`, `_tools.py` | the steps after a file lands ([media.md](media.md)): the chain and its lanes, archive extraction, `--compress`, probes and ffmpeg runs, and finding ffmpeg and 7zz | the job and the engine |

The rule between them: a source imports `flnd.source`, its helpers in `flnd/sources/common/`,
the toolkit (`flnd._*`) and the standard library, and nothing of the engine. The engine imports the sources
only through `flnd.sources.SOURCES`.

## One download, start to finish

`flnd follow ins:example_user --only posts`:

1. **The CLI** (`flnd/cli.py`) parses the flags with argparse and calls `fl.follow(...)` of
   the library (`flnd.open`), which runs the engine with the target and `--only posts`.
2. **Resolving.** `engine.resolve` splits the text at `:` (or `?` for a query), finds the source
   by its code (or a bare link by its `domains`), and calls `Source.resolve`. The first route
   whose pattern matches gives `Target("ins", "user", "example_user")`. A short link no route
   reads is followed once with a HEAD request, and the link it leads to is resolved instead.
3. **Checking.** Before any request, `options.categories_by_source` and `options.validate` check
   `--only` against the target's route, and every folder template against its fields. A mistake
   is a usage error (exit code 2).
4. **Recording the target.** `Home.record` writes the target's row in `flnd.db`, followed when
   `follow` asked, keeping its cursors and found targets from earlier runs.
5. **Grouping by source.** The engine runs the targets of one source together, one after
   another, inside one `source_run`: the source's saved cookies are read from the vault, its
   HTTP client is made (paced to `Source.pace` on its own domains), and its `connect` hook runs
   (Telegram's client). A source that `requires_login` and has none fails here, for each of its
   targets; any other source without a login warns and goes on with public access.
6. **One target** (`job.download`). The job removes the stale parts an interrupted run left in
   the target's folder (a week old, and held by no run), and queues the files that failed last
   time, so they are tried first. Nothing locks the target: each file's part locks itself.
7. **Listing.** The route runs and returns the profile; then each wanted category yields its
   posts, page by page. A category whose listing was interrupted resumes from its saved cursor,
   then catches up from the newest post. `--when` drops posts outside the range; after 5 posts
   in a row older than the range, a `newest_first` category stops.
8. **Planning each post** (`Ledger.take`). Each file is given its path (from the source's
   `folder` and `filename` templates) and its history key (`<code>:<key>`). A file whose key is
   in the history is skipped. On a sync, 5 saved posts in a row end a `newest_first` category.
   A lazy post's files are listed only once the post is known to be new.
9. **Downloading** (`Downloads`). Each new file goes to a `flnd._loop.Pool` of
   `Source.concurrency` threads (8 by default). The pool's queue is bounded, so the listing waits
   when downloads fall behind. `common/net.download` opens and locks the file's part
   (`<name>.part`, see `docs/storage`), skipping a file another run is saving, then streams the
   file into it, resuming what a failed run kept. It tries mirrors in order, merges a DASH video
   with its audio through ffmpeg, checks the content (a web page instead of a photo is a
   failure, not a file) and gives it the extension its content calls for. The part gets its
   post's date (at noon UTC) as its modified time and is renamed onto the real name.
10. **Finishing a file.** Once every file of its post is in place, the post's rows are written
    to `flnd.db` in one statement: path, size and modified time (from the part, so nothing stats
    the file again), post id, category and date. The same file in two categories is saved once
    and linked from the second (a relative symlink, or a hard link where symlinks aren't
    allowed). The file is then handed to the run's step chain (`flnd._steps`), which extracts an
    archive and, with `--compress`, compresses a photo or video on threads of its own
    ([media.md](media.md)).
11. **Ending the target.** The job saves the categories it listed to their end and the cursors
    of those it didn't in `flnd.db` (each failed file was written there as it failed), counts the
    target's files for `flnd ls`, then prints one summary line.
12. **The end.** The engine writes refreshed cookies back to the vault, prints "Complete" with the
    count of files, and the CLI exits with 0, or 1 if a target failed.

`flnd sync` runs steps 5 to 12 for every target whose row has `sync` on, always stopping at
saved posts. A query target (`mht?words`) first lists the targets it finds, then runs each one;
on a sync, only the targets it hadn't seen before.

## Threads

A run uses a few threads, and every rule below protects one of them:

- **The main thread** runs the engine and each target's listing: the route, the categories and
  the planning. A source's plain `def` routes therefore run top to bottom, without locks.
- **The download pool** (`flnd._loop.Pool`, per target) runs the file downloads. Each pool
  thread writes its file's part, dates it, renames it and writes its history row; the job's
  `_writing` lock guards the run's own lists of written and failed files, and SQLite serializes
  the history writes.
- **The run's loop** (`flnd._loop.Loop`, made by `site.shared_browser`) is one asyncio event loop
  on a thread of its own. Async Playwright runs on it: one browser per run
  (`flnd._browser.RunBrowser`), launched the first time a target needs a tab and shared by every
  source, so one Chrome window serves them all. Each target gets its own tab, and up to six in
  all with the warm tabs that pre-load the pages it will open next (`flnd._browser.Tabs`). A
  source never holds a Playwright object: each browser verb of `Site` (`open`, `wait`, `scroll`,
  `feed`, `document`, `evaluate`, `click`) is one call handed to the loop (`Loop.run`, about
  40 µs), which waits for the answer. A response feed reads the matching bodies on the loop as
  they arrive, in order, and the listing thread parses them.
- **Async sources** run on the same loop through `SourceRun.run_async`: a source with an async
  SDK (Telegram's Telethon), and any `async def` route or category, which reaches the browser by
  awaiting `site.tab` instead of calling the verbs.
- **The step chain's lanes** (`flnd._steps`): one thread for archives, two for photos and one
  for videos, fed by bounded queues once a file's history row is written. They update that row
  when they change the file; the engine closes the chain, waiting for them, before it closes
  history.
- **Ctrl-C** is handled by `flnd._run.stopping`: the first one sets `stop.soon` (stop listing,
  let running files finish), the second sets `stop.now` (stop at once, keep the parts).

## The browser

`flnd._browser` is a library of its own: it knows Playwright, tabs and scrolling, and nothing of
sources. A source reads a site that only shows its data to a logged-in page by watching the
page's own API calls: `site.feed(match, scroll=True, cursor=reader)` keeps the JSON of every
response whose URL `match` takes, and scrolls the tab only when no body is waiting.

**A scroll step is driven by what the page does**, never by a fixed sleep:

1. **Scroll.** With `scroll = "human"` (the default), the tab glides down 1.2 to 1.8 screens in
   three to six uneven wheel steps, and now and then (7%) first scrolls back up a little and
   hesitates, as a person going back to look at something. With `"fast"`, it jumps to the
   bottom in one go.
2. **Watch.** The step watches for progress: the next matching response (the capture wakes it
   at once) or the page growing (checked every 50 ms). At the very bottom with nothing yet, it
   waits up to 3 s. With `human` it also pauses, counted from the scroll: a lognormal time with
   a median of 0.4 s when new media just came, 0.25 s when not (a glance), never more than four
   medians.
3. **Go on, or end.** Progress, or room left to scroll, means another step. The feed ends as
   soon as the site says so: the source's cursor reader read a page with no next one, or the
   same cursor came back twice more (the site answering one page again). Only a feed without
   that word ends on silence: a whole 3 s wait with nothing, then 2 s more.

**At most two tabs of one login scroll at once with `human`** (`Page.gate`, one semaphore per
login in the run's browser), since a person doesn't scroll many tabs of one account at the same
moment. `fast` has no such limit.

**Where the style comes from.** A source sets `scroll = "human"` or `"fast"` on its class;
`TargetSite` hands it to the target's `Tabs`, which every feed and `site.scroll` of that target
use. The config file's `[source.<code>] scroll` will override it at that same spot.

A target's tab scrolls by script (`window.scrollBy` with a wheel event per step), not with real
mouse input: the tabs of one shared window are background tabs to Chrome, which gives them no
mouse events. An element that scrolls on its own (`scroll(within=…)`) gets real wheel events.

## Where things are kept

Everything lives in the home folder, `.flnd` in the current folder:
`media/<code>/<target>/<category>/…` for the files, `flnd.db` for the history, the targets,
the runs and each source's state, and `secrets.db` with `secret.key` for the login vault. [storage.md](storage.md) lists every file and
what its rows hold.

## Errors

Every error is a `flnd.Error` from one hierarchy (`flnd/errors.py`): `UsageError`,
`LoginError`, `SiteError` (with `Blocked`, `NotFound` and `Changed`), `NetworkError`,
`DiskError`, `ToolError` and `SourceError`. The HTTP client classifies what it meets, and a
source raises the same classes from `flnd.source`. The job turns a failure of one file into a
failed file, and a failure of a listing into a failed target, so one bad target never stops the
others; a refused login stops its source, and a full disk stops the run.
[errors.md](errors.md) lists every error class and what to do about it.

## Where to go next

- [cli.md](cli.md): every command and flag.
- [library.md](library.md): flnd from a Python script.
- [writing-a-source.md](writing-a-source.md): adding a site.
- [performance.md](performance.md): what is fast, what isn't, and how it is measured.
- [sources/](README.md#sites): what each site's source does.
