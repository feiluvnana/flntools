# Phase 8: draft

**A draft, not a plan** (user, 2026-11-13). Nothing here is decided. It becomes a plan once the
user answers the questions at the end, after Phase 7 is built. Earlier feature ideas (watches, a
viewer, more finders) are kept in [ideas](ideas.md) for a later phase.

## What the user asked

1. **Harden `shared`**, so it is absolutely robust and minimal.
2. **Separate flnd and flndm**:
   - **flnd** is only a listing engine: listing what to download, and listing a query's results.
   - **flndm** is a download and IO manager built for extreme performance. It takes requests and
     carries them out as efficiently as possible. flnd calls flndm as part of its own flow.
   - **flndm is general**: any program can use it, not only flnd.
3. **Both live in a `projects/` folder**: `projects/flnd`, `projects/flndm`.
4. **No `licenses/` folder.** Done 2026-11-13: removed in `1e90b56` (the deletion was swept into
   that commit) and `ca00e6b` (the bundle, its tests and the `packaging` dev dependency).
5. **`sync` as a group, opt-in** (the user's design, 2026-11-13):
   - `sync` does nothing by default; `download --sync` and `find --sync` add targets and queries to it;
   - `flnd sync` runs what is marked, `flnd sync ls` lists every known target and query with its sync
     status (a target downloaded without `--sync` is listed with sync off), and `flnd sync toggle
     <targets…>` marks or unmarks;
   - it replaces Phase 7's `target` group and the earlier "watches" idea.

The user's words for 1–4 are kept in [the handover](#the-users-words). Everything else in this file
is a proposal until the user answers the questions at the end.

## Where Phase 7 leaves the code

- `shared/`, about 12,400 lines with docstrings: the toolkit both sides use (`http`, `kv`,
  `atomic`, `media`, `browser`, `log`, `cli`, …). Phase 7 step 7 ([shared](../phase-7/shared.md))
  makes it a black box with no `shared._` imports.
- `flnd/`: sites, downloaders, finders, and **one job** that both listing and finding feed. The job
  names each file, dedupes by key, then hands the file to `net.download` (on `shared.http`) and to
  the postprocessors (shrink, the date as the modified time, links). It writes the history last.
- Phase 7 step 8 ([speed](../phase-7/speed.md)) puts the fast downloader in `shared.http`: pieces
  pulled from a queue, host caps, resume, hash and free-space checks.

So most of flndm already exists by the end of Phase 7. It sits in `shared.http`, `flnd/common/net.py`
and the download half of the job. Phase 8 moves it out behind a request API, and leaves flnd
listing only.

## Layout after

```
projects/
  flnd/                  the listing engine: sites, downloaders, finders, the CLI `flnd`
    flnd/
    tests/
    pyproject.toml
  flndm/                 the download and IO manager: a library, and the CLI `flndm`
    flndm/
    tests/
    pyproject.toml
shared/                  the toolkit both use (or projects/shared: question 1)
plan/  scripts/  AGENTS.md  Makefile  pyproject.toml (a uv workspace over the three)
```

- **A uv workspace**: one `uv.lock` and one `.venv`. Each project has its own `pyproject.toml`,
  dependencies and entry point, and `check` runs over all of them.
- **flnd depends on flndm and shared; flndm depends on shared only, never on flnd.** A test
  enforces it, as `tests/test_names.py` enforces the source import rule today.

## flndm: the design to decide

### What a request is

A request says what to fetch and where it goes, nothing about sites:

```python
Request(
    urls=("https://cdn.example.com/a.mp4", "https://cdn2.example.com/a.mp4"),   # mirrors, best first
    dest=Path("media/ins/example_user/posts/2026-01-02_C1abc.mp4"),
    headers={"Referer": "https://example.com/"},                                 # per request; no cookie jar of a site
    audio=None,                    # a second stream to merge (DASH)
    size=None, sha256=None,        # what the caller knows, checked when given
    kind="video",                  # checked as media, or None for any file
    mtime=None,                    # set once the file is in place
    key="ins:C1abc_0",             # the caller's id, given back in every event
    priority=0,
)
```

### What comes back

flndm gives back events, never callbacks into the caller (the writing rules forbid a callback from
a producer into its consumer): `Started`, `Progress(done, total)`, `Placed(path, size, headers)`,
`Failed(error)`, `Expired(status)`. `Expired` is a 403 or 410 on every mirror. flnd answers it by
listing the post again and sending a new request with the same `key`, and flndm resumes the part.

### How flnd calls it

**As a library** (user, 2026-11-13). The other two shapes are kept below as history.

1. **In process, as a library.** `with flndm.Manager(limits) as manager:` then
   `manager.submit(request)` and `for event in manager.events():`. It costs nothing to call, and
   one process holds all the state.
2. **A daemon** that several flnd runs and other programs share, over a local socket. It can keep
   global host caps and one queue for everything, but it brings a lifecycle, authentication and a
   protocol to keep stable.
3. **A subprocess per run** (`flndm serve` on stdin and stdout, JSON lines). It isolates crashes,
   but each run pays a start-up and the line protocol.

The library comes first in every case. A daemon or a subprocess wraps it later if wanted.

### What moves into flndm

- From `shared.http`: the downloader (pieces, mirrors, resume, `If-Range`, host caps, back-off for
  CDNs, HTTP/2 for small files).
- From `flnd/common/net.py` and the job: the pool, the encode queue, the media check, naming by
  content, atomic placement, the modified time, the hard link and symlink fallback, the free-space check.
- **IO**: one writer per disk, preallocation, `fsync` policy, rename onto the final name, and Windows
  replace retries.
- **Postprocessing as generic steps**: `shrink` (a `shared.media` call) and `mtime` become flndm
  steps a request names. Question 3 asks whether shrink belongs there.
- **Its own CLI**: `flndm get <url>… [-o dest] [-j N]` and `flndm get -i requests.jsonl`, so it is
  useful without flnd. It is also its first test of being general.

### What stays in flnd

Sites, downloaders, finders, the listing memory and dates, file names and keys, `history.db` and
`targets.db`, logins (flnd builds each request's headers), sync's repair decisions, and the CLI
`flnd`. **flnd opens no file for writing except its stores and run logs**; flndm writes every
media byte.

### "Extreme efficiency": what is measured

A benchmark suite runs on a local server, so it is repeatable without the network: 1,000 small
photos, 20 large videos, a slow host, a host that refuses after N connections, and a cut-off
connection. It measures throughput, the CPU time and memory per GB, the time to the first byte
saved, and the overhead against a plain `curl`/aria2c baseline. Phase 8 sets targets from the first
measurement (question 5) and keeps them in CI as a budget, as the API snapshot keeps names.

## Hardening `shared`

**Robust**: each module proves it survives what can go wrong.
- **Faults, injected in tests**: a full disk, a read-only folder, a file removed mid-write, a
  `kill -9` during an atomic write (a child process), a dropped connection at every phase of a
  response, a slow-loris server, a clock that jumps, two processes writing one `kv` store.
- **Hostile input, fuzzed** with property-based tests (Hypothesis, a dev dependency): cookie files,
  HTTP dates and ranges, `Retry-After`, archive member paths (zip slip, symlinks), JSON and HTML
  parsers, file names on all three platforms.
- **Every error typed**: each module's exceptions sit under its `Error`, and no bare `OSError` or
  `ValueError` leaks without a `shared` type around it (a test walks each public function's raises).
- **Threads**: every public object says whether it is thread-safe. A stress test runs the live area,
  `kv` and the pool from many threads.
- **Resources**: no file handle, thread or child process outlives its `with`. Tests check it with
  the process's open files and threads before and after.

**Minimal**: `shared` holds only what flnd or flndm uses, with one spelling each.
- After the split, each `shared` module is used by flnd, flndm or both. Phase 7's rule ("unused
  doesn't mean dead; keep shared's API") would keep the rest. Question 2 asks whether Phase 8's
  "minimal" replaces that rule.
- Modules only one project uses move into that project: `bundler`, `cli` and `prompt` may become
  flnd's, and `http`'s downloader becomes flndm's.
- A size budget per module, and the API snapshot, show every addition in review.

**Absorbing what feels bloated** (user, 2026-11-13): hardening `shared` starts by finding where
flnd (and flndm, once it exists) builds generic machinery itself, so that `shared` takes that case
over behind a **simple outer API** and the caller shrinks to a call. Robust framework, minimal
features: `shared` grows only where it makes a caller simple, and loses what no caller uses.

*How it is found.* A pass over flnd tags every function as **site knowledge** (how a site answers),
**listing logic** (targets, categories, queries, names, keys, dates, sync), or **generic machinery**
(pacing, pools, retries, watching responses, stores, files). Site knowledge and listing logic stay
in flnd. Generic machinery moves to `shared` when both hold:
1. the move turns the caller's code into one or two calls with sensible defaults; and
2. it serves two callers, or removes at least 30 lines from the one it serves.

Anything else stays where it is, however generic it looks.

*Candidates already seen* (Phase 7's audits of the code; each to be measured again after Phase 7):

| flnd builds today | `shared` would offer | What the caller loses |
| --- | --- | --- |
| a `Pacer` and a `Backoff` per site, wired through `made[0]` lambdas, reading rate headers and budgets (`pace.py`, `net.py`, `site.py`) | `http.Client(pace=Pace(floor, budgets))`: rate headers, budgets, a learned gap and 429 back-off per host, inside the client | the pace wiring, `Transport`, `patiently`, `retrying` |
| a bounded download pool with room-waiting, pending counts, a condition and a refresh queue (`job.py`'s `_room`, `_pending`, `_running`, `_refreshes`) | `worker.Pool(…, bound=n)` whose `submit` waits for room and honours a `stop` event | about half of `Job`'s threading state |
| JSON from a page's responses, scrolling until a request can be replayed, then replaying it page by page (`sites/common/watch.py`, `replay.py`, ins and fbk feed readers) | `browser.Page.responses(match)`, an iterator of JSON bodies, and `Page.replay(request, cursor=…)` | the site-agnostic half of `watch`, `replay` and both feed readers |
| a site's cookies copied into flnd's own client before each fetch (`site.py`'s `net.cookie_jar(...)`) | one cookie jar shared live by a `Browser` and an `http.Client` | the copying, and cookies going stale between them |
| "remember this refusal for a day" in a site's state (ins's `_refused`/`_refuse`), a lookup cache for a day (finders) | `kv.Store.get(key, max_age=…)` / `set(key, value, ttl=…)` | hand-rolled timestamps and comparisons |
| leftover `.part` files found and removed by `repair` | `atomic.sweep(folder)`, removing what `atomic` itself leaves behind | repair's own part patterns |
| `_sleep`, `_clock`, `_random` seams faked in three modules for tests | one injectable clock for `shared` (`shared.clock`), used by `http`, `worker` and `log` | three sets of seams |
| a status row per target with counts redrawn from many threads (Phase 7's output step) | `log.status(label, fields=…)`, a row whose counts any thread may bump | the count bookkeeping around the row |

What stays in flnd, however generic it looks: the name and key formats (`fill`), `day()`'s reading
of a site's dates, the listing memory, sync's decisions, the query language's planning. They are
listing logic, and moving them would make `shared` know about listing.

## `sync` as a group: the design

```
flnd download <targets…> [--sync]              # --sync: also keep these up to date
flnd find <site> '<query>' [--sync]            # --sync: keep finding, and download what's new
flnd sync [-v]                                 # update everything marked; nothing else
flnd sync ls                                   # every known target and query, synced or not
flnd sync toggle <targets or query ids…>       # flip each one; prints its new state
```

- **Off by default.** A target downloaded without `--sync` is still known (settings, history) and
  listed with sync off; `sync` leaves it alone.
- **`sync ls`** lists targets and queries in one table: kind, the target or `site 'query'`, an id
  (`q1`, `q2` for queries; a target is its own id), sync on or off, saved settings (`--shrink`),
  files and size, last sync, each category's listing state. It reads the stores and asks no site.
- **`sync toggle`** flips each entry and prints its new state. Nothing is deleted: settings,
  history and cursors stay, so turning an entry on again resumes it.
- **`flnd sync`** runs the entries marked on: targets (repair, then fetch, as Phase 7's sync does),
  then queries (the finder runs; results not seen before have their targets downloaded, with sync
  on, so their new chapters are fetched on later syncs). With nothing marked, it says so and exits 0.
- **A synced query remembers the result ids it has seen**, so a result is downloaded once. Its first
  run only remembers, unless `find --sync now`, so a broad query never starts a thousand downloads by
  surprise; `limit` caps each run.
- **Stores**: `targets.db` rows gain `sync` and a kind; a query row holds its site, query text,
  seen ids and last run. A row written before this, with no `sync` field, counts as on, as it was
  synced then; nothing is rewritten.
- **`shared.cli`** gains a group's default command (`app.group("sync", default=fn)`), so `flnd
  sync` and `flnd sync ls` both work; today a group can't also run by itself.
- With flndm (above), `sync` stays flnd's: it decides what to list and asks flndm for the files.

## A proposed order

1. **Layout**: `projects/flnd`, a uv workspace, `check` over it. Purely a move, so Haiku does it,
   with the import rule test.
2. **Synced queries** ([above](#sync-as-a-group-the-design)): `find --sync`; the target half is built
   in Phase 7's step 10.
3. **flndm as a library inside the workspace**: the request and event API, then moving the
   downloader, IO and postprocessing out of flnd one piece at a time, with flnd calling it.
   `check` stays green at each move.
4. **flndm's CLI and the benchmark suite**, with its first targets.
5. **Hardening `shared`**: first the pass that absorbs flnd's generic machinery behind simple calls
   (above); then faults, fuzzing, typed errors, threads and resources, module by module; then
   minimal, removing what neither project uses.

## Decided (user, 2026-11-13)

- **Minimal `shared`**: Phase 8 may remove `shared` API neither flnd nor flndm uses. `shared` is
  **robust as a framework, with minimal features**. This replaces Phase 7's "unused is not dead"
  from Phase 8 on.
- **flndm is a library** flnd imports; no daemon or subprocess mode for now.
- **`sync`'s target half moves into Phase 7's step 10** (`flnd sync`, `sync ls`, `sync toggle`,
  `download --sync`); Phase 8 adds synced queries (`find --sync`).
- **No licence files**: flnd is the user's private tool, never distributed, so the bundled GPL
  ffmpeg needs no licence or source offer.

## Questions for the user

1. **`shared`'s place**: stay at the root, move to `projects/shared`, or split between flnd and
   flndm, so that no shared toolkit is left?
2. **Shrink and other media steps**: flndm steps a request names (proposed), or flnd's, run after
   flndm places the file?
3. **Performance targets**: set from the first benchmark (proposed), or numbers you already have
   in mind, such as matching aria2c on many small files?
4. **Packaging**: one bundle holding both commands, or two? (Private use: no publishing.)
5. **Names**: `flndm` for both the package and the command?
6. **A synced query's first run**: only remember what it finds (proposed), or download at once?

## The user's words

As the user sent them on 2026-11-13 (to the Phase 7 session, which handed them over):

> And add a draft phase 8. Phase 8 will be focused on harden shared so it absolutely robust and
> minimal, make flndm and flnd separated. flnd. will just be a listing engine to listing download,
> listing query. flndm. a extreme performance download and io manager. It will accept the request
> from flnd and execute it in extreme efficiency and optimization. (flnd will invoked flnmd in it
> flow) make flndm a general download manager, so it can use with everything, not just flnd. flndm
> and flnd should be in projects folder and remove licenses folder for me.

And on `sync` (to this session):

> sync will default not doing anything (find and download will have the --sync option to add it to
> sync). Now sync api will have `flnd sync`, `flnd sync ls`, `flnd sync toggle <targets>` (make
> target being track by sync or not) instead of watch, target api. Moreover, maybe the targets also
> in the sync listing (but with sync status = 0).
