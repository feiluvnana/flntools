# Agent guidelines

These rules are for anyone, person or agent, who changes this repository. `PLAN.md` is the design of the rebuild; this file says how to work on it.

## Status
- `PLAN.md` on branch `next` is the plan, and `ARCHITECTURE.md` explains the design for readers: flnd becomes one fast package (a library plus a thin CLI, no `shared` toolkit), rebuilt in the ranked commits of its section 15.
- **The user approved it on 2026-11-16**, with the recommended answer to every question in its section 16. The rebuild follows section 15's commits in order.
- Until the rebuild lands, the code still has today's layout: `shared/`, `flnd/` and `scripts/dev.py`. The sections below give today's commands and the target rules side by side wherever they differ.

## Commit convention
Every commit message has this form:
```
<action>:<summary>
```
- `<action>` is a lowercase type: `feat`, `fix`, `perf`, `refactor`, `docs`, `test`, `chore` or `style`.
- The colon follows it with no space.
- `<summary>` is imperative, lowercase, with no trailing period.
- **No co-author.** Never add a `Co-authored-by:` trailer or any other attribution.

Examples:
- `fix:flush a telegram part before saving its offsets`
- `perf:stream archive entries to disk`
- `docs:document the download pipeline`

## Git
- Commit as `feiluvnana`, SSH-signed, through the repo-local git config. Never change the global git config.
- One logical change per commit, with `make check` green at every commit.
- Commit or push only when asked. Pull first (`git pull --rebase`), and commit only your own paths (`git add <paths>`): other sessions may share the working tree.
- **A performance commit** quotes its benchmark before and after in the message (`make bench`, `PLAN.md` §14.4). Never claim a speed-up you didn't measure.

## Checks
- **Target** (after `PLAN.md` §15.4):
  - `make check` runs lint (ruff) and the tests, and must be green before every commit;
  - `make setup` syncs `.venv` from `uv.lock`. To change a dependency, edit `pyproject.toml`, then run `uv lock`.
  - `make api` regenerates `tests/data/api.txt`, the public surface (`flnd`, `flnd.source`, `flnd.testing`). Read its diff after any public change: it is the review of that change.
  - `make bench` runs the offline benchmarks; `make docs` builds the API reference.
  - `make type` runs pyright on the three public modules. It is informational, never a gate, and a wrong public hint is still worth fixing.
- **Today, until then:** `uv run --no-project scripts/dev.py check` (or `make check`) runs lint, pyright and the tests, and `scripts/dev.py api` regenerates the snapshot.
- **`make bench`** (today `scripts/dev.py bench`) runs the offline benchmarks of `tests/bench/`, never part of `check`. `make bench ARGS=--gate` fails on a regression over 15% against `tests/bench/gates.json`; `ARGS=--record` stores a run as the new baseline. `docs/performance.md` says how to read them.
- **On Windows**, install make with `winget install ezwinports.make`, or use the one that comes with Git for Windows. Each Makefile recipe is a single `uv` command you can also type yourself.

## Privacy and live data
- Never read, print or commit the `*_cookies.txt` files in the repo root, or anything under `.flnd/`.
- Never pass a file name, caption or any other text from a site to a shell. Run commands with an argument list.
- Never put a cookie value, key, token or password in a log line, an error message, a comment, a doc or a test.
- Use only example handles in code, tests and docs: `example_user`, `example_creator`, `example_handle`, `example.page`, `examplechannel`, and the domain `example.com`. Never use a real username.
- Live checks against real sites are done by hand, on the authorized test accounts, and never in unit tests. Unit tests fake the network, the browser and the clock (`flnd.testing`; today, `httpx.MockTransport` and `clock.Fake`).

## Layout (target, `PLAN.md` §4)
- **`flnd/` is one package.** Only three modules are public:
  - `flnd`: the library, with `Flnd`, events and errors;
  - `flnd.source`: the API a source or plugin is written against;
  - `flnd.testing`: the test kit for sources.
- **Everything else is private**, with a leading underscore: `flnd/_net.py`, `_browser.py`, `_db.py`, `_files.py`, `_download.py` and the rest.
- **`flnd/cli.py`** is the thin CLI (argparse). It imports only `flnd`, so a script can do anything the CLI can.
- **`flnd/sources/`** has one module per built-in site (`fbk`, `ins`, `loi`, `mht`, `tik`, `tlg`, `twt`) and `_manifest.py`, the list `flnd --help` and target resolution read without importing a source. A source imports only `flnd.source` and the stdlib.
- **`ARCHITECTURE.md`** at the root is the map of the design; keep it true when a change moves a part. **`docs/`** holds the guides for people (§14.6). `examples/` holds a runnable script and an example plugin with its test.
- **Plugins** live outside the repo: in the user's plugin folder (`$FLND_SOURCES`, else `~/.config/flnd/sources/`), or as installed packages with a `flnd.sources` entry point. flnd never loads code from a home folder, since a home may be a folder someone else made.

