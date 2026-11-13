# api: a simpler `shared`, module by module

Part of [Phase 8](README.md). The user, 2026-11-13: "simplify the api of shared", keeping every
module and what it does, and: "shared exists because in future, maybe other module need it, not
just flnd. So keep that in my for any design decision."

So each module is simplified **for any caller**, not around flnd: no name is judged by how often
flnd calls it, no flnd idea (site, target, category, post) enters `shared`, and every capability
stays. The changes are the same few kinds everywhere:
- **one way** to do each thing (no second spelling, no two routes to one result);
- **fewer parameters**, and a record where a call takes five or more options;
- **fewer public names**: plumbing (constructors taking private types, internal attributes)
  becomes private, while every capability stays reachable;
- **general, plain names** (AGENTS.md: a verb for an action, a noun for a read, a property for a
  read with no argument, options after the first keyword-only);
- **every error typed** under the module's `Error`.

Each module's change is **one commit**: the module, its tests, flnd's call sites, the API
snapshot (`tests/data/api.txt`) and the module's [bugs](bugs.md) on the same lines (AGENTS.md:
a module's redesign is one commit). Phase 7 already plans R7 (`fetch`/`try_fetch` for `http` and
flnd's site) and R9 (a record for `download`'s options); they aren't repeated here.

Call sites are flnd's at `37147c1` (file:line); the `log` and `theme` rows are at `af6eef3`, after
Phase 7 step 9. The snapshot's `shared` part is 445 lines at `af6eef3`.

## The new names from the tagging pass

