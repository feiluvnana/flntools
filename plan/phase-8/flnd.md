# flnd: smaller and plainer inside

Part of [Phase 8](README.md). flnd keeps its layout and everything it does (user, 2026-11-13: no
split, no `projects/`). Phase 8 makes its engine side as small and plain as it can be: code that
repeats, wrappers with one caller, state that could be local, and generic machinery that belongs
in `shared`. It also writes down, in one place, how flnd stops, resumes, limits, shows and logs,
which the earlier draft had spread over a separate download manager.

It builds on the code Phase 7 leaves: steps 9–12 (status lines, the site split, mht, the docs
diet) and their refactors (readable R1–R11, lean E1–E10, S1–S11) aren't repeated here. Line
counts are at `37147c1`, docstrings included.

## 1. Module by module

flnd's engine side is 7,237 lines (3,531 code, 2,361 docstring, 520 comment) in the modules
below; sites aside. Each change keeps behaviour; most are one-line-of-thought edits that a test
already covers.

| Module (lines) | Change | Lines | Risk |
| --- | --- | --- | --- |
| `job.py` (1,431) | `_Work`, a frozen class with one method, becomes a `Ledger` method; `_queue_all` and `_queue_listed` merge | −13 | low |
| | a post's first key is computed once and passed (today twice per post, and `_saved_first` rebuilds it) | −8 | low |
| | "list a post's files" written once (three places today) | −2 | low |
| | `posts()` and `_read()` share one listings generator (route lookup, wanted categories, context) | −10 | medium |
| | `wait(row)` uses `self.row`; `finish()` stops waiting a second time | −5 | low |
| | the dead "on the listing thread" branch of `_expired` goes (pool tasks never run there) | −4 | low |
| | the "stopped" ending written once (twice today); a chained ternary at `1406` becomes `if` | −3 | low |
| | `worker` imported at the top, not lazily in two methods | −6 | low |
| `engine.py` (828) | `_written_back` folds into `SourceRun.close`; `_check_browser` (a test seam) goes; `ALL` defined once; `logins` opens the vault once per site, not three times | −17 | low |
| | `_unrecorded` goes: it serves folders from before Phase 5, which the no-migration rule leaves behind (question 3) | −13 | low |
| `source.py` (1,365) | `Post.replace` and `filed` stop writing to a frozen object (`_assign`): a frozen dataclass with `__post_init__` checks and `dataclasses.replace` | −15 | medium |
| `site.py` (496) | `SourceRun.state`'s lazy branch is dead (`source_run` always passes it); `_connect` and `_source` inline into `page` | −10 | low |
| `settings.py` (170) | `complete` and `keep_cursors` become one `settle(…, complete=, cursors=)`: one write per target | −10 | low |
| `repair.py` (218), `common/files.py` (160) | one folder walk instead of four; the part helpers go to `atomic.sweep` (section 2) | −3 | low |
| `common/net.py` (415) | `_base_transport` goes; the response hook, thread-local and `_recording` go once `http.download` hands its check the response's headers | −24 | medium |
| `common/compress.py` (257) | the module's warning lock and the `warned` set passed around become `Compressor`'s; `ONE_AT_A_TIME` becomes `kind == VIDEO` | −8 | low |
| `common/auth.py` (472) | `secrets()`, a wrapper of `Vault(...)`, goes; the move of Phase 5's `.flnd/cookies/` files goes (question 3) | −56 | low |
| `sources/common/watch.py` (131) | `watch_responses` wraps `Responses`, which wraps a generator: one generator (most of the module then goes to `Page.responses`, section 2) | −17 | low |
| `query.py` (381) | `_after` becomes one comparison | −2 | low |

**About 230 lines** go here, 49 of them medium risk, none high. With section 2's moves, flnd's
engine side loses **about 650 lines** in all.

**One target's path** today is about 30 calls from `cli` to the history entry. Dropping `_Work`,
merging the queue functions, computing the key once, folding `_written_back` and one `settle`
remove six; Phase 7's E4, R3 and E7 take it to about 20.

## 2. Generic machinery into `shared`

