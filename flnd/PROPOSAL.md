# Proposal: a leaner flnd

Status: approved 2026-11-23 with every recommendation (D1–D9). Building step by step.

## Goal
Cut flnd's source by about half, from 42k lines to about 21k, and its tests from 33k to about
22k. Every user-facing feature in SPEC.md stays: the commands, the plugins, sync and early
stops, dedupe, logins and the vault, the extension, the Python API, Windows support.

## Where the lines are now

| Area | Lines | Code only |
|---|---:|---:|
| `src/engine/` | 17,000 | 10,400 |
| `src/plugins/` + `plugin.py`, `testing.py`, `_library.py`, `cli.py` | 12,650 | 5,300 |
| `flnkit/` | 9,200 | about 5,500 |
| `src/ui/` | 3,070 | about 2,000 |
| Total source | 42,000 | about 23,000 |
| `tests/` (with `tests/bench`) | 33,000 | |

About 45% of the source is docstrings, comments and blank lines. Code alone can shrink by
about a third. Reaching half also needs thinner docstrings, which the rule "docs live in
flnddocs, code comments are short" already asks for.

## What makes it big
1. **Things we built that a dependency already does.**
   - `flnkit/net.py` drives curl_cffi's internal handles by hand (that is why curl_cffi is pinned
     to 0.17). `curl_cffi.AsyncSession` already does sessions, redirects, streaming, proxies
     and impersonation.
   - `engine/jobs.py` has 8 pool and limit classes; asyncio semaphores do the same job.
   - `run._Channel` and `ctx.Bridge` re-make `asyncio.Queue` and
     `run_coroutine_threadsafe`.
   - Config validators and the row converters re-make what msgspec typed structs do.
2. **One idea written in several places.**
   - Folder and name logic: `config.py`, `crawl.py` and `store.py`.
   - Counting: `run.Counts/Tally/Flight`, `crawl._Progress`, `_engine` totals, and `events`
     snapshot structs that mirror the live ones.
   - Rows: the `store` row structs, their converters, and `events.Record/Failure/...`.
   - Two tab and feed stacks: `ctx.TargetTabs/_Feed` (Playwright) and
     `chrome.ExtensionTabs/...` (extension).
   - Segmented fetch, split between `jobs.Segments` and `save._Split`.
   - Retry and error classification, spread over jobs, crawl, save and ctx.
   - In plugins: the reload-on-block loop (7 copies), Meta GraphQL guards (ins, thr, fbk),
     `best_file` (5 copies), date parsing (6 copies), pacing delays (12+). `thr` is about 70%
     the same as `ins`.
   - `testing.FakeCtx` is a second, full copy of `Ctx`.
3. **Dead code and shims.**
   - `flnkit/stream.py` (641 lines, plus 401 lines of tests) is used by nothing in `src/`.
   - `chrome.ExtensionTabs.take_buffer` is never called.
   - The `OLD_NAMES` plugin API 1 refusal.
   - The store v1 to v2 migration (about 150 lines).
4. **Ceremony.**
   - flnkit is a second package with its own public API snapshot, though only flnd uses it.
   - `plugins/_manifest.py` is 775 lines of generated data saved as Python.
   - `__init__.py` lists each name three times.
   - `_library.py` has five forwarding group classes.
   - The TUI (forms, lists, run screen; 1,930 lines) is hand-built on textual, with a termios
     fallback.

## Target layout