Generic machinery found in flnd ([flnd › machinery](flnd.md#2-generic-machinery-into-shared)),
made general for another tool, each where it belongs:

| Name | Module | What any caller gets | Lines out of flnd |
| --- | --- | --- | --- |
| `cli.stopping()` → `Stopping(soon, now)` | cli | a two-stage Ctrl-C: the first sets `soon` (finish what runs), the second sets `now` and raises | 38 |
| `Page.responses(match, *, lines=False, scroll=False) -> Watch` | browser | the JSON bodies a page receives, as an iterator, scrolling until quiet if asked | about 90 |
| `http.Client.cookies` (a settable `Jar`) | http, cookies | one cookie jar a client and a browser both read and write | about 35 |
| `Row.part() -> progress` | log | one row for a file in several streams: each part reports its own done and total | about 45 |
| `Pool.run_on_owner(fn)` | worker | from a task, run `fn` on the thread that submits and wait (for a single-threaded library) | about 90 |
| `worker.Loop()` with `.run(coroutine, *, stop=None)` | worker | an event loop on its own thread that a task's error never kills | about 20 |
| `atomic.sweep(folder, *, keep=7 days, settle=1 hour)`, `atomic.lock(folder)` | atomic | remove the parts `atomic` and `http` left; a lock a second process respects | about 35 |
| `media.verify(path, *, kind=None, decode=True) -> extension`, `media.NotMediaError` | media | is this file the media it claims, and what extension does its content call for | about 70 |

## http

| Before | After | flnd sites | Snapshot |
| --- | --- | --- | --- |
| `fetch(url, *, client=…, …)`, `download(url, dest, *, client=…, …)`: an httpx client passed two ways (`client=` and `Client(raw)`) | `client=` goes; a caller's own httpx client goes through `http.Client(raw)` | none | −2 |
| two retry loops (`fetch`'s and `Client.call`'s) | one: `Client.fetch` sends once and retries through `call` ([H4](bugs.md#http-download-pace)) | `site.py:257` unchanged | 0 |
| Retry-After clamped at 300 s in `Client.download` only | one clamp for every path ([H2](bugs.md#http-download-pace)) | none | 0 |
| `fetch(data=bytes)` deprecated in httpx | the same signature, sent as `content=` | none | 0 |
| cookies copied by hand between a browser, a jar and httpx | `Client.cookies`: a settable `cookies.Jar` the client sends and updates, which a `Browser` can share | `net.py:86-91`, `site.py:249, 360, 494-496` | +1 |
| `httpx.InvalidURL` leaks | `http.Error` ([H3](bugs.md#http-download-pace)) | `site.py:484` catches it by name | 0 |

`Client`, `call`, `back_off`, `speeds`, `Pace`, `Response`, `Download`, `Transport` and the error
tree stay as they are.

## kv

| Before | After | flnd sites | Snapshot |
| --- | --- | --- | --- |
| `Store(link: _Link, table: str)`: a public constructor taking a private type | made only by `kv.open` and `Store.table` | none (a type at `site.py:90, 97`, `engine.py:495, 499`) | 1 changed |
| `sqlite3` errors leak (documented on `open`) | `kv.Error`, the sqlite error as its cause | `auth.py:303` drops its `sqlite3.Error` | 0 |
| inherited `items`, `values`, `clear`, `update` (N+1 queries, quadratic `clear`) | the same names, one statement each ([speed](speed.md#kv), [K2](bugs.md#kv-crypto)) | none | +4 |

## atomic

| Before | After | flnd sites | Snapshot |
| --- | --- | --- | --- |
| `write(dest, data)` replaces; `writing(dest, *, replace=False)` refuses: two defaults for one idea | `writing(dest, *, replace=True)`; a caller that must not overwrite says `replace=False` | `net.py:309` drops its argument | 1 changed |
| repair's own copy of `atomic`'s part pattern | `sweep(folder, *, keep, settle) -> int`, `lock(folder)` | `files.py:43-76`, `repair.py:126-145` | +2 |

## clock

| Before | After | flnd sites | Snapshot |
| --- | --- | --- | --- |
| `Fake.time`, a second read of `Fake.now()` | private; `now()` is the one read | none | −1 |

## crypto

| Before | After | flnd sites | Snapshot |
| --- | --- | --- | --- |
| `md5`, `sha1`, `sha256`, `sha512`, `blake2b`: five names of one shape | `digest(data, *, algorithm="sha256") -> str`, any `hashlib` name, bytes-likes taken ([C1](bugs.md#kv-crypto)) | `loi.py:325, 347` | −5, +1 |
| a wrong key length is a plain `ValueError` | `crypto.Error` (still a `ValueError`) | none | 0 |

## log

| Before | After | flnd sites | Snapshot |
| --- | --- | --- | --- |
| `Progress`, the class every caller calls a "row" | `Row`; `StatusRow` keeps its name | none (neither class is named in flnd) | renamed |
| `Progress(label, total, show, _group, *, wait)`, `StatusRow(status, _group)`, `Group(theme, title, parent)` public | made only by `log.progress`, `Group.add` and `Group.add_status` | none | 3 changed |
| `Group.parent` public | private (plumbing for `under`) | none | −1 |
| `label` and `wait` plain attributes; setting `label` doesn't redraw | `label` a settable property, like `caption`; `wait` private (given when the row opens) | none | 1 changed, −1 |
| `finished` and `outcome` on both rows: two reads of one state | `outcome` only (`None` while running) | `job.py:235, 274` | −2 |
| `log.event(kind, …)`: the kinds fixed in `theme.EVENTS` (`skip`, `link`, `shrink`, flnd's words) | `kind` is any name the theme has as both a glyph and a palette colour; another raises `log.Error`. `EVENTS` goes; the three stay theme defaults | `job.py:558, 597, 599, 645, 757, 1155` unchanged | −1 |
| one stream per row | `Row.part()`: a `progress(done, total)` for one more stream, the row showing the sums | `net.py:367-412` (`_Row` goes), `tlg.py` | +1 |
| `record(path: Path)` | `record(path: str \| os.PathLike)` | `engine.py:375` unchanged | 1 changed |

`progress`, `group`, `under`, `Group.add`, `Group.add_status`, the levels and `Row`'s other
members stay.

## theme, style

| Before | After | flnd sites | Snapshot |
| --- | --- | --- | --- |
| three ways to reach a drawer: `draw(kind, value)`, `drawers.<kind>`, `draw_log`/`draw_progress`/`draw_status`/`draw_prompt`/`draw_help` | `draw` and `drawers` | none | −5 |
| `LogRecord(level, message, bar, kind)` with `Bar(label, …, finished, …, wait)`: the label twice, ok/error split across two fields | `LogRecord(level, message, kind)`; `Bar(label, count, total, caption, outcome, show, tick, wait)` | none | changed |
| `Status.label`, painted in the palette's `category`, and `Status.subject` in `target`: flnd's words, and `label` already names a row's text and a help column | `Status.section`, painted in `section`; `Status.subject`, painted in `subject` | `job.py:339-349` | 1 changed |
| `HelpModel.prog`, never drawn | gone | none | −1 |
| `scope(**changes)`, checked at run time | `scope(*, color=…, unicode=…, screen=…)`, typed | none (tests) | 1 changed |
| `Screen(dest, inp, …)`, `Screen.lock` public | `Screen(dest, keys, …)`; the lock private | none | changed, −1 |
| `Namespace` public | private | none | −1 |
| `style`'s representation fields (`fg`, `bg`, `fg_ramp`, `bg_ramp`, `href`, `modifiers`) public | private; `color`, `gradient`, `link`, `paint`, `strip` stay | none | −6 |
| `style.paint(text, depth)` | `paint(text, *, depth)` | none | 1 changed |

`Status`, `Count`, `gradient`, `motion`, `paint`, `palette`, `glyphs` and the detection stay.

## prompt

| Before | After | flnd sites | Snapshot |
| --- | --- | --- | --- |
| `keys: bool \| None` on every question, and the global `keys_shown`: a three-state bool | `keymap.shown: bool`, one setting | none | −2 |
| `ask(…, mask=)`, meaningful only with `secret` | the mask is a theme glyph | none | 1 changed |
| `choose(…, many, min, max)`: four overloads; `min` without `many` a `TypeError` | `choose(message, options, *, …) -> value` and `choose_many(message, options, *, min=0, max=None, …) -> list` | `cli.py:149, 157, 200, 211` unchanged (single choice) | +1 |
| `Keymap` class exported | the `keymap` instance only | none | −1 |
| the alias `accept` for `submit` | gone (no aliases) | none | 0 |

## cli

| Before | After | flnd sites | Snapshot |
| --- | --- | --- | --- |
| `App`'s plumbing public: `children`, `default`, `handler`, `parent`, `name`, `version`, `description`, `epilog`, `row_text()` | private; the constructor's arguments stay | none | −9 |
| `app()` exits and `app.run(argv)` returns; `cli.run(fn)` exits: `run` means two things | `app.run(argv) -> int` only; `cli.run(fn)` becomes `cli.main(fn)` | `__main__.py:8` | 2 changed |
| `arg(help=None, *, …, stdin=True)`: `help` positional (keyword in `opt`); a `*` argument reads stdin unless told not to | `arg(*, help, …, stdin=False)`: reading stdin is declared | `cli.py:75` adds `stdin=True`; `cli.py:116, 172` drop `stdin=False` | 1 changed |
| bare `Annotated[T, "-o"]` and `Annotated[T, "?"]`: second spellings of `opt` and `arg` | gone | none (tests) | 0 |
| `UsageError.usage` set after the error is made | `UsageError(message, *, usage="")` | `cli.py:108, 130, 144, 181` unchanged | 1 changed |
| two-stage Ctrl-C in flnd's engine | `cli.stopping()` (from the tagging pass) | `engine.py` | +2 |

## fmt

| Before | After | flnd sites | Snapshot |
| --- | --- | --- | --- |
| `size(n: int)`, though progress counts are floats | `size(n: float)` | `net.py:382` | 1 changed |
| `filename(…, maxlen=200)`, the unit unsaid | `filename(…, max_bytes=200)` | none pass it | 1 changed |
| two URL scrubbers (`fmt.url`, `log`'s own) | one rule in `fmt.url`, used by `log.record` ([L5](bugs.md#log-theme-prompt-cli-fmt)) | none | 0 |

## media

| Before | After | flnd sites | Snapshot |
| --- | --- | --- | --- |
| eleven `Media` properties copying `Info` (`kind`, `format`, `width`, `height`, `duration`, `fps`, `created`, `location`, `tags`, `codec`) beside `info` | `Media.info` (the record) and `Media.size` (no probe); `codec` becomes an `Info` field | `compress.py:148, 178, 192` already use `.info` | −10 |
| `compress(level, *, format, codec, quality, resolution, fps, audio_bitrate, strip, progress)`: nine options | `compress(level: str \| Encoding = "mid", *, format=None, strip=True, progress=None)`; public `Encoding(quality, resolution, fps, audio_bitrate, codec)` and `media.LEVELS`, a level adjusted with `dataclasses.replace` (R9's private record, made the public way to tune) | `compress.py:153, 189` | changed, +7 |
| `codec=None` means the container's default (H.264), so flnd keeps `OWN_CODECS` | `codec=None` keeps the source's codec when the format can write it (question 8) | `compress.py:59, 148` (`OWN_CODECS` goes) | 0 |
| `progress=` on `compress` and `merge` only | on every verb that encodes (`convert`, `trim`, `audio` too), one meaning | none | changed |
| `merge(…, fps, duration)`: two ways to time photos | `duration` only (`1 / fps`) | `net.py:250` unchanged | 1 changed |
| `frames(*, dest, …)`: the main argument keyword-only | `frames(dest, *, …)` | none | 1 changed |
| `Media.audio(…)`, a noun for an action | `extract_audio(…)` | none | renamed |
| `ImportError` for no Pillow; `RuntimeError` from ffmpeg ([M6](bugs.md#media)) | `media.MissingError`; a `media.Error` subclass | `compress.py:159, 164`, `files.py:153` (their broad catches narrow) | 0 |
| flnd's own media check and naming by content | `media.verify` and `media.NotMediaError` (from the tagging pass) | `files.py:120-160`, `net.py:315-357`, `repair.py:150` | +2 |

`open`, `sniff`, `kind`, `check`, `convert`, `resize`, `crop`, `trim`, `save`, `close` and the error
tree stay.

## worker

| Before | After | flnd sites | Snapshot |
| --- | --- | --- | --- |
| `as_ordered`, `as_completed`, `pool(**options: Any)`: six options invisible in the signatures | explicit keyword-only `concurrency, retries, timeout, label, progress, processes` | `repair.py:111`, `engine.py:489`, `compress.py:229` unchanged | 3 changed |
| `pool(**options)` beside `Pool(run: _Run)` exposing a private type | `worker.Pool(*, …)`, the one way | `job.py:773, 781` | −1 |
| flnd stops a pool by calling `pool.__exit__(type, error, tb)` | `Pool.stop()` (drop queued, wait for running); `close()` and `join()` for a normal end | `job.py:756` | +1 |
| `join()` returns results and so does `results` | `join(timeout=None) -> bool`; results through `results` | `job.py:758` | 1 changed |
| `Result.item` is `args[0]`, or a tuple when several; `_position` in the public signature | `submit(fn, item)` takes one item (more through `partial`); `_position` not a parameter | `job.py:698, 759, 892` | changed |
| `as_ordered` (a list, an `as_` prefix) | `worker.map(fn, *iterables, …) -> list`, the stdlib's word | none | renamed |
| — | `Pool.run_on_owner(fn)`, `worker.Loop` (from the tagging pass) | `job.py` refresh queue, `site.py:115-127` | +3 |

## archive

| Before | After | flnd sites | Snapshot |
| --- | --- | --- | --- |
| `zip(sources, dest, …)` (also writes 7z and tar, and shadows the builtin), `unzip(parts, dest, …)`, `ls(path, …)` | `pack(sources, dest, *, password, split)`, `unpack(path, dest, *, password, conflict, limits)`, `entries(path, *, password, limits)` | none | 3 renamed |
| `unzip` takes one path or a list of parts; `ls` one path | both take a path or parts | none | 1 changed |
| `ImportError` for no `pyzipper`/`py7zr` | `archive.MissingError` (exists) | none | 0 |

## bundler

| Before | After | flnd sites | Snapshot |
| --- | --- | --- | --- |
| `build(…)` raises bare `RuntimeError`, `ImportError`, `FileNotFoundError`; `bundler.Error` never raised | `BuildError(Error, RuntimeError)`, `MissingError` for no PyInstaller; `hidden` and `data` typed as sequences ([B1](bugs.md#worker-archive-bundler)) | `scripts/dev.py:148` | +1 |

## browser

| Before | After | flnd sites | Snapshot |
| --- | --- | --- | --- |
| `open(*, profile, cdp, headless, viewport, locale, timezone, args, stealth)`: eight options, and `cdp` excludes six of them with a `ValueError` | `open(launch: Launch = Launch())` and `attach(cdp, *, viewport=None)`; `Launch(profile, headless, viewport, locale, timezone, args, stealth)` | `site.py:389`, `tlg.py:501` | changed, +9 |
| `Browser(context, …)` and `Page(raw, browser)` constructors public; flnd subclasses `Page` | made only by `open`/`attach` and `Browser.open`; flnd's `_PacedPage` goes (it passes `Scroll`) | `site.py:301-308, 443-455` | 2 changed |
| `on_response(match, handler)`, `on_request(match, handler)`, `Subscription`: callbacks into the caller | `responses(match, *, lines=False, scroll=False) -> Watch`, `requests(match) -> Watch`; a `Watch` iterates, has `.request` and `stop()`, works with `with` | `watch.py:49-131` (about 90 lines go), `tik.py:303, 425, 483` | −4, +6 |
| `fetch(…, soft: bool)`, three overloads | `fetch(…)` and `try_fetch(…)`, as R7 does for `http` | `site.py:377` | changed |
| `scroll(*, within, until, count, idle, wait, pause, on_step)`: seven options | `scroll(*, until=None, within=None, timing=Scroll())`; `Scroll(idle=10.0, wait=3.0, pause=0.3, count=None, on_step=None)` | `site.py:450-455`, `watch.py:92` | changed, +4 |
| `handoff` raises a builtin `RuntimeError` when headless; waits use `time.monotonic` | a `browser.Error`; `shared.clock` | `site.py:298` | 0 |

## cookies

| Before | After | flnd sites | Snapshot |
| --- | --- | --- | --- |
| `Jar.filter(*, domain: str, names)` | `filter(*, domains: str \| Iterable[str] \| None = None, names=None)` | `auth.py:329` (`_own`'s loop becomes one call) | 1 changed |
| `Error(shared.Error, ValueError)`, so callers catch `ValueError` | `Error(shared.Error)`; callers catch `cookies.Error` | `auth.py:88, 104, 193, 303` | 1 changed |
| a BOM or an empty expiry refused ([R3](bugs.md#browser-cookies-selector)) | read | none | 0 |

## selector

| Before | After | flnd sites | Snapshot |
| --- | --- | --- | --- |
| `script(id=None, *, pattern=None)`: exactly one of two, else `ValueError` | `script(id)` and `match(pattern) -> Selection` (JSON from the first group, searched in the source text: [speed](speed.md#browser-selector)) | `tik.py:287, 444, 658` unchanged | +1 |
| query errors are raw `cssselect`, `jmespath`, `jsonpath` or `ValueError` | `selector.QueryError(Error, ValueError)` | none | +1 |
| `jmespath` splits a list result into matches and drops nulls; `jsonpath` does neither | both: a list result is its matches, so `first` and `all` mean one thing in each | `fbk.py:635` (the one JSONPath) | 0 |
| `Selection(raw)` public | made only by `parse` | none | 1 changed |

## The snapshot after

About 65 lines leave the `shared` part of `api.txt` (plumbing made private, second spellings
gone) and about 45 arrive (records, `Watch`, the tagging pass's names): from 445 to about 425,
with no call taking more than five options, every capability kept, and no name meaning two things.
