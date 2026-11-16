# How flnd fits together

This page is a map of flnd's code as it is today: its layers, what each part does, and the path
one download takes from the command line to the disk. Read it before changing the engine, or
when a module's docstring mentions a part you haven't met yet. The words it uses are defined in
the [glossary](glossary.md).

A change that moves a part described here changes this page in the same commit, so the page
always describes the code you have checked out. Every module also starts with a docstring that
says what it holds, who calls it, what it calls, its main flow and the rules a change must keep:
this page is the map, and those are the detail. The last two sections say why flnd is built
this way: [how it compares with similar tools](#compared-with-similar-tools), and
[the decisions](#decisions) behind what it does.

## The layers

flnd's modules sit in four layers. Each layer imports only from the layers below it, and
`tests/test_layers.py` checks the rules by reading every file's imports.

```
                 you
                  │
   ┌──────────────┴───────────────────────────────────────────────────────────────┐
   │ 1. the command line      flnd/cli.py                                         │
   │    argparse: flags in, one library call, events out as lines or a screen     │
   └──────────────┬───────────────────────────────────────────────────────────────┘
                  │ imports only `flnd` (plus the manifest for --help, and _prompt, _ui)
   ┌──────────────┴───────────────────────────────────────────────────────────────┐
   │ 2. the public API        flnd (Flnd, Run, events, errors), flnd.source,      │
   │                          flnd.testing                                        │
   └──────────────┬───────────────────────────────────────────────────────────────┘
                  │
   ┌──────────────┴───────────────────────────────────────────────────────────────┐
   │ 3. the engine side       engine, job, site, options, _home, _transfer,       │
   │                          _steps, _run, _events, _stats, _check, _plugins,    │
   │                          _screen, sources/                                   │
   └──────────────┬───────────────────────────────────────────────────────────────┘
                  │
   ┌──────────────┴───────────────────────────────────────────────────────────────┐
   │ 4. the library modules   _net, _browser, _loop, _files, _unpack, _shrink,    │
   │                          _media, _tools, _cookies, _vault, _db, _html,       │
   │                          _text, _time, _prompt, _ui   (and flnd/errors.py)   │
   └──────────────────────────────────────────────────────────────────────────────┘
```

The rules between them:
- **The CLI imports only the library** (`flnd`), the sources' manifest for `--help`, and the
  prompt and UI modules. Every decision is the library's, so a script can do anything the
  command line does.
- **A source imports only `flnd.source`** and the standard library (and, lazily, an SDK its
  site needs). The engine and the private modules can change without breaking one.
- **A library module** does one general job (HTTP, the browser, files, archives, outside tools,
  cookies, the vault, a SQLite store, HTML, text, time, prompts, progress, media) and imports
  from flnd only other library modules and `flnd/errors.py`. It knows nothing of sources,
  targets or `flnd.db`'s schema: flnd's own code passes those in. So each one reads on its own,
  and could leave flnd as a package of its own later.
- **`flnd/errors.py`** is a leaf: it imports only the standard library, so every layer raises
  its classes and there is one hierarchy.
- **Nothing heavy loads at import.** `import flnd` imports only `flnd/__init__.py`; each public
  name loads its own module on first use, and curl_cffi, Playwright and Rich load only when a
  command needs them, so `flnd --help` stays under 40 ms (`tests/test_packaging.py`).

## The parts

```
   flnd/cli.py        the command line: flags in, progress lines (or the full screen) out
   flnd/__init__.py   the library's names, each loaded on first use
   flnd/_library.py   flnd.open and Flnd: each command as a method
   flnd/_run.py       Run: one command on its own thread, read as a stream of events
        │
        ▼
   flnd/engine.py     Engine: resolves targets, checks options, runs each source's targets
        │
        ├── flnd/options.py     Options (what one target downloads), RunShared (what a run shares)
        ├── flnd/_home.py       the home: flnd.db (files, targets, runs, failures), the logins
        ├── flnd/site.py        SourceRun and TargetSite: what a source acts through
        │       └── flnd/_transfer.py   one file into its part, checked by its content
        ├── flnd/_steps.py      the step chain after each file: dedupe, unpack, compress, sidecar
        └── flnd/job.py         one target: list its posts, plan its files, download, record
                │
                └── flnd/sources/<code>.py   one class per site (flnd/source.py is their API),
                    and the plugins: <code>.py files in the plugin folder, or installed packages
```

| part | what it holds | who calls it |
| --- | --- | --- |
| `flnd/cli.py` | the commands: `download`, `follow`, `sync`, `retry`, `ls`, `status`, `check`, `login`, `logout` and the rest; the views of a run (`--ui full`, `inline`, `plain`, `--json`); `--exec`; the exit codes | the `flnd` script, `python -m flnd` |
| `flnd/__init__.py` | the public names, each imported from its module on first use | scripts |
| `flnd/_library.py` | `open` and `Flnd`: each command as a method, over the engine; the reads (`targets`, `files`, `logins`, `status`), as rows | scripts and the CLI |
| `flnd/_run.py` | `Run`: a command on its own worker thread, read as a stream of events; `Stop` and `stopping`, the Ctrl-C state | `Flnd`, the engine |
| `flnd/_events.py` | the events (`Saved`, `Skipped`, `Failed`, `Progress`, `Planned`, `Stats`), the public `Report`, and the rows the reads return (`Login`, `Record`, `Tracked`, `Status`, `Failure`, `Checked`, `Installed`) | the engine, the job, `Run`, `Flnd` |
| `flnd/engine.py` | `Engine`: every command as a method; target resolution (`resolve`, `owner`); searches and the targets listings link to | `Flnd` |
| `flnd/job.py` | one target's run: the listing, the early stops and the listing's head, resuming cursors and gaps, retrying failures, planning (`Ledger`), the downloads on the loop (`Downloads`), the progress rows (`Board`), history rows | the engine |
| `flnd/site.py` | `SourceRun` (a source's cookies, HTTP session, clients and browser for a run), `TargetSite` (the `Site` a route sees: requests, `pages`, the browser verbs, wall detection, `back_off`), `LoginSite`, and `shared_browser`, the run's loop and browser | the engine, the job |
| `flnd/options.py` | `Options` and `RunShared`, the check of `--only` and the templates before any work, and `config`: every setting of config.toml, read and checked in one place | the engine, the job, the library, `site`, `_steps` |
| `flnd/source.py` | source API 1: `Source` and its checks, `@target`, `Post`, `File`, `Target`, `Site`, `Login` and `cookies`, `Request`, `dig`, errors | every source |
| `flnd/sources/` | the seven built-in sources, one module each, and `SOURCES`, the registry of every source, plugins included, each imported on first use | the engine and the library |
| `flnd/sources/_manifest.py` | each built-in source as plain data (code, domains, forms, categories), made by `make manifest` | `--help`, `SOURCES` (which imports a source only when used), link resolution |
| `flnd/_plugins.py` | finding plugins (installed packages' `flnd.sources` entry points, then the user's plugin folder), importing one when its code is first used, and disabling one that breaks, with its file and line ([writing-a-source.md](writing-a-source.md#10-shipping-it-as-a-plugin)) | `SOURCES` |
| `flnd/testing.py` | the public test kit: `FakeSite`, `collect`, `check`, `fake_clock` | a source's tests, flnd's own (`tests/fakes.py`) |
| `flnd/_home.py` | the home: its layout, `flnd.db` (files, posts, targets, runs, failures, sources' state), the migration of an old home, and the logins in the vault | the engine, the job, the site, `Flnd` |
| `flnd/_transfer.py` | one post's file into its part: mirrors, DASH merges, a source's `fetch` function, the content check | the job |
| `flnd/_steps.py` | the step chain and its lanes: dedupe, unpack, compress, the sidecar, `Saved` ([media.md](media.md)) | the job and the engine |
| `flnd/_stats.py` | live stats: what a run's parts track (limits, browser, boards, the chain, transfers, the home), sampled into a `flnd.Stats` once a second while the run is read, with psutil for RAM and CPU | `Run` |
| `flnd/_screen.py` | the full-screen view (`--ui full`): Rich on the alternate screen, drawn from the events and `Stats`, with `q` and `p` read by a key thread | the CLI |
| `flnd/_check.py` | `check`: the history against the disk, and the repair ([storage.md](storage.md#checking-a-home)) | `Flnd.check` |
| `flnd/errors.py` | the one error hierarchy, `Stopped`, and `classify` and `disk`, which turn any exception into one of them | every module |

The library modules, each a toolkit of its own:

| module | what it offers |
| --- | --- |
| `_net` | HTTP over curl_cffi on an asyncio loop: sessions, two clients, pacing and adaptive caps per host, retries, resumable downloads into a part, `follow`, and a fake for tests |
| `_browser` | the run's one browser on async Playwright: tabs, warm tabs, response feeds, human-like scrolling |
| `_loop` | one asyncio loop on a thread of its own, which other threads hand work to (`run`, `submit`) |
| `_files` | atomic files: parts that lock themselves, hashing as bytes are written, renames, sweeps, kinds by name and by content |
| `_unpack`, `_shrink`, `_media`, `_tools` | archive extraction, compression of photos and videos, probes and ffmpeg runs, and finding ffmpeg and 7zz |
| `_cookies`, `_vault` | reading browsers' cookie stores and cookie files; sealed secrets on disk |
| `_db` | a SQLite store: one connection behind a lock, transactions, waiting for another process's writer |
| `_html`, `_text`, `_time` | HTML parsing, names and sizes and redaction, a clock tests can fake |
| `_prompt`, `_ui` | questions on the terminal; log lines, progress rows and the live view |

## Plugins

A plugin is a source that isn't built in. `SOURCES` knows the built-ins from the manifest, and
looks for plugins the first time a code that isn't built in is needed: first the `flnd.sources`
entry points of installed packages, then `<code>.py` files and `<code>/` packages in the plugin
folder (`$FLND_SOURCES`, else `~/.config/flnd/sources/`, `%APPDATA%\flnd\sources\` on
Windows). Finding them reads names only, since a plugin's code is its entry point's or its
file's name; `exm:example_user` then imports `exm` alone. A link imports the plugins only when no
built-in's domains match it, `flnd sources` imports them all, and `--help` none. A code that is
taken already is refused, never replaced.

A plugin runs in flnd's process. A broken one (an import error, a class that fails its checks,
a newer source API) is disabled alone, with a warning naming its file and line. An error inside
its listing fails that target alone, like a built-in's, with a hint naming the plugin. Its async
code runs on the run's loop, so a stretch that runs 10 s without awaiting is warned about
(`flnd._loop.held`). flnd never loads code from a home: a home is often the folder you are in,
and code there may not be yours.

## One download, start to finish

`flnd follow ins:example_user --only posts`:

1. **The CLI** (`flnd/cli.py`) parses the flags with argparse, opens the home
   (`flnd.open`), and calls `fl.follow("ins:example_user", only=["posts"])`.
2. **Resolving.** The library resolves the target at once (`engine.resolve`): it splits the
   text at `:` (or `?` for a search), finds the source by its code (or a bare link by its
   `domains`), and calls `Source.resolve`. The first route whose pattern matches gives
   `Target("ins:example_user", kind="user")`. A short link no route reads is followed once
   with a HEAD request, and the link it leads to is resolved instead.
3. **Checking.** Before any request, `options.categories_by_source` and `options.validate` check
   `--only` against the target's route, and every folder template against its fields. `--when`
   and `--limit` are read too. A mistake is a `UsageError` (exit code 2).
4. **A run that hasn't started.** `follow` returns a `flnd.Run`. Nothing happens until the CLI
   reads it: the first read starts the run's worker thread, which makes an `Engine` with the
   run's event sink and stop state, and calls `Engine.download(..., sync=True, keep=True)`.
   From here to step 12 everything runs on that worker thread (the *listing thread*), except
   the downloads, which are tasks on the run's loop (see "Threads and queues").
5. **Recording.** The engine opens the command's row in `runs` (each failure names it), then
   `Home.record` writes the target's row in `flnd.db`, followed, with `--only posts` kept for
   later syncs, keeping its cursors, heads and gaps from earlier runs.
6. **Grouping by source.** The engine runs the targets of one source together, one after
   another, inside one `source_run`: the source's saved cookies are read from the vault, its
   HTTP session is made (see "Network"), and its `connect` hook runs (Telegram's client). A
   source whose `login` is `required` and missing fails here, for each of its targets; any
   other source without a login warns and goes on with public access. The run's loop and its
   one browser (`site.shared_browser`) are shared by every source.
7. **One target** (`job.download`). The job removes the stale parts an interrupted run left in
   the target's folder (a week old, and held by no run). Nothing locks the target: each file's
   part locks itself.
8. **Listing.** The route runs and returns the profile. The files earlier runs failed on go
   first: each failed post is listed again from the cursor of its page, and its failed files
   queued (see "Smart sync"). Then each wanted category yields its posts, page by page. A
   category whose full download was interrupted resumes from its saved cursor, then catches up
   from the newest post. `--when` drops posts outside the range; after 5 posts in a row older
   than the range, a `newest_first` category stops.
9. **Planning each post** (`Ledger.take`). Each file is given its path (from the source's
   `folder` and `filename` templates) and its history key (`<code>:<key>`). A file whose key is
   in the history, or whose file is on disk, is skipped (a `Skipped` event). A file saved in
   another category's folder is linked instead. A post with no day waits until its category
   ends, then takes the day of its nearest dated neighbour (marked estimated). Once a
   `newest_first` category was read to its end, a later run stops it after 5 saved posts in a
   row (`stop_after` in config.toml), or sooner, after the head (the newest post the last full
   listing saw, kept in `flnd.db`) and the post after it. The source never stops a listing
   itself: the job closes its generator. A lazy post's files are listed only once the post is
   known to be new.
10. **Downloading** (`Downloads`). Each new file is submitted to the run's loop as a task. The
    target's queue holds 4 times its places, so the listing waits when downloads fall behind
    and memory stays flat; a task waits for one of the target's places (32, as many as one
    host's cap grows to, or the source's largest `limits` above that), then for a place under
    its host's cap. `_transfer.save` opens and locks the file's part (`<name>.part`, see
    [storage.md](storage.md)), skipping a file another run is saving, then streams the file into
    it, resuming what a failed run kept. It tries mirrors in order, merges a DASH video with
    its audio through ffmpeg, checks the content (a web page instead of a photo is a failure,
    not a file) and gives it the extension its content calls for, hashing the bytes (SHA-256)
    as they are written. The part gets its post's date (at noon UTC) as its modified time and
    is renamed onto the real name. A file the site or the network fails fails alone: a
    `Failed` event, and a row in `failures` for the next run to retry.
11. **Finishing a file.** As each file lands, dedupe replaces a file whose bytes are already
    saved by a hard link to them. Once every file of its post settled, the post's rows are
    written to `flnd.db` in one transaction: path, size and modified time (from the part, so
    nothing stats the file again), hash, post id, category and date. Each file then goes down
    the step chain (below), whose last step sends `Saved` with the file's final path.
12. **Ending the target.** The job waits for its downloads, makes the links it held back, and
    saves the categories it listed to their end, the cursors of those a download cut short,
    the gaps a sync left and the heads in `flnd.db` (each failed file was written there as it
    failed). It counts the target's files for `flnd ls`, then ends the target's rows. A failed
    listing fails the target alone (a `Failed` event); a refused login fails the source's
    remaining targets; a full disk stops the run.
13. **Links to other sites.** Targets a listing yielded (a post linking to another site) are
    downloaded after the run's own targets, each by its own source, up to two links deep.
14. **The end.** The engine writes refreshed cookies back to the vault and closes the `runs`
    row with the count of saved files. The run's thread ends; the CLI prints a summary from the
    run's `Report` and exits with 0, 1 if a file or a target failed, 3 if a login is needed, 4
    if flnd itself stopped (a full disk, a missing tool, a broken source), 130 on Ctrl-C
    ([cli.md](cli.md)).

`flnd sync` runs steps 6 to 14 for every target whose row has `sync` on, always stopping at
saved posts. A search (`mht?words`) first lists the targets it finds, then downloads each one;
on a sync, only those it hadn't seen before, at most 25 a run. `flnd retry` runs every target
with failed files, listing only the failed posts' pages. `--dry` plans the same listing and
sends a `Planned` event per file instead of downloading, and writes nothing.

## Smart sync

A run remembers enough, per target and category, in `flnd.db` (`targets.heads`, `gaps`,
`cursors`, and the `failures` table), to do as little as it can and still miss nothing. Each of
the three is in the events and in the run's report.

1. **Failures first.** Every download and sync of a target first retries the files that failed
   on earlier runs (`job._retry_failed`), before its new posts. It works from `(target, post,
   file)`, never from a stored URL: it groups the failures by category and by the cursor of the
   page each post was on, lists from that cursor until it has met each failed post (or
   `RETRY_WINDOW` posts past it), and queues the post like any other, which downloads only its
   files that aren't saved. A file is retried this way on at most 3 runs, then left for `flnd
   retry`; a gone file never. A post the listing no longer has is recorded gone. Each file
   brought back is a `Saved` event with `retried=True` (`report.retried`).
2. **Nothing new.** A `newest_first` listing keeps its head: the newest post a run that read it
   from the top saw. A sync stops after the head and the post after it, so a target with
   nothing new costs one page, which on a site that pages by cursor is one request. It says so
   with a `Skipped` event whose `reason` is `"nothing new"` (`report.unchanged`).
3. **No gap.** A full download cut short keeps the cursor of the page it was reading, and the
   next run resumes there, then lists from the newest down to the posts it had. A sync cut short
   (Ctrl-C, an error, `--limit`) keeps a gap instead (`job._sync_one`): the next sync starts
   from the newest page, since that is where new posts are, and once it meets saved posts on
   top, jumps to the gap's cursor and goes on from there until it meets saved posts again: a
   `Progress` event with the note `"resuming from the last run"` (`report.resumed`). Gaps stack,
   newest first, when syncs are cut short twice. A feed that isn't paged by cursor leaves a gap
   without one, and the next sync reads down past saved posts to the head instead. The head
   moves only when a sync leaves no gap, so every gap lies above it.

## Threads and queues

A run uses a few threads, joined by bounded queues, and every rule below protects one of them:

- **The run's worker thread** (`flnd._run.Run`) runs the engine and each target's listing: the
  route, the categories and the planning. Targets run one after another on it, so a source's
  plain `def` routes run top to bottom, without locks. Outside a library run (a test calling
  the engine), the caller's thread plays this part.
- **The reader** is whoever iterates the run: the CLI's main thread, or a script. It takes
  events off the run's queue (256 events): when the queue is full, the worker waits, so a slow
  reader slows the listing rather than piling events up in memory. `run.wait()` stops queueing
  (events are still counted into the report) and waits for the worker.
- **The run's loop** (`flnd._loop.Loop`, made by `site.shared_browser`) is one asyncio event loop
  on a thread of its own. Everything that waits on the network or the browser runs there:
  - **every HTTP transfer**: the listing's requests (`Site.fetch` hands each one over and
    waits) and every file's download;
  - **the downloads**: one task per file, handed over from the listing thread
    (`Loop.submit`). Each target's queue holds 4 times its places; `Downloads.submit` waits
    on the listing thread while it is full. A task streams its file into its part, dates it
    and renames it; its post's rows commit when the post's last file settles. The job's
    `_writing` lock guards the target's lists of written and landed files, the board's lock
    its counts, and `Home`'s one connection is shared behind a lock of its own;
  - **async Playwright**: one browser per run (`flnd._browser.RunBrowser`), launched the first
    time a target needs a tab and shared by every source, so one Chrome window serves them all.
    Each target gets its own tab, and up to six in all with the warm tabs that pre-load the
    pages it will open next (`flnd._browser.Tabs`). A source never holds a Playwright object:
    each browser verb of `Site` (`open`, `wait`, `scroll`, `feed`, `document`, `evaluate`,
    `click`) is one call handed to the loop (`Loop.run`, about 40 µs), which waits for the
    answer. A response feed reads the matching bodies on the loop as they arrive, in order,
    and the listing thread parses them;
  - **async sources**, through `SourceRun.run_async`: a source with an async SDK (Telegram's
    Telethon), and any `async def` route or category, which reaches the browser by awaiting
    `site.tab` instead of calling the verbs.

  What would block the loop runs off it: a part's bytes go to one writer thread 4 MiB at a time
  (and to one hasher thread for their SHA-256, beside the write),
  a part's open and rename and dedupe's hard link to worker threads, a source's blocking
  `fetch` function and ffmpeg's DASH merge to worker threads too.
- **A lazy post's expired link.** A source's code runs on the listing thread only, so a
  download task whose link expired queues a refresh for that thread (`job._Refresh`), which
  answers it between two files, and waits for the fresh link.
- **The step chain's lanes** (`flnd._steps`): one thread for archives, two for photos and one
  for videos, each fed by a bounded queue once a file's history row is written. They update
  that row when they change the file, and send the file's `Saved` event when they are done with
  it (a file with no slow step sends it at once, from its download task). Two hard links of one
  file are never compressed at once: a lock per hash makes the second wait, then find it
  already done. The engine closes the chain, waiting for its lanes, before it closes history.
- **Ctrl-C** is handled by `flnd._run.stopping`, on the reader's thread when it is the main
  thread: the first one sets `stop.soon` (stop listing, drop queued files, let running files
  finish), the second sets `stop.now` (stop at once, keep the parts) and warns that a third
  quits, and the third calls `flnd._run.quit_now`: it runs the undo steps registered with
  `flnd._run.restoring` (the full screen's `Screen.restore_now`, which puts back the key
  reader's terminal mode and leaves the alternate screen without a lock or a join), then
  `os._exit(130)`. `run.suspend()` sets
  `stop.suspended`, which each source's limits wait on before a new request; a stop clears it.
- **Live stats** are made on the reader's thread: between two events (or every 0.1 s) the run
  asks its `flnd._stats.Meter` for a sample, at most once a second. The meter copies what the
  loop owns (each source's hosts and waits, the tabs, the files in flight) with one call handed
  to the loop, waiting at most 50 ms for it, so a loop busy handing an event to this same
  reader never deadlocks it; the boards, the chain's lanes and psutil's process numbers are
  read on the reader's thread, so the system calls never hold up a transfer. The parts of a
  run show themselves to the meter (`_stats.tracked`) through the run's context, as the clock
  reaches them. A `Stats` never goes through the event queue.
- **The full screen** (`flnd._screen`) draws on Rich's refresh thread at 10 frames a second
  from what the events stored; a key thread reads `q` and `p` in cbreak mode and puts the
  terminal's mode back when the screen closes, however the run ended.

## The step chain

Every saved file goes through the same steps, in one fixed order:

```
download (hashing) → dedupe → unpack → compress → date → place → record → sidecar → Saved → --exec
```

- **download, date, place, record** run in the file's download task (`flnd.job`): the bytes are
  hashed as they are written, the part is dated and renamed into place, and the post's rows are
  written together.
- **dedupe** runs as the file lands, before its row: one indexed lookup of its hash, and a file
  whose bytes are already saved becomes a hard link to them (`[defaults] dedupe = "link"`).
- **unpack** and **compress** (`--compress`) are slow, so the job hands the recorded file to
  the run's `Chain`, whose lanes run them on threads of their own; they re-run date, place and
  record for a file they change, and compress keeps dedupe's links ([media.md](media.md)).
- **sidecar** (`[defaults] sidecars = true`) writes the post's caption, author, tags and link
  beside the file, and **Saved** is sent last, with the file's final path and size.
- **`--exec`** is the CLI's: it runs a command for each `Saved` event it reads, never through a
  shell ([cli.md](cli.md#running-a-command-per-file)).

Each step is on or off by its flag or setting; the order never changes, so no combination can
compress a file before dedupe linked it. A step's failure is the file's, never the run's: it is
logged, and the file is still finished. `flnd sync --compress` feeds the same lanes from
`flnd.db` with the files saved before (`Chain.backlog`).

## Events

A run's only output is its events (`flnd/_events.py`), each a frozen msgspec struct a script
may keep after the run and `encode` to JSON:

| event | sent when | made by |
| --- | --- | --- |
| `Saved` | a file went through every step: its target, category, post, final path, day, size, and `retried` | the step chain's last step |
| `Skipped` | a file or post was already saved, another run is saving it, or (with `reason="nothing new"`) a sync found nothing new | the job's `Ledger` and `Downloads` |
| `Failed` | a file failed (with its post and path), or a whole target did; `error` is the `flnd.Error`, with its `hint` | the job and the engine |
| `Progress` | a category's counts changed (found, saved, skipped, failed, bytes), its listing ended, or a note ("resuming from the last run") | the job's `Board` |
| `Planned` | a dry run planned a file: `"new"` or `"saved"` | the engine's dry run |
| `Stats` | about once a second while the run is read: hosts, transfers, listings, waits, the machine | the run's meter, on the reader's thread |

`Run` counts the events into a public `flnd.Report` (saved, skipped, bytes, failed, retried,
resumed, unchanged), which `run.report` shows so far and `run.wait()` returns. The engine keeps
its own `engine.Report` of one `TargetReport` per target, for the `runs` row and the totals; a
script never sees it. There are no callbacks and no hooks: a script iterates the run.

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
use. The home's `[sources.<code>] scroll` in config.toml overrides it at that same spot
(`TargetSite._browser`).

A target's tab scrolls by script (`window.scrollBy` with a wheel event per step), not with real
mouse input: the tabs of one shared window are background tabs to Chrome, which gives them no
mouse events. An element that scrolls on its own (`scroll(within=…)`) gets real wheel events.

## Network

One HTTP library (`flnd/_net.py`) does every request and download, on the run's loop:

- **A session per source** (`_net.Session`, made by `site.source_run`): one curl_cffi
  `AsyncSession` with Chrome's TLS and HTTP/2 fingerprint, the login's cookies (kept by the
  session, and written back to the vault at the end), the source's `user_agent`, and its
  `sign` hook for requests to its own domains, run before every try. A `proxy` under
  `[sources.<code>]` in config.toml goes to curl for every transfer of the session; the browser,
  one window for every source, isn't proxied.
- **The home's settings win over the class's**: `source_run` reads the source's
  `[sources.<code>]` table (`options.config`), whose `pace` replaces the source's, whose
  `limits` merge over the source's, and whose `scroll` the target's tabs take.
- **Two pools** (`_net.Limits`). *Pages*: the listing's requests to the source's own domains
  share a token bucket per host at the source's `pace`; a 429's `Retry-After` (or X's
  `x-rate-limit-reset`) holds it, and halves its rate until 20 requests go through. *Media*:
  every file, and the listing's requests to other hosts, under an adaptive cap per host
  (below). A target's downloads also take one of its own places (32, or the source's largest
  cap above that), and its queue holds 4 times as many.
- **Adaptive concurrency per host** (`_net.Cap`, AIMD). Each media host starts at 8 transfers
  at once (`site.MEDIA_CAP`) and may grow to 32 (`site.MEDIA_MOST`), or to the cap the source's
  `limits` gives it:
  - a 429, a 503, a reset, a timeout or a transfer cut short halves it, once per round of
    transfers that started together;
  - every 10 clean transfers close a window, whose throughput is the cap times the bytes
    moved over the seconds the transfers took (Little's law, for a host kept busy). A window
    in which a place stood free with nobody waiting is dropped: the cap didn't bind;
  - otherwise the cap grows by one. A step that didn't raise the throughput by half of what
    one more place would add on a host that scales is taken back, and the cap holds for 100
    clean transfers before it probes again.

  The session's curl handles (`Limits.total`) leave room for 8 hosts at 32, plus every host in
  `limits`; curl makes a handle only when a transfer needs one. `SourceRun.load` (from
  `Limits.load`) is a snapshot of each host's cap, transfers in flight and waiting.
- **Two clients** share the session and the pools: `web` lists and stops at the first Ctrl-C;
  `files` downloads and stops at the second.
- **The retry policy** is the library's ([errors.md](errors.md)): 4 more tries after waits of
  about 1, 2, 4 and 8 s for a timeout, a reset or a 5xx; a 429 waited out, and `Blocked` on the
  third in a row.
- **A file** (`_transfer.save`) goes into its part: resumed with `Range` and `If-Range` from what
  a failed run kept, tried mirror by mirror, a DASH pair fetched at once and joined by ffmpeg.
  Segmented downloads of big files exist (`Client(segments=True)`) but are off until a real-CDN
  benchmark shows they pay.
- **The fake** (`_net.fake`) answers every request from routes or a handler in tests and
  benchmarks; nothing in `make check` reaches the network.

Instagram's API calls fall back to being sent from the page (`site.fetch(in_page=True)`) when
the site refuses flnd's client; whether curl_cffi's fingerprint makes that fallback unneeded is
for a live check on the test account.

## Where things are kept

Everything lives in the home folder, `.flnd` in the current folder unless `--home` or
`$FLND_HOME` names another:

- `media/<code>/<target>/<category>/…`: the files, each written as `<name>.part` first, which
  locks itself, so two runs may share a home and never write one file twice;
- `flnd.db`: what the home remembers (`flnd/_home.py`): `files` (one row per saved file, keyed
  `<code>:<key>`, with its hash), `targets` (followed or not, its options, cursors, heads and
  gaps), `runs`, `failures` (the one record of what failed, which status, the automatic retry
  and `flnd retry` all read), each source's `state`, and how each source logged in;
- `secrets.db` with `secret.key` (or the key in the OS keychain, `[vault] key`): the login
  vault (`flnd._vault`), whose values never reach a message, a log line or `flnd.db`;
- `logs/`: each run's scrubbed samples of answers a source couldn't read, kept a month.

Two rules hold every write together: a post's rows commit in one transaction once all its
files are in place, and a row is written only after its file, so a crash in between leaves a
whole file without a row, which the next run finds on disk and skips. `flnd check` compares
the rows with the disk and repairs toward the disk. [storage.md](storage.md) lists every file
and what its rows hold.

## Errors

Every error is a `flnd.Error` from one hierarchy (`flnd/errors.py`): `UsageError`,
`LoginError`, `SiteError` (with `Blocked`, `NotFound` and `Changed`), `NetworkError`,
`DiskError`, `ToolError` and `SourceError`. `str(e)` is the one-line message and `e.hint` says
what to do. Ctrl-C is `flnd.Stopped`, a `KeyboardInterrupt`, so `except flnd.Error` never
swallows it.

How a failure travels:
1. **Where it starts.** The HTTP client classifies what it meets (a status, a timeout); a
   source raises the same classes from `flnd.source`; anything else a listing lets escape is
   turned into one by `errors.classify` (a bug in a source is a `SourceError`, and its hint
   names a plugin's author).
2. **A file's failure** stays the file's: a `Failed` event with its post and path, a row in
   `failures`, and a retry on the next runs. A 404 or 410 records it gone, never asked again.
3. **A target's failure** (its listing failed) ends that target alone: a `Failed` event, and a
   `failures` row for the whole target. A 401 or 403 from the source's own domains is a
   `LoginError`, which ends the source's later targets too.
4. **A run's failure**: a usage mistake (before anything is fetched) or a full disk stops the
   run, and `Run` raises it to the reader. Nothing else is raised: a script reads
   `report.failed`.
5. **The CLI** turns what ended the run into an exit code and prints the message and hint,
   never a traceback for a `flnd.Error`.

[errors.md](errors.md) lists every error class and what to do about it.

## Compared with similar tools

flnd was designed after reading four tools that solve the same problem, at their latest commits
in November 2026: gallery-dl and yt-dlp (many sites each), instaloader (Instagram) and twscrape
(Twitter/X).

| question | gallery-dl | yt-dlp | instaloader, twscrape | flnd |
| --- | --- | --- | --- | --- |
| How is a site written? | an `Extractor` class per kind of URL, each with a `pattern` regex | an `InfoExtractor` per site, with `_VALID_URL` and `_TESTS` | one package for one site | **one `Source` class per site**; each kind of target is a decorated method, a *route* |
| What does a site produce? | message tuples: `Directory`, `Url`, and `Queue` (hand a URL to another extractor) | info dicts with standard fields | Python objects (`Post`, `Profile`, `Tweet`) | **typed objects**: `Post`, `File`, and `Target` to hand a link to another source |
| The parts of a profile | a "user" extractor dispatching to sub-extractors, picked with `include` | — | — | **categories** under a route, picked with `--only` |
| Stopping at old posts | `skip = "abort:N"` | `--break-on-existing` | `--fast-update` | **the engine** stops a sync after 5 saved posts in a row (`stop_after` per source), and after the newest post it had |
| What was downloaded | a SQLite archive of keys | a text file of ids | a timestamps file | **`flnd.db`**: files, targets, runs and failures, with real columns |
| After a file lands | post-processor modules on events | a post-processor chain (ffmpeg merge, metadata, exec) | — | **a fixed step chain**: dedupe, unpack, compress, date, place, record, sidecar, then `--exec` |
| Loading only what is used | imports extractor modules until a pattern matches | a generated `lazy_extractors.py` | — | **a generated manifest** (`flnd/sources/_manifest.py`), like yt-dlp's |
| Plugins | extra module folders in the config | a `yt_dlp_plugins` package in the user's folder, or from pip | — | **the user's plugin folder, or a `flnd.sources` entry point**, never the current folder |
| HTTP | `requests`, with browser-like headers | pluggable handlers; curl_cffi for `--impersonate` | httpx; twscrape async with a pool of accounts | **curl_cffi on one asyncio loop**, with Chrome's TLS and HTTP/2 fingerprint |
| Rate limits | `sleep-request`, `sleep-429`, retries | retries | instaloader tracks Instagram's windows per query | **a token bucket per source**, `Retry-After` kept, and the rate halved after a 429 |
| Concurrency | one file at a time | the fragments of one video | twscrape: async, rotating accounts | **listing and downloading overlap**, with adaptive concurrency per host |
| Errors | `StopExtraction` and friends | `ExtractorError(expected=True)` prints no traceback | — | **one hierarchy**; a mistake you can fix prints no traceback |
| Sites that need a browser | cookies only | no browser | no browser | **Playwright**, only for the sites and pages that need it |
| Tests | live result files per extractor | live `_TESTS` | — | **offline fixtures** (`flnd.testing`); live runs by hand |

What flnd took from them:
- yt-dlp's **generated manifest**, so `flnd --help` imports no source, and its **plugin
  locations**: the user's own folder and installed packages.
- gallery-dl's **`Queue`**, as `Target`: a search, or a post that links to another site, hands
  the target on, and the source that reads it downloads it.
- gallery-dl's **job types**, as readers of one event stream: the full screen, `--ui plain`,
  `--json`, `--dry` and a script's loop all read the same events.
- gallery-dl's **skip-and-abort**, owned by the engine, so no source repeats it.
- yt-dlp's **expected errors**: an error the user can fix prints one line and a hint.

Where flnd differs on purpose:
- **One class per site, not one per kind of URL.** gallery-dl's Instagram is 17 extractor
  classes; flnd's is one class with routes and categories, so a site's shared state (its API
  client, its caches) lives in one place.
- **History is a database with columns**, not a list of keys, because `sync`, `ls`, `check`,
  `status` and the automatic retry all ask questions of it.
- **Concurrency is built in.** flnd follows whole profiles, where overlapping the listing with
  the downloads is most of the speed.
- **A browser when a site demands it.** Some of flnd's sites only show their data to a
  logged-in page, so a source can scroll a real tab and read the site's own API responses.

Why flnd doesn't reuse their site code:
- **gallery-dl is GPL-2.0**, so importing its extractors would bind flnd's license. They are
  also synchronous and built on `requests`, while flnd's engine is one async loop.
- **yt-dlp is about one video per URL**, while flnd follows whole accounts with categories,
  stories and logins. flnd doesn't hand unknown links to it.
- The cost is real: four of flnd's sites (Instagram, X, TikTok, Facebook) are kept up by large
  communities in those tools, and by one person here. Offline fixtures per source
  (`tests/test_<code>.py`) and the scrubbed samples of answers a source couldn't read
  ([errors.md](errors.md#samples-of-changed-answers)) are how a site change is found and fixed.

What flnd leaves out, and why:
- **Making things from the files** (slideshows, cards, albums): flnd downloads, keeps and
  compresses; the rest is another tool's job.
- **Process isolation for plugins.** A plugin needs a logged-in `Site`, so a separate process
  wouldn't protect the logins. The safeguard is where plugins load from: never a home folder.
- **A plugin API for steps.** The chain after a file lands has one fixed order, so no
  combination can, say, compress a file before dedupe has linked it.

## Decisions

The choices that explain how flnd behaves, made with its user in November 2026:

1. **Flat verbs**: `flnd ls`, `flnd login`, `flnd follow`, not groups such as
   `flnd auth login`. Every verb is unique; `login` with no source lists the logins.
2. **`--only`, and every category without it.** There is no `--all`; a target takes every
   category, other people's content (tagged posts, reposts, likes) included.
3. **Following is a verb.** `flnd follow` opts a target in; a one-off `download` never grows
   every later sync.
4. **Scrolling is human-like by default** (a source's `scroll = "human"`), with `"fast"` for a
   source that can take it.
5. **Concurrency per media host starts at 8 and adapts up to 32** while throughput still rises
   ([performance.md](performance.md#concurrency-per-host)). Segmented downloads of big files
   are built but off, until a real-CDN check shows they pay.
6. **Old homes are migrated on first open**, reading the old files and never changing them
   ([storage.md](storage.md#migrating-an-old-home)).
7. **Dedupe hard-links** a file whose bytes are already saved; a file system without hard links
   keeps both copies ([storage.md](storage.md#dedupe)).
8. **Sidecars are off** by default; `sidecars = true` turns them on.
9. **The home is `$FLND_HOME`, else `./.flnd`**, which suits cron as well as a folder per
   project.
10. **Plugins load from the user's folder or installed packages, never from a home**, since a
    home is often the current folder, and code there may not be the user's own.
11. **The vault's key is a file** beside it (`secret.key`), so an unattended sync can open it;
    `[vault] key = "keychain"` keeps it in the OS keychain instead.
12. **`--compress` encodes videos to AV1**, with `[compress] video = "hevc"` in config.toml
    for devices that can't play it, and photos in-process with Pillow and pyoxipng, which are
    part of the core install ([media.md](media.md)).
13. **One `Source` class per site**, with routes and categories (above).
14. **A fixed chain of steps** after each file (above).
15. **Ruff is the only lint, and pyright is informational** (`make type`), never a gate.
16. **Settings resolve in one order**: a flag (or a library argument), then the environment,
    then config.toml, then the default. The file is read and checked in one place; a bad value
    is a usage error naming the key, and an unknown key a warning.

## Where to go next

- [cli.md](cli.md): every command and flag.
- [library.md](library.md): flnd from a Python script.
- [writing-a-source.md](writing-a-source.md): adding a site, testing it, and shipping it as a
  plugin.
- [storage.md](storage.md): every file of a home, and the rows of `flnd.db`.
- [media.md](media.md): the steps after a file lands, and `--compress`.
- [performance.md](performance.md): what is fast, what isn't, and how it is measured.
- [sources/](README.md#sites): what each site's source does.
