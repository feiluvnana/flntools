# API

Phase 6, step 4. Every public API in `shared` and flnd as it is on 2026-11-12, each with its Phase 6 change, or **—** for no change. Usage was counted with `grep` over `flnd/`. "No caller" means nothing in flnd uses it: only `shared` itself or the tests do.

Two rules, decided by the user on 2026-11-12:
- **flnd's own APIs** (`Source`, `Session`, `Options`, `Engine`, the commands) lose whatever flnd doesn't use. They exist only for flnd's sources and commands, so an unused member is just something to read past.
- **`shared`'s APIs are judged by how useful they are**, not by whether flnd calls them today. A general tool a script would want (a hash, a `map`, a multi-select prompt, `archive`) stays, even unused; what goes is what duplicates another way, or what nobody would reach for. A name that only `shared` uses inside itself loses its public spelling (a leading `_`), so the public surface shows only what a caller needs.

Sizes are lines of code (no docstrings, comments or blank lines). `shared` is 4,472 today, against the 5,000 ceiling. Phase 6 leaves it about the same (about 4,600 with [classify](classify.md)), because its changes are in what's public, not in what's there. Three modules are over Rule 3's 400: `archive` (432), `cli` (473) and `theme` (411). They stay, each a little over, because splitting any of them would make two modules where a reader expects one.

## flnd

### Commands

The [cli](cli.md) step covers them; the user decided this shape on 2026-11-12.

| Command | Today | Phase 6 |
| --- | --- | --- |
| `flnd download <targets…>` | `--from`, `--since`, `--until`, `--unpack`, `--zip`, `--compress-image`, `--compress-video`, `--format-image`, `--format-video`, `--locale`, `--timezone`; targets from stdin when none are given | `--from` —; **`--time <since>,<until>`** replaces `--since` and `--until`; **`--image`, `--video`, `--audio` `<format>,<level>`** replace the four compress and format options; `--unpack`, `--zip`, `--locale`, `--timezone` **removed**; exits 1 when any target failed |
| `flnd sync [targets…]` | `--only`, and the same options as `download` | **no options**: repair, fetch new posts with the target's saved settings, and a summary |
| `flnd login <source>` | `--method`, `--path`, `--browser`, `--profile` | **`flnd login`**, no arguments: source, method and the rest are asked |
| `flnd logout [source]` | `--all` | **`flnd logout <sources…>`** or `flnd logout all` |
| `flnd install-browser` | `playwright install chromium` | **removed**: `browser.connect` offers to download Chromium when Chrome is missing |

### `Source` (`flnd/sources/base.py`)

The [source](source.md) step covers it: 18 fields and 7 hooks become **8 fields and 5 hooks**.

| Member | Today | Phase 6 |
| --- | --- | --- |
| `code`, `name`, `examples` | declarations | — |
| `domains`, `short_links` | hosts, and the short-link hosts among them | **`domains` only**: a URL `resolve` can't read is followed once, then resolved again |
| `kinds_of_target` | declared by five sources | **removed**: nothing reads it |
| `categories`, `default_categories` | two tuples | **`categories = {name: taken by default}`** |
| `needs` | cookie names that prove a login | renamed **`cookies`** |
| `login_methods` | `("browser", "file")` | renamed **`logins`** |
| `requires_login` | tlg | **removed**: an SDK source's `connect` raises `LoginError` without a login |
| `user_agent` | tik and twt, the same string | **removed**: one Chrome user agent for every source |
| `viewport`, `concurrency`, `pause` | never overridden | **removed**: engine constants |
| `browser_args`, `stealth` | tik `--no-sandbox`, twt `False` | **removed**: every source hides `navigator.webdriver` and keeps the sandbox |
| `wait` | tlg's flood wait | **removed**: tlg calls `session.throttled(retry_after=…)` |
| — | | **`pace`** (new, 1.0 s): between page loads and API calls, and between scrolls |
| `resolve(text)`, `run(session)` | required | — |
| `headers(cookies)`, `sign(request)` | headers for every request, CDNs included; signing | merged into **`prepare(request, cookies)`**, only for the source's `domains` |
| `login(session, method)`, `connect(session)` | tlg | — |
| `logged_in(session)` | tlg | **removed**, with `requires_login` |
| `FetchError(message, status=)`, `LoginError` | errors | — |

