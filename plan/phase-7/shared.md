# shared

Phase 7, step 7. The user said on 2026-11-13: **`shared` should be a black box; its user (flnd)
shouldn't have to think.** A read-only audit the same day found where flnd thinks for `shared`
today: it rebuilds what `shared` should do, reaches into private modules, reads side channels, and
matches error text. Each fix follows the conventions (redesign §2) and changes the API snapshot
where marked.

## The rule

A `shared` call does the whole job a caller means, returns what the caller needs, raises only
`<module>.Error` types the caller can catch by name, and can be stopped. flnd never imports
`shared._x`, never copies a `shared` constant, and never wraps a `shared` call the same way twice.
A test (`tests/test_names.py`) fails on a `shared._` import outside `shared/`.

## What changes

| # | Today | After | Snapshot |
| --- | --- | --- | --- |
| 1 | **flnd never calls `http.fetch`**: `TargetSite.fetch` rebuilds it on raw httpx, with its own copies of `SOFT_STATUSES`, `CONNECTION_ERRORS`, `retry_after_seconds` and `_where`; its messages keep the URL's query. | `http.fetch` takes `before=` (a hook each attempt calls first: flnd's pacer) and waits out a 429 itself through a per-host back-off; `site.fetch` is `http.fetch` plus turning `RefusedError` into `FetchError`. The copies go. | yes |
| 2 | **`http.download` returns a `Path`**, so flnd reads headers through a thread-local, a transport branch and shared's private extension string; **`Page.goto` returns a status**, so flnd listens to every response for the document's headers. | `download` returns a `Download` (`.path`, `.headers`, `.url`); `goto` returns a `Visit` (`.status`, `.headers`, `.url`) or None. flnd's `_seen`, `_recording`, `ONE_ATTEMPT` and `_on_document` go, and `Last-Modified` comes for free ([lean](lean.md)'s "kept" case). | yes |
| 3 | **A download can't be stopped**: the second Ctrl-C raises from a progress callback, which runs only when bytes arrive; a slot wait, a retry sleep or a stalled read holds a file for up to ~100 s. | `download(…, stop=event)`: checked between chunks, every wait is `stop.wait()`, a slot wait polls in short steps; a stopped download keeps its resumable part. | yes |
| 4 | **Playwright errors are matched by message text** (`NAVIGATING`, `GONE_BODY`), and sources catch a bare `Exception` or a `browser.PlaywrightError` that is `Exception` until a browser opens. | `browser.RawError`, Playwright's error class, resolved lazily and stable; the message matches in one `_gone(error)`; no mutable module globals. | yes |
| 5 | **"Tool missing" errors are builtin `RuntimeError`/`TimeoutError`** (ffmpeg, browser, archive, cookies, worker), so flnd catches broad builtins that also swallow bugs. | `shared.MissingError(Error, RuntimeError)` for a missing program, extra or browser; `media.StalledError(Error, TimeoutError)` and the worker's; flnd catches them by name. | yes |
| 6 | **The live area's drawing thread dies silently** on any exception (a `show=` that raises, a closed stderr), and the screen freezes for the rest of the run. | The thread catches it, logs it at debug, resets, and draws again when the next group opens. | no |
| 7 | **flnd imports `shared._browser`, `_cookies`, `_selector`** (to subclass `Page`, for `Match`, `Jar`, `Selection`). | Through `from shared import browser, cookies, selector`; `browser.Match` made public. | `Match` added |
| 8 | **Public API flnd doesn't use** (`theme`'s drawers, `style`'s colours and gradients, `prompt.keymap`, `crypto`'s hashes, `worker.as_ordered`, `archive.ls`, …). | **Kept.** The user decided on 2026-11-13 that `shared`'s public API is judged by how useful it is, not by whether flnd calls it: unused is not dead. A name goes only when it is an alias or a second spelling of another (the no-aliases rule), and each is listed in the commit that removes it. `style`'s gradients are what step 9's motion is drawn with. | only for aliases |
| 9 | **Terminal edges**: rows fill the whole width, so a terminal that wraps at the last column (Windows' old console) or a window made narrower miscounts the area's lines. | Rows fit `width - 1`; a width change clears the area and draws it whole. | no |
| 10 | **Windows file locks**: one `os.replace` in `atomic`, which an antivirus or a thumbnail scanner holding the file makes fail. | On Windows, the replace is retried for about a second on `PermissionError`. | no |

Also in step 7, by the user's decision of 2026-11-13, so they are built once and not moved again
in Phase 8: **`http.Client(pace=…)`**, the pace object (readable R4: rate headers, budgets, a learned
gap and 429 back-off per host, inside the client), and **`shared.clock`**, one injectable clock for
every module (readable R11).

Also, folded into [speed](speed.md): `http`'s process-wide state (the default client never closed,
`_slots` growing, `MAX_PER_HOST` fixed) becomes one run-scoped client with per-host limits; and
`timeout` gets one meaning everywhere: per phase, plus a whole-request deadline.

## Decided

- **`media`'s API stays whole** (`convert`, `crop`, `resize`, `trim`, `fps`, `frames`, `location`),
  as the Phase 5 and 6 decisions and the rule above say (user, 2026-11-13).
- **The size rule**: plan/README.md's "no size ceiling" (2026-11-12) wins over Rule 3's 150–300
  lines a module, which is read as history; the README says so.

## Tests

`site.fetch` paces through `http.fetch`'s hook and turns a 404 into `FetchError`; `download` gives
the headers, and `goto` the document's; a stopped download returns within a second and keeps its
part; a source catches `browser.RawError` before any browser opens; a missing ffmpeg raises
`MissingError`; a `show=` that raises leaves the next group drawing; a `shared._` import in flnd
fails `test_names`; a full-width row on an 80-column screen is cut at 79.
