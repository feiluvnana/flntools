# Phase 8: fix bugs, speed up, simplify

**Status: on hold** (user, 2026-11-13: "phase 8 is on hold, only plan, no work"). Plan only; nothing
here is built until the user resumes it.

**A design** (2026-11-13). Beyond the [decided](#decided-user-2026-11-13) section, the
[questions](#questions-for-the-user) and the [added items](#added-defaults-adopted) take the
defaults chosen without asking; the user may reverse any. Earlier feature ideas (a viewer, more
query routes) are kept in [ideas](ideas.md) for a later phase.

**The direction changed the same day.** The user withdrew the flnd/flndm split and the
`projects/` folder, kept every `shared` module and feature, and set Phase 8 to **fix bugs, improve
performance and simplify the API**, with `shared` designed for any future tool (see
[decided](#decided-user-2026-11-13)). The earlier design of a separate download manager is gone;
what still applies without it (stopping, resume, limits, status, logging, the tagging pass) is in
[flnd](flnd.md).

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

The user's words for 1–4 are kept in [the handover](#the-users-words). The questions at the end
take their defaults.

Items 2 and 3 were withdrawn by the user later on 2026-11-13, and item 1 became: fix bugs,
improve performance and simplify the API, removing nothing ([decided](#decided-user-2026-11-13)).

## The parts

| File | What it holds |
| --- | --- |
| [bugs](bugs.md) | 67 bugs found and reproduced in `shared` and flnd, each with a file:line, a failure, a fix and its test |
| [speed](speed.md) | each module's hot paths, what is slow and why, the change, the target |
| [api](api.md) | a simpler `shared` for any caller, module by module: before and after, flnd's call sites, the snapshot; the new names from flnd's generic machinery |
| [flnd](flnd.md) | flnd's engine made smaller and plainer; the tagging pass; how flnd stops, resumes, limits, shows and logs |
| [shared](shared.md) | `shared` as a general toolkit, and proving it robust: typed errors, faults, fuzzing, threads, leaks, a size budget |
| [tests](tests.md) | a failing test per bug, the fault and leak harnesses, fuzzing, stress, counts that stand in for speed |
| [bench](bench.md) | the benchmark suite with no network, its baselines and how targets are kept |
| [sync](sync.md) | synced queries: built in Phase 7 step 10; the Phase 8 follow-ups for them |

## Where Phase 7 leaves the code

- `shared/`, about 14,300 lines with docstrings, 18 modules, 445 lines of public names in the API
  snapshot (at `af6eef3`). Phase 7 made it a black box (step 7), put the fast downloader in it
  (step 8) and added status rows, palette roles and event lines to `log` and `theme` (step 9).
- `flnd/`: about 7,200 lines on the engine side (sites aside). Step 9 added status lines; steps
  10–12 add the `Site`/`Downloader` split with query routes and the `sync` and `auth` groups, the
  mht site, and the docs diet.

## Phase 8 in numbers

| | Now | After |
| --- | --- | --- |
| bugs | 67 shown: 6 high, 29 medium, 32 low ([bugs](bugs.md)) | each fixed with its test |
| the worst slow paths | piece stalls 4.1× slower on cold connections; animated WebP opens in 340 ms; re-dating 1 s per target; `kv.clear` quadratic ([speed](speed.md)) | within 10%; < 3 ms; < 5 ms; < 2 ms |
| `shared`'s public API | 445 snapshot lines; calls with up to 13 options; two ways to do several things | about 425 lines; no call over 5 options; one way each; nothing removed ([api](api.md)) |
| flnd's engine side | 7,237 lines | about 650 fewer: 230 by plain edits, 420 moved into `shared` as general tools ([flnd](flnd.md)) |
| errors escaping as builtins | about 15 found; 104 builtin raises unchecked | 0 world errors as builtins, enforced by a scan ([shared](shared.md#2-every-error-typed)) |

## Order

Each step ends with `check` green; one logical change per commit; a module's API change is one
commit with its tests, flnd's call sites and the snapshot (AGENTS.md). **Haiku** marks a
mechanical commit for a low-cost subagent (AGENTS.md › Models).

0. **Phase 7 finished** (step 9 built; steps 10–12 to go).
1. **The six high bugs**, one commit each, test first: F1 (with F2, one fix), H1, L1, M1, M2, M3.
2. **The bench suite** and its first run on the code as it is: the starting numbers.
3. **The tagging pass** on Phase 7's code (a doc commit to
   [flnd §2](flnd.md#2-generic-machinery-into-shared)), then `cli.stopping()` with flnd's engine
   on it.
4. **Module by module through `shared`**, in this order: `http` (with download and pace), `kv`,
   `atomic`, `log` and `theme`, `media`, `worker`, `browser`, `cookies`, `selector`, `cli`,
   `prompt`, `fmt`, `style`, `crypto`, `clock`, `archive`, `bundler`. For each:
   1. its medium and low bugs, one commit each, test first;
   2. its API change, with any flnd machinery moving into it (renames and call-site updates by
      **Haiku**);
   3. its speed changes, each run through `bench --check`.
5. **flnd**: its remaining bugs (F3–F11), then [flnd §1](flnd.md#1-module-by-module)'s low-risk
   edits per module, then the medium ones.
6. **Hardening** ([shared §9](shared.md#9-order)): typed errors, faults, fuzzing, threads, leaks,
   the size budget.
7. **Synced queries**: none; built in Phase 7 step 10 ([sync](sync.md)).
8. **Docs**: README and AGENTS.md for the new names.

Bugs come first because they are cheap and some lose data (F1, F4). The bench runs before any
speed change so every change is measured against today. Hardening comes after the API changes so
no time goes into code about to change.

## Added: defaults adopted

Each item is a **default adopted; the user may reverse** it (chosen without asking, 2026-11-13).

| # | Added | Where |
| --- | --- | --- |
| A1 | **Bugs first**, each with a failing test before its fix | [bugs](bugs.md), [tests §1](tests.md#1-a-test-per-bug-first) |
| A2 | **Eight general tools in `shared`** from flnd's machinery: `cli.stopping()`, `browser.Page.responses`, `http.Client.cookies`, `log.Row.part()`, `worker.Pool.run_on_owner`, `worker.Loop`, `atomic.sweep` and `atomic.lock`, `media.verify` | [api](api.md#the-new-names-from-the-tagging-pass), [flnd §2](flnd.md#2-generic-machinery-into-shared) |
| A3 | **Renames to general, plain names**: `archive.pack`/`unpack`/`entries`, `crypto.digest`, `worker.map` and `worker.Pool(…)`, `log.Row`, `cli.main`, `media.extract_audio`, `prompt.choose_many` | [api](api.md) |
| A4 | **Records for long option lists**: public `media.Encoding` and `LEVELS`, `browser.Launch` with `attach`, `browser.Scroll` | [api](api.md) |
| A5 | **Iterators, not callbacks**: `browser`'s `on_response`/`on_request` become `responses()`/`requests()` | [api › browser](api.md#browser) |
| A6 | **The part sidecar gets a version** | [flnd §4](flnd.md#4-resume-and-the-part-format) |
| A7 | **A lock per target folder** between runs | [flnd §4](flnd.md#4-resume-and-the-part-format) |
| A8 | **The logging boundary**: `shared` writes debug only, but for waits it imposes and misuse it sees | [flnd §7](flnd.md#7-logging-boundaries) |
| A9 | **Typed errors by a scan** and `raises.txt`, which may only shrink | [shared §2](shared.md#2-every-error-typed) |
| A10 | **`threads.txt`**, stress tests, the leak check, fault helpers | [shared §5–6](shared.md#5-threads), [tests](tests.md) |
| A11 | **A size budget** per module, set from Phase 8's result | [shared §7](shared.md#7-a-size-budget-per-module-added) |
| A12 | **Counts in `check`** that stand in for speed | [tests §6](tests.md#6-counts-that-stand-in-for-speed) |
| A13 | **The bench suite**: ratios against same-run baselines, bounds in process, `bench --check` before hot-path commits | [bench](bench.md) |
| A14 | **`shared`'s tests import no flnd**, checked by `test_layers` | [tests §2](tests.md#2-shared-stands-alone) |

## Decided (user, 2026-11-13)

- **Minimal `shared`**: Phase 8 may remove `shared` API neither flnd nor flndm uses. `shared` is
  **robust as a framework, with minimal features**. This replaces Phase 7's "unused is not dead"
  from Phase 8 on.
- **flndm is a library** flnd imports; no daemon or subprocess mode for now.
- **`sync`'s target half moves into Phase 7's step 10** (`flnd sync`, `sync ls`, `sync toggle`,
  `download --sync`); Phase 8 adds synced queries (`find --sync`). (superseded: see **Query
  targets, not find** below)
- **No licence files**: flnd is the user's private tool, never distributed, so the bundled GPL
  ffmpeg needs no licence or source offer.
- **Query targets, not find** (user, 2026-11-13): "So should only have download. Now download can
  choose a query target. Remove finder and call it a day." and "delete finder, make everything
  downloader, downloader can have multiple kind of target". Synced queries moved to Phase 7 step 10,
  so [sync](sync.md) keeps only Phase 8's follow-ups.

Changed by the user later on 2026-11-13, replacing what the bullets above say where they differ:

- **No flndm, no `projects/` folder, no workspace.** One flnd and one `shared`; the layout stays as
  it is. "I dont think I want to separate into flndm or make project folder anymore." This
  withdraws the earlier "flndm is a library" decision and items 2 and 3 of what the user asked.
- **No `shared` module is removed, and none of its features**: `archive`, `media`'s verbs,
  `worker`'s options and the rest stay. "I dont think remove archive,... is ok, but maybe just fix
  bug, improve performance and simplify the api of shared." This **reverses** the earlier
  "Minimal `shared`: Phase 8 may remove `shared` API neither flnd nor flndm uses".
- **Phase 8 is: fix bugs, improve performance, simplify the API**, with every module in `shared`
  and flnd made as small and plain as it can be while keeping what it does. "But simplify all
  module to bare minimal."
- **`shared` serves future tools, not only flnd**: its API is designed for a general caller, no flnd
  idea leaks into it, and its tests and docs stand without flnd. "shared exists because in future,
  maybe other module need it, not just flnd. So keep that in my for any design decision."
- **No questions; defaults are chosen**: "Dont ask me anything, fully automated." Each open
  question and added item takes its recommendation as the default.
- **Phase 8 is on hold**: "Just finish phase 7, phase 8 is on hold, only plan, no work."

## Questions for the user

The user said not to ask (2026-11-13), so each recommendation is now the chosen default, which the
user may reverse. The earlier draft's questions on `shared`'s place, shrink in flndm, packaging and
the name `flndm` no longer apply.

1. **Order**: bugs first, then `shared` module by module, then flnd, hardening?
   **Default (chosen without asking, 2026-11-13):** yes, as [above](#order). Cheap fixes that stop
   data loss come before anything that reshapes code.
2. **History paths**: the spec says an entry's path is relative to `media/`; the code stores it as
   given (relative to the working folder, or absolute), so moving `.flnd/` breaks entries.
   **Default (chosen without asking, 2026-11-13):** write new entries relative to `media/` and read
   both forms, with no rewrite of old entries (the no-migration rule).
3. **Code for pre-Phase-5 data**: moving `.flnd/cookies/` files into the vault (45 lines) and
   `_unrecorded` for folders without a history (13 lines).
   **Default (chosen without asking, 2026-11-13):** remove both: Phase 7's layout step already left
   old `.flnd/` folders behind.
4. **Two runs on one target** (a scheduled `sync` during a `download`):
   **Default (chosen without asking, 2026-11-13):** the second run says another run holds the target
   and goes on with the next, and repair skips a held folder; the alternative, waiting, can hang a
   cron job behind a long download.
5. **`kv` writes with `synchronous=NORMAL`**: 4× faster writes; WAL keeps the store whole, and a
   power cut may lose the last commits (a history entry lost means one file downloaded again).
   **Default (chosen without asking, 2026-11-13):** yes.
6. **Re-dating memory**: a file still undated after asking is asked again only after 30 days,
   instead of every sync (up to 27 minutes of paced requests a sync for 2,000 files).
   **Default (chosen without asking, 2026-11-13):** yes.
7. **`media`'s size cap**: the docs say the longer side; the code and its tests cap the shorter (as
   "1080p" does).
   **Default (chosen without asking, 2026-11-13):** keep the code, fix the docs, so a level means
   what video people expect; a panorama keeps its length.
8. **`media.compress(codec=None)`** keeps the source's codec when the format can write it, instead
   of the container's default (H.264).
   **Default (chosen without asking, 2026-11-13):** yes: "compress in its own format" then means its
   own codec too, for any caller, and flnd's `OWN_CODECS` table goes.
9. **A loopback server for download benchmarks**, while unit tests stay on `MockTransport`.
   **Default (chosen without asking, 2026-11-13):** allow it for `dev.py bench` only, on
   `127.0.0.1`, never in `check`.
10. **The renames** (A3): `archive.zip`/`unzip`/`ls` → `pack`/`unpack`/`entries`, the five hashes →
    `crypto.digest`, `as_ordered` → `worker.map`, `Progress` → `Row`, `cli.run` → `cli.main`.
    **Default (chosen without asking, 2026-11-13):** take them, one commit per module, no alias
    left: each old name either shadows a builtin, means two things, or isn't what callers call it.

## Risks

- **A bug fix that changes behaviour a user relies on**: F1's fix makes `--time` runs list from
  the newest again; each fix's test states the new behaviour, and the run log records it.
- **API renames across `shared` and flnd at once**: one commit per module, with the snapshot diff
  as the review and the full `check`; no aliases.
- **Moving flnd machinery into `shared`** could bake in flnd's assumptions; each new name gets
  `shared`-only tests with generic examples before flnd moves onto it.
- **Speed changes that are slower elsewhere**: every hot-path commit runs the whole bench.
- **Phase 7 still being built on `main`**: Phase 8 starts after step 12; the audit's line numbers
  are redone then.

## Left out

The flnd/flndm split, `projects/` and a workspace (withdrawn); removing any `shared` module or
feature (reversed); a journal or persistent queue; per-run speed caps; a per-disk limit (unless a
hand run shows 15%); the features in [ideas](ideas.md).

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

Later the same day, on the direction (to this session, through the coordinator):

> I dont think I want to separate into flndm or make project folder anymore. But simplify all
> module to bare minimal. I dont think remove archive,... is ok, but maybe just fix bug, improve
> performance and simplify the api of shared.

> shared exists because in future, maybe other module need it, not just flnd. So keep that in my
> for any design decision.
