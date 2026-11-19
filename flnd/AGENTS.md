# Agent guidelines

The rules shared by the whole repository (commits, privacy, checks that apply to every project,
writing, models) are in `../AGENTS.md`. This file holds flnd's own rules. Run every command in
this folder, `flnd/`.

These rules are for anyone who changes flnd: how to check, lay out, name, write and measure. The
`src/docs/architecture.md` page is the map of the code, and says why it is built the way it is.

## Checks
- `make check` runs ruff, then the tests. It must be green at every commit, not only the last
  one. Ruff's rules are `select = ["F", "E9", "B", "UP", "I", "ASYNC", "RUF006"]` in
  `pyproject.toml`.
- `make setup` syncs `.venv` from `uv.lock`. To change a dependency, edit `pyproject.toml`,
  then run `uv lock`.
- `make test ARGS="-k fetch"` runs the tests, or some of them. It sets `FLND_TEST_NO_PROFILES=1`
  and runs Python with `-W "error:coroutine:RuntimeWarning"`, so a never-awaited coroutine
  fails its test.
- `make lint` runs ruff alone.
- `make api` regenerates the public surface snapshot (`tests/data/api.txt`). After any public
  change, read its diff: it is the review of that change.
- `make bench` runs the offline benchmarks in `tests/bench/`. `make bench ARGS=--gate` fails on
  a regression over 15% against `tests/bench/gates.json`. Run the benchmarks before a speed
  change, to get the "before" numbers. Run `ARGS=--gate` on any change that touches speed,
  before its commit. `ARGS=--record` stores a run as the new baseline. See
  `src/docs/performance.md`.
- `make check PY=3.11` runs on the oldest Python that `requires-python` allows. Run it before
  a release.
- `make docs` builds the API reference into `build/docs/`. `make dist` builds a one-file binary.
- `make clean` removes `build`, `dist` and `.try`.
- **On Windows**, install make with `winget install ezwinports.make`, or use the one that comes
  with Git for Windows. Each Makefile recipe is one `uv` command you can also type yourself.

## Privacy
- Use only example handles in code, tests and docs: `example_user`, `example_creator`,
  `example_handle`, `example.page`, `examplechannel`, and the domain `example.com`.
- `FLND_TEST_NO_PROFILES=1` (set by `make test`) makes the tests refuse real browser profiles,
  the real OS keychain and `~/.config/flnd/`.

## Layout
Two packages sit side by side in this folder: `src/`, which installs as the package `flnd`,
and `flnkit/`, a standalone package imported as `flnkit`. Its tree:

```text
src/                 the package flnd
  __init__.py      public names, each loaded on first use
  __main__.py      python -m flnd
  _library.py      open, Flnd, and the groups Targets, Auth, Plugins, Extension, Config
  cli.py           the commands
  errors.py        the one error hierarchy
  plugin.py        plugin API 2 (public)
  testing.py       the plugin test kit (public)
  engine/
    run.py         Run, the loop's thread, the event channel, Ctrl-C, the journal
    events.py      the events, Report, State, and the rows the reads return
    jobs.py        the scheduler: Job, pools, priorities, retry policy, holds, segments, lanes
    crawl.py       one target: listing, smart sync, early stops, the Ledger
    save.py        a file job: transfer, dedupe, unpack, sidecar, exec, the commit
    resolve.py     target text -> (plugin, Target, categories)
    plugins.py     the plugin manager
    auth.py        accounts, switching, the sealed vault, login methods
    extension.py   the native-messaging host, the protocol, the relay
    store.py       flnd.db: schema, migrations, rows, check and repair
    config.py      config.toml: structs, reading, editing
  ui/              imports only flnkit, ui/, engine/events.py and src/errors.py
    theme.py out.py screen.py lists.py forms.py
  plugins/         built-ins, importing only flnd.plugin and the standard library
    __init__.py _manifest.py fbk.py ins.py loi.py mht.py tik.py tlg.py twt.py web.py
  extension/       manifest.json background.js page.js popup.html popup.js
  docs/
flnkit/              the standalone package flnkit, imports nothing of flnd
  net.py browser.py files.py html.py dig.py text.py clock.py db.py cookies.py vault.py media.py
  stream.py
```

The import rules, checked by `tests/test_layers.py` by reading every file's imports:
1. `flnkit/*` imports only `flnkit/*`, the standard library and third-party packages: nothing
   of flnd. `src/errors.py` imports only `flnkit/errors.py`.
2. `src/ui/*` imports only `src/ui/*`, `flnkit`, `src/engine/events.py` and `src/errors.py`.
3. `src/engine/*` imports `src/engine/*`, `flnkit`, `src/ui/*`, `src/errors.py` and
   `src/plugin.py`. `src/engine/events.py` imports only `src/errors.py`, `src/plugin.py`,
   `flnkit`, `msgspec` and the standard library.
