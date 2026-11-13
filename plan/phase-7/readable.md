# readable

Phase 7. The user said on 2026-11-13: **the code is still hard to follow; make it easy to read,
simplify what can be simplified, and keep a lean core, `shared` and `flnd` both.** A read-only
audit the same day measured why. **The user approved this file on 2026-11-13**, both its writing rules (which replace comments.md) and R3's `browser = True`. This file holds the writing rules that replace Phase 6's
[comments](../phase-6/comments.md) where it made the code verbose, and the refactors beyond
[lean](lean.md) and [shared](shared.md).

## Why it is hard to follow

Long functions are not the problem: 7 of 1,203 have more than 40 lines of code. These are:

1. **More prose than code.** 11,302 code lines carry 7,656 docstring lines and 1,786 comment
   lines. 277 of 969 docstrings are longer than the body they describe (`http.download`: 63 lines
   for 29; `source.target`: 30 for 1). `flnd/source.py`'s module docstring is a 124-line tutorial.
   Phase 6's comments.md asks for this: a complete `Args:` list "even when obvious", a comment on
   every caught exception, a heading comment every ~15 lines. The code is buried under it.
2. **The plan and the past written into the code.** About 130 references: plan ids ("B3", "A9
   rule 3"), `.md` files, "fixes #N", "Phase 5", "as it always did". A reader must open `plan/`
   to follow a comment.
3. **Control spread thin.** One file's download passes about 18 frames in 5 files; one request's
   pacing is split between a `Pacer` and a `Backoff` wired through `made[0]` lambdas; 318
   functions are two lines or fewer.
4. **Control turned inside out.** The listing generator calls back into its consumer
   (`saved=lambda: job.caught_up`) to learn whether the post it just yielded was saved; `caught_up`
   is a three-state `bool | None`; `Job` holds 25 attributes, threads and counts together.
5. **Names that don't fit.** Modules are renamed on import (`post as postprocess`, `settings as
   saved_settings`, `files as disk`); `row` is a progress row and a history entry in one method;
   `Done` is a class in one module and a dict in another.
6. **Clever and dense.** A bytecode walk (`needs_browser`, ~50 lines) stands in for a one-word
   declaration; 22 chained ternaries; 657 lines over 100 characters; options lists of 11–15.

## The writing rules

They replace comments.md's rules where those differ, and AGENTS.md says them in short. They hold
for every line written from now on, in `shared` and `flnd`.

- **A docstring is one sentence**, saying what the function does. An example only on a public
  entry point. `Args:` only for an argument whose name and type don't say it all; `Raises:` only
  for an exception a caller catches by name; `Returns:` only when the type doesn't say it. At most
  about 10 lines. `shared.cli` still reads a command's help from its `Args:`, so `flnd/cli.py`
  keeps them.
- **A module docstring** says what the module is for, its entry point, and where to look next, in
  at most 15 lines. A tutorial (how to write a site) lives in `tests/example_site.py`, as code that
  runs.
- **A comment says why**: a site's quirk, a magic value, a lock, a platform branch, a non-obvious
  choice. Never what the next line does, never a plan id, an issue number, a phase or "as before":
  history belongs in the commit message, and a regression guard in a test's name.
- **Each fact once.** A Protocol and its implementation aren't both documented; an overload set
  shares one docstring.
- **Size and shape.** Lines of at most 100 characters (ruff `E501`). Functions of at most about
  30 lines of code and 3 levels of nesting; a longer one is split where a name explains the part.
  No one-caller helper unless its name says something the code doesn't.
- **Plain control.** No chained ternaries; no `bool | None` for three states (an `Enum`); no
  callbacks from a producer into its consumer when the consumer can hold the loop; no mutating a
  frozen object.
- **Five or more options become a record** (a dataclass), passed as one argument.
- **Names.** A module is named for what it holds, so it is never renamed on import. One word, one
  meaning across modules (`row` is a progress row; a history row is an `entry`). No `run`,
  `values`, `fields`, `context` for different things in one place.
- **Module order**: docstring, imports, constants, the main entry point, then its helpers in the
  order it calls them, so a module reads top to bottom.
- **Enforced** where a machine can: ruff `E501` at 100; `tests/test_names.py` fails on plan ids,
  `.md` references and "fixes #" in code, on an import alias of a flnd module, and on a docstring
  longer than its function's body by more than 10 lines. comments.md's planned "complete `Args:`"
  lint is dropped.

## The refactors

Beyond [lean](lean.md) and [shared](shared.md); each in the step that touches its files, or as its
own commit before them, with the check green and behaviour unchanged.

| # | Refactor | Where | Size |
| --- | --- | --- | --- |
| R1 | **The job holds its loop.** `job.download` runs the categories itself; `queue` returns an `Outcome` (`QUEUED`, `SAVED`, `EMPTY`, `STOPPED`); the `ended`, `saved` and `listed` callbacks, the three-state `caught_up` and the frozen-object write go. | job.py, dry.py | −40 |
| R2 | **`Job` splits in two**: a single-threaded `Ledger` (queued, held, skips, counts) and `Downloads` (the pools, the refresh queue, the condition). 25 attributes become about 10 + 12, each class explainable in a sentence. | job.py | −20 |
| R3 | **`Downloader.browser = True`** declares what the bytecode walk guesses; the walk becomes a test that every downloader using `goto` or `page` declares it. This reverses Phase 6's redesign "no field for it", by the user's decision of 2026-11-13: readable over clever. | engine.py, sites | −45 |
| R4 | **One pace object per site**: `net.Backoff` merges into `Pacer`; the `made[0]` lambdas, `show_wait` and `on_hit` go. [shared](shared.md) #1's per-host back-off and [speed](speed.md)'s site/CDN split are two instances of it. | site.py, net.py, pace.py | −40 |
| R5 | **Five engine layers become two.** `download`/`_download` and `sync`/`_sync` become one logged method each; `_fetch`, `_run`, `_run_group`, `_run_targets`, `_connected` become `_run` and `_run_site`. lean's E5 alone keeps all five. | engine.py | −60 |
| R6 | **Modules named for what they hold**: `settings.py` → `targets.py`, `session.py` → `options.py`, `session.Settings` → `RunShared`, `common/files.py` → `paths.py`; no import aliases left. Mechanical: a Haiku subagent. | flnd | ±0 |
| R7 | **`site` documented once**, on `flnd.site.Site`; `soft=` becomes a second method (`fetch` and `try_fetch`), so three overloads in three places go. | site.py, _http.py | −20 code, −150 docs |
| R8 | **The docs diet** to the rules above, `shared` and `flnd` both: module docstrings to 15 lines, obvious `Args:` trimmed, tutorials to runnable tests. Checked by comments.md's own test that the code without docstrings is unchanged. | all | −3,000 doc lines |
| R9 | **Records for long option lists**: `media.output`'s 15 become an `Encoding`; `http.download`'s 12 keep their names but group in a `Transfer`; `net.download` becomes about 10 lines taking a `File`. | _media.py, _http.py, net.py | −30 |
| R10 | **No plan or history in code**: the ~130 references go, lines wrap to 100, the `test_names` patterns land. | all | ~700 lines touched |
| R11 | **One clock to fake**: `_sleep`, `_clock` and `_random` seams in engine, site and net become one injected clock. | engine.py, site.py, net.py | −15 |

## Order

1. **Now**: the rules go into AGENTS.md and replace comments.md's verbose parts, so every step
   from here writes to them.
2. **With each step**: R1 and R2 with step 4 (the listing memory rewrites `queue`); R4 and R11
   with step 7 (`shared` as a black box); R5 with step 8 (sites side by side); R3, R6, R7 with step
   10 (the site split); R9 with steps 7–8.
3. **Last, step 12**: R8 and R10 over both trees, mechanical where it is (Haiku), and the
   enforcement switched on, so the finished code reads one way.

## Target

`shared` and `flnd` together drop from about 20,700 non-blank lines (11,300 code, 9,400 prose) to
about 16,000: code about 10,500 after lean, shared, speed and these refactors, and prose about
5,500, so a reader meets more code than prose in every file.