```text
src/                         the package flnd                         now -> target
  __init__.py  errors.py  __main__.py                                   240 ->    150
  cli.py        command table over Flnd                               1,197 ->    800
  _library.py   Flnd; groups kept as thin attribute objects             891 ->    550
  plugin.py     public plugin API (no OLD_NAMES, table validators)    1,965 ->  1,300
  plugkit.py    NEW public helpers: reloading, pace, to_day,              0 ->    350
                best_stream, image_ext, meta_guard, Profile
  testing.py    test kit: the real Ctx over a fake transport          1,185 ->    600
  plugins/      ins+thr share a meta base; manifest becomes JSON      7,170 ->  4,300
  engine/
    model.py    events, counts, rows: one struct per concept            750 ->    450
    run.py      Run, Stop, Ctrl-C stages, Queue event stream          1,130 ->    650
    sched.py    semaphores, host caps, token bucket, one retry, hold  1,350 ->    700
    layout.py   name templates, folder spec, target roots               350 ->    220
    config.py   msgspec-typed config, tomlkit edit                      820 ->    480
    store.py    Store, check/repair, erase                            1,545 ->    900
    fetch.py    Saver, segmented fetch, unpack, exec, sidecar, link   1,610 ->  1,000
    crawl.py    plan, sync, early stops, Ledger; queue stages         2,650 ->  1,400
    ctx.py      Ctx, PluginRun, one Tab protocol, 2 backends          2,210 ->  1,250
    engine.py   Engine + runner                                       1,950 ->  1,000
    auth.py  resolve.py  plugins.py                                   2,060 ->  1,300
    extension.py  native host, one transport                          1,245 ->    700
  ui/           rich.live run screen, plain prompts, simple lists     3,070 ->  1,400
  kit/          was flnkit; same import rule, no separate package     9,200 ->  4,100
    net.py      AsyncSession wrapper: resume, meter, retry-after     1,796 ->    550
    browser.py  Playwright: launch, Page, scroll, Capture, Feed, Tabs 2,872 ->  1,300
    files.py    parts, sniff, tools, archives                         1,434 ->    650
    others      cookies, vault, db, text, clock, html, dig, errors    2,457 ->  1,600
    stream.py   deleted                                                 641 ->      0
                                                                     ------    ------
                                                                     42,000 -> ~21,500
```

## Rules for the rebuild
- Each step leaves `make check` green, and keeps behaviour tests while dropping
  internals-shape tests.
- Any step that touches speed runs `make bench ARGS=--gate` before its commit. The net and
  browser steps re-record a baseline only after the user agrees.
- Tests we keep, because they test behaviour: `test_crawl` (sync and early stops), `test_save`,
  `test_store`, `test_sync`, `test_archive` (zip-slip, size limits, passwords), part files and
  resume, cookie parsers, the vault round trip and secret hiding, net retry, Retry-After,
  Range and redaction, the extension framing, `test_fs` (Windows names and renames), and every
  plugin test.
- Tests we delete or rewrite, because they test internals: `test_stream`; frame-exact TUI tests
  (rewritten at about 400 lines); curl-handle tests in `test_net`; flnkit's API and name
  snapshots; most of `tests/bench` other than net, e2e and startup.
- The public Python API (`tests/data/api.txt`) changes only where a decision below says so.
  Read the `make api` diff at every step.

## Steps (serial, one commit each or a few)
0. **Free cuts.** Delete `flnkit/stream.py`, `take_buffer`, `OLD_NAMES` and the dead
   `TYPE_CHECKING` block. Turn `_manifest.py` into JSON. Generate the `__init__` name lists.
   About 2,100 lines, low risk.
1. **net on AsyncSession.** Rewrite `flnkit/net.py` over `curl_cffi.AsyncSession`. Keep
   resume (Range, If-Range), Retry-After, redirect userinfo stripping, scrubbing, the meter and
   the `_Sink` writer. The fake transport stays for the tests. Unpin curl_cffi. Bench gate.
2. **The rest of flnkit.** Archives through one 7z path plus the zip-slip and limits guard.
   Smaller `Part`. Shorter `Tool`, `html`, `dig`, `vault` and `clock`. Browser: drop the
   chromium-path disk cache and the profile and install layers that Playwright covers. Keep
   human-like scroll and end-of-feed detection.
3. **Fold flnkit into `flnd.kit`.** It needs D2. `test_layers` still enforces the import rule.
4. **Engine model.** `model.py` (one struct each for events, counts and rows) and `layout.py`
   (all the folder and name code). Rewire the callers; this mechanical part goes to Haiku.
5. **Scheduler.** `sched.py` on semaphores, a per-host cap and one token bucket. One retry and
   error classifier, shared by crawl, save and ctx. Bench gate.
