# flntools plan

flntools is flnd, a media downloader, and `shared`, the toolkit it is built on. flnkit tried to be a general framework that replaced the libraries it used, and grew to about 38,800 lines: `net` alone was 3,100 lines and `browser` 3,300. `shared` exists to avoid that.

## Position

- **`shared` is flnd's toolkit, not a general library product.** flnd is its only user, since loi and tlg became flnd sources. The package boundary stays, because it keeps the engine and the sources clean. Nothing is added for imaginary outside users.
- **Each module is small, with one obvious way to use it.** If the standard library already does the job, the module doesn't exist.
- **Ceiling.** `shared` stays under **4,500 lines of code** (about 3,600 after Phase 4). Going over means removing something first. Lines of code don't count docstrings, comments or blank lines, so the size limits never push against documentation.

## Rules

1. **Wrap one library; don't rebuild it.** A module adds only what scripts would otherwise write by hand.
2. **Every wrapper has `.raw`.** A wrapper is a small object with the methods a script needs, and `.raw` is the real library object: a `Browser`'s Playwright `BrowserContext`, a page's Playwright `Page`, a selection's `parsel.Selector`, `kv`'s `sqlite3.Connection`, a worker pool's executor. Anything rarer goes through `.raw`, so a wrapper never grows into its library's whole API.
3. **Stay small.** About 150–300 lines of code per module. A module is all of its files: `cli` is `_cli.py` + `_params.py`, and `theme` is `_theme.py` + `_screen.py`. Past 400 lines, stop and recheck the scope.
4. **Safe by default.** Anything that reads untrusted input (archives, cookies, HTML) refuses dangerous input unless the caller explicitly allows it.

```python
from shared import style, fmt, theme, log, prompt, cli        # stdlib only
from shared import archive, crypto, cookies, kv, selector, browser, worker, bundler
```

## Modules after Phase 4

