# Agent Guidelines

## Commit Convention

All commits must strictly follow this message format:

```
<action>:<summary>
```

### Format Rules
- `<action>`: Lowercase verb or type representing the action taken (e.g., `feat`, `fix`, `chore`, `docs`, `refactor`, `test`, `style`, `perf`).
- `:`: Colon immediately follows `<action>` without spaces before it.
- `<summary>`: Concise description of the changes made, written in imperative lowercase without a trailing period.
- **NO CO-AUTHOR**: Do not include `Co-authored-by:` trailers or any other co-author attributions.

### Examples
- `feat:implement continuous scroll to end for all crawlers`
- `fix:support user reposts extraction in instagram extractor`
- `fix:add user originals timeline posts crawling for twitter`
- `test:add unit tests for source parsing and scrolling logic`
- `chore:initialize git repository and github remote`
- `docs:update readme with posts and reposts folder layout`

## Git
- Commit as `feiluvnana`, SSH-signed, through the repo-local git config. Never change the global git config.
- One logical change per commit, with `check` green at every commit. A `shared` module's redesign is one commit: the module, its tests, flnd's call sites and the API snapshot.
- Commit or push only when asked.

## Checks
- `uv run --no-project scripts/dev.py check` (or `make check`): lint, pyright (0 errors, 0 warnings) and unittest. It must be green before every commit.
- `uv run --no-project scripts/dev.py setup` syncs `.venv` from `uv.lock`. Change dependencies in `pyproject.toml`, then run `uv lock`.
- After any change to a public name in `shared` or `flnd`'s API, run `uv run --no-project scripts/dev.py api` to regenerate `tests/data/api.txt`. Read its diff: it is the review of the rename.

## Privacy and live data
- Never read, print or commit the `*_cookies.txt` files in the repo root, or anything under `.flnd/`. Never put a cookie value, key or password in a log line, an exception message or a test.
- Use only example handles in code, tests and docs: `example_user`, `example_creator`, `example_handle`, `example.page`, `examplechannel`, and the domain `example.com`. Never use a real username.
- Live checks against real sites are done by hand on the authorized test accounts, never in unit tests. Unit tests fake the network (`httpx.MockTransport`), the browser and the clock.

## Layout
- `shared/` is the toolkit. Every file starts with `_`, and `shared/__init__.py` is the only public surface.
- `flnd/` keeps its entry points and engine modules at the top level: `cli`, `engine`, `source` (what sources import), `site`, `job`, `post`, `settings`, `repair` and `session` (options).
- `flnd/common/` holds the engine side's helpers (`auth`, `compress`, `files`, `net`, `pace`, `target`).
- `flnd/sources/` holds one module per site. `flnd/sources/common/` holds what sources share (`dash`, `relay`, …).
- A source imports only `flnd.source`, `flnd.sources.common`, `shared` and the stdlib (plan/phase-6/redesign.md, B4).

## API and naming
- Follow plan/phase-6/redesign.md §2:
  - a verb for an action, a noun for a read;
  - a property for a read with no arguments;
  - no `get_`, `to_`, `load_from_` style prefixes;
  - options after the first parameter are keyword-only;
  - one word means one thing: `dest`, `timeout`, `progress(done, total)`, `label`, `level`.
- One spelling: no aliases or deprecated names. `shared` has one user, flnd, which is changed in the same commit.
- Every module's errors share a `<module>.Error` base, under `shared.UnsafeError`, `BrokenError` or `RefusedError` where they fit.
- `tests/test_names.py` enforces these rules, and the writing rules a machine can check. Its allowlist may only shrink.

## Writing (plan/phase-7/readable.md, user 2026-11-13)
- Code a reader can follow top to bottom; more code than prose in every file. `shared` and `flnd` both stay lean.
- A docstring is one sentence. An example only on a public entry point. `Args:`, `Returns:`, `Raises:` only where the name and type don't say it (`flnd/cli.py` keeps `Args:`: `shared.cli` reads help from them). About 10 lines at most; a module docstring at most 15.
- A comment says why: a site quirk, a magic value, a lock, a platform branch, a non-obvious choice. Never what the next line does; never a plan id, a `.md` file, an issue number, a phase or "as before" (history goes in the commit message, a regression guard in a test name).
- Each fact once: a Protocol and its implementation aren't both documented.
- Lines of at most 100 characters; functions of about 30 code lines and 3 levels of nesting at most. No one-caller helper unless its name explains something.
- No chained ternaries; no `bool | None` for three states (an `Enum`); no callback from a producer into its consumer; no writing to a frozen object. Five or more options become a record.
- A module is named for what it holds, never renamed on import; one word, one meaning across modules. A module reads: docstring, imports, constants, main entry point, helpers in call order.
- Plain over clever: declare what code would otherwise guess (a downloader that opens a browser says `browser = True`).

## Plans
- The plan is in `plan/`. In Phase 6, `plan/phase-6/redesign.md` wins over every other Phase 6 file, and `simplify.md` wins over the rest.
- Build in the order given in `plan/phase-6/README.md`.
- Ask the user before building a step whose decision is still open. Record each decision in the plan, with its date.

## Models
- **Mechanical changes use a low-cost model** (user, 2026-11-12): Claude Haiku in Claude Code, or the low-cost tier of whichever tool is in use.
  - Mechanical means renames, moves, import updates, applying a given pattern to many call sites, regenerating snapshots, merge conflicts with an obvious answer, and doc edits that follow a decision.
  - An orchestrator sets the model when it delegates such a task, for example `model: "haiku"` on a Claude Code subagent.
- Design, new behaviour, debugging, security-sensitive code and reviews keep the stronger default model.