### `Session`

The [session](session.md) step covers it. In short:

| Member | Phase 6 |
| --- | --- |
| `target`, `folder`, `cookies`, `categories`, `since`, `until`, `wants(category=, date=)` | — |
| `fetch_json(url, *, method, headers, soft, **kw)`, `fetch_html(url, **kw)` | paced by `Source.pace` |
| `http`, `select(value)` | **removed**: no caller |
| `open_page(url)` | **removed**: `goto` opens the browser on first use |
| `goto(url, *, idle=False, timeout=30.0)` | waits out a 429 and loads again; a status of 400 or more, or a login wall, returns False with a warning; paced |
| `on_response(match, handler, *, lines=False)` | — |
| `on_request`, `wait_for_selector`, `wait_for_url`, `wait_for_load`, `browser` | **removed**: no caller, or `session.page` has them |
| `scroll(*, by, count, more, idle=4, delay=2.0, max=200)` | `scroll(*, count=None, more=None, delay=None)`: ends after 10 s with no progress, has no scroll cap, and counts the folder's saves by default |
| `handoff(*, until, message, poll, timeout)` | — |
| `page` | — |
| `client` | — |
| `save(source, name, *, category, date, folder, key, password, size, headers, audio)` | **`password=` goes** with `--unpack`; the other keywords have callers |
| `downloaded(key)` | **new** (from the WIP commit) |
| `write(name, text)`, `full` | — |
| `state`, `secrets` | — |
| `interactive`, `ask(message, *, secret, description)`, `info`, `warn` | — |
| `choose(message, options)` | **removed**: no caller |
| `challenged()` | becomes **`throttled(reason, *, retry_after=None)`** |

### `Options`, `Engine`, `Target`