4. `src/plugin.py` imports only `flnkit`, `src/errors.py` and the standard library.
   `src/testing.py` imports `flnd`, `src/plugin.py`, `src/engine/*` and `flnkit`.
   `src/_library.py` imports `src/engine/*`, `flnkit`, `src/errors.py` and `src/plugin.py`.
5. `src/plugins/*` imports only `flnd.plugin` and the standard library, plus its own SDK
   (`telethon`, `qrcode`), imported inside a function.
6. `src/cli.py` imports only `flnd`, `src/ui/*` and `src/plugins/_manifest.py`.
7. `src/__init__.py` imports nothing at import time beyond the standard library.

Also: `flnkit/` never imports `src/engine/`, `src/ui/` or `src/plugins/`, and `src/ui/` never
imports `src/plugins/`.

## Concurrency
- One event loop per run.
- Plugin code is async.
- Only `engine/jobs.py` (and `ctx.pause`, on the run's clock) retries, sleeps, limits, or picks
  threads or processes.
- A commit is one per post, and it runs on the loop. It never blocks the loop.

## Output
- Everything printed goes through `src/ui` and its theme.
- No `print`. No colour or glyph outside `theme.py`.
- Debug output goes to the journal only.

## Flags
- Add a flag only when the target, the config and the library are all wrong for the choice.

## Naming
- Short names.
- An action is a verb, with no `get_`, `to_` or `try_` names. A read is a property, or a
  method with keyword filters.
- One word, one meaning across the package. `plugin` is the one word for an extension that
  handles a site, never `source`. `retry` is the verb; `hint` is the advice on an error;
  `hold` is a timed wait; `Record` is a saved file's row.
- At most 5 options, and they are keyword-only.
- A resource is a context manager (`with flnd.open(home) as fl:`).
- No aliases.
- `tests/test_names.py` checks the public surface. Its allowlist may only shrink.

## Errors
- Every error is a `flnd.Error` subclass from the one hierarchy: `flnkit/errors.py` defines
  what the kit raises, and `src/errors.py` re-exports it and adds flnd's own classes, hints
  and `classify`:

```text
Error(Exception)            .hint: str (what to do; "" when nothing)
├── UsageError              exit 2; raised before any request
├── LoginError              exit 3; a 401 or 403 from the plugin's own domains,
│                           a declared wall
├── SiteError
│   ├── Blocked             .wait: float | None (seconds the site asked for)
│   ├── NotFound            404 or 410: recorded gone, never asked again
│   ├── Hidden              it exists, but this account can't see it
│   └── Changed             the answer can't be read; a scrubbed sample is kept
├── NetworkError            timeout, reset, DNS, TLS; retried
├── DiskError               exit 4; stops the run
├── ToolError               missing tool: exit 4, the run stops; a failing tool fails its file
└── PluginError             a bug in a plugin; exit 4 for a broken plugin
Stopped(KeyboardInterrupt)  Ctrl-C; exit 130
```

- `str(e)` is one line, and `e.hint` is one line.
- `errors.classify(exception)` turns any exception into one of these. Anything a plugin lets
  escape becomes a `PluginError`.
- `errors.disk(OSError)` is the one disk mapper.
- A user error never shows a traceback.
- `Stopped` derives from `KeyboardInterrupt`, never from `flnd.Error`.

## Plugin API
- The API grows by additions within API 2: a new verb, a new optional field. An old plugin
  keeps working, so such a change needs no new major version.
- A change that breaks plugins means API 3, plus an adapter that runs API 2 plugins with a
  deprecation warning for at least one release.
- `flnd.plugin.API` and `src/docs/writing-a-plugin.md` ("Plugin API versions") say which
  version this flnd has.

## Bugs
- Write the failing test first, and run it against the unfixed code. It must fail there.
- Put the test and its fix in the same commit.

## Speed
- Measure before any speed change, with a benchmark. Keep the numbers.
- A `perf:` commit quotes both runs (before and after) in its message.
- A change that makes a gated number worse says why.
- Never claim a speed-up you did not measure.
- The counting guards in `make check` (requests, `stat` calls, SQL statements, imported
  modules) stay.

## Dead code
- The dead-name, docstring and API-snapshot checks cover all of `src/` and `flnkit/`. Remove
  code they flag.

## Dependencies
- Each dependency has a reason beside it in `pyproject.toml`.
- The planned set is curl_cffi, playwright, selectolax, cryptography, telethon, cryptg, qrcode,
  rich, msgspec, tomlkit and textual. Textual stays only if `ui.open` is at most 250 ms.

## Size
- The limits live in `tests/test_size.py`: `CODE_LINES` for `src/` and `flnkit/`, and
  `TEST_LINES` for `tests/`.
- They only go down, except when a new feature needs room. Then raise the limit in the same
  commit, and say why in the commit body.

## Writing
- A module docstring is 1 to 3 lines. It names the `src/docs` page that explains the module.
- A public name has a summary line. Then `Args:`, `Returns:` and `Raises:`, only when they add
  something to the name and type.
- Comments cover only what the code cannot say. Keep them short.
- A behaviour change updates its `src/docs` page in the same commit.
