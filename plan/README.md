# flntools plan

flntools is flnd, a media downloader, and `shared`, the toolkit it is built on. flnkit tried to be a general framework that replaced the libraries it used, and grew to about 38,800 lines: `net` alone was 3,100 lines and `browser` 3,300. `shared` exists to avoid that.

## Position

- **`shared` is flnd's toolkit, not a general library product.** flnd is its only user, since loi and tlg became flnd sources. The package boundary stays, because it keeps the engine and the sources clean. Nothing is added for imaginary outside users.
- **Each module is small, with one obvious way to use it.** If the standard library already does the job, the module doesn't exist.
- **No size ceiling.** The user removed `shared`'s ceiling (about 5,000 lines of code) on 2026-11-12. A module is judged by how good and useful its API is, not by its line count. Earlier phase files still mention the ceiling, as history.

## Rules

1. **Wrap one library; don't rebuild it.** A module adds only what scripts would otherwise write by hand.
2. **Every wrapper has `.raw`.** A wrapper is a small object with the methods a script needs, and `.raw` is the real library object: a `Browser`'s Playwright `BrowserContext`, a page's Playwright `Page`, a selection's `parsel.Selector`, `kv`'s `sqlite3.Connection`, a worker pool's executor. Anything rarer goes through `.raw`, so a wrapper never grows into its library's whole API.
3. **Stay small.** About 150–300 lines of code per module. A module is all of its files: `cli` is `_cli.py` + `_params.py`, and `theme` is `_theme.py` + `_screen.py`. Past 400 lines, stop and recheck the scope. **`media` is the one module allowed past 400** (about 1,100 in six files): it wraps two engines, Pillow and ffmpeg, for three kinds of file, and splitting it would bring back the two-module API its [plan](phase-5/media.md#files-and-budget) removes.
4. **Safe by default.** Anything that reads untrusted input (archives, cookies, HTML) refuses dangerous input unless the caller explicitly allows it.

```python
from shared import style, fmt, theme, log, prompt, cli        # stdlib only
from shared import archive, crypto, cookies, kv, selector, browser, worker, bundler, media
```

## Modules

| Module | Plan | File | Wraps | Extra |
| --- | --- | --- | --- | --- |
| `style` | [phase-1](phase-1/style.md), [trim](phase-4/terminal.md#style) | `_style.py` | ANSI codes | — |
| `fmt` | [phase-1](phase-1/fmt.md), [phase-4](phase-4/fmt.md) | `_fmt.py` | — | — |
| `theme` | [phase-1](phase-1/theme.md), [trim](phase-4/terminal.md#templates-are-deleted) | `_theme.py`, `_screen.py` | `termios` / `msvcrt` | — |
| `log` | [phase-1](phase-1/log.md), [trim](phase-4/terminal.md#log) | `_log.py` | — | — |
| `prompt` | [phase-1](phase-1/prompt.md), [huh look](phase-4/prompt.md) | `_prompt.py` | — | — |
| `cli` | [phase-1](phase-1/cli.md), [trim](phase-4/terminal.md#cli) | `_cli.py`, `_params.py` | `argparse` | — |
| `archive` | [phase-4](phase-4/archive.md) | `_archive.py` | `zipfile`, `tarfile`, `pyzipper`, `py7zr` | `archive` |
| `crypto` | [phase-4](phase-4/crypto.md) | `_crypto.py` | `hashlib`, `cryptography` | `browser` (for `cryptography`) |
| `kv` | [phase-4](phase-4/kv.md) | `_kv.py` | `sqlite3` | — |
| `atomic` | [phase-6](phase-6/redesign.md#fmt-style-theme-cli-archive-bundler) | `_atomic.py` | `os.replace` | — |
| `cookies` | [phase-4](phase-4/cookies.md) | `_cookies.py` | `sqlite3`, `cryptography` | `browser` for `from_browser` |
| `selector` | [phase-4](phase-4/selector.md) | `_selector.py` | parsel, jmespath, python-jsonpath | `selector` |
| `http` | [phase-6](phase-6/redesign.md#http-new-fetch-download-response) | `_http.py` | httpx | `http` |
| `browser` | [phase-4](phase-4/browser.md) | `_browser.py` | Playwright | `browser` |
| `worker` | [phase-4](phase-4/worker.md) | `_worker.py` | `concurrent.futures`, `asyncio` | — |
| `bundler` | [phase-4](phase-4/packaging.md#bundler) | `_bundler.py` | PyInstaller | `bundler` |
| `media` | [phase-5](phase-5/media.md), [ffmpeg](phase-5/ffmpeg.md) | `_media.py`, `_ffmpeg.py`, `_pillow.py`, `_media_image.py`, `_media_video.py`, `_media_audio.py` | Pillow, pi-heif, the ffmpeg program (imageio-ffmpeg) | `media` |

Removed in Phase 4 (`http` came back in Phase 6, as a module of its own):
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
  media --> log
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

**Phase 5: media.** Built in this order:
1. [media](phase-5/media.md): `shared.media`, one module for photos, videos and audio, with `probe`, `convert`, `compress`, `check` and `sniff` for any file, and `media.image`, `media.video` and `media.audio` for one kind. Photos go through Pillow and pi-heif in process; videos and audio through the ffmpeg program, which [ffmpeg](phase-5/ffmpeg.md) gets from the `imageio-ffmpeg` wheel and runs behind input whitelists.
2. [flnd on media](phase-5/flnd.md) (built): extensions from content, DASH video and audio merged (Facebook and Instagram), downloads checked, `--compress-image` / `--compress-video` with `--format-image` / `--format-video`, and `flnd sync` with its saved target settings in `.flnd/targets.db`.

**Phase 6: lean and simple, and the redesign.** Based on an audit of the whole repository on 2026-11-12 ([overview](phase-6/README.md)). The Phase 7 proposal, a fluent and consistently named API for sources, the engine and `shared`, was merged into it as [redesign](phase-6/redesign.md) the same day, and wins over the other Phase 6 files. Built in this order ([detail](phase-6/README.md#order)):
1. [fixes](phase-6/fixes.md): 16 bugs, among them corrupted downloads on Windows, failed targets shown as ok with exit status 0, and Twitter's login sent to its CDNs. **Built.**
2. [setup](phase-6/setup.md): `uv sync` with a committed `uv.lock`, the browser downloaded only when there is no Chrome, every task in a stdlib `scripts/dev.py`, and a Makefile that runs the same on every OS. **Built.**
3. [cli](phase-6/cli.md) and [pace](phase-6/pace.md): `flnd download`, `sync`, `login` and `logout`; `--time`; `--image`, `--video`, `--audio`; saved settings; Chrome only; the pace. **Built.**
4. [the `shared` API](phase-6/redesign.md#part-c-the-shared-api): the [conventions](phase-6/redesign.md#2-api-conventions) (verbs for actions, nouns for reads, one vocabulary, fluent objects) with a test, then each module redesigned, and `http`.
5. [routes](phase-6/redesign.md#a3-targets-are-routes): targets read by `@target` patterns.
6. [categories, posts, `site` and the job](phase-6/redesign.md#a4-categories-are-methods), and the six sources rewritten on them, with [session](phase-6/session.md)'s rules and [dates](phase-6/dates.md).
7. [the engine API](phase-6/redesign.md#part-b-the-engine-api), one method per command, and [every credential encrypted](phase-6/redesign.md#b7-every-credential-encrypted).
8. [downloads](phase-6/redesign.md#b6-what-flnd-takes-from-download-managers-idm) that resume, split, keep 6 connections per host and refresh expired links.
9. [ins](phase-6/ins.md) and [flows](phase-6/flows.md): API first for every source, with what gallery-dl does.
10. [gallery-dl's other lessons](phase-6/redesign.md#b5-what-flnd-takes-from-gallery-dl): `sync` stopping at saved posts, cookie write-back, a `try` dev task.
11. ~~[classify](phase-6/classify.md): `--classify`, on `Media.labels`.~~ Removed 2026-11-13.
12. [ui](phase-6/ui.md): download rows that no longer blink, a prompt cursor where the text goes, and log lines in their level's color.
13. [comments](phase-6/comments.md): a docstring for every module, class and function, *why* comments at every non-obvious rule, and the redesign's docs.

**Phase 7: honest listings and a calm screen.** From a live Facebook run the user read on 2026-11-13 ([overview](phase-7/README.md)). Built in this order:
0. [output › columns and skips](phase-7/output.md#built-in-step-0): progress lines with the name last, skips summed per category, `download -v`. **Built** (`9df3e36`).
1. [layout](phase-7/layout.md): `.flnd/media/`, `.flnd/metadata/` and `.flnd/log/`, with a log of every run to audit later.
2. [shrink](phase-7/shrink.md): `--shrink` replaces `--image`, `--video` and `--audio`: a balanced compress in each file's own format.
3. [dates › one listing memory](phase-7/dates.md#1-one-listing-memory-in-the-job): each item listed once, under its dated name, in every source.
4. [dates › every file dated](phase-7/dates.md#2-every-file-dated): a source's `date` hook, then an estimate; `sync` dates saved files.
5. [links](phase-7/links.md): a file found in a second tab is symlinked there.
6. [output › status lines](phase-7/output.md#status-lines): live status lines by default; `-v` adds every line.
   And [lean](phase-7/lean.md): proposals to make flnd about 425 lines leaner, built with the step that touches each file.

**Deferred.** These are not built until something real needs them: `log.track`; the runtime `type=` checks with TypedDict support; strict pyright and the `TypeVarTuple` overloads; `worker`'s `rate`, `setup` and `task`; HMAC; writing RAR.

## Conventions

- **Python and dependencies.** Python 3.11 and later. 3.10 reached its end of life in October 2026, and Pillow and PyAV have dropped it. `style`, `fmt`, `theme`, `log`, `prompt`, `cli`, `kv` and `worker` use only the stdlib, and so does `cookies` apart from `from_browser`.
- **Public surface.** Every file in `shared` starts with `_`, and [`shared/__init__.py`](../shared/__init__.py) is the only public surface.
- **Lazy imports.** `archive`, `atomic`, `crypto`, `cookies`, `http`, `kv`, `selector`, `browser`, `worker`, `bundler` and `media` load through a module `__getattr__`, with a `TYPE_CHECKING` block for type checkers. `from shared import log` never imports Playwright, parsel, cryptography or Pillow.
- **Missing extras.** A missing extra raises `ImportError("shared.browser needs playwright: pip install 'flntools[browser]'")` on first use.
- **Readable code.** Code is written for the next person to read it, and kept short:
  - **Order.** A module reads top to bottom: public names first, then the helpers they call, in the order they are called.
  - **Docstrings.** Every module, public class and public function has one, from Phase 6 to the [comments](phase-6/comments.md) standard: one plain sentence saying what it does, a short example, then `Args:` for every argument, `Returns:`, `Raises:`, and the rules a caller must know (what is slow, what can't be undone).
  - **Comments** explain *why*, at the place a reader would otherwise have to work out a rule: why a timed-out thread can't be stopped, why archive links are refused. Every magic value, site quirk, lock, caught exception and platform branch gets one. A comment never restates the next line.
  - **Names** say what a value is (`queued`, `stop_requested`, `jar`), never `tmp`, `data`, `flag`, `x` or a one-letter loop variable outside a short comprehension.
  - **Concise.** One way to do each thing, no dead code, no option without a caller. When a function grows past about 40 lines, it is split where a reader would naturally pause.
- **Tests.** `unittest`, with one `tests/test_<module>.py` per module. Terminal tests use a fake `theme.screen`. Browser tests use local pages, and are skipped without Chromium. flnd's network tests use `httpx.MockTransport`.

## Naming

These are the [API conventions](phase-6/redesign.md#2-api-conventions) that the user asked for on 2026-11-12. They replace the earlier rules from Phase 6 step 4 on, and apply to every public name in `shared`, `flnd.source` and flnd's engine API. This is the summary; the full text, with the examples, is in redesign §2.

**Words** ([2.1](phase-6/redesign.md#21-words)):

- **W1** A verb changes something, waits for something, or talks to the network or a person: `save`, `open`, `fetch`, `wait`, `ask`. Everything else is a noun.
- **W2** A noun is a property when it takes no argument (`page.url`), and a method named for what it returns when it takes one (`store.keys(prefix="")`). What a read costs goes in its docstring, not its name.
- **W3** One word when one word says it, two only when one is ambiguous: `cookies.load`, `crypto.sha256`, `log.group`.
- **W4** The familiar word wins, the one the stdlib, Unix or the wrapped library already uses: `open`/`close`, `as_completed`, `goto`, `css`, `zip`/`unzip`.
- **W5** A boolean property is an adjective or a participle (`ok`, `finished`, `closed`), not `is_ok` or `done`.
- **W6** Plural for a collection, singular for one: `pages`, `keys()`; `first`, `page`.
- **W7** No prefix that only says "function": `get_`, `do_`, `to_`, `load_from_`, `make_`, `create_`. It's `jar.header`, not `cookies.to_header(jar)`.

**One vocabulary** ([2.2](phase-6/redesign.md#22-one-vocabulary)). A word means the same thing everywhere, and nothing else means that:

| Word | Means |
| --- | --- |
| `open(…)` | start a live resource, which `close()` or `with` ends |
| `load(path)` / `save(dest)` | file to value / value to file, atomically |
| `parse(text)` | text to value, raising `ValueError` |
| `fetch(url)` | one network request, returning a `Response` |
| `download(url, dest)` | network to a file |
| `check()` | validate, raising the module's error |
| `wait(…)` | block until a condition |
| `ask` / `confirm` / `choose` | ask a person |
| `on_<event>(…)` | subscribe; returns a `Subscription` |
| `replace(**changes)` | a copy with fields changed |
| `filter(…)` | a copy with fewer items |
| `dest` | where output goes; never `out`, `dest_dir` or `path` |
| `timeout` | seconds, as a `float` |
| `progress` | a callback `progress(done, total)`; never a label |
| `label` | the text of a progress row that `shared` draws |
| `level` | `best`, `high`, `mid` or `low`, as the CLI names them; replaces `preset` |
| `match` | a `str` (substring), compiled regex or predicate over a URL |
| `until` | a predicate that ends a loop |

**Shapes** ([2.3](phase-6/redesign.md#23-shapes)):

- **S1** Fluent objects for things worked on several ways (`Selection`, `Jar`, `Media`, `Page`); functions for one-shot jobs (`archive.zip`, `http.download`, `crypto.encrypt`).
- **S2** Transforms never write over their input. Only `save` writes, atomically: `media.open("a.mov").trim(end=10).compress("mid").save("a.mp4")`.
- **S3** The main input is positional, and options are keyword-only, named from the vocabulary: `http.download(url, dest, *, parts="auto")`.
- **S4** State is a settable property, and transitions are verbs: `bar.total = 10`, then `bar.advance()`.
- **S5** Every live resource is a context manager with `close()`, and every wrapper has `.raw` (Rule 2).
- **S6** One error base per module, `<module>.Error`, with `<Kind>Error` under it. Errors about untrusted, damaged or refused input also subclass `shared.UnsafeError`, `shared.BrokenError` or `shared.RefusedError` ([C4](phase-6/redesign.md#c4-errors)).
- **S7** One spelling: no aliases, and no deprecated names kept. `shared` has one user, changed in the same commit.

**Enforced.** `tests/test_names.py` walks the [API snapshot](phase-6/redesign.md#c5-namespace-and-the-api-snapshot) and fails on a public name with a W7 prefix, a module whose errors don't share a `<module>.Error` base, a public function with an option after its first parameter that isn't keyword-only, and an option outside the vocabulary (`out=`, `dest_dir=`, `preset=`, a `progress=` that isn't callable). Breaches still in today's code are listed in its `ALLOWED`, per module, and that list can only shrink. A rename shows in review as a one-line diff of `tests/data/api.txt`.

A chalk-style color name keeps chalk's compounds: `style.bright_red`, `bg_red`.

## pyproject

```toml
[project.optional-dependencies]                                  # for shared alone, without flnd
archive = ["pyzipper", "py7zr"]                                  # password zips and 7z; plain zip and tar need nothing
selector = ["parsel>=1.9.0", "python-jsonpath"]
http = ["httpx>=0.27.0", "parsel>=1.9.0", "python-jsonpath"]     # httpx for `fetch`; selector's packages for Response.html
browser = ["playwright==1.65.*", "cryptography>=42"]             # a minor pin: each release downloads a new browser
media = ["pillow>=11.3", "pi-heif>=1.0", "imageio-ffmpeg>=0.6"]   # Pillow and HEIC for photos; the ffmpeg binary

[dependency-groups]
dev = ["pyright[nodejs]==1.1.414", "typing_extensions>=4.12", "packaging"]
dist = ["pyinstaller>=6"]                                        # only the `dist` task syncs it

[tool.uv]
default-groups = ["dev"]
```

flnd's own dependencies are `httpx`, `playwright==1.65.*`, `parsel`, `python-jsonpath`, `cryptography`, `telethon`, `qrcode`, `pillow`, `pi-heif` and `imageio-ffmpeg`, because flnd always needs them (`qrcode` draws tlg's QR login, and `imageio-ffmpeg` brings ffmpeg). `pyzipper` and `py7zr` are not among them, since flnd saves archives as the site sends them; they stay in the `archive` extra for `shared` alone. The dev group adds `pyright[nodejs]` (with its own Node.js), `typing_extensions` and `packaging` (for the licence test). `uv.lock` pins every version.

## Makefile

From [Phase 6, step 2](phase-6/setup.md), every task is in [`scripts/dev.py`](phase-6/setup.md#scriptsdevpy), stdlib only, so it runs the same on macOS, Linux and Windows with nothing but uv: `uv run --no-project scripts/dev.py <task>`. The Makefile is a shortcut for people who have make:

```makefile
TASKS := setup test lint type check baseline api flnd dist try
.PHONY: $(TASKS)

$(TASKS):
	uv run --no-project scripts/dev.py $@ $(ARGS)
```

| Task | Runs |
| --- | --- |
| `setup` | `uv sync --extra archive` from `uv.lock`; no browser download |
| `test`, `lint`, `type`, `check` | unittest, compileall, pyright, and the three in that order |
| `flnd` | `uv run flnd $(ARGS)` |
| `dist` | `uv sync --group dist`, `bundler.build`, and `licenses/ffmpeg-source.txt` ([flnd › Packaging](phase-5/flnd.md#packaging)) |

`requires-python` is `>=3.11`. Before a release, run `uv run --no-project scripts/dev.py check --python 3.11`. `make flnd ARGS="download ins:example_user"` passes arguments through.

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
- **media:** see its [Rejected](phase-5/media.md#rejected) list: a public `which_ffmpeg()` or `run(*args)`, writing HEIC, JPEG XL, AV1 encoding, hardware encoders, filters and effects, metadata editing, HLS downloads.
- **everything:** async variants of the APIs (`worker` runs async functions, and every `shared` API stays synchronous), and subprocess helpers. A module may privately run one outside program with arguments it builds itself, as `archive` runs `unrar` and `media` runs `ffmpeg`; it never offers a general "run this program" function.