## API and naming
- **Short names**, one word where one word says it.
- **An action is a verb** (`download`, `follow`, `sync`, `unfollow`, `check`, `retry`), with no `get_`, `to_`, `try_` or `load_from_` names. A read is a property (`fl.logins`), or a method with keyword filters (`fl.files(target=…, kind=…)`).
- **One word, one meaning**, across the whole package. `retry` is the verb, so an error's advice is `hint`. `wait` waits for a page or a run, so a timed sleep is `pause`. `File` is a file to download (`flnd.source`), so a saved file's row is `Record`.
- **Options are keyword-only, at most 5.** Past that, use a record or the config file.
- **Progress is the event stream** (`Saved`, `Skipped`, `Failed`, `Progress`). A script iterates a run. There are no callbacks and no hooks: one way to do one thing.
- **A resource is a context manager** (`with flnd.open(home) as fl:`), so nothing leaks a thread, a browser or a connection.
- **One spelling:** no aliases and no deprecated names.
- **Errors are `flnd.Error` subclasses** from the one hierarchy in `PLAN.md` §8. The message is `str(e)`, and `e.hint` says what to do. A user error never shows a traceback. `flnd.Stopped` (Ctrl-C) derives from `KeyboardInterrupt`, never from `flnd.Error`.
- **The source API is the most important API.** A source declares what it is (code, domains, login, routes, categories) and implements a few well-named hooks. A mistake in a source fails when the class is made, with a message naming the attribute and the fix. The source API is versioned (`api = 1`).
- `tests/test_names.py` checks the naming rules on the public surface. Its allowlist may only shrink.

## Writing: code that people can read (`PLAN.md` §14.6)
The user, 2026-11-16: "the codebase should have extensive comment and docs for human to read."

**Docstrings, everywhere:**
- **A module** starts with a docstring that says what it holds, who calls it, what it calls, its main flow in numbered steps, and any rule a change must keep (threads, locks, ordering, invariants). There is no length cap.
- **A class, function or method** has a Google-style docstring: a summary line, then a paragraph on behaviour and edge cases, then `Args:`, `Returns:` and `Raises:`. Public names also get an `Example:`, which `tests/test_docs.py` runs.
- **`Args:` says what an argument means** (unit, range, what `None` does), not its type: the annotation says that, and two copies drift.
- Private functions get one too, unless the body is a few obvious lines.

**Comments explain why, and also how,** wherever the code isn't obvious at a glance:
- site quirks, with the date seen;
- magic numbers and where they come from;
- locks, which thread a call runs on, and the order writes must keep;
- platform branches;
- the steps of a long algorithm, as numbered comments;
- how a failure is handled, and why it is safe.

Section comments split a long module into parts. A comment never restates the line under it, and never cites a plan step or a commit, since those change; it may point to a `docs/` page.

**Guides in `docs/`:**
- `architecture`, `library`, `cli`, `writing-a-source`, `errors`, `storage` and `performance`;
- one page per site, `docs/sources/<code>.md`;
- a glossary.

A change of behaviour updates its docstrings and its `docs/` page in the same commit. `tests/test_docs.py` checks the docstrings, runs the `python` blocks in the docs, and matches CLI flags and source pages against the code.

**Style:**
- Plain English and short sentences. Define a term where it first appears.
- Lines of at most 100 characters.
- Prefer functions short enough to read in one screen, with shallow nesting. A long function is fine when numbered comments walk through it.
- Plain over clever:
  - no chained ternaries;
  - a three-way state is an `Enum`, not `bool | None`;
  - no writing to a frozen object;
  - declare what code would otherwise guess.
- Type hints are for readers and editors, not a checking gate. Annotate the public API fully, and internals where it helps. Don't write overloads or Protocols only to please a checker.

## Performance
- Measure before changing anything for speed, with `make bench` or a script in your scratchpad, and keep the numbers in the commit message.
- Use the hot-path rules of `PLAN.md` §5:
  - one asyncio loop for network and browser;
  - no extra `stat` calls or lock files per file (a `.part` is its own lock);
  - one database transaction per post, not per file and not on a writer thread;
  - lazy imports, so `flnd --help` stays under 40 ms.
- A regression guard in `make check` counts things (requests, `stat` calls, statements, imported modules) instead of timing them.

## Plans
- `PLAN.md` holds the design and the order of the rebuild, and is updated as decisions are made. It is deleted when the rebuild is done; what still matters moves into `docs/`.

## Models
- **Mechanical changes use a low-cost model** (user, 2026-11-12): Claude Haiku in Claude Code, or the low-cost tier of whichever tool is in use.
  - Mechanical means renames, moves, import updates, applying a given pattern to many call sites, regenerating snapshots, merge conflicts with an obvious answer, and doc edits that follow a decision.
  - The orchestrator sets the model when it delegates such a task, for example `model: "haiku"` on a Claude Code subagent.
- Design, new behaviour, debugging, security-sensitive code, reviews and the docs of `PLAN.md` §14.6 keep the stronger default model.
