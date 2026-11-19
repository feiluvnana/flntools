# How flnd fits together

A map of flnd's code as it is built: its layers and their import rules, what each part holds, the
path one download takes from the command line to the disk, and the threads and queues that carry
it. The words it uses are defined in the [glossary](glossary.md). Every module starts with a short
docstring that names this page and says what the module holds: this page is the map, the
docstrings are the detail. A change that moves a part described here changes this page in the
same commit.

## The layers

flnd is two packages side by side in the `flnd/` folder. `src/` is the package `flnd`, in layers.
`flnkit/` is a standalone package that flnd imports and that imports nothing of flnd.

```
you
 │
1 the command line    src/cli.py          flags in, one library call, events out
 │                                        imports flnd, src/ui/ and the plugins' manifest
2 the library         src/__init__.py     flnd.open, Flnd, and the groups Targets, Auth,
 │                    src/_library.py     Plugins, Extension, Config; flnd.plugin (the plugin
 │                                        API); flnd.testing (the plugin test kit)
3 the engine          src/engine/         run, events, jobs, crawl, save, resolve, plugins,
 │                                        auth, chrome, extension, store, config, ctx
 │                    src/plugins/        the built-in plugins, and the installed ones beside them
4 the screen          src/ui/             theme, out, log, screen, forms, lists
 │
5 the kit             flnkit/             net, browser, files, media, stream, clock, dig, html,
                                          text, db, cookies, vault, errors
```

`src/errors.py` sits over `flnkit/errors.py` for every layer: the one error hierarchy.

The rules between the layers. `tests/test_layers.py` reads every file's imports with `ast` and
checks them:

- **The kit imports only the kit**, the standard library and third-party packages, never flnd.
  Each module does one general job (HTTP, the browser, files, cookies, a SQLite store) that
  another program could use. A kit module logs through the standard library's `logging` as
  `flnkit.<module>`; `src/ui/log.py` routes those records into flnd's lines and the journal.
  What the browser asks a person comes from a `Screen` its caller passes in.
- **The screen** (`src/ui/`) imports only itself, the kit, `src/engine/events.py` and
  `src/errors.py`.
- **The engine** imports itself, the kit, the screen, `src/errors.py` and the plugin API
  (`flnd.plugin`). `src/engine/events.py` imports only the errors, the plugin API, the kit and
  `msgspec`. The plugin manager reads the built-ins' plain-data manifest and never imports a
  built-in plugin's module.
- **The plugin API** (`src/plugin.py`) imports only the kit, `src/errors.py` and the standard
  library.
- **The library** (`src/_library.py`) imports the engine, the kit, `src/errors.py` and the plugin
  API. The plugin test kit (`src/testing.py`) imports flnd, the plugin API, the engine and the kit.
- **A built-in plugin** (`src/plugins/`) imports only `flnd.plugin` and the standard library. Its
  own SDK (Telethon, qrcode) is imported inside a function, so `--help` loads none of it.
- **The command line** imports only flnd, `src/ui/` and the plugins' manifest. Nothing imports
  `flnd.cli` except `src/__main__.py`, so every decision is the library's and a script can do
  anything the command line does.
- **Nothing heavy loads at import.** `import flnd` loads only `src/__init__.py`; each public name
  loads its own module on first use, and curl_cffi, Playwright and Rich load only when a command
  needs them (`tests/test_packaging.py`).

## The commands and the library

The command line is a thin layer over the library. Each command is one library call:

| command | library |
| --- | --- |
| `flnd get [targets]` | `Flnd.get`, which returns a `Run` |
| `flnd target list`, `flnd target clean` | `Targets.list`, `Targets.clean` |
| `flnd auth list`, `login`, `logout` | `Auth` |
| `flnd plugin list`, `install`, `uninstall` | `Plugins` |
| `flnd extension install`, `uninstall` | `Extension` |
| `flnd status` | `Flnd.status`; `Flnd.check(fix=True)` when the user agrees to a repair |
| `flnd config` | `Config` |
| `flnd host` (hidden; Chrome starts it) | `Extension.host` |

The library's `Targets` also has `follow`, `unfollow` and `retry`, which no command calls: `get`
follows a target, `target clean` forgets it, and failed files are retried by the next `get`.

The commands and their flags are in [cli.md](cli.md). The library is in [library.md](library.md).

## The parts

The command line and the library:

- **`src/cli.py`**: the commands, a run's screen and summary (or `--json` lines), `--exec`, and the
  exit codes.
- **`src/_library.py`**: `open` and `Flnd`, each command as a method over the engine, and the
  groups above. The reads (`targets`, `files`, `auth`, `status`) return rows.
- **`src/__init__.py`**: the public names, each loaded on first use.
- **`src/plugin.py`**: plugin API 2 (`flnd.plugin`): `Plugin` and its checks, `@route`, `Post`,
  `File`, `Target`, `Ctx` (its protocol), `Login` and `Cookies`, `Request`, `Response`, `dig`, the
  errors. A mistake in a plugin's class raises when the class is made.
- **`src/testing.py`**: the plugin test kit: `FakeCtx` (with `Reply`, `FakePage` and `Seen`),
  `collect`, `check`, `fake_clock`. It answers like the engine, from fakes.
- **`src/errors.py`**: flnd's face of the error hierarchy: the kit's classes, `PluginError`, and
  `classify`, which turns any exception into one of them.

The engine (`src/engine/`):

- **`run.py`**: `Run`, one command on the run's loop, read as a stream of events; the Ctrl-C
  stages (`Stop`, `stopping`, `quit_now`); the run's live counts (`Tally`) and their snapshots.
- **`events.py`**: the events (`Saved`, `Skipped`, `Failed`, `Planned`), the public `Report`, the
  `State` snapshot, and the rows the reads return (`Installed`, `Record`, `Tracked`, `Failure`,
  `Status`, `Checked`).
- **`_engine.py`**: `Engine`: every command as a method over the resolver. It makes each plugin's
  and account's run instance (`_Runner`), runs targets, and runs a search's targets.
- **`crawl.py`**: one target, as a coroutine on the run's loop: its listing, its smart sync and
  early stops, the `Ledger` that plans each file, the `Downloads` that save them, `Claims` (a file
  queued by one target is held for the others), and the history rows.
- **`save.py`**: a post's files as jobs (`Saver`): the transfer by mirrors, a DASH pair or a
  plugin's `fetch` function; the content check; dedupe; the sidecar; the post's commit; unpack;
  `Saved`; `--exec`.
- **`jobs.py`**: the scheduler: every request, tab, step and write as a job, the pools they wait
  for, the one retry policy, the per-host caps, the holds, and the segments of a big file.
- **`ctx.py`**: `PluginRun`, a plugin's resources for a run (cookies, HTTP session, clients,
  stores, loop, scheduler), and `Ctx`, the plugin API's context for one target: `fetch`, `pages`,
  the browser verbs, `hold`, `pause`, `call`, `ask`, `log`, and the account's `state` and
  `secrets`.
- **`resolve.py`**: target text (`code:value/categories`, `code?query`, a link) read into a plugin,
  a value, a kind and categories, from the manifest's plain data, without importing a plugin.