The tagging pass (the draft's) marks each function **site knowledge**, **listing logic** or
**generic machinery**. The first two stay in flnd. Generic machinery moves to `shared` when both:
1. the caller's code becomes one or two calls with sensible defaults; and
2. it serves two callers, or removes 30 lines or more from the one it serves.

And, by the user's rule that `shared` serves future tools too, **the moved API is shaped for any
caller**: no site, target or category in it, named for what it does, tested and documented
without flnd.

| Today in flnd | Generic? | Into `shared` as | Callers | Lines out | Fixes |
| --- | --- | --- | --- | --- | --- |
| `engine._stop_waits_on_ctrl_c` | yes | `cli.stopping()` → `Stopping(soon, now)` | flnd's engine; any CLI with work to finish | 38 | — |
| `sources/common/watch.py` (subscribe, iterate JSON, scroll until quiet) | yes | `browser.Page.responses(match, *, lines, scroll) -> Watch` | ins, fbk, twt, tik | about 90 | callbacks into the caller |
| cookies copied between flnd's httpx client and the browser (`net.cookie_jar`, `site._names`, the copy in `fetch` and `close`) | yes | `http.Client.cookies`, a `cookies.Jar` a `Browser` shares | flnd's site | about 35 | a browser round trip per fetch |
| `net._Row` (one bar, several streams) | yes | `log.Row.part()` | `net`, tlg's split download | about 45 | [F8](bugs.md#flnd) |
| the job's refresh queue (`_room`, `wait`, `_answer`, `_Refresh`, `_expired`'s wait) | yes: "run this on the thread that owns a single-threaded library" | `worker.Pool.run_on_owner(fn)`; `submit` and `join` answer it while they wait; `Pool.stop()` | flnd's job | about 90 | [F4](bugs.md#flnd), [F11](bugs.md#flnd) |
| `site.run_async` (a loop thread for an async SDK) | yes | `worker.Loop` (worker's async backend already holds the same code) | tlg; worker itself | about 20 | [F3](bugs.md#flnd) |
| `files.PART_GLOB`, `part_info`, `resumable`; repair's leftover removal | yes: copies of `atomic`'s naming | `atomic.sweep(folder, *, keep, settle)`, `atomic.lock(folder)` | repair; any tool resuming files | about 35 | [F9](bugs.md#flnd) |
| `files.checked`, `net._as_media`, `_sniffed`, `same_extension`, `NotMediaError`; repair's `_broken` | yes | `media.verify(path, *, kind, decode) -> extension`, `media.NotMediaError` | the download check, repair | about 70 | — |
| `engine._prune` (logs past 30 days) | yes | — | one, 12 lines | — | stays |
| `site._PacedPage` (scrolls at the learned speed) | yes | goes: the caller passes `browser.Scroll(pause=…)` | — | 15 | — |
| `Ledger._link` (symlink, else hard link) | yes | — | one, 25 lines | — | stays |
| `net.download`'s mirror loop | yes | — (Phase 7's E7 folds it into `http.download`) | — | — | — |
| `compress.py` (flnd's shrink policy over `media`) | no: a policy | — | — | — | stays |
| `net.SPLIT_HOSTS`, `split_parts` | no: which CDNs passed a live check | — | — | — | stays |

The API of each new name is in [api](api.md#the-new-names-from-the-tagging-pass). The pass is
done again on the code Phase 7 leaves, as the first commit of the build, and this table updated.

## 3. Stopping: Ctrl-C

One rule for the whole program, now `shared.cli.stopping()`'s:
- **The first Ctrl-C sets `soon`**: listing stops between posts; no new download starts; every
  pace, 429 or block wait ends (the sites' and CDNs' `http.Client`s take `stop=soon`); running
  downloads finish; a repair stops between files and leaves unchecked entries unstamped
  ([F10](bugs.md#flnd)); targets end "stopped"; exit 130.
- **The second sets `now`**: downloads stop at their next chunk or wait, within a second, keeping
  their parts (`http.download(stop=now)`); a `fetch` function (Telegram) stops at its next
  progress call, which raises an `Exception` on the site's loop, never a `BaseException`
  ([F3](bugs.md#flnd)); ffmpeg is killed and its part removed.
- **`shared` installs no signal handler of its own** apart from `cli.stopping()`, which only the
  program's entry point enters. A library call takes events; it never reads signals.
- The three places flnd tests "the first but not the second" (`job.py:860, 1036, 1206`) become one
  read of `Stopping`.

## 4. Resume and the part format

- **A part** is `.<name>.<8 hex>.part` beside its file (`shared.atomic`), so the rename onto the
  file never crosses a volume. `http.download` keeps its state in `.part.json` beside it.
- **The sidecar gets a version** (added): `{"v": 1, "url": …, "validator": …, "size": …,
  "ranges": …, "segments": [[start, end, done], …]}`. `url` has no query (a signed link's token
  never reaches the disk). A sidecar without `v` reads as version 1, so parts left before Phase 8
  resume; an unknown version drops the part. The format is `shared`'s, documented once in
  `http.download`.
- **What a crash leaves, and the next run:**

  | Crash during | Left on disk | Next run |
  | --- | --- | --- |
  | a transfer | the part and its sidecar | the same file resumes it, even from a fresh URL |
  | a DASH merge | both stream parts, a merge part | the streams resume; the merge runs again ([F7](bugs.md#flnd)) |
  | the rename | the part, or the whole file | a part resumes; a whole file without an entry is checked and recorded |
  | shrinking | the original, a temporary part | the original is shrunk again; the part is swept |
  | before the history entry | a whole file, no entry | listed again, found in place, recorded |
  | a part never asked for again | an old part | `atomic.sweep` from `sync`'s repair, after 7 days |

- **The history entry stays the commit point**: an entry means a whole, checked, shrunk, dated
  file. No journal is added; flnd §15 already excludes a persistent queue.
- **Two runs on one target** (a scheduled `sync` during a `download`): each takes
  `atomic.lock(<target folder>)`; repair skips a locked folder and keeps parts younger than an
  hour ([F9](bugs.md#flnd)); a second `download` of a locked target says another run holds it
  and goes on with the next (question 4).

## 5. Limits

Each number is declared once, where it is used; this table is where they are listed:

| Limit | Value | Where |
| --- | --- | --- |
| files at once per target | 8 (tlg 3) | `Source.concurrency` |
| downloads waiting per target before listing waits | 2 × files at once | the job |
| targets at once | one per site, sites side by side | the engine |
| connections per host for downloads | 4, growing to 8; capped after two refusals, for 10 minutes | `http.download` |
| connections per file | up to 8, pieces of 2–4 MB from 4 MB | `http.download` |
| video encodes at once | 1 across the run | `Compressor` |
| photo encodes at once | 8 | `Compressor` |
| repair checks at once | 8 | repair |
| a site's pace | the site's floor, budgets and rate headers | `Source.pace` → `http.Pace` |

No limit per disk: each connection writes its own piece in place, measured at 97% of a raw copy
in Phase 7. A per-disk limit is added only if a hand run on a spinning disk shows a gain of 15%.

## 6. Status

Phase 7 step 9 draws the status lines (one per running target and category: found, saved,
linked, skipped, failed, bytes, speed; and downloading, shrinking). Phase 8 adds no screen. It
only gives the counts one source: the job's counts record (lean E10), read by the status line each
frame, and a file of several streams drawn as one row through `log.Row.part()`.

## 7. Logging boundaries

- **`shared` writes debug lines, and nothing louder, with two exceptions**: a wait it makes its
  caller sit through (the countdown row and its warning in `http.Client`), and a misuse it can see
  (a pool never joined). Everything else is returned or raised, and the caller decides what the
  user reads.
- **flnd turns errors and events into lines**: a failed file with its causes, a warn-once per
  kind of shrink failure, a category's skip count.
- **No secret in any line, error or row**, from either: URLs go through `fmt.url` (no user info,
  query or fragment: [L5](bugs.md#log-theme-prompt-cli-fmt),
  [R1](bugs.md#browser-cookies-selector)),
  remote text through `log`'s field cleaning ([L3](bugs.md#log-theme-prompt-cli-fmt)), and no
  cookie, header value or key is ever a field.

## 8. Order

1. The tagging pass on Phase 7's code (a doc commit to section 2).
2. `cli.stopping()`, with flnd's engine on it (section 3).
3. Section 1's low-risk edits, one commit per module, behaviour unchanged.
4. Each move of section 2, in the same commit as its `shared` module's API change
   ([api](api.md)).
5. Section 1's medium-risk edits last, each with the tests that pin its behaviour first.
