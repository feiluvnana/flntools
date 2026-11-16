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
   flnd/cli.py ───┤  the command line (cyclopts): flags in, progress lines out
   flnd/__init__  │  a few library functions over the engine
                  ▼
   flnd/engine.py    Engine: resolves targets, checks options, runs each target in turn
        │
        ├── flnd/targets.py     targets.db: each target's settings, cursors and failures
        ├── flnd/options.py     Options: what one target downloads; RunShared: what a run shares
        ├── flnd/site.py        SourceRun and TargetSite: what a source acts through
        │       └── flnd/common/  auth (logins), net (HTTP and downloads), paths (the home)
        └── flnd/job.py         one target: list its posts, plan its files, download, record
                │
                └── flnd/sources/<code>.py   one class per site (flnd/source.py is their API)

   flnd/_*.py   the private toolkit the rest is built on: http, browser, db, files, loop, ui,
             cookies, vault, unpack, html, prompt, cli, text, time and a few more
```

| part | what it holds | who calls it |
| --- | --- | --- |
| `flnd/cli.py` | the commands: `download`, `sync`, `target …`, `auth …` | the `flnd` script |
| `flnd/__init__.py` | `source`, `resolve`, `open`, `download`, and the engine's types | scripts |
| `flnd/engine.py` | `Engine`: every command as a method, and target resolution | the CLI, scripts |
| `flnd/job.py` | one target's run: listing, planning, the download pool, history rows | the engine |
| `flnd/site.py` | `SourceRun` (a source's cookies, clients and browser for a run) and `TargetSite` (the `Site` a route sees) | the engine, the job |
| `flnd/source.py` | the source API: `Source`, `@target`, `Post`, `File`, `Site`, `Target`, errors | every source |
| `flnd/sources/` | the seven built-in sources and their shared helpers (`sources/common/`) | the engine, through `SOURCES` |
| `flnd/targets.py` | the rows of `targets.db`, and the checks of `--from` before any work | the engine, the job |
| `flnd/options.py` | `Options` and `RunShared` | the engine, the job |
| `flnd/common/` | `auth` (the login vault), `net` (the HTTP client and file downloads), `paths` (the home's layout and content sniffing) | the engine, the site, the job |
| `flnd/dry.py` | a contributor's tool: prints the files one target would save | `make try` |
| `flnd/_*.py` | the private toolkit: HTTP over httpx (`_http`), Playwright (`_browser`), SQLite stores (`_db`), atomic files (`_files`), a thread pool (`_loop`), the terminal UI (`_ui`) | everything above |

The rule between them: a source imports `flnd.source`, its helpers in `flnd/sources/common/`,
the toolkit (`flnd._*`) and the standard library, and nothing of the engine. The engine imports the sources
only through `flnd.sources.SOURCES`.

## One download, start to finish

`flnd download ins:example_user --from posts --sync`:

1. **The CLI** (`flnd/cli.py`) parses the flags with cyclopts and calls
   `Engine(home=Path(".flnd")).download(["ins:example_user"], categories=["posts"], sync=True)`.
   With no targets on the command line, it reads them from standard input, one per line.
2. **Resolving.** `engine.resolve` splits the text at `:` (or `?` for a query), finds the source
   by its code (or a bare link by its `domains`), and calls `Source.resolve`. The first route
   whose pattern matches gives `Target("ins", "user", "example_user")`. A short link no route
   reads is followed once with a HEAD request, and the link it leads to is resolved instead.
3. **Checking.** Before any request, `options.categories_by_source` and `targets.validate` check
   `--from` against the target's route, and every folder template against its fields. A mistake
   is a usage error (exit code 2).
4. **Recording the target.** `targets.record` writes the target's row in `targets.db`, with
   `sync` on because of `--sync`, keeping its cursors and failures from earlier runs.
5. **Grouping by source.** The engine runs the targets of one source together, one after
   another, inside one `source_run`: the source's saved cookies are read from the vault, its
   HTTP client is made (paced to `Source.pace` on its own domains), and its `connect` hook runs
   (Telegram's client). A source that `requires_login` and has none fails here, for each of its
   targets; any other source without a login warns and goes on with public access.
6. **One target** (`job.download`). The job takes a lock for the target's folder (in
   `metadata/locks/`), removes stale stage files an interrupted run left there, and queues the
   files that failed last time, so they are tried first.
7. **Listing.** The route runs and returns the profile; then each wanted category yields its
   posts, page by page. A category whose listing was interrupted resumes from its saved cursor,
   then catches up from the newest post. `--time` drops posts outside the range; after 5 posts
   in a row older than the range, a `newest_first` category stops.
8. **Planning each post** (`Ledger.take`). Each file is given its path (from the source's
   `folder` and `filename` templates) and its history key (`<code>:<key>`). A file whose key is
   in the history is skipped. On a sync, 5 saved posts in a row end a `newest_first` category.
   A lazy post's files are listed only once the post is known to be new.
9. **Downloading** (`Downloads`). Each new file goes to a `flnd._loop.Pool` of
   `Source.concurrency` threads (8 by default). The pool's queue is bounded, so the listing waits
   when downloads fall behind. `common/net.download` streams the file into a stage file, tries
   mirrors in order, merges a DASH video with its audio through ffmpeg, checks the content (a
   web page instead of a photo is a failure, not a file) and gives it the extension its content
   calls for. The stage is then renamed onto the real name.
10. **Finishing a file.** The file's modified time is set to its post's date (at noon UTC), and
    its history row is written: path, size, post id, category and date. The same file in two
    categories is saved once and linked from the second (a relative symlink, or a hard link where
    symlinks aren't allowed).
11. **Ending the target.** The job saves the categories it listed to their end, the cursors of
    those it didn't, and the files that failed, all in `targets.db`, then prints one summary line.
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
  thread writes its file's stage, renames it, sets its date and writes its history row; the job's
  `_writing` lock guards the run's own lists of written and failed files, and SQLite serializes
  the history writes.
- **The browser thread.** Playwright runs on one thread of its own (`site._BrowserPool`), shared by
  every source of a run, so one Chrome window serves them all. A source's calls on `site.page`
  are forwarded to that thread and wait for the answer.
- **An event loop for async sources.** A source with an async SDK (Telegram's Telethon) runs its
  coroutines on its own loop through `SourceRun.run_async`.
- **Ctrl-C** is handled by `flnd._cli.stopping`: the first one sets `stop.soon` (stop listing,
  let running files finish), the second sets `stop.now` (stop at once, keep the stage files).

## Where things are kept

Everything lives in the home folder, `.flnd` in the current folder:
`media/<code>/<target>/<category>/…` for the files, and `metadata/` for the history, the
targets, each source's state and the login vault. [storage.md](storage.md) lists every file and
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