- **`plugins.py`**: the plugin manager: the plugin folder, `plugins.json`, install, uninstall, and
  loading an installed plugin on first use.
- **`auth.py`**: accounts, the account a target runs as, the sealed vault (`secret.db`), the
  login methods, `logout`, and the cookies a run writes back.
- **`chrome.py`**: the Chrome backend for `Ctx`'s browser verbs, and the Chrome login's cookies.
- **`extension.py`**: the Chrome extension's side: the native-messaging host (`flnd host`), the
  relay a run reaches it by, the run's client, and the install.
- **`store.py`**: `flnd.db`: its tables, the migration of a version-1 home, the rows and queries,
  the save's commit, and `check`, which compares the history with the disk.
- **`config.py`**: `config.toml`: every setting, read and checked in one place, and edited with
  its comments kept.

The screen (`src/ui/`):

- **`theme.py`**: every colour and glyph flnd draws with, and when to drop colour or Unicode.
- **`out.py`**: every line flnd prints: lines, folded warnings, errors with their hints, tables,
  the help. Text from a site is cleaned of escape codes first.
- **`log.py`**: the levels of log lines and their scope, the folding of repeated warnings, the
  run journal, and the route of the kit's `logging` records.
- **`screen.py`**: the run's live block, drawn from the run's `State`, and the end summary.
- **`forms.py`**: questions in the terminal (Textual, inline): a pick, a line of text, a yes or no.
  Without a terminal, a pick and a line raise `UsageError`, and a yes or no gives its default.
- **`lists.py`**: the interactive lists of targets, accounts and plugins.

The plugins (`src/plugins/`): the eight built-in plugins, one module each (`tik`, `fbk`, `ins`,
`twt`, `loi`, `tlg`, `mht`, `web`), `_manifest.py` (each built-in as plain data, made by
`make manifest`), and `__init__.py`, the registry. The plugins' own pages are in
[plugins/](plugins/).

The kit (`flnkit/`), each module a toolkit of its own:

- **`net`**: HTTP over curl_cffi on an asyncio loop: sessions with Chrome's TLS fingerprint,
  clients that make one try, downloads into parts, and a fake for tests.
