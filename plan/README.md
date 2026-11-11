# flntools plan

flntools gives scripts small helpers that make common libraries nicer to use. It does not replace them. flnkit tried to replace them and grew to about 38,800 lines: `net` alone was 3,100 lines and `browser` 3,300, because each module rebuilt its library's concepts (its own request, response, locator, and cookie types).

Every `shared` module follows three rules:

1. **Wrap one library; don't rebuild it.** A module adds defaults, removes boilerplate that scripts repeat, and adds a few functions the library lacks. It does not define its own versions of the library's types.
2. **Always hand back the real object.** `http.Client` *is* an `httpx.Client`. `browser.open` yields a Playwright `Page`. `store.sql` is the `sqlite3.Connection`. Anything the helper doesn't cover is one attribute away.
3. **Stay small.** About 150–300 lines per module. If a module goes past 400 lines, stop and recheck its scope before writing more.

```python
from shared import cli, fmt, log, prompt, style, theme          # Phase 1, stdlib only
from shared import http, browser, path, archive, crypto, store  # Phase 3
```

## Modules

| Module | Plan | File | Wraps | Extra |
| --- | --- | --- | --- | --- |
| `style` | [phase-1/style.md](phase-1/style.md) | `shared/_style.py` | ANSI codes | — |
| `fmt` | [phase-1/fmt.md](phase-1/fmt.md) | `shared/_fmt.py` | — | — |
| `theme` | [phase-1/theme.md](phase-1/theme.md) | `shared/_theme.py` | `termios` / `msvcrt` | — |
| `log` | [phase-1/log.md](phase-1/log.md) | `shared/_log.py` | — | — |
| `prompt` | [phase-1/prompt.md](phase-1/prompt.md) | `shared/_prompt.py` | — | — |
| `cli` | [phase-1/cli.md](phase-1/cli.md) | `shared/_cli.py` | `argparse` | — |
| `path` | [phase-3/path.md](phase-3/path.md) | `shared/_path.py` | `pathlib` | — |
| `archive` | [phase-3/archive.md](phase-3/archive.md) | `shared/_archive.py` | `zipfile`, `tarfile` | — |
| `crypto` | [phase-3/crypto.md](phase-3/crypto.md) | `shared/_crypto.py` | `hashlib`, `hmac`, `secrets`, `cryptography` | `crypto` for `seal` / `unseal` |
| `store` | [phase-3/store.md](phase-3/store.md) | `shared/_store.py` | `sqlite3` | `crypto` for `secrets` |
| `http` | [phase-3/http.md](phase-3/http.md) | `shared/_http.py` | `httpx` | `http` |
| `browser` | [phase-3/browser.md](phase-3/browser.md) | `shared/_browser.py` | Playwright | `browser` |

```mermaid
flowchart LR
  cli --> prompt & log & theme
  log --> theme
  prompt --> theme
  theme --> style
  http --> log & fmt & path
  browser --> log & http
  store --> crypto
  archive --> path
```

## Phases

Each phase ends with flnd working and `make test` green. Each plan file is self-contained: it says what it depends on, what to build, and its tests.

**Phase 1: the terminal.** [style](phase-1/style.md), [fmt](phase-1/fmt.md), [theme](phase-1/theme.md), [log](phase-1/log.md), [prompt](phase-1/prompt.md), and [cli](phase-1/cli.md), built in that order. Stdlib only.

**Phase 2: flnd on Phase 1.**
1. [sources](phase-2/sources.md): one registry for sources.
2. [output](phase-2/output.md): `print` calls become `log`.
3. [commands](phase-2/commands.md): `download`, `auth set-cookie`, prompt mode, and the missing-cookie prompt.

Phase 2 removes about 50 lines of `create_parser` and 7 lines of `format_bytes`. The 62 prints in the sources become about 62 log calls. What flnd gains is the look: colored levels, a live bar per file, a row per target, a cookie prompt instead of a dead end, and prompt mode for anyone who does not know the flags.

**Phase 3: library helpers.**
1. [path](phase-3/path.md), [archive](phase-3/archive.md), [crypto](phase-3/crypto.md), [store](phase-3/store.md), [http](phase-3/http.md), and [browser](phase-3/browser.md), in that order.
2. [flnd](phase-3/flnd.md): the sources move onto `http`, `browser`, and `path`, and [`flnd/utils.py`](../flnd/utils.py) mostly goes away.

## Conventions

- **Python and dependencies.** Python 3.10. Phase 1 modules use only the stdlib.
- **Public surface.** Every file in `shared` starts with `_`, and [`shared/__init__.py`](../shared/__init__.py) is the only public surface. `log`, `style`, and `theme` are objects, so their properties can check what is assigned. The other modules are modules. Because no public name is also a submodule, `import shared.log` can never replace the `log` object with a module.
- **Lazy imports.** `shared/__init__.py` loads `http`, `browser`, `crypto`, and `store` through a module `__getattr__`. `from shared import log` never imports httpx or Playwright.
- **Missing extras.** A missing extra raises `ImportError("shared.http needs httpx: pip install 'flntools[http]'")` on first use.
- **Readable code.** A module puts its public names first, then the small helpers they call. Names say what the value is. A comment is a plain sentence where a reader would otherwise have to reconstruct the rule. A comment does not restate the next line.
- **Tests.** `unittest`, with one `tests/test_<module>.py` per module. Terminal tests use a fake `theme.screen`. `http` tests use `httpx.MockTransport`. `browser` tests use a local HTML file and are skipped when Chromium is not installed.

## pyproject

```toml
[project.optional-dependencies]
http = ["httpx>=0.27.0"]
browser = ["playwright>=1.40.0"]
crypto = ["cryptography>=42"]
```

flnd keeps `httpx`, `playwright`, and `parsel` as normal dependencies, because flnd always needs them. The extras matter for other scripts that only use part of `shared`.

## Makefile

[`Makefile`](../Makefile) has four targets. `install` and `run` go away.

```makefile
PYTHON ?= 3.14

.PHONY: setup test lint flnd

setup:
	uv venv .venv --python $(PYTHON)
	uv pip install -e '.[http,browser,crypto]' --python .venv/bin/python
	.venv/bin/playwright install chromium

test:
	.venv/bin/python -m unittest discover -s tests

lint:
	.venv/bin/python -m compileall -q flnd shared

flnd:
	.venv/bin/flnd $(ARGS)
```

`requires-python` stays `>=3.10`. Before a release, run `make setup PYTHON=3.10 test` to catch any use of 3.11+ features. `lint` is only a syntax check. `make flnd ARGS="download ins:example_user"` passes arguments through.

## Out of scope

These are what made flnkit big. They stay out.

- **cli:** config files, env prefixes, shell completion, before-hooks, dataclass flag groups, and conflicts and implies.
- **terminal:** a multiline editor, a fuzzy finder, full-screen widgets, a box model, file rotation, JSON log mode, extra log levels, and a public `format_log` / `format_help` / `format_prompt`.
- **http:** its own request or response types, middleware, a crawler, an HTTP server, response caching, and auth plugins.
- **browser:** its own locator or element types, a login-flow framework, and stealth beyond hiding `navigator.webdriver`.
- **crypto:** JWT, TOTP, certificates, asymmetric keys, and a password-hashing API.
- **store:** an ORM, a query builder, migrations, and a cache with expiry.
- **path / archive:** file watching, grep, tree printing, disk usage, and file locks.
- **everything:** async variants and subprocess helpers.