6. **One Tab protocol.** Merge `chrome.py` into `ctx.py`, with Playwright and extension
   backends. Replace `Bridge` with asyncio primitives.
7. **Crawl and engine.** Queue stages instead of `_Queued/_Kept/_Filed/_Work/_Held`. Query
   sync in one place. One home-state command path. Port `test_crawl` first.
8. **Store, fetch, config.** msgspec rows and config. Segmented fetch in `fetch.py`. Check and
   repair as one unit. Drop the v1 migration if D4 says so.
9. **Extension host.** One transport (D5). Framing on `asyncio` streams.
10. **Plugins.** `plugkit.py` helpers, the `ins`/`thr` meta base, and every plugin rewired. One
    plugin per commit, with its tests unchanged.
11. **Test kit.** `testing.py` runs the real `Ctx` over a fake transport.
12. **Library, CLI, UI.** A command table, thin groups, and a `rich.live` run screen with plain
    prompts and lists.
13. **Docstring sweep and test prune.** Move Args/Raises prose into flnddocs, keeping the
    coverage test green. Re-record the bench baselines.

Steps 0–3 stand alone and already save about 7,500 lines. Steps 4–9 are the engine, where the
risk is highest. Steps 10–12 follow from the earlier ones.

## Decisions (my recommendation first)
- **D1. Thinner docstrings, prose moved to flnddocs.** Yes. Without this, the cut is about 35%,
  not half.
- **D2. Fold flnkit into `flnd.kit`.** Yes. It keeps the "library modules stay separate" rule:
  each kit module still imports only libraries and kit errors, and `test_layers` checks that.
  What goes is the second package, its API snapshot and the `__all__` ceremony. If you want
  flnkit to stay installable on its own, say no and step 3 is skipped (about 400 lines kept).
- **D3. Unpin curl_cffi, use AsyncSession.** Yes, behind the bench gate. If the gate fails by
  more than 15% on big files, keep a small hand-paused stream for that one path.
- **D4. Drop the store v1 to v2 migration.** Yes, if there is no v1 home left that you care
  about. Otherwise it moves out to a one-shot `scripts/migrate_v1.py`.
- **D5. Extension transport.** Keep the Windows named pipe with its ACL, and drop the
  duplicated `_Conn` and framing layers. I advise against loopback TCP: any local process
  could reach it, with only the token to stop it. Saving about 350 lines (not 500) is worth
  keeping the security.
- **D6. Unused plugin API (`ctx.call`, `ctx.extension`, `Settings`, `Clock`, `Landed`,
  `Element`).** Keep. These are in SPEC for third-party plugins. Only `OLD_NAMES` goes.
- **D7. `Cap` AIMD concurrency in the scheduler.** Drop it, unless you rely on it. It is not in
  SPEC, and fixed per-host caps plus Retry-After cover throttling.
- **D8. TUI.** Keep the live run screen (on `rich.live`) and one list view with actions. Forms
  become plain prompts that also work on Windows (today's `_keys` uses termios). The three
  separate list views fold into one.
- **D9. Python API groups (`fl.targets`, `fl.auth`, `fl.config[...]`).** Keep the spelling, as
  thin objects, so SPEC "Python" and `test_docs` stay valid.

## Risks to watch
- Sync and early stops (`seen`, `heads`, `gaps`, cursors), SPEC "Sync": `test_crawl` is the
  net and must be ported before step 7 changes anything.
- Dedupe across categories (link rows) and "the row is written after the file": steps 7 and 8.
- Logins, account switching and wall retry, which can only be checked live: hand-check on the
  test accounts after steps 6 and 7.
- Windows renames and long names (the last commit fixed one): keep `test_fs`. Run
  `make check` on Windows before step 13.
- twt search date windows and rate pacing: these do not move into the shared helpers.
- Speed: net, scheduler and warm tabs go through the 15% bench gate.

## Cost
Opus does the steps that need design and review. Sonnet builds steps 1, 2 and 4–12, and Haiku
does the mechanical moves (steps 0, 3, the rewiring in 4, and the docstring move in 13). One
builder at a time.