- **`browser`**: async Playwright: launching Chrome or Playwright's Chromium, tabs, warm tabs,
  response capture, and human-like scrolling. See [The browser](#the-browser).
- **`files`**: safe file writes, parts that lock themselves, hashing as bytes are written, sweeps,
  running an outside tool, and archive extraction.
- **`media`**: one ffmpeg join of a video stream and its audio stream into one file.
- **`stream`**: reads HLS and DASH playlists into segments, with `pick` for the tallest variant
  under a cap. flnd's save does not use it, as [The save](#the-save) says.
- **`clock`**: a clock tests can fake: monotonic and wall time, interruptible sleeps, site dates.
- **`dig`**, **`html`**: walking JSON by a path; parsing HTML (selectolax).
- **`text`**: safe file names, URLs and bodies without secrets, readable numbers, name templates.
- **`db`**: a SQLite store: one connection behind a lock, transactions, and waiting for another
  process's writer.
- **`cookies`**, **`vault`**: reading cookie files and the browsers' cookie stores; secrets sealed
  per account and name.
- **`errors`**: the error classes the kit raises, the root of flnd's hierarchy, and `disk`.

## Installed plugins

A plugin is a class per site. The built-in eight live in `src/plugins/`, and their manifest,
`src/plugins/_manifest.py`, lists each one as plain data (its code, domains, forms, categories and
routes). Resolving a target and `flnd --help` read the manifest, so neither imports a plugin. A
built-in's module is imported the first time its code is used.

A plugin that isn't built in is an installed one. It lives in the plugin folder: `$FLND_PLUGINS`,
else `~/.config/flnd/plugins/` (`%APPDATA%\flnd\plugins\` on Windows). `flnd plugin install <path>`
copies a `.py` file or a package folder (one with an `__init__.py`) into that folder and records it
in `plugins.json`, with its name, its domains and the SHA-256 of its file. `flnd plugin uninstall
<code>` removes it. Installation refuses what can't be a plugin: a home folder, a cookie file
(`*_cookies.txt`), a symbolic link to a file, or anything but a `.py` file or a package. A code that
a built-in already has is refused, never replaced.

The manager loads an installed plugin the first time its code is used. It checks the file again
when the file's digest changed. A plugin that breaks (an import error, a class that fails its
checks, a newer plugin API, a file that no longer matches its code) is disabled alone, with a
warning that names its file and line. An error inside a plugin's listing fails that target alone,
as a built-in's would, with a hint that names the plugin.

flnd never loads code from a home folder. A home is often the folder you are in, and code there
may not be yours. Plugins run in flnd's process, because a plugin needs its login's session.

## One download, start to finish

`flnd get ins:example_user/posts`:

1. **The command line** (`src/cli.py`) parses the flags with argparse, opens the home (`flnd.open`),
   and calls `fl.get("ins:example_user/posts")`.
2. **Resolving.** The library resolves the target at once (`Engine.chosen`, through
   `src/engine/resolve.py`). It reads the manifest alone, finds the plugin by its code (or a bare
   link by its domains), and the first route whose pattern matches gives `Target("ins:example_user",
   kind="user")`. The categories after the last `/` are checked against the route's. Without them, a
   known target takes the categories it stores, and a new one the route's own. A short link that no
   route reads is followed once with a HEAD request.
3. **Checking.** `--when` and `--limit` are read before any request. A mistake is a `UsageError`,
   which exits with code 2.
4. **A run that hasn't started.** `Flnd.get` returns a `flnd.Run`. Nothing happens until the command
   reads it: iterating the run or calling `wait()` starts the run's loop on its thread, `flnd-loop`.
   The loop makes an `Engine` with the run's event sink and stop state, and awaits
   `Engine.run_download(..., sync=True, keep=True)`. From here until the target ends, everything
   runs on that loop, except a plugin's blocking code, which runs on a thread of the scheduler's
   (see [Threads and queues](#threads-and-queues)).
5. **Recording.** The engine opens the command's row in `runs` (each failure names it), and
   `Store.record` writes each target's row in `flnd.db`. The row keeps the target's cursors, heads
   and gaps from earlier runs, so later syncs can use them.
6. **One `PluginRun` per plugin and account.** The engine makes a `PluginRun` the first time one of
   a plugin's targets runs, discovered and linked targets included. It reads the login's saved
   cookies from the vault, makes the plugin's HTTP session (see [Network](#network)), and runs the
   plugin's `connect` hook (Telegram's client) once. The run ends it, and an error at its exit
   becomes the run's failure. A plugin whose login is required and missing fails each of its
   targets. Any other plugin without a login warns and goes on with public access. The run's browser
   is shared by every plugin.
7. **Targets run at once, within limits.** A plugin whose `browser` is not `Browser.NONE` lists one
   target at a time, and one that only sends requests lists four (`HTTP_AT_ONCE`). The run holds at
   most 16 active targets (`ACTIVE`, the same number as the listing threads), each while it lists or
   has more files queued than its places. A target's route waits until every earlier target has
   called its own, or waits for a place, a hold or the site. So a full disk at one target's first
   step stops the run before a later target's route runs. A target named twice runs once. A file one
   target queued is held for the others (`crawl.Claims`), as a file saved by one run would be. A
   target a listing discovers runs inside that listing when it is of the same plugin. One of another
   plugin is handed to the run as a target of its own, which the listing doesn't wait for, so no two
   listings wait for each other's places. A login that a site refused fails the plugin's targets
   that hadn't started.
8. **One target** (`crawl.download`). The crawl removes the stale parts an interrupted run left in
   the target's folder: a part untouched for a week and held by no run. Nothing locks the target as
   a whole. Each file's part locks itself.
9. **Listing.** The route runs and yields the posts. The files that earlier runs failed on come
   first: each failed post is listed again from the cursor of its page, and its failed files are
   queued (see [Smart sync](#smart-sync)). Then each wanted category yields its posts, page by page.
   A category whose full download was cut short resumes from its saved cursor, then catches up from
   the newest post. `--when` drops posts outside the range. After 5 posts in a row older than the
   range, a `newest_first` category stops.
10. **Planning each post** (`Ledger.take`). Each file gets its path (from the plugin's `folder` and
    `filename` templates) and its history key (`<code>:<key>`). A file whose key is in the history,
    or whose file is already on disk, is skipped (a `Skipped` event). A file on disk without a row
    gets its row from the one `stat` that found it. A file saved in another category's folder is
    linked instead. A post with no day waits until its category ends, then takes the day of its
    nearest dated neighbour, marked as estimated. A lazy post's files are listed only once the post
    is known to be new.
11. **Downloading** (`Downloads`). Each post's new files go to the run's save (`save.Saver.post`) as
    one task on the run's loop. The target's queue holds 4 times its places, so the listing waits
    when downloads fall behind and memory stays flat. Each file is a `FILE` job of the run's
    scheduler. It waits for one of the target's places (32, or the plugin's largest `limits` entry
    above that), then for a place under its host's cap. The save opens and locks the file's part
    (`<name>.part`, see [storage.md](storage.md)), streams the file into it, resuming what a failed
    run kept, and tries the mirrors in order. A DASH video is merged with its audio by ffmpeg. The
    content is checked, and the file takes the extension its content calls for. A file that the site
    or the network fails fails alone: a `Failed` event, and a row in `failures` for a later run to
    retry.
12. **Finishing a file.** Dedupe replaces a file whose bytes are already saved with a hard link to
    them. Once every file of its post has settled, the post's rows are written to `flnd.db` in one
    transaction. Each landed file then goes through the rest of the step chain (see [The
    save](#the-save)), whose last step sends `Saved`.
13. **Ending the target.** The crawl waits for its downloads, makes the links it held back, keeps
    the cursors, gaps and heads of the categories it listed, and counts the target's files for `flnd
    target list`. A failed listing fails the target alone (a `Failed` event). A refused login fails
    the plugin's remaining targets. A full disk stops the run.
14. **Links to other sites.** Targets that a listing yields (a post that links to another site) are
    downloaded after the run's own targets, each by its own plugin, up to `FOLLOW_DEPTH` (2) links
    deep.
15. **The end.** The engine writes the refreshed cookies back to the vault and closes the `runs` row
    with the count of saved files. The run's thread ends, and the command line prints the summary
    and exits. The exit codes are in [Errors](#errors).

`flnd get` runs items 6 to 15 of that list for every target it follows. A search (`mht?words`) first
lists the targets it finds, then downloads each one. On a sync, only the targets it hadn't seen
before are downloaded, at most 25 a run (`MAX_SYNC_NEW`). A target with failed files lists the
failed posts' pages first. `--dry` plans the same listing, sends a `Planned` event for each file,
and writes nothing.

## Threads and queues

A run has one loop, and the rules below protect it.

- **The run's loop** (`flnd.engine.run.Run`) is one asyncio event loop on one thread, `flnd-loop`,
  for the whole command. The engine, each target's crawl, the planning and the events all run on
  it as coroutines. Outside a library run (a test calling the engine directly), the caller's thread
  runs the loop.
- **The reader** is whoever iterates the run: the command line's main thread, or a script. It
  takes events off the run's channel, which holds 256 events (`QUEUE`). Producers are on the loop
  only, and a producer waits while the channel is full. A slow reader therefore slows the run
  rather than piling events up in memory. The end marker goes in after the loop has closed, so it
  is always the last thing read. `run.wait()` stops listening (events are still counted into the
  report) and waits for the loop's thread.
- **A plugin's blocking code** (a `File`'s `fetch` function, a `ctx.call` step, the `connect` hook,
  a login check) runs on a `CALL` thread of the run's scheduler, one call per job, while the crawl
  awaits it. Each browser verb of `Ctx` runs on a thread of its target's tab set, and each browser
  step it needs is handed to the loop one call at a time. A lazy post's files, a `refresh` and a
  `date` hook open any tab they need as one of their own, so the listing's tab never leaves its
  feed.
- **On the loop** are every HTTP transfer (the listing's requests and every file's download), the
  downloads (one task per post, as item 11 says), async Playwright (one browser per run, shared by
  every plugin, with one tab per target and warm tabs for the pages it will open next), and async
  plugin code (a plugin with an async SDK, such as Telethon, and any `async def` route, which awaits
  the `Ctx` verbs directly).
- **What would block the loop runs off it.** A part's bytes go to its device's writer thread in 4
  MiB pieces. A hash runs on the hasher threads (`HASH`, one per core). A part's open, its rename
  and dedupe's hard link run on worker threads, and so do a plugin's blocking `fetch` function, the
  ffmpeg join and the target's rows at its end.
- **A lazy post's expired link.** When a download is refused with 403 or 410, the save lists the
  post's files again (`Ctx.aside`) and tries the fresh link once. The save doesn't need the listing
  to be running.
- **A lazy post the site refuses** (gone, hidden, or an answer that can't be read) fails alone. Its
  `failures` row is under the post's first key, a gone post is never asked again, and the listing
  goes on.
- **The save's steps** are jobs of the run's scheduler. Blocking work (opening and landing a part, a
  hash, an unpack, a program) runs on the threads of its kind (`Scheduler.on`). A post's rows commit
  on the loop, as a `WRITE` job. If another process holds `flnd.db`'s lock, the job goes back in the
  queue every 20 ms (`jobs.Busy`), so the loop never waits.
- **Ctrl-C** is handled by `flnd.engine.run.stopping`, on the reader's thread when it is the main
  thread. The first Ctrl-C sets `stop.soon`: listing stops, queued files are dropped, and files in
  flight finish. The second sets `stop.now`: files in flight are cancelled and their parts kept, and
  a line says a third Ctrl-C quits. The third calls `quit_now`, which runs the undo steps registered
  with `restoring` (whatever changed the terminal registers its undo there), then ends the process
  with exit status 130. It runs no `finally` and joins no thread. `run.suspend()` holds every job
  of the scheduler before it starts, until `run.resume()` or a stop.
- **The run's display** is drawn from a snapshot. Every part of a run counts on the run's `Tally`:
  a target's listings, each file's steps, the files in flight with their bytes, and the waits a site
  asked for. A task on the loop turns the tally into a frozen `State` up to 30 times a second, only
  when something changed, and the run swaps it in as `run.state`. On a terminal, the screen
  (`flnd.ui.screen`) runs a `flnd-screen` thread that reads `run.state` and redraws when its version
  moves. It never touches the engine's counts, so it takes none of their locks. Warnings printed on
  standard error go through the screen's queue and print above the block.

## The scheduler

`flnd.engine.jobs` is the one place that retries, sleeps, limits, or picks threads. There is one
`Scheduler` per run. Every job has a kind, and the kind says which pool it waits for:

| kind | what it is | its pool |
| --- | --- | --- |
| `PAGE` | a request to the plugin's domains | a token bucket per account, at `pace` (0.5 s) |
| `TAB` | a browser tab | 6 tabs per run (`TABS`); 2 of them stay free for warm tabs (`WARM_FREE`) |
| `FILE` | a download, or a request to another host | the host's cap, and the target's places |
| `CALL` | a plugin's blocking step | `min(32, cores + 4)` threads |
| `HASH` | a hash | one thread per core |
| `MERGE` | a DASH join by ffmpeg | `cores // 2` threads |
| `UNPACK` | an archive's extraction | one thread, so one archive at a time |
| `EXEC` | a program for `--exec` or `[defaults] exec` | one thread, in save order |
| `WRITE` | a commit to `flnd.db` | one thread per device |

A retried job goes first. Otherwise the order is `PAGE` and `TAB` (1), then `FILE`, `CALL` and
`WRITE` (2), then the rest (3).

Each target has its own places: at least 32 (`PLACES`), as many as one host's cap grows to, or
the plugin's largest `limits` entry above that. A target queues at most 4 files per place
(`QUEUED`). A plugin's account has a scroll gate of 2 (`SCROLLING`): at most two tabs of one login
scroll at once in `human` style.

The retry policy is the scheduler's, and the only one:

- A timeout, a reset or a 5xx (for a request that is safe to send twice) gets 4 more tries, after
  waits of about 1, 2, 4 and 8 s, each scaled by a random 0.5 to 1.5.
- A 429 holds the bucket for the site's wait, or 30 s when the site names none. Its rate halves
  until 20 clean requests go through. Three 429s in a row make a block round: the bucket waits 30,
  then 120, then 480 s. A block round is forgotten after 10 minutes with no block.
- A login wall on a page that was asked for lifts after 30 and then 120 s. One more landing is a
  `LoginError`. A switch to another account is possible when the plugin allows it, through
  `Switcher` (see [Accounts and login](#accounts-and-login)).
- A 429 or 503 halves the host's cap.
- A waiting job never waits longer than an hour (`MAX_WAIT`).

**The adaptive cap** (`jobs.Cap`, AIMD) limits the transfers at once to each host. It starts at 8
(`CAP`) and may grow to 32 (`MOST`), or to the host's `limits` entry. A failure of one round halves
it once. Every 10 clean transfers (`STEP`), the cap judges its throughput, which is the cap times
the bytes moved over the seconds the transfers took. A window in which a place stood free with
nobody waiting is dropped, since the cap didn't bind. A step up that didn't raise the throughput by
at least half of what one more place would add is undone, and the cap holds for 100 clean transfers
(`HOLD`) before it probes again. [Performance](performance.md#concurrency-per-host) has the numbers.

**Free space** is kept on each device: 64 MiB (`SPARE`) beyond the files in flight. A full disk
stops the run.

**Segments** (`jobs.Segments`). A file of 16 MiB or more (`SPLIT_FILE`) from a host that answered
`Accept-Ranges: bytes` or a 206 may take more places. From 2 s into its transfer (`SPLIT_AFTER`),
and then at each piece written (once a second at most), while the host's cap has a free place and
no other file waits for it, the largest range left of 8 MiB or more (`SPLIT_RANGE`) is halved, up to
8 segments (`SEGMENTS`). A plugin's `fetch` file of 16 MiB or more that the plugin serves by ranges
is cut into up to 4 even segments as it starts (`FETCH_SEGMENTS`). A part's sidecar keeps each
segment's place, so a resumed run continues every segment.

**The tab budget** (`jobs.Budget`) is one target's share of the run's six tabs. A warm tab opens
only while two tabs are free, so it never takes the last one an active target would wait for.

## Resolving targets

`src/engine/resolve.py` reads a target's text and returns the plugin, the value, the kind and the
categories. The text forms are `code:value`, `code:value/categories`, `code?query` for a search, and
a link. The resolver reads the manifest's plain data only, so resolving never imports a plugin. A
link goes to the plugin whose domains match its host (the `m.`, `www.` and similar prefixes are the
same site). A link that no plugin claims goes to the `web` plugin. A route's pattern names the
value, and the categories after the last `/` are checked against the route's categories. The link's
`user:password@` part is removed before the text reaches a key, a message or a log line.

## The crawl and the engine

`src/engine/_engine.py` holds `Engine`, which runs each command as a method. `src/engine/crawl.py`
runs one target. The pieces are these:

- **`Options`** says what one target downloads: its time range, its limit, whether it runs again,
  what it does with the failures earlier runs left (`Retry.FIRST` or `Retry.ONLY`), and the link it
  was typed with.
- **`RunShared`** is what a run shares among its targets: the settings, the database, the scheduler,
  the stop state and the tally.
- **`Ledger`** plans each post's files (`Ledger.take`), as item 10 of the download says. It reads
  `flnd.db` to skip what is saved.
- **`Downloads`** queues each post's new files to the save and holds the target's places.
- **`Claims`** holds a file one target queued for the others.
- **`discover`** runs the targets that a listing yields, each one as its own target of the run.

The engine's `_Runner` keeps one `_Entry` per plugin and account, so each plugin's session and
stores are made once per run. A `TargetReport` is the per-target result. The engine keeps the
totals for the `runs` row. A script never sees them: it sees the events.

## Smart sync

A run remembers enough, per target and category, in `flnd.db` (the `targets` table's `heads`, `gaps`
and `cursors`, and the `failures` table), to do as little as it can and still miss nothing. Each of
the three is in the events and in the run's report.

1. **Failures first.** Every download and sync of a target first retries the files that failed on
   earlier runs (`crawl._retry_failed`), before its new posts. It works from `(target, post, file)`,
   never from a stored URL. It groups the failures by category and by the cursor of the page each
   post was on, lists from that cursor until it has met each failed post (or `RETRY_WINDOW`, 100
   posts, past it), and queues the post like any other. The post downloads only its files that
   aren't saved. A file is retried this way on at most 3 runs (`store.RETRIES`); after that it waits
   for `r` in `flnd target list`, and a gone file is never retried. A post the listing no longer has
   is recorded as gone. Each file brought back is a `Saved` event with `retried=True`
   (`report.retried`).
2. **Nothing new.** A `newest_first` listing keeps its head: the newest post that a run which read
   the category from the top saw. A sync stops after the head and the post after it
   (`HEAD_IN_A_ROW`, 2), so a target with nothing new costs one page, which on a site that pages by
   cursor is one request. It says so with a `Skipped` event whose `reason` is `"nothing new"`
   (`report.unchanged`).
3. **No gap.** A full download that was cut short keeps the cursor of the page it was reading, and
   the next run resumes there, then lists from the newest post down to the posts it had. A sync that
   was cut short (Ctrl-C, an error, `--limit`) keeps a gap instead (`crawl._sync_one`). The next
   sync starts from the newest page, since that is where new posts are. Once it meets saved posts on
   top, it jumps to the gap's cursor and goes on from there until it meets saved posts again. The
   run counts it in `report.resumed`. Gaps stack, newest first, when syncs are cut short twice. A
   feed that isn't paged by cursor leaves a gap without one, and the next sync reads down past saved
   posts to the head instead. The head moves only when a sync leaves no gap, so every gap lies above
   it.

## The save

`src/engine/save.py` holds `Saver`, which takes a post's files from their parts to their rows. Every
saved file goes through the same steps in one fixed order:

```
transfer → dedupe → sidecar → commit → unpack → Saved → --exec
```

- **Transfer.** A file's part is opened and locked, and the bytes come in. The source is one of: the
  mirrors in order, each tried once; a DASH pair, whose two streams are fetched at once and joined
  by ffmpeg (`MERGE`, `flnkit/media.py`); or the plugin's `fetch` function. A file resumes what a
  failed run kept, with `Range` and `If-Range`. A refused link (403 or 410) is asked for again once
  (see [Threads and queues](#threads-and-queues)). The content is checked: a web page where a photo
  was asked for is a failure, and a file that names a playlist is refused, since the save does not
  stream playlists (see [The kit](#the-kit) and `flnkit/stream.py`).
- **Dedupe** (`[defaults] dedupe = true`) hashes a file only when a saved file has its size, and
  makes a file whose bytes match a hard link to the saved one before its row is written.
- **Sidecar** (`[defaults] sidecars = true`, off by default) writes the post's caption, author, tags
  and link beside the file as `<name>.json`. The row names it, so it is written before the commit.
- **Commit.** Once every file of the post has settled, all their rows are written in one transaction
  (a `WRITE` job), with the failures of that post.
- **Unpack** is slow, so it is an `UNPACK` job, one at a time. An archive is extracted beside
  itself, and the row records `unpacked` and the folder. The archive stays on disk.
- **`Saved`** is sent once the file is unpacked, with its final path and size.
- **`--exec`** runs a command for each `Saved` event that the command line reads. It never runs
  through a shell (see [cli.md](cli.md#running-a-command-per-file)). **`[defaults] exec`**, read
  only from the user's own `~/.config/flnd/config.toml`, runs its program after each `Saved` as an
  `EXEC` job, with `{path}`, `{target}` and `{post}` filled in and `$FLND_PATH`, `$FLND_TARGET` and
  `$FLND_POST` set. It never runs through a shell. A failure is a warning, and the file stays saved.

Each step is on or off by its flag or setting, but the order never changes. A step's failure is the
file's, never the run's: it is logged, and the file is still finished.

A file's name (`save.STEM`, for a file with no extension of its own) takes the extension its bytes
call for, then the one its `Content-Type` names, then the one in its URL.

## Events

A run's only output is its events (`src/engine/events.py`). Each is a frozen msgspec struct that a
script may keep after the run and encode to JSON:

- **`Saved`**: a file went through every step. It has the target, the category, the post, the final
  path, the day, the size, and `retried`. The step chain's last step sends it.
- **`Skipped`**: a file or post was not downloaded. Its `reason` is `"saved"` (already saved),
  `"busy"` (another run is saving it), or `"nothing new"` (a sync found nothing new).
- **`Failed`**: a file failed (with its post and path), or a whole target did. Its `error` is the
  `flnd.Error`, with its `hint`.
- **`Planned`**: a dry run planned a file, with the state `"new"` or `"saved"`.

`Run` counts the events into a public `flnd.Report` (saved, skipped, bytes, planned, failed,
retried, resumed, unchanged, stopped). `run.report` shows it so far, and `run.wait()` returns it.
What the run is doing now isn't an event: it is `run.state`, the snapshot (see
[Threads and queues](#threads-and-queues)). There are no callbacks and no hooks: a script iterates
the run.

## The browser

`flnkit/browser.py` is a library of its own. It knows Playwright, tabs and scrolling, and nothing of
plugins. Inside flnd, `flnd.engine.ctx.TargetTabs` builds the browser verbs that a plugin's `Ctx`
calls on it, and each verb's browser step is one call across to the run's loop (`RunBrowser.run`).

What the library offers, from the bottom up:

- `launch` starts Chrome, or Playwright's Chromium (offering to download it, when no Chrome is
  found), and gives a `Browser`. `Browser.tab` opens a `Page`, one tab.
- `Page` drives one tab: `goto`, `wait`, `fetch` (a request sent by the page itself), `content`,
  `evaluate`, `click`, `scroll`, and `capture`, which keeps the bodies of the responses the tab
  receives. Every `Page` method is a coroutine that runs on the loop.
- `RunBrowser` is one browser shared by many users on one loop. It launches the first time someone
  asks for a tab, and adds each user's cookies once. In flnd there is one per run.
- `Tabs` is one user's share of it (in flnd, a target's): the tab it works in, a stack of extra tabs
  for nested scrolling feeds, and warm tabs that pre-load the pages it will open next. Its methods
  are plain functions, called from the user's own thread.
- `Feed` iterates the JSON of the matching responses that a user's tab receives, in order. It
  scrolls the tab for more when asked, and ends when the site's own cursor says there is no more.
- `local_storage` reads a page's local storage from a copy of a Chrome profile, on a loop of its
  own.

A plugin reads a site that only shows its data to a logged-in page by watching the page's own API
calls: `ctx.feed(match, scroll=True, cursor=reader)` keeps the JSON of every response whose URL
`match` takes, and scrolls the tab only when no body is waiting. The main flow of a scrolled feed:

1. The feed starts a `Capture` on the user's tab (one call to the loop). It keeps the bodies of the
   responses that `match` takes.
2. The user loads a page. Its API calls arrive as responses, and the capture reads each matching
   body on the loop, keeping their order.
3. Iterating the feed parses the bodies that arrived, on the user's thread, and yields them. The
   caller's cursor reader says whether the site has another page.
4. When none is waiting, the feed scrolls one step at a time (one call to the loop each) until one
   is waiting, the cursor says there is no more, or the scroller says the feed went quiet.

**A scroll step is driven by what the page does**, never by a fixed sleep:

1. **Scroll.** In `human` style (the default), the tab glides down 1.2 to 1.8 screens in three to
   six uneven wheel steps. Now and then (7% of steps) it first scrolls back up a little and
   hesitates, as a person going back to look at something. In `fast` style it jumps to the bottom in
   one go.
2. **Watch.** The step watches for progress: the next matching response (the capture wakes it at
   once), or the page growing (checked every 50 ms). At the very bottom with nothing waiting, it
   waits up to 3 s. `human` style also pauses, counted from the scroll: a lognormal time with a
   median of 0.4 s when new media came, 0.25 s when not (a glance), and never more than four
   medians.
3. **Go on, or end.** Progress, or room left to scroll, means another step. The feed ends as soon as
   the site says so: the cursor reader read a page with no next one, or the same cursor came back
   twice more. A feed without that word ends on silence: a whole 3 s wait with nothing, then 2 s
   more.

A target's tab scrolls by script (`window.scrollBy`, with a wheel event per step), not with real
mouse input. The tabs of one shared window are background tabs to Chrome, which sends them no mouse
events. An element that scrolls on its own (`scroll(within=...)`) gets real wheel events.

**Where the style comes from.** A plugin class may set `scroll = "human"` or `"fast"`. The default
is `"human"`. `TargetTabs` hands the style to the target's tabs, which every feed of that target
uses. config.toml has no scroll key.

Rules a change must keep:

- Playwright objects are used only on the loop's thread. Everything another thread holds (a `Page`,
  a `Capture`) is touched there only through `RunBrowser.run`.
- `RunBrowser.run` must not be called on the loop itself, since it would wait for itself. Code on
  the loop awaits instead.
- Responses are kept in the order they arrived, even though their bodies are read at once.
- A `human` scroll step waits for its turn (`Page.turn`) at the account's scroll gate. A person
  doesn't scroll many tabs of one account at the same moment.

## Network

`flnkit/net.py` does every HTTP request and download, on the run's loop:

- **A session per plugin** (`flnkit.net.Session`, made with the plugin's `PluginRun`). It is a
  set of libcurl handles on one multi handle, through curl_cffi, set up once with Chrome's TLS and
  HTTP/2 fingerprint and kept from one transfer to the next. It also holds the login's cookies,
  which are written back to the vault at the end. The plugin's `user_agent` and its `sign` hook for
  requests to its own domains run before every try. A `proxy` in the plugin's
  `[plugins.<code>]` table goes to curl for every transfer of the session. The browser is not
  proxied.
- **The home's settings win over the class's.** The run reads config.toml once as it starts. A
  plugin's `[plugins.<code>]` table can replace its `pace`, merge its `limits`, and replace its
  `folder` and `filename` templates on the run's instance.
- **Every request is a job of the run's scheduler** (see [The scheduler](#the-scheduler)). Requests
  to a plugin's own domains and the pages it opens are `PAGE` jobs, paced per host. Every file, and
  every request to another host, is a `FILE` job under the host's adaptive cap.
- **Two clients** share the session. `web` makes the listing's requests, and stops at the first
  Ctrl-C. `files` downloads, and stops at the second. A client makes one try. It neither paces nor
  retries: the scheduler does both.
- **A file** (`save.Saver`) goes into its part, resumed with `Range` and `If-Range` from what a
  failed run kept, and tried mirror by mirror. A plugin's `ranged` fetch, and any segmented file,
  go as described in [The scheduler](#the-scheduler).
- **Names** (plugin API 2). A `File.name` with its own extension keeps it. A stem, or the
  `filename` template's name, takes the extension its bytes call for, as [The save](#the-save)
  says.
- **Instagram's API calls** move into the page (`ctx.fetch(..., in_page=True)`) when the site
  refuses flnd's client, or answers one with a page instead of JSON. The tab's own fetch sends them.
- **The fake** (`flnkit.net.fake`) answers every request from routes or a handler in tests and
  benchmarks. Nothing in `make check` reaches the network.

## Accounts and login

`src/engine/auth.py` keeps each plugin's accounts and the one a target runs as (`pick`). A login
is an account. The login methods are these:

- **`browser`**: flnd's Chromium opens, visible, at the site's login page. flnd reads the window's
  cookies every second until the plugin's own cookie name is set on its domains, then closes it. The
  wait is 10 minutes at most.
- **`file`**: a cookie file, a Netscape `cookies.txt` or a JSON export.
- **`chrome`**: the user's Chrome, through the extension (see [The Chrome
  extension](#the-chrome-extension)).
- **`firefox`** and **`safari`**: the browser's own cookie store. flnkit reads only these two
  stores. Chrome logins are not read from a store: they go through the extension.
- **The plugin's own way** (`Login.start`, such as a QR code or a phone number). Only `Login.start`
  may write `ctx.secrets`.

After a login, `Login.check` must pass, and the account is named by `Login.whoami`, else by the
user, else by the plugin's default account's name. Logging in again replaces that account's login.

**The vault.** Each account's secrets are sealed in `metadata/secret.db`, one value per account and
name, with AES-256-GCM (`flnkit/vault.py`). The key is a 32-byte file at `~/.config/flnd/key`
(`%APPDATA%\flnd\key` on Windows), mode 0600. It is made the first time a secret is sealed, and it
is kept outside every home. flnd refuses a key file inside a home, since a copy of the home would
carry its key. So an unattended sync opens its logins without a password, and a copied home carries
no readable login.

**Switching.** When a job is refused on one account (its login has expired, or the account is
held), `Switcher` moves the job to the plugin's next free account, if the plugin allows switching.
An account a login error ended is marked expired for the next run.

## The Chrome extension

Chrome's own tabs can serve a plugin whose `browser` is `Browser.CHROME`, or a `Browser.ANY` plugin
whose `[plugins.<code>]` table sets `browser = "chrome"`. The extension makes that work. Its files
are in `src/extension/` (`manifest.json`, `background.js`, `page.js`, `popup.html`, `popup.js`).
[extension.md](extension.md) explains how to use it.

- **Install.** `flnd extension install` copies the extension to `~/.config/flnd/extension/unpacked`,
  and writes the native-messaging host manifest (`com.flnd.host.json`) into each Chromium browser it
  finds. It prints the steps that load the extension into Chrome. `uninstall` removes both.
- **The host.** Chrome starts `flnd host` (hidden from the help) when the extension connects. The
  host speaks Chrome's native messaging (length-prefixed JSON on standard input and output). It
  relays each message to a local socket in a 0700 folder, or to a named pipe on Windows.
- **The run's client.** A run reaches the host through that relay. `extension.connect` dials the
  relay and says `hello` with the shared token, which is kept in the extension's folder with mode
  0600 and never printed. The extension's version comes back with the answer.
- **The operations.** A run may send these: `hello`, `cookies`, `tabs.open`, `tabs.list`,
  `tabs.close`, `capture.start`, `capture.read`, `capture.stop`, `document`, `click`, `scroll`,
  `evaluate`, `storage`, `screenshot` and `media`. The extension's background page does each in one
  of the user's Chrome tabs.
- **The backend.** `src/engine/chrome.py` gives `Ctx` the same browser verbs over the extension:
  `ExtensionTabs` is a target's tab set, and `ExtensionFeed` a feed. `extension_cookies` reads the
  Chrome login's cookies for the plugin's domains.

A message is limited to 768 KiB, and a longer one is split into parts of 576 KiB. The host closes a
connection that says nothing for 5 s. `flnd extension install` waits up to 10 minutes for the
extension to connect.

## Where things are kept

Everything lives in the home folder: `.flnd` in the current folder, unless `--home` or
`$FLND_HOME` names another. Inside it:

- **`media/<code>/<value>/`**: a target's files, one folder per target. Each file is written as
  `<name>.part` first, which locks itself, so two runs may share a home and never write one file
  twice. The category folder inside a target comes from the plugin's `folder` template.
- **`metadata/`**: the databases. `flnd.db` holds what the home remembers: `files`, `targets`,
  `runs`, `failures`, `state` and `accounts`. `secret.db` holds the accounts' sealed logins (see
  [Accounts and login](#accounts-and-login)).
- **`log/`**: each run's scrubbed samples of answers a plugin couldn't read, kept 30 days
  (`LOG_DAYS`).

`flnd.db` is `flnkit.db`'s SQLite store, in WAL mode, behind one connection that every thread
shares, with explicit transactions. Its schema is version 2. A version-1 home is migrated when it is
first written: `flnd.db` is copied whole to `metadata/flnd.v1.db` first, then migrated in place.

Two rules hold every write together. A post's rows commit in one transaction once all its files are
in place. A row is written only after its file, so a crash in between leaves a whole file without a
row. The next run finds it on disk, records it and skips it. `flnd status` compares the rows with
the disk, and offers to repair the rows toward the disk, which `Flnd.check(fix=True)` does.
[storage.md](storage.md) lists every file and what its rows hold.

## Settings

`src/engine/config.py` reads and checks config.toml, in one place. Every setting is a typed key with
its own check. A bad value is a usage error that names the key, and an unknown key is a warning.

A setting's value comes from the first of these that sets it:

1. the call's argument (a flag, or a library argument),
2. the home's `config.toml`,
3. the user's `~/.config/flnd/config.toml` (`%APPDATA%\flnd\config.toml` on Windows),
4. the default.

`[defaults] exec` is read from the user's file only: a home's `exec` is ignored, with a warning.
Each plugin has a `[plugins.<code>]` table, with the engine's keys (`pace`, `limits`, `proxy`,
`browser`, `folder`, `filename`, and the rest) and the plugin's own `Settings`. `flnd config` lists
every key with its value, default and source, and edits one on a terminal. `Config.set` edits a key
with its comments kept.

## Errors

Every error is a `flnd.Error` from one hierarchy, defined in `flnkit/errors.py` and re-exported by
`src/errors.py` ([errors.md](errors.md#where-the-classes-live)):

- `UsageError`: a mistake the user can fix, raised before any request. Exit code 2.
- `LoginError`: a login the target needs, or a 401 or 403 from the plugin's own domains. Exit code
  3.
- `SiteError`, with `Blocked` (the site asked for a wait), `NotFound` (404 or 410, recorded as gone
  and never asked again), `Hidden` (it exists, but this account can't see it) and `Changed` (the
  answer can't be read; a scrubbed sample is kept).
- `NetworkError`: a timeout, a reset, DNS or TLS. It is retried.
- `DiskError`: a full disk or an unwritable folder. It stops the run. Exit code 4.
- `ToolError`: a missing tool stops the run (exit code 4). A failing tool fails its file.
- `PluginError`: a bug in a plugin, or a broken plugin. Exit code 4.
- `Stopped`: Ctrl-C. It derives from `KeyboardInterrupt`, never from `flnd.Error`, so `except
  flnd.Error` never swallows it. Exit code 130.

`str(e)` is one line, and `e.hint` says what to do. `errors.classify` turns any exception into one
of these, so anything a plugin lets escape becomes a `PluginError`, and its hint names the plugin's
author.

How a failure travels:

1. **Where it starts.** The HTTP client classifies what it meets (a status, a timeout). A plugin
   raises the same classes from `flnd.plugin`. Anything else that a listing lets escape is turned
   into one by `errors.classify`.
2. **A file's failure** stays the file's: a `Failed` event with its post and path, a row in
   `failures`, and a retry on a later run. A 404 or 410 records the file as gone.
3. **A target's failure** (its listing failed) ends that target alone: a `Failed` event, and a
   `failures` row for the whole target. A `LoginError` ends the plugin's later targets too.
4. **A run's failure**: a usage mistake (before anything is fetched), a full disk, a missing tool or
   a broken plugin stops the run. `Run` raises it to the reader. Nothing else is raised: a script
   reads `report.failed`.
5. **The command line** turns what ended the run into an exit code and prints the message and hint.
   It never prints a traceback for a `flnd.Error`. The exit codes are: 0 for success; 1 when a file
   or a target failed; 2 for a usage error; 3 when a login is needed; 4 when flnd itself stopped (a
   full disk, a missing tool, a broken plugin, or a bug); 130 on Ctrl-C.

[errors.md](errors.md) lists every error class and what to do about it.

## The kit

`flnkit/` is the standalone package, and its modules are described in [The parts](#the-parts). Its
rules:

- It imports only the standard library, third-party packages and itself. It never imports flnd.
- Each module does one general job and knows nothing of plugins, targets or flnd's schema. flnd's
  code passes those in.
- `flnkit/text.py` is pure: any program and any thread may call it. Its `redact` removes the user
  info, query and fragment from a URL, where tokens and signatures live. Its `scrub` replaces the
  values under secret-looking names. Neither may print or return a secret it was given.
- `flnkit/text.py`'s `filename` names files already saved on disk. Its output for a given input must
  not change, or a re-run would save the same file again under a new name.
- `flnkit/text.py`'s `fill` makes a name template (such as `"{date}_{post}"`) from its values. It
  splits the template at each `/` outside braces, fills each segment with `str.format`'s own parser
  (an empty field takes the separators beside it along), drops a segment that comes out empty, and
  passes each other segment through `filename`.

`flnkit/stream.py` reads HLS (`.m3u8`) and segmented DASH (`.mpd`) playlists into their segments and
fetches them at once. It is tested on its own. flnd's save does not use it: a file that names a
playlist (the `web` plugin yields one for a page's stream) fails that file with a usage error. A
DASH pair of separate video and audio files, which a plugin names directly, is joined by
`flnkit/media.py` as described above.

## Compared with similar tools

flnd was designed after reading four tools that solve the same problem: gallery-dl and yt-dlp (many
sites each), instaloader (Instagram) and twscrape (Twitter/X).

Where they differ, question by question:

- **How a site is written.** gallery-dl: an `Extractor` class per kind of URL, each with a regex
  `pattern`. yt-dlp: an `InfoExtractor` per site, with `_VALID_URL` and `_TESTS`. instaloader and
  twscrape: one package for one site. flnd: **one `Plugin` class per site**, and each kind of target
  is a decorated method, a *route*.
- **What a site produces.** gallery-dl: message tuples (`Directory`, `Url`, `Queue`). yt-dlp: info
  dicts with standard fields. instaloader and twscrape: Python objects. flnd: **typed objects**:
  `Post`, `File`, and `Target`, which hands a link to another plugin.
- **The parts of a profile.** gallery-dl: a "user" extractor that dispatches to sub-extractors,
  picked with `include`. flnd: **categories** under a route, named in the target (`/reels`).
- **Stopping at old posts.** gallery-dl: `skip = "abort:N"`. yt-dlp: `--break-on-existing`.
  instaloader and twscrape: `--fast-update`. flnd: **the engine** stops a sync after 5 saved posts
  in a row, and after the newest post it had.
- **What was downloaded.** gallery-dl: a SQLite archive of keys. yt-dlp: a text file of ids.
  instaloader and twscrape: a timestamps file. flnd: **`flnd.db`**, with real columns for files,
  targets, runs and failures.
- **After a file lands.** gallery-dl: post-processor modules on events. yt-dlp: a post-processor
  chain (ffmpeg merge, metadata, exec). flnd: **a fixed step chain**: dedupe, sidecar, unpack, then
  `--exec`.
- **Loading only what is used.** gallery-dl: imports extractor modules until a pattern matches.
  yt-dlp: a generated `lazy_extractors.py`. flnd: **a generated manifest**
  (`src/plugins/_manifest.py`), like yt-dlp's.
- **HTTP.** gallery-dl: `requests`, with browser-like headers. yt-dlp: pluggable handlers, and
  curl_cffi for `--impersonate`. instaloader and twscrape: httpx, and twscrape's pool of accounts.
  flnd: **curl_cffi on one asyncio loop**, with Chrome's TLS and HTTP/2 fingerprint.
- **Concurrency.** gallery-dl: one file at a time. yt-dlp: the fragments of one video. twscrape:
  async, with rotating accounts. flnd: **listing and downloading overlap**, with adaptive
  concurrency per host.
- **Sites that need a browser.** gallery-dl: cookies only. yt-dlp, instaloader and twscrape: no
  browser. flnd: **Playwright**, only for the sites and pages that need it.
- **Tests.** gallery-dl: live result files per extractor. yt-dlp: live `_TESTS`. flnd: **offline
  fixtures** (`flnd.testing`); live runs by hand.

What flnd took from them:

- yt-dlp's **generated manifest**, so `flnd --help` imports no plugin.
- gallery-dl's **`Queue`**, as `Target`: a search, or a post that links to another site, hands the
  target on, and the plugin that reads it downloads it.
- gallery-dl's **job types**, as readers of one event stream: the screen, `--json`, `--dry` and a
  script's loop all read the same events.
- gallery-dl's **skip-and-abort**, owned by the engine, so no plugin repeats it.
- yt-dlp's **expected errors**: an error the user can fix prints one line and a hint.

Where flnd differs on purpose:

- **One class per site, not one per kind of URL.** gallery-dl's Instagram is 17 extractor classes.
  flnd's is one class with routes and categories, so a site's shared state (its API client, its
  caches) lives in one place.
- **History is a database with columns**, not a list of keys, because `target list`, `status` and
  the automatic retry all ask questions of it.
- **Concurrency is built in.** flnd follows whole profiles, where overlapping the listing with the
  downloads is most of the speed.
- **A browser when a site demands it.** Some of flnd's sites only show their data to a logged-in
  page, so a plugin can scroll a real tab and read the site's own API responses.

Why flnd doesn't reuse their site code:

- **gallery-dl is GPL-2.0**, so importing its extractors would bind flnd's license. They are also
  synchronous and built on `requests`, while flnd's engine is one async loop.
- **yt-dlp is about one video per URL**, while flnd follows whole accounts with categories, stories
  and logins. flnd doesn't hand unknown links to it.
- The cost is real. Four of flnd's sites (Instagram, X, TikTok, Facebook) are kept up by large
  communities in those tools, and here by one person. Offline fixtures per plugin
  (`tests/test_<code>.py`) and the scrubbed samples of answers a plugin couldn't read
  ([errors.md](errors.md#samples-of-changed-answers)) are how a site change is found and fixed.

What flnd leaves out, and why:

- **Making things from the files** (slideshows, cards, albums). flnd downloads and keeps; the rest
  is another tool's job.
- **Process isolation for plugins.** A plugin needs its login's session in the same process, so a
  separate process wouldn't protect the logins. The safeguard is where plugins load from: never a
  home folder.
- **A plugin API for steps.** The chain after a file lands has one fixed order.

## Decisions

The choices that explain how flnd behaves, made with its user:

1. **One verb to run, groups to look after**: `flnd get` runs targets; `flnd target`, `flnd auth`
   and `flnd plugin` list and change what flnd keeps. There is no `download`, `follow` or `sync`.
2. **Categories in the target** (`ins:example_user/reels`, `/all`). Without them, a target takes the
   plugin's own content (`own=True`), and never other people's (tagged posts, reposts, likes) unless
   they are named.
3. **Getting follows.** `flnd get` adds a target that isn't a single post to the followed ones, so
   `flnd get` alone syncs them. `flnd target clean` forgets one.
4. **Scrolling is human-like by default.** A plugin class sets `scroll = "fast"` for a site that can
   take it. config.toml has no scroll key.
5. **Concurrency per media host starts at 8 and adapts up to 32** while throughput still rises
   ([performance.md](performance.md#concurrency-per-host)). Big files from a host that accepts
   ranges are split into up to 8 segments as they download.
6. **SQLite stores everything** in `metadata/`: `flnd.db` for the library state, and `secret.db` for
   the plugins' logins and secrets ([storage.md](storage.md)).
7. **Dedupe hard-links** a file whose bytes are already saved. A file system without hard links
   keeps both copies ([storage.md](storage.md#dedupe)).
8. **Sidecars are off** by default. `sidecars = true` turns them on.
9. **The home is `$FLND_HOME`, else `./.flnd`**, which suits cron as well as a folder per project.
10. **Plugins load from the user's folder, never from a home**, since a home is often the current
    folder, and code there may not be the user's own.
11. **Secrets are sealed in `metadata/secret.db`** under a key file kept outside every home
    (`~/.config/flnd/key`). An unattended sync opens them without a password, and a copied home
    carries no readable login.
12. **One `Plugin` class per site**, with routes and categories (above).
13. **A fixed chain of steps** after each file (above).
14. **Ruff is the one checker** (`make lint`), with no type checker. Type hints are for readers, and
    a never-awaited coroutine fails its test instead.
15. **Settings resolve in one order**: the call's argument, then the home's config.toml, then the
    user's config.toml, then the default. A bad value is a usage error naming the key.

## Where to go next

- [cli.md](cli.md): every command and flag.
- [library.md](library.md): flnd from a Python script.
- [writing-a-plugin.md](writing-a-plugin.md): adding a site, testing it, and shipping it as a
  plugin.
- [storage.md](storage.md): every file of a home, and the rows of `flnd.db`.
- [performance.md](performance.md): what is fast, what isn't, and how it is measured.
- [errors.md](errors.md): every error and its hint.
- [extension.md](extension.md): the Chrome extension, and how to connect Chrome.
