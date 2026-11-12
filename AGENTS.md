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
- `tests/test_names.py` enforces these rules. Its allowlist may only shrink.

## Docs and comments
- Every module, public class and public function has a docstring to plan/phase-6/comments.md: one plain sentence, a short example, then `Args:`, `Returns:` and `Raises:`.
- Comments explain why, at every magic value, site quirk, lock, caught exception and platform branch. A comment never restates the next line.

## Plans
- The plan is in `plan/`. In Phase 6, `plan/phase-6/redesign.md` wins over every other Phase 6 file, and `simplify.md` wins over the rest.
- Build in the order given in `plan/phase-6/README.md`.
- Ask the user before building a step whose decision is still open. Record each decision in the plan, with its date.

## Models
- **Mechanical changes use a low-cost model** (user, 2026-11-12): Claude Haiku in Claude Code, or the low-cost tier of whichever tool is in use.
  - Mechanical means renames, moves, import updates, applying a given pattern to many call sites, regenerating snapshots, merge conflicts with an obvious answer, and doc edits that follow a decision.
  - An orchestrator sets the model when it delegates such a task, for example `model: "haiku"` on a Claude Code subagent.
- Design, new behaviour, debugging, security-sensitive code and reviews keep the stronger default model.
