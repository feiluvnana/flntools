# Agent guidelines

These rules are for anyone, person or agent, who changes this repository: how to commit, check, lay out, name, write and measure. `docs/architecture.md` is the map of the code, and says why it is built the way it is.

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
- **A performance commit** quotes its benchmark before and after in the message (`make bench`, `docs/performance.md`). Never claim a speed-up you didn't measure.

## Checks
- The `Makefile` holds every task; each recipe is one `uv` command.
  - `make check` runs lint (ruff) and the tests, and must be green before every commit.
  - `make setup` syncs `.venv` from `uv.lock`. To change a dependency, edit `pyproject.toml`, then run `uv lock`.
  - `make api` regenerates `tests/data/api.txt` (`tests/api_snapshot.py`), the public surface (`flnd`, `flnd.source`, `flnd.testing`). Read its diff after any public change: it is the review of that change.
  - `make docs` builds the API reference into `build/docs/`. `make dist` builds a one-file binary.
  - `make type` runs pyright on the public modules. It is informational, never a gate, and a wrong public hint is still worth fixing.
  - `make check PY=3.11` runs on the oldest Python `requires-python` allows; run it before a release.
  - Ruff's rules are `F`, `E9`, `B`, `UP` and `I`, set in `pyproject.toml`.
- **`make bench`** runs the offline benchmarks of `tests/bench/`, never part of `check`. `make bench ARGS=--gate` fails on a regression over 15% against `tests/bench/gates.json`; `ARGS=--record` stores a run as the new baseline. `docs/performance.md` says how to read them.
- **On Windows**, install make with `winget install ezwinports.make`, or use the one that comes with Git for Windows. Each Makefile recipe is a single `uv` command you can also type yourself.

## Privacy and live data
- Never read, print or commit the `*_cookies.txt` files in the repo root, or anything under `.flnd/`.
- Never pass a file name, caption or any other text from a site to a shell. Run commands with an argument list.
- Never put a cookie value, key, token or password in a log line, an error message, a comment, a doc or a test.
- Use only example handles in code, tests and docs: `example_user`, `example_creator`, `example_handle`, `example.page`, `examplechannel`, and the domain `example.com`. Never use a real username.
- Live checks against real sites are done by hand, on the authorized test accounts, and never in unit tests. Unit tests fake the network, the browser and the clock (`flnd.testing` and its `fake_clock` for sources, `flnd._net.fake` for HTTP, `flnd._tools.fake` for outside programs).

## Layout
- **`flnd/` is one package.** Only three modules are public:
  - `flnd`: the library, with `Flnd`, events and errors;
  - `flnd.source`: the API a source or plugin is written against;
  - `flnd.testing`: the test kit for sources.
- **Everything else is internal**, never imported by a script or a source: the `_*.py` modules (`flnd/_net.py`, `_browser.py`, `_db.py`, `_files.py`, `_transfer.py` and the rest) and the engine's modules (`engine.py`, `job.py`, `site.py`, `options.py`). `flnd/errors.py` holds the one error hierarchy, which `flnd` and `flnd.source` re-export.
- **Library modules stay separate** (user, 2026-11-16). A private module that does a general job (HTTP, the browser, files, archives, outside tools, cookies, the vault, the SQLite store, HTML, text, time, prompts, progress, media) is a standalone library: it imports from flnd only other library modules and `flnd.errors`, never the engine, job, site, sources or the public API. flnd-specific code (a source's pace, flnd.db's schema, error advice) lives in flnd's modules and is passed in. So each library reads on its own and could leave flnd as a package later. `tests/test_layers.py` lists them and checks it.
- **`flnd/cli.py`** is the thin CLI (argparse). It imports only `flnd`, so a script can do anything the CLI can.
- **`flnd/sources/`** has one module per built-in site (`fbk`, `ins`, `loi`, `mht`, `tik`, `tlg`, `twt`) and `_manifest.py`, the list `flnd --help` and target resolution read without importing a source. A source imports only `flnd.source` and the stdlib.
- **`docs/architecture.md`** is the map of the design; keep it true when a change moves a part. **`docs/`** holds the guides for people (see Writing).
- **Plugins** live outside the repo: in the user's plugin folder (`$FLND_SOURCES`, else `~/.config/flnd/sources/`), or as installed packages with a `flnd.sources` entry point. flnd never loads code from a home folder, since a home may be a folder someone else made.

## API and naming
- **Short names**, one word where one word says it.
- **An action is a verb** (`download`, `follow`, `sync`, `unfollow`, `check`, `retry`), with no `get_`, `to_`, `try_` or `load_from_` names. A read is a property (`fl.logins`), or a method with keyword filters (`fl.files(target=…, kind=…)`).
- **One word, one meaning**, across the whole package. `retry` is the verb, so an error's advice is `hint`. `wait` waits for a page or a run, so a timed sleep is `pause`. `File` is a file to download (`flnd.source`), so a saved file's row is `Record`.
- **Options are keyword-only, at most 5.** Past that, use a record or the config file.
- **Progress is the event stream** (`Saved`, `Skipped`, `Failed`, `Progress`). A script iterates a run. There are no callbacks and no hooks: one way to do one thing.
- **A resource is a context manager** (`with flnd.open(home) as fl:`), so nothing leaks a thread, a browser or a connection.
- **One spelling:** no aliases and no deprecated names.
- **Errors are `flnd.Error` subclasses** from the one hierarchy in `flnd/errors.py` (`docs/errors.md`). The message is `str(e)`, and `e.hint` says what to do. A user error never shows a traceback. `flnd.Stopped` (Ctrl-C) derives from `KeyboardInterrupt`, never from `flnd.Error`.
- **The source API is the most important API.** A source declares what it is (code, domains, login, routes, categories) and implements a few well-named hooks. A mistake in a source fails when the class is made, with a message naming the attribute and the fix. The source API is versioned (`api = 1`).
- `tests/test_names.py` checks the naming rules on the public surface. Its allowlist may only shrink.

## Writing: code that people can read
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
- `architecture`, `library`, `cli`, `writing-a-source`, `errors`, `storage`, `media` and `performance`;
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
- Keep the hot-path rules (`docs/architecture.md`, `docs/performance.md`):
  - one asyncio loop for network and browser;
  - no extra `stat` calls or lock files per file (a `.part` is its own lock);
  - one database transaction per post, not per file and not on a writer thread;
  - lazy imports, so `flnd --help` stays under 40 ms.
- A regression guard in `make check` counts things (requests, `stat` calls, statements, imported modules) instead of timing them.

## Models
- **Mechanical changes use a low-cost model** (user, 2026-11-12): Claude Haiku in Claude Code, or the low-cost tier of whichever tool is in use.
  - Mechanical means renames, moves, import updates, applying a given pattern to many call sites, regenerating snapshots, merge conflicts with an obvious answer, and doc edits that follow a decision.
  - The orchestrator sets the model when it delegates such a task, for example `model: "haiku"` on a Claude Code subagent.
- Design, new behaviour, debugging, security-sensitive code, reviews and the docs for human readers (`docs/` and docstrings) keep the stronger default model.