| API | Today | Phase 6 |
| --- | --- | --- |
| `Options(categories, since, until, unpack, zip, compress_image, compress_video, format_image, format_video)` | frozen dataclass | **`Options(categories, since, until, image, video, audio)`**, each of the last three a `Choice(format, level)` or None ([cli](cli.md#what-changes-in-the-code)) |
| `Engine(*, home, limit, headless, locale, timezone, sources)` | | **`locale` and `timezone` go** |
| `Engine.resolve(texts) -> list[Target]` | | — |
| `Engine.run(targets, options) -> int` | the number of files written | returns **`Outcome(files, failed)`**, so the commands can set their exit status; a source whose `connect` or login fails marks its targets' rows failed, not ok ([fixes](fixes.md#failed-groups)) |
| `Engine.login(source, method)` | | — |
| `Target(source, kind, value)`, `TargetError`, `target.resolve(text, sources, follow=)` | | — |

flnd's other modules (`net`, `login`, `vault`, `sync`, `dash`, `target`'s helpers) are private to flnd and are covered by [fixes](fixes.md) and [session](session.md#what-moves-out-of-sessionpy).

## shared

### `style` (247 lines)

| API | Used by flnd | Phase 6 |
| --- | --- | --- |
| `style.<color>`, `style.bold` … chained, `style(text)` | through `theme` | — |
| `style.color(value, *, background=False)` | through `theme` | — |
| `style.strip(text)`, `strip(text)` | yes | — |
| `style.paint(text, depth)` | through `theme` | — |
| `style.gradient(*stops)` | no caller | — : a general colour tool |
| `style.rainbow(text)` | no caller | **removed**: it's `gradient` with fixed stops, a second way to do the same |
| `style.url(target)` | no caller | — : clickable terminal links are worth having |
| `current_depth`, `parse_color`, `color_code`, `color_rgb`, `nearest_ansi`, `nearest_index`, `ramp_colors`, `graphemes`, `Chain` | only `style` and `theme` | **private** (`_` names) |

### `fmt` (`_fmt.py`)

| API | Used by flnd | Phase 6 |
| --- | --- | --- |
| `fmt.filename(text, *, fallback="untitled", maxlen=200)` | yes | — |
| `fmt.size(n)` | yes | — |
| `fmt.duration(seconds)` | yes | — |
| `fmt.date(value, pattern=None)` | no caller | — : formatting a date is `fmt`'s job |

### `theme` (411 lines: `_theme.py`, `_screen.py`)

| API | Used by flnd | Phase 6 |
| --- | --- | --- |
| `theme.screen.interactive` | yes | — |
| `theme.screen.out`, `.inp`, `.tty`, `.width`, `.height`, `.write(text)`, `.readkey()` | through `log`, `prompt`, `cli` | — |
| `theme.color` (get and set), `theme.scope(**changes)`, `theme.paint(name, text, *, on=)`, `theme.draw(kind, value)` | through `log`, `prompt`, `cli` | — |
| `theme.palette`, `theme.glyphs` (the `Namespace`s) | through the drawers | — |
| `draw_log`, `draw_progress`, `draw_help`, `draw_prompt` | through `theme.draw` | — |
| `LogRecord`, `Bar`, `HelpModel`, `PromptView` | the records the drawers take | — |
| `detect_color`, `detect_unicode`, `display_width`, `fit`, `chain_of`, `isatty`, `key_name` | only `shared` | **private** |

### `log` (`_log.py`)

| API | Used by flnd | Phase 6 |
| --- | --- | --- |
| `log.debug`, `log.info`, `log.ok`, `log.warn`, `log.error` | yes | — |
| `log.level` (get and set) | through `cli` | — |
| `log.progress(label, *, total=None, show=str)` → `ProgressBar` | yes | — |
| `ProgressBar.advance`, `.update`, `.finish`, `.fail`, `.total`, `.caption`, `.done`, `.finished` | yes | — |
| `log.multi(title="")` → `Group`; `Group.add(label, *, total, show)` | yes | — |
| `Group.end(outcome)`, `Group.changed`, `Group.rows` | only `log` | **private**: `end` is called by the `with` block |
| `LiveArea`, `live_area`, `render` | only `log` and `prompt` | **private** |

### `prompt` (`_prompt.py`)

| API | Used by flnd | Phase 6 |
| --- | --- | --- |
| `prompt.input(message, *, description, default, placeholder, …)` | yes | — |
| `prompt.secret(message, *, description, default, mask, …)` | yes | — |
| `prompt.choice(message, options, *, description, default, filter, …)` | yes | — |
| `prompt.confirm(message, *, default, yes, no, …)` | no caller today | — (`browser.connect` asks with it before downloading Chromium) |
| `prompt.choices(message, options, *, min, max, …)` | no caller | — : a multi-select is a standard kind of question |
| `prompt.defaults()`, `prompt.keymap`, `Keymap`, `keys` | no caller | — : how a script changes the prompts' keys and look |
| `PromptError` | yes | — |

### `cli` (473 lines: `_cli.py`, `_params.py`)

| API | Used by flnd | Phase 6 |
| --- | --- | --- |
| `cli.App(name, …)`, `@app.command`, `app.run(argv=None)`, `app()` | yes | — |
| `app.help(*path)`, `app.parse(argv)` | through `run` | **private** |
| `app.group(name, description)` | no caller | — : subcommands |
| `cli.run(fn)` | no caller | — : the shortest way to turn one function into a command |
| `cli.opt(*flags, help, metavar, choices, type, …)`, `cli.arg(help, metavar, nargs, choices, type, stdin, …)` | yes | — |
| `cli.UsageError` | yes | — |
| the `datetime` parameter type | no caller | — : the pair of `date` |
| `Command`, `Opt`, `Arg`, `Param`, `read_signature` | only `cli` | **private** |

### `archive` (432 lines)

flnd no longer calls it: `--unpack` and `--zip` go ([cli](cli.md#--unpack-and---zip-go)). It **stays in `shared`**, because safe extraction (traversal and bomb checks, passwords, split parts) is exactly what a script shouldn't write by hand. `pyzipper` and `py7zr` leave flnd's dependencies and stay in the `archive` extra.

| API | Used by flnd | Phase 6 |
| --- | --- | --- |
| `archive.zip(sources, dest, *, password=None, split=None)` | no longer | — |
| `archive.unzip(parts, dest, *, password=None, conflict="error", limits=…)` | no longer | — ; it refuses `:` and Windows device names in entry names ([fixes](fixes.md#windows)) |
| `UnsafeError`, `BombError`, `ConflictError`, `PasswordError`, `Limits` | no longer | — |

### `crypto` (`_crypto.py`)

| API | Used by flnd | Phase 6 |
| --- | --- | --- |
| `crypto.hash_sha256(data)` | yes | — |
| `crypto.random_key(size=32)` | yes | — |
| `crypto.encrypt_aes_gcm`, `crypto.decrypt_aes_gcm` | yes | — |
| `crypto.decrypt_aes_cbc` | yes (`cookies` reads Chrome's) | — |
| `crypto.hash_md5`, `hash_sha1`, `hash_sha512`, `hash_blake2b` | no caller | — : checksums sites publish |
| `crypto.derive_scrypt`, `crypto.encrypt_aes_cbc` | no caller | — : a key from a password, and the pair of `decrypt_aes_cbc` |
| `CryptoError` | yes | — |

### `kv` (`_kv.py`)

| API | Used by flnd | Phase 6 |
| --- | --- | --- |
| `kv.open(path, *, table="default")` → `KV`, a `MutableMapping` | yes | — |
| `KV.get`, `[]`, `in`, `del`, `len`, iteration | yes | — |
| `KV.get_keys(prefix="")` | yes | — |
| `KV.set(key, value, *, ttl=None)` | no caller passes `ttl` | — : a cache with expiry is a common use |
| `KV.raw`, `KV.close`, `with` | yes | — |

### `cookies` (`_cookies.py`)

| API | Used by flnd | Phase 6 |
| --- | --- | --- |
| `cookies.load_from_browser(domain, *, browser="chrome", profile=None)` | yes | — |
| `cookies.load_from_file(path, *, domain=None)` | yes | — |
| `cookies.to_file(jar, path)` | yes | — |
| `cookies.to_header(jar)` | no caller | — : a jar for any other HTTP client |
| `CookieError` | yes | — |

### `selector` (`_selector.py`)

| API | Used by flnd | Phase 6 |
| --- | --- | --- |
| `selector.select(source, query="")` → `Selection` | yes | — |
| `.css`, `.xpath`, `.jmespath`, `.script(id=, pattern=)` | yes | — |
| `.first`, `.all`, `.text`, `.raw` | yes | — |
| `.jsonpath(query)` | once, in `fbk` | — ; Meta's embedded results use it too (`$..__bbox.result`), so it replaces `relay`'s hand-written walk ([scripts](scripts.md#meta_results)) |

### `browser` (529 lines)

| API | Used by flnd | Phase 6 |
| --- | --- | --- |
| `browser.connect(*, profile, cdp, headless, viewport, locale, timezone, args, stealth)` | yes | **Google Chrome only**: when it's missing, `connect` asks in a terminal whether to download Chromium ([cli](cli.md#chrome-only)). The keywords stay: `cdp=` attaches to a running browser, and `locale=` / `timezone=` are useful outside flnd, which stops passing them |
| `browser.installed_profile(browser="chrome", profile="Default")` | yes | — ; its copy is deleted even when the launch fails ([fixes](fixes.md#profile-copies)) |
| `Browser.open_page(url=None, *, idle, timeout)`, `Browser.close`, `with` | yes | — |
| `Browser.cookies.all`, `Browser.cookies.add(jar)` | yes | — |
| `Browser.pages`, `Browser.cookies.clear_all()` | no caller | — |
| `Page.goto(url, *, idle=False, timeout=30.0) -> bool` | yes | returns the **status** (`int | None` when it didn't load), so the session can see a 429 |
| `Page.on_response(match, handler, *, lines=False)` | yes | — |
| `Page.on_request(match, handler)` | no caller once the session's goes | — : the pair of `on_response` |
| `Page.wait_for_selector`, `wait_for_url`, `wait_for_load` | no caller once the session's go | — : the waits a script needs most |
| `Page.scroll(*, by, count, limit, more, idle=4, delay=2.0, max=200, onscroll)` | yes | `idle` becomes **seconds** with no progress (10.0), and `max` defaults to None, no cap ([session](session.md#scroll)); `by` and `limit` stay |
| `Page.handoff(*, until, message, poll, timeout)` | yes | — |
| `Page.selector`, `Page.raw`, `Page.close` | yes | — |
| `Page.url`, `Page.title` | no caller | — |

### `worker` (553 lines, 366 of code)

| API | Used by flnd | Phase 6 |
| --- | --- | --- |
| `worker.race(fn, *iterables, **options)` | yes | — |
| `worker.pool(**options)` → `Pool`; `.submit`, `.results`, `.submitted`, `.close`, `.join`, `.raw`, `with` | yes | — |
| `Result.item`, `.value`, `.error`, `.ok` | yes | — |
| `worker.map(fn, *iterables, **options)` and its overloads, `TaskError` | no caller | — : the most common use of a worker pool |
| options `concurrency`, `progress` | yes | — |
| options `retries`, `timeout`, `processes`, and running `async def` functions | no caller | — |

### `bundler` (`_bundler.py`)

| API | Used by flnd | Phase 6 |
| --- | --- | --- |
| `bundler.build(entry, *, name, hidden=(), data=(), out="dist")` | `scripts/dev.py dist` | — (its PyInstaller dependency moves to the `dist` group, [setup](setup.md#pyproject)) |

### `media` (1,098 lines in six files)

flnd calls `media.check`, `media.sniff`, `media.compress`, `media.video.merge` (the DASH path), `BrokenError` and `UnsafeError`, and from Phase 6 `media.classify` and `media.video.frames`. The rest stays: it is the toolkit the user approved on 2026-11-11, and each function is one a media script would want.

| API | Used by flnd | Phase 6 |
| --- | --- | --- |
| `media.check(path)` | yes | — |
| `media.sniff(path)` | yes | — |
| `media.compress(src, *, dest, format, preset, quality, codec, …)` | yes | `preset` takes **`best`, `high`, `mid`, `low`**, the CLI's levels; `small` and `balanced` go ([cli](cli.md#--image---video---audio)) |
| `media.video.merge(sources, *, dest, audio, …)` | yes (one video and one audio) | — |
| `media.probe(path)` → `Info` | inside `check` and `compress` | — |
| `Info.created`, `.location`, `.tags` | no caller | — |
| `media.convert(src, *, dest, quality, fps, codec)` | no caller | — |
| `media.video.trim` | no caller | — |
| `media.video.frames` | no caller today | — : `media.classify` reads a video's frames with it |
| `media.classify(path, *, labels, top, threshold)` | — | **new** ([classify](classify.md)), with `media.LABELS` |
| `media.image.resize`, `media.image.crop` | no caller | — |
| `media.audio.extract` | no caller | — |
| `BrokenError`, `UnsafeError` | yes | — |
| `Preset`, `Container` | through `compress` | — |
| `transcode`, `output`, `muxer`, `holds`, `raw_input`, `decoded`, `writing`, `writable`, `kind_of`, `same_kind`, `check_range` | only `media` | **private** |

`media` grows by `classify`, about 130 lines, to about 1,230.

## Totals

| | Today | After Phase 6 |
| --- | --- | --- |
| `shared`, lines of code | 4,472 | about 4,600, `classify` included |
| public names in `shared` | about 190 | about 160 (internal helpers made private) |
| `Session` members | 35 | 26 |
| flnd's dependencies | 12 | 9 (`pyzipper` and `py7zr` go to the `archive` extra; `python-jsonpath` stays); `classify` is an extra |
| flnd commands, and `download`'s options | 5 and 11 | 4 and 6 |

## Tests

Each removed API's tests go with it. Each `shared` module gets an `__all__` listing exactly its public names above, so a name made private can't be reached by accident.
