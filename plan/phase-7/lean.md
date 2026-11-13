# lean

Phase 7. The user asked on 2026-11-13 for flnd's code to be leaner, under the same conventions.
flnd is about 4,900 lines of code without docstrings and comments (11,000 with them): about 2,035
in the six sources and `flnd/sources/common/`, the rest in the engine. Two read-only audits on
2026-11-13 found the proposals below, each checked against its call sites.

**About 425 lines could go (9%), about 515 with the riskier ones**, plus one module (`post.py`) and
eight concepts. Most of the saving is code Phase 6's [simplify](../phase-6/simplify.md) already
said goes and that is still there, or code two sources each wrote for themselves.

**Every low-risk proposal is taken** (user, 2026-11-13); each is built in the Phase 7 step that
touches its files, or as its own commit before them. So are the three [calls](#calls-the-user-made),
so about 515 lines go in all. [shrink](shrink.md) cuts about 60
more lines (`Choice` and its format tables).

## Engine

| # | Proposal | Lines | Risk | API snapshot |
| --- | --- | --- | --- | --- |
| E1 | **Finish simplify's "one retry layer".** `net.Transport` still retries, and `ONE_ATTEMPT`, `retry_wait`'s callable branch, `Wait` and `Source.wait` are still there (`net.py` 192–334, `source.py` 179, 1009); no source sets `wait`. `Transport` sends once; `TargetSite.fetch` and `_fetch_in_page` share one 429 back-off. A 5xx's `Retry-After` is no longer read (1, 2, 4 s instead). | 50–60 | medium | yes |
| E2 | **Options no source uses go:** `Source.pause` (and the sleep between targets, `_sleep`, `net.jittered`), `viewport`, `browser_args`, `Category(name=, filename=)`, `File(headers=)`. | 25 | low | yes |
| E3 | **`post.py` folds into the job.** With classify removed, the postprocessor chain is two fixed steps: compress, then the modified time. `Fields`, `Postprocessor` and `ORDER` go. | 30, one module | low | no |
| E4 | **`dry` stops copying the engine:** `job.plan` (one caller) moves into `dry`; one categories parser and one "open, check the login, connect" context for both. | 25 | low | no |
| E5 | **One run, grouped by source only.** Options become per target, so targets with different saved `--image` no longer run in batches; `job.Done` becomes `TargetReport`; `targets.db` isn't reopened per target. One live area for the whole run, as [output](output.md#status-lines) wants. | 25 | medium | no |
| E6 | **One host matcher and one URL scrubber.** `host == d or host.endswith("." + d)` is written four times; "a URL without its query" three times, and the [run log](layout.md#run-logs) needs it again. | 15 | low | no |
| E7 | **`shared.http.download` does the mirrors and the row**, which it already can: a list of URLs, `label=`, `progress=`. A failed mirror becomes a debug line. | 15 | low | no |
| E8 | **Dead code:** `SourceRun.on_loop`, `engine.COOKIE_METHODS`, `post.Fields.values`, `cli.BROWSERS` (a copy of `auth.BROWSERS`), and the alias `Engine.LoginError`, which the no-aliases rule forbids: `flnd.LoginError`. | 8 | low | yes |
| E9 | **Built** (step 1). **One table of `.flnd/` paths** in `files.py` (`MEDIA`, `METADATA`, `LOGS` and each store's file), which [layout](layout.md) needs anyway. | 5 | low | no |
| E10 | **Small repeats:** `--time` held twice (`Options.since/until` and `Range`); the format values, the category table and the route lookup each built twice; `Job`'s six counters become one counts record with the status lines. | 12 | low | no |

## Sources

| # | Proposal | Lines | Risk |
| --- | --- | --- | --- |
| S1 | **One feed reader** in `common/replay.py` for "read what the page embeds, scroll until a replayable request, replay it with `pages()`, scroll again if a replay is refused". ins and fbk each wrote it (ins `read_tab` and its helpers, fbk `read_tab`, `replayed`, `replay_lines`, `albums`); the site knowledge is only how to load, parse, find the cursor and read a wait. It is where the [listing memory](dates.md) is fed from. fbk's Tiles quirk (scroll past `has_next_page: false`) becomes a flag. | 60 | medium |
| S2 | **tik: one "API, else browser" list** for videos, stories and tabs, each writing its own fallback, count and seen-set today; `Feed`/`FEEDS` merge into `LISTS`. | 40 | medium |
| S3 | **The engine names files keyed by id** from the source's `filename` format: fbk `post_of`, twt `media_file`, ins's grid files and tik `post_of` build `<date>_<id>` themselves, which the engine renames by content anyway. Needed by [dates](dates.md#1-one-listing-memory-in-the-job), so a date found later reaches the name. | 15 | low |
| S4 | **Timestamps through `day()`**: fbk `posted_on` and tik `stamp_of` re-check digit strings and a 1973 floor that `day()` checks more strictly. | 12 | low |
| S5 | **ins's own repeats:** `lazy_files` repeats `Api.post`'s "media API, else the post's page"; `reels` and `stories` each write "refused once: remember, warn, return None". | 16 | low |
| S6 | **twt's `unsupported` as a comprehension**, as fbk's is (8 paths × 2 hosts by hand). | 13 | none |
| S7 | **Per-source "N found" counts go**, as the status lines count found (fbk, ins, twt, tik). | 20 | low |
| S8 | **`relay`'s hand walk becomes `$..__bbox.result`**, as scripts.md planned; to check first whether a nested `__bbox` ever comes. | 10 | low–medium |
| S9 | **One method behind several tab categories** (fbk 5, ins 4, twt 5, tik 3, two lines each): a factory in `common`, or a `Site.category` member; also about 130 docstring lines. Category order (fbk's `posts` first) needs care. | 26 | medium |
| S10 | **Trivia:** ins `is_graphql` becomes the string `"graphql"`, as fbk's `FEED` is. | 5 | none |
| S11 | **tlg `resolve` through route patterns**, if they can give `-100` and `+` forms; unverified. | 15 | unverified |

## Calls the user made

All taken as recommended (user, 2026-11-13):
- **tik's direct API path is deleted** (about 50 more lines): tik.py's own note of 2026-11-12 says
  TikTok's WAF answers flnd's client with a challenge, so live runs always used the browser. This
  reverses [flows](../phase-6/flows.md)' "list API direct first".
- **E1 is taken**: a 5xx's `Retry-After` is no longer read; it waits 1, 2, 4 s like any 5xx.
- **E5 is taken**: targets with different saved settings run at once, in one live area.

## Kept, looked at

Site knowledge stays: each site's login wall and block checks, tlg's login flows, loi's signing,
fbk's `media_items` walk (it needs the nearest dated parent, which a JSONPath can't give). Also
kept: the job's refresh queue (ins replays must run on the listing thread), the vault (simplify
decision 7), reading Phase 5 `compress_*` rows, `Last-Modified` capture (it would need
`shared.http.download` to return headers).
