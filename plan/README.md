# flntools plan

flntools is flnd, a media downloader, and `shared`, the toolkit it is built on. flnkit tried to be a general framework that replaced the libraries it used, and grew to about 38,800 lines: `net` alone was 3,100 lines and `browser` 3,300. `shared` exists to avoid that.

## Position

- **`shared` is flnd's toolkit, not a general library product.** flnd is its only user, since loi and tlg became flnd sources. The package boundary stays, because it keeps the engine and the sources clean. Nothing is added for imaginary outside users.
- **Each module is small, with one obvious way to use it.** If the standard library already does the job, the module doesn't exist.
- **Ceiling.** `shared` stays under **about 5,000 lines of code** (about 3,400 after Phase 4, about 4,500 after Phase 5, about 4,600 after [Phase 6](phase-6/api.md), `classify` included). The user raised it from 4,500 on 2026-11-11, so `media` has room. Going over means removing something first. Lines of code don't count docstrings, comments or blank lines, so the size limits never push against documentation.

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
| `crypto` | [phase-4](phase-4/crypto.md) | `_crypto.py` | `hashlib`, `cryptography` | `browser` or `cryptography` |
| `kv` | [phase-4](phase-4/kv.md) | `_kv.py` | `sqlite3` | — |
| `cookies` | [phase-4](phase-4/cookies.md) | `_cookies.py` | `sqlite3`, `cryptography` | `browser` for `load_from_browser` |
| `selector` | [phase-4](phase-4/selector.md) | `_selector.py` | parsel, jmespath, python-jsonpath | `selector` |
| `browser` | [phase-4](phase-4/browser.md) | `_browser.py` | Playwright | `browser` |
| `worker` | [phase-4](phase-4/worker.md) | `_worker.py` | `concurrent.futures`, `asyncio` | — |
| `bundler` | [phase-4](phase-4/packaging.md#bundler) | `_bundler.py` | PyInstaller | `bundler` |
| `media` | [phase-5](phase-5/media.md), [ffmpeg](phase-5/ffmpeg.md), [classify](phase-6/classify.md) | `_media.py`, `_ffmpeg.py`, `_pillow.py`, `_media_image.py`, `_media_video.py`, `_media_audio.py`; `_classify.py` from Phase 6 | Pillow, pi-heif, the ffmpeg program (imageio-ffmpeg); ONNX Runtime for `classify` | `media`; `classify` |

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

**Phase 6: lean and simple.** Based on an audit of the whole repository on 2026-11-12 ([overview](phase-6/README.md)). Built in this order:
1. [fixes](phase-6/fixes.md): 16 bugs, among them corrupted downloads on Windows, failed targets shown as ok with exit status 0, and Twitter's login sent to its CDNs.
2. [setup](phase-6/setup.md): `uv sync` with a committed `uv.lock`, the browser downloaded only when there is no Chrome, every task in a stdlib `scripts/dev.py`, and a Makefile that runs the same on every OS.
3. [architecture](phase-6/architecture.md): gallery-dl's design. A source yields `Post`, `File` and `Queue` items with metadata, and one job filters, names from format strings, checks the history, downloads and post-processes.
   [cli](phase-6/cli.md): `flnd download`, `sync` (repair and fetch only, no options), `login` and `logout`; `--time <since>,<until>`; `--image`, `--video`, `--audio` `<format>,<level>`; `--unpack`, `--zip`, `--locale`, `--timezone` and `install-browser` gone, and with them `shared.archive`; Google Chrome only, with Chromium offered when it's missing.
   [source](phase-6/source.md): 8 fields and 5 hooks instead of 18 and 7.
   [pace](phase-6/pace.md): requests as fast as each site allows: its rate headers, Instagram's known budgets, and a learned gap above a floor.
   [session](phase-6/session.md): 26 members instead of 35; scrolling ends after 10 s without progress and has no cap; page loads and API calls are paced and checked for 429s and login walls.
4. [api](phase-6/api.md): every public API with its change or none; flnd's own APIs lose what flnd doesn't use, and `shared`'s are kept or removed by how useful they are.
5. [ins](phase-6/ins.md): tagged and reposts posts read through Instagram's media info API, skipped before any request when already saved or out of range, and every throttle waited out. With [flows](phase-6/flows.md): what gallery-dl does for each site, taken where it's better.
6. [scripts](phase-6/scripts.md): the sources rewritten shorter, with `save_post`, `day` and `meta_results` in `flnd/sources/common.py`.
   [dates](phase-6/dates.md): the engine dates every file, in its name and its modified time, with the CDN's `Last-Modified` or the download day when the source has none.
7. [classify](phase-6/classify.md): `media.classify`, a small CLIP-style model on ONNX Runtime, and `--classify`, which groups files by category in `labels/<label>/` link folders without renaming them.
8. [ui](phase-6/ui.md): download rows that no longer blink (overwritten in place, synchronized, cursor hidden, short-lived rows never drawn), a prompt cursor where the text goes, and log lines in their level's color.
9. [comments](phase-6/comments.md): a docstring for every module, class and function, with its arguments, result, errors and an example, and *why* comments at every non-obvious rule.

**Deferred.** These are not built until something real needs them: `log.track`; the runtime `type=` checks with TypedDict support; strict pyright and the `TypeVarTuple` overloads; `worker`'s `rate`, `setup` and `task`; HMAC; writing RAR.

## Conventions

- **Python and dependencies.** Python 3.11 and later. 3.10 reached its end of life in October 2026, and Pillow and PyAV have dropped it. `style`, `fmt`, `theme`, `log`, `prompt`, `cli`, `kv` and `worker` use only the stdlib, and so does `cookies` apart from `load_from_browser`.
- **Public surface.** Every file in `shared` starts with `_`, and [`shared/__init__.py`](../shared/__init__.py) is the only public surface.
- **Lazy imports.** `archive`, `crypto`, `cookies`, `kv`, `selector`, `browser`, `worker`, `bundler` and `media` load through a module `__getattr__`, with a `TYPE_CHECKING` block for type checkers. `from shared import log` never imports Playwright, parsel, cryptography or Pillow.
- **Missing extras.** A missing extra raises `ImportError("shared.browser needs playwright: pip install 'flntools[browser]'")` on first use.
- **Readable code.** Code is written for the next person to read it, and kept short:
  - **Order.** A module reads top to bottom: public names first, then the helpers they call, in the order they are called.
  - **Docstrings.** Every module, public class and public function has one, from Phase 6 to the [comments](phase-6/comments.md) standard: one plain sentence saying what it does, a short example, then `Args:` for every argument, `Returns:`, `Raises:`, and the rules a caller must know (what is slow, what can't be undone).
  - **Comments** explain *why*, at the place a reader would otherwise have to work out a rule: why a timed-out thread can't be stopped, why archive links are refused. Every magic value, site quirk, lock, caught exception and platform branch gets one. A comment never restates the next line.
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
bundler = ["pyinstaller>=6"]
media = ["pillow>=11.3", "pi-heif>=1.0", "imageio-ffmpeg>=0.6"]   # Pillow and HEIC for photos; the ffmpeg binary
```

flnd's own dependencies are `httpx`, `playwright`, `parsel`, `python-jsonpath`, `cryptography`, `telethon`, `qrcode`, `pyzipper`, `py7zr`, `pillow`, `pi-heif` and `imageio-ffmpeg`, because flnd always needs them (`qrcode` draws tlg's QR login, and `imageio-ffmpeg` brings ffmpeg). The dev group adds `pyright` and `typing_extensions`.

## Makefile

From [Phase 6, step 2](phase-6/setup.md), every task is in [`scripts/dev.py`](phase-6/setup.md#scriptsdevpy), stdlib only, so it runs the same on macOS, Linux and Windows with nothing but uv: `uv run --no-project scripts/dev.py <task>`. The Makefile is a shortcut for people who have make:

```makefile
TASKS := setup test lint type check flnd dist
.PHONY: $(TASKS)

$(TASKS):
	uv run --no-project scripts/dev.py $@ $(ARGS)
```

| Task | Runs |
| --- | --- |
| `setup` | `uv sync` from `uv.lock`, then `playwright install --no-shell chromium` only when there is no Chrome |
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