| Module | Plan | File | Wraps | Extra |
| --- | --- | --- | --- | --- |
| `style` | [phase-1](phase-1/style.md), [trim](phase-4/terminal.md#style) | `_style.py` | ANSI codes | — |
| `fmt` | [phase-1](phase-1/fmt.md), [phase-4](phase-4/fmt.md) | `_fmt.py` | — | — |
| `theme` | [phase-1](phase-1/theme.md), [trim](phase-4/terminal.md#templates-are-deleted) | `_theme.py`, `_screen.py` | `termios` / `msvcrt` | — |
| `log` | [phase-1](phase-1/log.md), [trim](phase-4/terminal.md#log) | `_log.py` | — | — |
| `prompt` | [phase-1](phase-1/prompt.md), [huh look](phase-4/prompt.md) | `_prompt.py` | — | — |
| `cli` | [phase-1](phase-1/cli.md), [trim](phase-4/terminal.md#cli) | `_cli.py`, `_params.py` | `argparse` | — |
| `archive` | [phase-4](phase-4/archive.md) | `_archive.py` | `zipfile`, `tarfile`, `pyzipper`, `py7zr` | `archive` |
| `crypto` | [phase-4](phase-4/crypto.md) | `_crypto.py` | `hashlib`, `cryptography` | `browser` or `cryptography` |
| `kv` | [phase-4](phase-4/kv.md) | `_kv.py` | `sqlite3` | — |
| `cookies` | [phase-4](phase-4/cookies.md) | `_cookies.py` | `sqlite3`, `cryptography` | `browser` for `load_from_browser` |
| `selector` | [phase-4](phase-4/selector.md) | `_selector.py` | parsel, jmespath, python-jsonpath | `selector` |
| `browser` | [phase-4](phase-4/browser.md) | `_browser.py` | Playwright | `browser` |
| `worker` | [phase-4](phase-4/worker.md) | `_worker.py` | `concurrent.futures`, `asyncio` | — |
| `bundler` | [phase-4](phase-4/packaging.md#bundler) | `_bundler.py` | PyInstaller | `bundler` |

Removed in Phase 4:
- **`http`:** downloads, retries and signing moved into flnd's [engine](phase-4/engine.md#downloads); response parsing moved to `selector`.
- **`path`:** `pathlib` covers it, and `safename` became `fmt.filename`.
- **`store`:** became `kv`.
- **`scrape`:** became `selector`.

```mermaid
flowchart LR
  cli --> log & theme
  log --> theme
  prompt --> theme
  theme --> style
  browser --> log
  worker --> log
  archive --> fmt
  browser -.-> cookies
  browser -.-> selector
```

## Phases

Each phase ends with flnd working and `make check` green. Each plan file is self-contained: what it depends on, what to build, and its tests.

**Phase 1: the terminal (built).** [style](phase-1/style.md), [fmt](phase-1/fmt.md), [theme](phase-1/theme.md), [log](phase-1/log.md), [prompt](phase-1/prompt.md) and [cli](phase-1/cli.md). Stdlib only.

**Phase 2: flnd on Phase 1 (built).** [sources](phase-2/sources.md) (one registry), [output](phase-2/output.md) (`print` becomes `log`), and [commands](phase-2/commands.md) (`download`, `auth set-cookie`, prompt mode, the cookie prompt).

**Phase 3: library helpers (built).** [path](phase-3/path.md), [archive](phase-3/archive.md), [crypto](phase-3/crypto.md), [store](phase-3/store.md), [worker](phase-3/worker.md), [scrape](phase-3/scrape.md), [http](phase-3/http.md), [browser](phase-3/browser.md), and [flnd](phase-3/flnd.md) on them. Phase 4 reshapes most of these.

**Phase 4: simplify, the engine, and new sources.** Based on an independent review of Phases 1–3 and an approved draft. Built in this order:
1. [fixes](phase-4/fixes.md) and [typing](phase-4/typing.md): the review's bugs in the modules that stay, plus `py.typed` and pyright in standard mode, checked from here on.
2. The reshaped modules:
   - [fmt](phase-4/fmt.md): `filename`, `size`;
   - [archive](phase-4/archive.md): `zip` / `unzip`, with passwords, parts, conflict rules, magic-byte detection, and refusing traversal and bombs;
   - [crypto](phase-4/crypto.md): `hash_<algo>`, `encrypt_` / `decrypt_<cipher>`, keys;
   - [kv](phase-4/kv.md): a sqlite map with `ttl`;
   - [selector](phase-4/selector.md): a parsel wrapper;
   - [browser](phase-4/browser.md): `connect()` gives a `Browser` (incognito, a profile, or an attached running Chrome; real Chrome when installed, with its own user agent), then `open_page()`; `b.cookies` is the live browser's jar; `page.selector` reads the live page; `page.scroll` scrolls at a person's pace;
   - [cookies](phase-4/cookies.md): `load_from_browser`, `load_from_file`, `to_file`, `to_header`;
   - [worker](phase-4/worker.md): `map`, `race`, `pool`, each with `concurrency=`.
3. [engine](phase-4/engine.md): flnd splits into an engine and sources. The engine absorbs `http`, and adds one set of download options for every source (`--from`, `--since`, `--until`, `--unpack`, `--zip`), `flnd login <source>` with per-source methods (browser, cookie file, or the source's own), secrets, and SDK clients. Only `login` asks questions.
4. [terminal](phase-4/terminal.md) and [prompt](phase-4/prompt.md): templates deleted in favor of `theme.palette` and drawer functions; unused `style` and `cli` forms removed; prompt mode removed from `cli` (a command that needs an answer asks with `prompt` itself, as `flnd login` does); the huh prompt look.
5. [loi](phase-4/loi.md): LOIBUS, API-only. `loi:<post id>`; its VIP archives are `--from archive`. `flnd login loi` from browser cookies or a file.
6. [tlg](phase-4/tlg.md): Telegram on Telethon. `flnd login tlg` (from Telegram Web in your browser, a QR code, or phone and code), then `flnd download tlg:<chat> --from … --since …`.
7. [packaging](phase-4/packaging.md): `make dist`, through a one-function `shared.bundler` over PyInstaller; Chromium installed with `flnd install-browser`.

**Deferred.** These are not built until something real needs them: `log.track`; the runtime `type=` checks with TypedDict support; strict pyright and the `TypeVarTuple` overloads; `worker`'s `rate`, `setup` and `task`; HMAC; writing RAR.

## Conventions

- **Python and dependencies.** Python 3.11 and later. 3.10 reached its end of life in October 2026, and Pillow and PyAV have dropped it. `style`, `fmt`, `theme`, `log`, `prompt`, `cli`, `kv` and `worker` use only the stdlib, and so does `cookies` apart from `load_from_browser`.
- **Public surface.** Every file in `shared` starts with `_`, and [`shared/__init__.py`](../shared/__init__.py) is the only public surface.
- **Lazy imports.** `archive`, `crypto`, `cookies`, `kv`, `selector`, `browser`, `worker` and `bundler` load through a module `__getattr__`, with a `TYPE_CHECKING` block for type checkers. `from shared import log` never imports Playwright, parsel or cryptography.
- **Missing extras.** A missing extra raises `ImportError("shared.browser needs playwright: pip install 'flntools[browser]'")` on first use.
- **Readable code.** Code is written for the next person to read it, and kept short:
  - **Order.** A module reads top to bottom: public names first, then the helpers they call, in the order they are called.
  - **Docstrings.** Every public function and class starts with one plain sentence saying what it does, then a short example. Rules a caller must know (what raises, what is slow, what can't be undone) come after.
  - **Comments** explain *why*, at the place a reader would otherwise have to work out a rule: why a timed-out thread can't be stopped, why archive links are refused. A comment never restates the next line.
  - **Names** say what a value is (`queued`, `stop_requested`, `jar`), never `tmp`, `data`, `flag`, `x` or a one-letter loop variable outside a short comprehension.
  - **Concise.** One way to do each thing, no dead code, no option without a caller. When a function grows past about 40 lines, it is split where a reader would naturally pause.
- **Tests.** `unittest`, with one `tests/test_<module>.py` per module. Terminal tests use a fake `theme.screen`. Browser tests use local pages, and are skipped without Chromium. flnd's network tests use `httpx.MockTransport`.

## Naming

1. **Use the familiar word.** If Unix or the stdlib already has a name, use it: `archive.zip` / `unzip`, `kv.open`, `worker.map`, `fmt.filename`.
2. **Related functions share a prefix that says what they are:** `crypto.hash_sha256`, `crypto.encrypt_aes_gcm`, `page.wait_for_url`, `page.on_response`.
3. **A wrapper keeps its library's names** for things it passes through: `user_agent`, `viewport`.
4. **Properties for values, methods for actions.** Something you only read, with no arguments, is a property named as a noun: `page.selector`, `page.url`, `selection.text`, `selection.first`, `b.cookies.all`, `b.pages`, `.raw`. A method is used only when it needs arguments, makes a request, or changes something. A read that needs an argument is `get_<thing>(…)`: `kv.get_keys(prefix=)`. A query that returns another wrapper is named for its language: `css`, `xpath`, `jmespath`, `jsonpath`. A network request is `fetch_<thing>(…)`: `session.fetch_json(url)`. Clear names are worth a few characters.
5. **Times are seconds, as a `float`.** A wrapper converts to its library's unit itself.
6. **Classes are `CapWords`, and exceptions end in `Error`.** Bad input data raises a `ValueError` subclass (`archive.UnsafeError`, `crypto.CryptoError`); a misuse raises a `RuntimeError` subclass (`prompt.PromptError`). Callers handle `worker.TaskError` and `cli.UsageError` on purpose.
7. **The main input is positional, and options are keyword-only.**
8. **Builtin names** are reused only where the name is the obvious one in its namespace, and the module never needs the builtin: `kv.open`, `archive.zip`, `prompt.input`.

flnd's own helpers keep normal `snake_case`. The chalk-style color names (`style.bright_red`, `bg_red`) keep chalk's compounds.

## pyproject

```toml
[project.optional-dependencies]
archive = ["pyzipper", "py7zr"]                                  # password zips and 7z; plain zip and tar need nothing
selector = ["parsel>=1.9.0", "python-jsonpath"]
browser = ["playwright>=1.40.0", "cryptography>=42"]
bundler = ["pyinstaller"]
```

flnd's own dependencies are `httpx`, `playwright`, `parsel`, `python-jsonpath`, `cryptography`, `telethon`, `pyzipper` and `py7zr`, because flnd always needs them. The dev group adds `pyright` and `typing_extensions`.

## Makefile

```makefile
PYTHON ?= 3.14

.PHONY: setup test lint typecheck check flnd dist

setup:
	uv venv .venv --python $(PYTHON)
	uv pip install -e '.[archive,selector,browser,bundler]' --group dev --python .venv/bin/python
	.venv/bin/playwright install chromium

test:
	.venv/bin/python -m unittest discover -s tests

lint:
	.venv/bin/python -m compileall -q flnd shared

typecheck:
	.venv/bin/pyright

check: lint typecheck test

flnd:
	.venv/bin/flnd $(ARGS)

dist:
	.venv/bin/python -c "from shared import bundler; bundler.build('flnd/__main__.py', name='flnd')"
```

`requires-python` is `>=3.11`. Before a release, run `make setup PYTHON=3.11 check`. `make flnd ARGS="download ins:example_user"` passes arguments through.

## Out of scope

These are what made flnkit big. They stay out.

- **cli:** config files, env prefixes, shell completion, before-hooks, dataclass flag groups, and conflicts and implies.
- **terminal:** a multiline editor, a fuzzy finder, full-screen widgets, a box model, file rotation, JSON log mode, extra log levels, string templates, and detecting the terminal background (OSC 11).
- **browser:** its own locator or element types, a login-flow framework, stealth beyond hiding `navigator.webdriver` and scrolling at a person's pace, and wrapping Playwright methods that `.raw` already gives.
- **crypto:** JWT, TOTP, certificates, asymmetric keys, and a password-hashing API.
- **kv:** an ORM, a query builder, migrations, batches, and storing secrets (flnd's engine seals them).
- **archive:** writing RAR, extracting nested archives automatically, file watching, and disk usage.
- **selector:** Scrapy itself, spiders, link following, item pipelines, and robots.txt.
- **worker:** a persistent or distributed job queue, scheduling, priorities, and a global thread limit.
- **everything:** async variants of the APIs (`worker` runs async functions, and every `shared` API stays synchronous), and subprocess helpers.
