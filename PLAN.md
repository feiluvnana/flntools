# PLAN: the revamp

## 0. Why

The user, 2026-11-16:
- **`shared`** is a toolkit useful to every script: a minimal, elegant API, fast, with good DX.
- **flnd** is a TUI app with three UI modes (plain, inline Textual, full-screen Textual), not log lines.
- **flnd compresses and extracts by itself** (user, 2026-11-16; this replaces the earlier flnc tool):
  - `--compress` makes every saved image, video and audio file visually lossless and as small as it goes, changing format (WebP, AVIF) when that is smaller;
  - archives a source downloads (loi's) are extracted automatically, with the password the source knows;
  - the history remembers what was compressed, so nothing is done twice.
- **flnd makes slideshows, cards and albums** from its media, with ffmpeg and Pillow: `flnd media show|card|album` (§5). There is no separate flns (user, 2026-11-16).
- **flnd is a library first**; its CLI is a thin layer and the only command.
- **Performance, simplicity and DX** are first-class: code is concise, short and intuitive, and annotations and decorators carry intent where that reads better than options.

Decisions (user, 2026-11-16):
- Textual for the TUI.
- Compression replaces files in place after verifying them. Its quality is always visually lossless, with no level on the CLI, and format change is allowed.
- No flnc: compression, archiving and extraction are built into flnd's downloader (user, 2026-11-16).
- `archive`/`extract` are the verbs.
- This is a revamp: names and the CLI may change, with no aliases.
- Old phases are ignored, and `plan/` is gone.

## 1. Principles

### 1.1 API rules
`tests/test_names.py` checks these over `tests/data/api.txt`.

1. **Short names.** One word where one word says it: `fetch`, `extract`, `probe`, `files`, `saved`.
2. **A read is a property**: `report.ok`, `fl.targets`, `page.url`. There is no `get_x()`, and no `x()` without arguments that only reads.
3. **A read with options is a property that returns a `Query`.** Option methods narrow it, and `.get()` runs it. `first` and `count` are properties of the query:
   ```python
   fl.targets.filter(source="ins", sync=True).get()            # list[Target]
   fl.files.filter(kind="image").sort("date", desc=True).limit(20).get()
   fl.files.count
   ```
4. **An action is a verb**: `download`, `sync`, `track`, `forget`, `compress`, `archive`, `extract`, `login`.
5. **One spelling.**
   - There are no aliases and no `try_x` twins.
   - No bool switches the return type. A soft failure is `check=False`, which returns the result with its status (`sh.run`, `http.fetch`).
6. **Options are keyword-only**, at most 4 of them. Past that, split the call into two verbs, use a record, or use a decorator.
7. **Progress goes to a sink, never a bare callback.** A sink is any object with `total` and `advance(n)`; `ui.Row` is one. Pools and transfers also expose `done`/`total` as properties.
8. **Module-level functions are verbs or pure computations** (`text.size(n)`, `clock.now()`). A module can't have plain properties, so rule 2 applies to objects; this is the one exception.
9. **Errors.** Each module has one `<module>.Error`, under `shared.UnsafeError`, `BrokenError`, `RefusedError` or `MissingError`. A message names the thing and what to do: `"7zz: not found; download it now? [Y/n]"`.
10. **Typed.** Generics flow through: `Query[T]`, `Task[R]`, `Table[T]`, `Result[R]`. Pyright reports 0 errors and 0 warnings, and `reveal_type` tests sit in `tests/typing/`.

### 1.2 Simplicity
- A module reads top to bottom: docstring, imports, constants, entry point, then helpers in call order.
- A docstring is one sentence. The example sits only in the module docstring and is the one in this plan.
- No wrapper class where a function does. No `raw` escape hatch unless a caller needs the library underneath; only `http.Client.raw`, `browser.Page.raw` and `ui.raw` keep it.
- Records are frozen `msgspec.Struct`s: fast, typed, and they encode to JSON.

### 1.3 Performance
- **Import is cheap.** `import shared` loads nothing heavy, and every module is lazy. `flnd --help` takes < 150 ms, which a test checks with `-X importtime` over a list of heavy modules.
- **Disk.** SQLite in WAL mode, not JSON rewrites. `os.scandir`, not `Path.rglob`, plus `stat` results reused. 1 MiB I/O chunks.
- **Network.** One HTTP/2 client per host, with keep-alive. Segmented range downloads for large files. Retries with jittered backoff and `Retry-After`.
- **CPU.** Bounded pools with backpressure, so 100k items never mean 100k futures in memory. Subprocess encoders on threads, since a subprocess frees the GIL.
- **Measured.** `scripts/dev.py bench` runs offline micro-benchmarks. `check` holds counts that stand in for speed (rows read, syscalls, bytes copied) so regressions fail without timing noise.

### 1.4 DX
- **Annotations carry intent.** `@app.command` with `Literal`/`Enum` choices, `@source.target("…")`, and `store.Row` records for typed tables.
- **Defaults do the right thing.** The UI mode follows the TTY. Tools are found on PATH or offered for download. Paths default to the home folder.
- **Fakes ship with the toolkit**: `clock.Fake`, `prompt.fake`, `sh.fake`, `http.fake`. Tests of any script need no monkeypatching.

## 2. `shared`, module by module

No module and no capability is removed; spellings are. Each entry gives the purpose, the API, an example, the performance work and the change from today.

### 2.1 root: errors and `Query`
```python
from shared import Query, Error, UnsafeError, BrokenError, RefusedError, MissingError

q = Query(lambda: rows)                    # a source that returns an iterable
q.filter(kind="image").filter(lambda r: r.size > 1e6).sort("size", desc=True).limit(5).get()
q.first; q.count
```
- `Query[T]` is immutable: each option method returns a new query.
  - `filter(**eq)` matches attributes or keys; `filter(fn)` takes a predicate. Both may be chained.
  - `sort(by, desc=False)` and `limit(n)`.
  - `get() -> list[T]`, `first -> T | None`, `count -> int`.
- **Performance**: a source may implement `Query.push(filters, sort, limit)`. `store` pushes all three into SQL, and `count` becomes `SELECT count(*)`. Plain iterables are filtered lazily, with `heapq.nsmallest` for sort with limit.
- **Change**: new; every collection read in `shared` and flnd returns one.

### 2.2 `text`
```python
text.size(1_234_567)         # "1.2 MB"
text.duration(3725)          # "1h 2m"
text.count(3, "file")        # "3 files"
text.filename("a/b:c?.jpg")  # "a_b_c_.jpg", safe on every OS, ≤255 bytes
text.redact(url)             # the URL without its signed query
text.fold("Ünïcode")         # "unicode"
```
- **Change**: flattened. `clean.filename` → `filename`, `clean.url` → `redact`, `fmt.size/duration/count` → `size/duration/count`. The `clean`/`fmt` namespace classes go.

### 2.3 `clock`
```python
clock.now(); clock.wall(); clock.sleep(2, stop=event)    # sleep returns False if stopped
with clock.use(clock.Fake()) as fake:
    fake.advance(10)
```
- **Change**: `Clock.now`/`wall` become properties on the object; the module functions stay. No other change.

### 2.4 `fs`
```python
fs.write("out.json", data)                  # atomic; sync=True to fsync
with fs.stage("big.mp4") as tmp:            # .big.mp4.stage beside dest, moved on success
    sh.run("ffmpeg", "-i", src, tmp)
with fs.stage("a.mp4") as tmp:              # download, check, then land: nothing reaches a.mp4 on error
    c.download(url, tmp)                    # resumes across runs, since the stage name is fixed
    media.probe(tmp)
fs.touch(path, mtime=old.stat().st_mtime)   # keep a date across a replace
with fs.lock("data/.lock", timeout=10): ...
fs.sweep("media/", keep=7 * 86400)          # remove stale temp parts
for entry in fs.walk("media/", suffix=(".jpg", ".mp4")): ...   # os.DirEntry, stat cached
```
- **Performance**:
  - `walk` uses `os.scandir`, recursive and iterative, and yields entries with their cached stat. It is 3–5× faster than `rglob` and `stat` on 100k files.
  - `write` streams bytes without a second copy.
  - `stage` stays on the same filesystem, so the move is a rename.
- **Staging is one mechanism.** `stage(dest)` uses a fixed name per destination, `.<name>.stage`, so:
  - a crashed run leaves its partial file where the next run finds it;
  - a `stage` that wraps `http.download` resumes too;
  - `fs.sweep` removes stages older than `keep`.
  - two writers of one destination can't share a stage: `stage` holds a lock on it, and the second writer waits or gets `fs.BusyError`, depending on `timeout`. flnd's `PART_GLOB` becomes `.*.stage`.
  
  On a clean exit `stage` renames the file into place; on an exception it keeps the partial file for resume (`stage(dest, keep=False)` deletes it). `http.download` and `fs.write` both stage through it, so there is one safe-write path in `shared`.
- **Change**:
  - The dual-return `write(dest)` context form becomes `stage`, with a fixed stage name instead of a random one.
  - New: `touch`, `walk`.

### 2.5 `sh`
```python
r = sh.run("git", "status", "--short")         # args, not a list; check=True raises sh.RunError
r.code, r.out, r.err
ff = sh.find("ffmpeg")                         # on PATH, or MissingError naming the tool
cz = sh.find("caesiumclt", specs=CAESIUM)      # downloads, verifies sha256, caches
for line in ff.stream("-i", src, "-progress", "pipe:1", dest):   # live stdout lines
    ...
cz.version                                     # parsed once from `--version`, cached
with sh.fake({"ffmpeg": handler}): ...         # tests: argv → (code, out, err)
```
- **Performance**: `find` caches its result for the life of the process. `version` is cached on disk, keyed by the binary's path and mtime, so `--compress` doesn't spawn every tool at each start.
- **Change**:
  - `run(cmd: list)` → `run(*args)`.
  - `tool(name, platforms=)` → `find(name, specs=)`.
  - `Result.returncode/stdout/stderr` → `code/out/err`.
  - `capture=False` → `Tool.stream`.
  - New: `version`, `fake`.

### 2.6 `store`: SQLite, typed tables
```python
db = store.open(".flnd/metadata/history.db")
db["state"]["cursor"] = "abc"                  # a Table is a MutableMapping of JSON values

class File(store.Row):                         # a msgspec Struct; the table is named in the class
    table = "files"
    state: str
    size: int
    mtime: int

files = db[File]                               # Table[File]: values decode to File
files["ins/a.jpg"] = File("kept", 1000, 17)
files.rows.prefix("ins/").filter(state="failed").get()   # Query[tuple[str, File]], pushed into SQL
with db.batch():                               # one transaction for many writes
    for k, v in updates: files[k] = v
```
- **Performance**:
  - WAL mode with `synchronous=NORMAL`. Values are msgspec JSON, so encoding and decoding cost about a microsecond.
  - A prepared statement per operation, and `batch()` for bulk writes.
  - **Targets:** 100k reads in under 0.3 s and 100k batched writes in under 0.5 s. Today the JSON store rewrites the whole file on every save.
  - One connection per thread, from a small pool.
- **Change**:
  - `Store.table(name)` → `db[name]` or `db[RowType]`.
  - `set`/`update`/`keys(prefix)`/`items()` → the Mapping protocol plus `rows`.
  - An existing JSON store is read once and converted.

### 2.7 `worker`: one `Pool`
`Pool` is the whole module's API: no `map`, no task decorator, no separate `Loop`. Sync functions run on threads; `async def` functions run on the pool's own event loop thread, so the same `submit` serves both.

```python
with worker.Pool(jobs=8, retries=2, stop=trap.soon, progress=row) as pool:
    for path in paths:                    # lazy: submit blocks once jobs × 2 are in flight
        pool.submit(thumb, path)          # Result.item is path; a coroutine function works too
    for r in pool.results:                # Result[Any] as each finishes, until all are done
        r.value if r.ok else r.error
    pool.pause(); pool.resume()           # no new task starts while paused
    pool.done, pool.total                 # what a UI or a script reads
    pool.call(fn)                         # run fn on the thread that owns the pool, wait for it
    pool.stop()                           # cancel what hasn't started; running tasks see stop
```
- Options (all keyword, all optional):
  - `jobs` takes an int, `"cpu"` (CPU count) or `"io"` (min(32, cpu + 4), the default);
  - `retries` backs off exponentially with jitter, capped at 8 s;
  - `stop` is an `Event` the pool watches;
  - `progress` is a sink.
- In-order results are the caller's business: `Result.item` names the input, and `sorted(pool.results, key=...)` or a dict by item does it. One rule, no second method.
- Leaving the `with` block waits for every task. An exception in the block stops the pool and cancels what's queued.
- **Performance**:
  - Backpressure on `submit`, so 1M items never means 1M futures. Today `map` materializes the list and keeps every future.
  - Finished futures are released as `results` yields them, so memory stays flat.
  - The event loop thread starts only on the first coroutine submitted.
- **Change**:
  - `map` goes; its callers become a `with Pool` loop.
  - `Loop` folds into `Pool` (coroutine functions).
  - `concurrency` → `jobs`.
  - `as_completed()` and `join()` → `results`; a block exit waits.
  - `progress=callback` → `progress=sink`.
  - `timeout` goes; a stalled task is the task's own timeout (`http`, `sh` have them).
  - The fake `run_on_owner` (it ran in place) → `call(fn)`, which really runs `fn` on the owner thread, as flnd's browser needs.
  - New: `pause`/`resume`, `done`/`total`.

### 2.8 `http`
```python
r = http.fetch("https://example.com/api", params={"q": "x"})
r.json; r.text; r.html.css("a::attr(href)").get(); r.status; r.ok
r = http.fetch(url, check=False)               # no raise on 4xx/5xx; read r.status

with http.Client(headers=H, rate=2.0, retry=http.Retry(3, statuses=(429, 503))) as c:
    c.fetch(url, body={"a": 1})                # a dict is JSON; bytes or str are raw
    c.download(url, "a.mp4", progress=row)     # stages via fs.stage, resumes, checks size, returns the Path
    c.download([mirror1, mirror2], "a.zip")    # falls back between mirrors
with http.fake({"https://example.com/api": {"ok": True}}): ...   # tests
```
- **Performance**:
  - HTTP/2 with a shared connection pool per Client. Downloads read 1 MiB chunks; today httpx's default is 64 KiB.
  - A file ≥ 64 MiB on a server that accepts ranges downloads in 4 parallel segments into one preallocated part.
  - The pacing per host is token-bucket based, not a sleep per call.
  - `Response.json` decodes with `msgspec.json`, about 3× faster than `json`.
- **Change**:
  - `soft` → `check`.
  - `data`/`json` → `body`.
  - `attempts`/`timeout` → `Client(retry=, timeout=)`, so each call has ≤ 4 options.
  - `progress` callback → sink.
  - New: `fake`.

### 2.9 `cookies`
```python
jar = cookies.load("chrome", profile="Default")    # or a cookies.txt path, or a JSON export
jar.filter(domains="example.com").header           # "a=1; b=2"
jar.merge(other).save("cookies.txt")
jar.list                                           # list[Cookie]
```
- **Change**: `Jar.raw` → `Jar.list`.

### 2.10 `selector`
```python
doc = selector.parse(html_or_json)
doc.css("a.post::attr(href)").get()            # list[str]
doc.css("h1::text").first
doc.script("__DATA__").json.jsonpath("$.items[*].id").get()
doc.xpath("//p").text                          # joined, stripped
```
- **Change**: `all` → `get()`, matching `Query`. Each narrowing method returns a `Selection`.

### 2.11 `browser`
```python
with browser.open(profile=".flnd/chrome", headless=False) as b:
    page = b.tab("https://example.com")
    for r in page.watch("/api/feed", until=page.scrolled):   # responses as they arrive
        items += r.json["items"]
    page.wait("#done", timeout=10)
    page.document.css("img::attr(src)").get()
    b.cookies                                   # a Jar; b.cookies = jar replaces it
```
- **Performance**:
  - One browser per profile is reused across tabs.
  - `watch` reads response bodies lazily, only when `.json`/`.text` is read.
  - `scroll` stops on a height and network idle check, not fixed sleeps.
- **Change**:
  - `Browser.open(url)` → `tab(url)`.
  - `BrowserCookies` → `cookies` property.
  - `responses()`/`requests()` → `watch(match, until=)`; each response carries its `request`.
  - `Watch.stop` stays.

### 2.12 `crypto`
```python
crypto.digest("big.mp4")                         # a path is streamed; bytes or str hashed directly
crypto.digest(b"x", algorithm="blake2b")         # any hashlib algorithm

# Default: AES-256-GCM, a random nonce, a framed blob that decrypt reads back without options.
blob = crypto.encrypt(data, key, context=b"cookies")
crypto.decrypt(blob, key, context=b"cookies")

# Anything else: a Cipher record names the algorithm, mode and parameters.
cbc = crypto.Cipher("aes-256-cbc", iv=iv, padding="pkcs7")
crypto.encrypt(data, key, cipher=cbc)            # raw ciphertext, interoperable with other tools
crypto.decrypt(ct, key, cipher=cbc)
crypto.decrypt(ct, key, cipher=crypto.Cipher("aes-128-ctr", iv=nonce))
crypto.decrypt(ct + tag, key, cipher=crypto.Cipher("chacha20-poly1305", iv=nonce))
crypto.Cipher.names                              # every supported name, for help and validation

key = crypto.derive("password", salt=salt, kdf="scrypt", length=32)   # or "pbkdf2", "argon2id", "hkdf"
key = crypto.token(32)                           # random bytes from `secrets`
```
- `Cipher(name, *, iv=None, padding=None, tag=16, framed=None)` is a frozen record:
  - **`name`**: `"<algorithm>-<bits>-<mode>"`, covering what cryptography offers.
    - AES-128/192/256 with GCM, CCM, SIV, GCM-SIV, OCB3, CBC, CTR, CFB, CFB8, OFB, XTS and ECB;
    - ChaCha20 and ChaCha20-Poly1305;
    - Camellia, SM4, and TripleDES (`3des-cbc`) for reading legacy data.
  - **`iv`**: the IV or nonce. `None` means a random one, which is then framed into the output.
  - **`padding`**: `"pkcs7"`, `"ansix923"`, `"iso7816"` or `"none"`. The default is PKCS7 for CBC/ECB and none elsewhere.
  - **`tag`**: the AEAD tag length, appended to the ciphertext as most tools expect.
  - **`framed`**: whether the output carries a version byte, the cipher name and the nonce, so `decrypt` needs no options. The default is framed when `iv` is `None`, and raw when an `iv` is given.
- **`context`** is the associated data for AEAD modes. It is refused (`crypto.Error`) for modes that can't authenticate it, rather than silently ignored.
- **Safety, stated in errors**:
  - An AEAD cipher with a fixed `iv` encrypts once per `Cipher` object; a second `encrypt` raises `crypto.UnsafeError`, since nonce reuse breaks GCM.
  - ECB and unauthenticated modes work, but only by naming them.
  - A key of the wrong length names the length the cipher wants.
- **Performance**:
  - A file is hashed in 1 MiB chunks with `hashlib.file_digest`.
  - `encrypt`/`decrypt` take `bytes`, a `Path` or a binary file. Large inputs stream through the cipher's update/finalize in 1 MiB chunks, so a 4 GB file never sits in memory. AEAD modes that cryptography only offers one-shot (GCM-SIV, SIV, OCB3) cap input at 2 GiB with a clear error.
  - Cipher objects are cached per (name, key).
- **Change**:
  - New: `Cipher`, `derive`, `token`, and streaming input.
  - The default blob stays version 1 (AES-256-GCM: version byte, nonce, ciphertext and tag), so flnd's vault still opens. A framed blob of any other cipher is version 2, which adds the cipher name. `decrypt` reads both.

### 2.13 `archive`
```python
a = archive.open("photos.7z", password="pw")    # zip, tar.*, 7z, rar
a.entries.filter(kind="file").get()             # Query[Entry]
a.extract("photos/", conflict="skip")           # limits on by default
archive.open("x.zip").extract("x/", passwords=["a", "b"])   # tries each
archive.create(["photos/"], "photos.7z", password="pw", level=9, split="2g")
```
- **Performance**: 7z/rar work runs in `7zz`/`unrar` subprocesses with `-mmt=on`. zip/tar run in-process, streaming. Limits are checked from the listing before any byte is written.
- **Change**:
  - `ls` → `Archive.entries`.
  - `unpack` → `Archive.extract`.
  - `pack` → `create` (`archive.archive` would stutter).
  - New: `split`, `level`, `passwords`.

### 2.14 `media` (new)
```python
info = media.probe("a.mp4")
info.kind, info.codec, info.width, info.duration, info.animated
media.compress("a.jpg", "a.avif", level="high")   # the format comes from dest's suffix
media.compare("a.jpg", "a.avif")                  # SSIM, 0–1
media.encoders                                    # Query[Encoder]: what this machine can do
```
- **Encoders**: tools come through `sh.find` with pinned specs. ffmpeg comes from PATH.
  - **caesiumclt** writes JPEG, PNG and WebP.
  - **avifenc** writes AVIF.
  - **oxipng** makes PNG lossless.
  - **ffmpeg** handles video, audio and animation: SVT-AV1, x265, libopus and libwebp_anim.

  | input | candidates | high / max |
  | --- | --- | --- |
  | JPEG | avif · jpeg (mozjpeg) · webp | avif q65/q55 · jpeg q80/q70 · webp q80/q70 |
  | PNG | png lossless (oxipng) · webp lossless (flat art) · avif (photos) | — · — · q65/q55 |
  | WebP | avif · webp | as above |
  | GIF animated | webp anim · avif sequence | q80/q70 |
  | video | mp4 SVT-AV1 10-bit preset 6, Opus 96k, `+faststart`, metadata copied; x265 when SVT-AV1 is missing | CRF 30/35 (x265 24/28) |
  | audio | opus | 96k/64k |
- **Performance**:
  - `probe` runs one ffprobe with JSON output and is cached per path and mtime. Images that Pillow can read are probed in-process, with no subprocess.
  - `compare` downscales both to ≤ 1024 px before SSIM.

### 2.15 `prompt`
```python
name = prompt.ask("Name?", default="x")
if prompt.confirm("Download Chromium (200 MB)?"): ...
pick = prompt.choose("Login with", ["chrome", "qr", "cookies.txt"])
prompt.tty()
with prompt.fake(["example_user", True]): ...     # tests
```
- In `ui.Mode.INLINE`/`FULL`, prompts open as Textual modals.
- **Change**: `interactive()` → `tty()`, `autofill()` → `fake()`, and `prompt.raw` goes.

### 2.16 `cli`
```python
app = cli.App("flnd", help="Download, compress and show media.")

@app.command
def show(targets: list[str], *, by: By = By.POST, style: Style = Style.CLASSIC, when: str = ""):
    """Make a slideshow per group of saved photos.

    Args:
        by: How photos are grouped: one show per post, day, month, year, or all.
    """

with cli.trap() as stop:      # Ctrl-C: stop.soon, then stop.now; stop.paused for the TUI
    ...
raise SystemExit(app.run())
```
- `Literal`/`Enum` annotations become choices, and the docstring `Args:` becomes help.
- Global options: `--ui plain|inline|full`, `-v`, `--home`.
- **Change**: `stopping()` → `trap()`, `Stopping` → `Stop`, plus `paused`.

### 2.17 `ui`: three modes
```python
ui.info("Saved {n} files", n=3); ui.warn(...); ui.error(...); ui.ok(...); ui.debug(...)
with ui.live(stop=stop, mode=ui.Mode.INLINE):   # the default: INLINE on a TTY, else PLAIN
    with ui.group("ins:example_user") as g:
        row = g.add("reels", total=40, unit="files")
        row.advance(); row.fail("HTTP 403"); row.finish()
ui.level = "debug"; ui.mode = ui.Mode.FULL
```

| mode | what it is | keys |
| --- | --- | --- |
| `PLAIN` | today's Rich output: log lines above live rows; plain lines when not a TTY | Ctrl-C |
| `INLINE` | a Textual app drawn inline under the prompt, kept in the scrollback | `q`/Ctrl-C stop (twice: now), `p` pause, `v` debug, `f` failures, `m` switch to FULL, `↑↓` |
| `FULL` | the same app on the alternate screen, with a full failures pane and log; the terminal is restored on exit | the same, `m` back to INLINE |

- `live` runs the work on the calling thread and Textual on its own thread with its own asyncio loop, so callers don't change. Rows and log lines go through one lock-free queue.
- `FLNT_UI` sets the default mode. `inline`/`full` without a TTY or without `textual` fall back to `PLAIN` with one debug line.
- **Performance**: rows are drawn at most 10 times a second, coalescing updates. 10k `advance` calls a second cost < 5% CPU, which a counter test checks via the drawn frames.
- **Change**:
  - The nested `UI.Error/Group/Row` duplicates and `Row.raw` go.
  - `Row.part()` goes; a `Row` is itself a progress sink.
  - New: `live`, `Mode`, `mode`.

### 2.18 `bundler`
`bundler.build("flnd/__main__.py", name="flnd")` is unchanged.

## 3. flnd

### 3.0 Three roles (user, 2026-11-16)
> "each source specify how to login (login and saved credentials flow, the source control all of it, engine just invoke), then a way to listing for each target (so target is for listing). and then those listing will be passed to downloader and also show progress"

| role | owns | the engine's part |
| --- | --- | --- |
| **login** (per source) | the whole flow (browser cookies, cookie file, QR, API key, password), where the credentials live, refreshing them, and whether the source is logged in | call `source.login/logout/logged_in` once per run, handing the source its own `Auth` |
| **listing** (per target) | a target names one listing; the source turns it into `Post`s (finders: `Target`s) | resolve, walk, apply `--from`/`--time`/`--limit` and the sync early stop |
| **downloader** (the engine) | dedupe, resume, dates, history, retries, parallelism, progress rows | everything; a source never touches the disk or the history |

**Routes kept** (user, 2026-11-16):
- every single-URL route: tik video/photo, ins post/reel, tweet, mht chapter, loi post;
- each source's main target: tik/ins/twt/fbk profile, tlg chat, loi post, mht manga;
- finders: `ins:me`, `mht?query`, and a new `loi?words`.

mht's author and genre routes go.

**Today, measured** at `a9b8d31`:
- 11,084 lines (engine side 6,811, sources 4,273);
- 102 concepts a source author learns;
- 5 ways a route can behave;
- 15 frames and 16 classes per download;
- 185 public `flnd` names.

**Problems found, each with file:line:**

| # | problem | evidence |
| --- | --- | --- |
| P1 | every `from_=` repeats the default (7 of 7) | loi.py:59, mht.py:51,66,106,119,132, tlg.py:73 |
| P2 | every route `folder=` repeats `Source.folder` (7 of 7) | loi.py:60, mht.py:52…133, tlg.py:74 |
| P3–4 | `discovers` is needed once (ins:me); `shown` works around the one-placeholder rule | ins.py:146, mht.py:131, source.py:278, 289 |
| P5 | `--from` validated twice; categories filtered twice | engine.py:122-123, options.py:54/63, targets.py:311, job.py:261 |
| P6 | folder checks run every download; `FOLDER_FIELDS` knows every source | targets.py:28, 255 |
| P7 | dead code: `getattr(route, "params")` (gone since e4fc627); `Source.browser` set by 4 sources, read by none | targets.py:283; fbk.py:156, ins.py:108, tik.py:317, twt.py:153 |
| P8 | the login check in 4 copies | engine.py:437, 810, 941, dry.py:42 |
| P9–10 | 6 download paths; 8 finder functions in 2 modules | engine.py:156, 418, 463, 495, 581, 755 |
| P12 | early-stop rules written 4 times; on sync each newest-first post is looked up twice | job.py:48-49, 1573; ins.py:858, tik.py:63, twt.py:457 |
| P13 | a thread-local carries the cursor between `pages()` and the job | source.py:807-881 |
| P14 | 184 lines of browser proxies; a thread hop per DOM call | site.py:599-782 |
| P15 | private coupling through `getattr` into `Site` and `http.Client` | feed.py:40-155, site.py:115-119 |
| P17 | async bridging in 6 places for one async source (tlg) | engine/job/site |

### 3.1 The library
```python
import flnd

fl = flnd.Flnd(".flnd")
for saved in fl.download("ins:example_user", only=["reels"], when="2026", compress=True):
    saved.path, saved.target, saved.post, saved.kind     # each file as it lands
report = flnd.Report.of(fl.download("twt:example_handle"))   # totals: ok, files, failed
fl.preview("twt:example_handle")             # list[Item]: what download would take, nothing saved
fl.forget("twt:example_handle")             # stop syncing; download syncs by default
fl.sync()                                    # every followed target; yields Saved too
fl.targets.filter(source="ins", sync=True).get()
fl.files.filter(target="ins:example_user", kind="image").sort("date").get()
fl.move(old, new)                            # keep history true when a file is renamed
fl.login("ins", "chrome:Default")            # or "cookies.txt", or "qr": the source decides
fl.logout("ins"); fl.logins.get()
with fl.connect("ins") as site:              # a Site for scripting
    site.fetch("https://www.instagram.com/api/...").json
flnd.resolve("https://x.com/example_handle") # Target
```
- **`download` is a generator** of `Saved` events, so a script can act on each file as it lands (make a card per post, say). `compress=True` compresses each file in the downloader itself (§4). The CLI consumes the same events to feed `ui.live`, which makes progress a consumer, not a callback.
- `flnd.Home` (`media`, `metadata`, `cache`, `made`) is the home's layout.

| before | after |
| --- | --- |
| `Engine` and the module-level `download`/`open` | `Flnd(home)` |
| `download(..., dry=, sync=) -> Report` | `download(...) -> Iterator[Saved]` (follows the target), `preview`; `Report.of(events)` |
| `target_ls`/`target_sync`/`target_del` | `targets`, `forget` |
| `auth_ls`, `logins`, `cookie_logins` | `logins` (`Query[Login]`) |
| `login(code, method, browser=, profile=, path=)` | `login(code, how)` |
| `TargetInfo`, `AuthInfo`, `TargetReport` | `Target`, `Login`, `Saved`, `Report` |
| `Site.try_fetch` | `Site.fetch(check=False)` |
| `Post.filed(category)` | engine-private |

### 3.2 Writing a source
```python
class Example(Source):
    """example.com: a user's posts, one post by URL, and a search."""
    code, folder = "exm", "{user}"

    def login(self, auth: Auth, how: str, /) -> None:        # optional: the default covers
        auth.cookies = auth.browser_cookies(how)              # "<browser>:<profile>" and files

    def logged_in(self, auth: Auth, /) -> bool:
        return "session" in auth.cookies

    @target("exm:{user}", "example.com/{user}")
    def profile(self, site: Site, user: str) -> dict:
        return site.fetch(f"https://example.com/api/{user}").json

    @profile.category(newest_first=True)
    def posts(self, site: Site, profile: dict) -> Iterator[Post]:
        for page in site.pages(f"/api/{profile['id']}/posts", cursor="next"):
            for p in page["items"]:
                yield Post(p["id"], [File(p["url"])], date=p["time"])

    @target(r"example.com/p/{id:\d+}")
    def post(self, site: Site, id: str) -> Iterator[Post]: ...

    @target("?{words}")                                       # a finder: yields Targets
    def search(self, site: Site, words: str) -> Iterator[Target]: ...
```
- **One decorator, three behaviours.**
  - A single post returns `Iterator[Post]`.
  - A main target has `@<route>.category` methods.
  - A finder is a `?{…}` pattern, or `finds=True` with a literal pattern (`@target("me", finds=True)`).
  - `shown`, `discovers`, `from_` and route `folder` go.
  - `from_` is always the route's categories, and the folder is the category's, then the Source's.
  - Folder templates are checked once, at class creation (`TypeError`), against `ENGINE_FIELDS | Source.fields`.
- **`Auth`** is the one credentials object a source gets:
  - `auth.secrets`, its own encrypted vault;
  - `auth.cookies`, a `Jar` saved on set;
  - `auth.browser_cookies(how)`, `auth.cookie_file(path)`, `auth.ask(...)` and `auth.expiring`.

  `auth.py` shrinks to the vault and these helpers (~250 lines est.).
- **Listings touch the site only**:
  - `site.pages(url, cursor=)` keeps the cursor in the `Job`, so there's no thread-local;
  - the early stops (5 older, 5 saved) live in the job only, so `caught_up`, `_hit_saved`, `older_streak` and `OLD_STREAK` leave tik, ins and twt;
  - `site.fetch`, `site.page`, `site.state`, `site.info/warn` and `site.back_off` stay.
- **loi search** (`loi?words`), from the API in e4fc627:
  - it calls `GET {API}user/loi/search/{quote(words)}/latest?page=N`;
  - it stops on an empty or repeated page;
  - each result is `loi:<id>`.

  It is words only for now; `author:`/`tag:` terms come later.
- `Source.browser` and `single_kinds` go.

### 3.3 The engine
| today | after |
| --- | --- |
| `Engine`, `dry.plan`, module functions | `Flnd(home)` |
| `RunShared`, `shared_browser`, `_BrowserPool`, `_SharedBrowser`, `_SharedCookies`, `_RawProxy`, `_Planned` | **`Run`**: one block's resources (stop trap, stores, one lazily opened `shared.browser`, a `Session` per source) |
| `SourceRun`, `source_run`, `_make_client`, account `TargetSite`, `_connected`, `_written_back`, 4 login checks | **`Session`**: one source; it calls login once, enters `connect`, owns one `http.Client(rate=, retry=)`, writes credentials back |
| `job.download` … `_steps`, `Resume`, `listing()` thread-local | **`Job`**: one target; it holds its cursors |
| 8 finder functions | `Run.find(target)`: one generator for download, preview and sync (seen set, at most 25 new a sync) |
| `Options`, `categories_by_source`, `_validate_from`, `wanted`, `selected` | `Options` (5 fields); `--from` checked once, at resolve |
| `TargetSite` + `_PacedPage` + tab stack + warm tabs | `Site` per Job; tabs, watch and warm move into `shared.browser` |
| `Ledger`, `Downloads`, `Board` | kept (they hold resume, dedupe and dating, and are well tested) |

```
Flnd.download ─┬─ resolve + check --from once
               └─ with Run(home) as run:
                     for target: session = run.session(source)        # login once, connect
                        ├─ finder? run.find(target) → Job per child
                        └─ Job(session, target, options).download()     # yields Saved
                              ├─ Site (fetch, goto, page, pages, warm)
                              ├─ listing → _kept (--time, streaks)
                              ├─ Ledger (history, dedupe, links, dates)
                              └─ Downloads (worker.Pool) → http.download → disk
```

| measure | today | after |
| --- | ---: | ---: |
| lines (engine + sources) | 11,084 | ~8,200 est. |
| author concepts | 102 | 90 |
| route behaviours | 5 | 3 |
| `@target` options | 4 | 1 |
| frames / classes per download | 15 / 16 | 6 / 9 |
| public `flnd` names | 185 | ~165 est. |
| tests (of 531) | — | ~6 deleted, ~95 rewritten, ~12 new |

**Behaviour kept, and its guards:**
- resume: test_job.py:1007-1049;
- sync early stop: test_job.py:453-576, test_sync.py:187;
- dedupe across categories: test_job.py:1188-1256;
- dates: test_job.py:1094-1157;
- login and write-back: test_engine.py:205-684;
- Ctrl-C: test_engine.py:343/367/920, test_job.py:757-896;
- async source: test_engine.py:401/424;
- finders: test_ins.py:802-839, test_mht.py:145-244;
- every source's output names: 221 tests.

**New tests:**
1. The job closes a newest-first listing before the next batch once 5 saved posts are seen, so there's no extra scroll.
2. One finder path gives the same children for download, preview and sync.
3. loi search pagination and stops.
4. A folder template error is raised at class creation.
5. One login call covers download, preview, sync and finders.
6. The browser runs on the job's thread, with refreshes through `Pool.call`.

**Risks:**
- Removing the source-side `_hit_saved` could add one scroll per stop. If a live run shows that, restore it once, as `site.enough`.
- Playwright must stay on one thread, so the browser change lands after `shared.browser`/`worker`.

### 3.4 The CLI
Three groups and two commands, few flags and good defaults; every other knob is a library option.

```
flnd download <targets…> [--from CATS] [--when WHEN] [--limit N] [--compress] [--dry]
flnd sync [targets…] [--compress]

flnd target ls [targets…]            # what you follow: files, size, last sync
flnd target forget <targets…>        # stop syncing a target; files stay

flnd auth login <source> [how]       # how: chrome | firefox | cookies.txt | qr; asked when left out
flnd auth logout <source>
flnd auth ls                         # each source's login state and expiring cookies

flnd media show  <targets|paths…> [--by post|day|month|year|all] [--style classic|fun|calm|story] [--music FILE] [--when WHEN]
flnd media card  <targets|paths…> [--by post|day|month] [--style strip|grid|polaroid|postcard] [--when WHEN]
flnd media album <targets|paths…> [--by month|year|all] [--when WHEN]

global: --ui plain|inline|full (default inline), --home DIR (default .flnd), -v (off by default)
```
- **Defaults** (user, 2026-11-16): the UI is `inline`, falling back to `plain` when stdout isn't a terminal; the home is `.flnd`; output isn't verbose unless `-v` is given.
- **`download` syncs by default.** Every downloaded target is followed, and `flnd sync` updates them all. `target forget` stops syncing a target; its files stay. `--sync`, `track`/`untrack` and `--again` are gone.
- **`--from` is optional.** The default is the target's own content (posts, reels, videos); `--from all` adds tagged posts, reposts and the rest.
- **`--when`** takes `2026`, `2026-06`, `2026-06-15`, `2026-01..2026-03`, or `30d` (the last 30 days). It replaces `--time SINCE,UNTIL`.
- **`--compress`** makes each saved file visually lossless and as small as it goes, as it lands (§4). `sync --compress` also compresses files saved before that aren't compressed yet.
- **Archives extract by themselves** (§4.3); there's no command for them.
- **`--by`** groups the output, so one command makes many: one slideshow, card or album per post, day, month, year, or one from everything. The defaults are `show --by post`, `card --by post` and `album --by year`.
- **`--style`** bundles the knobs.
  - On `show`: `classic` (3 s, fade), `fun` (random transitions, quicker), `calm` (slow cross-fades) and `story` (vertical 9:16).
  - On `card`: the layout.
- **Output** goes to `<home>/made/<target>/<command>/…`, named by group (`2026-06.mp4`, `post_C1abc.jpg`). A rerun skips any output whose inputs are unchanged.
- **Tools install themselves.** A missing ffmpeg, caesiumclt, avifenc, oxipng or 7zz is offered for download the first time it's needed.
- `download`, `sync` and the `media` commands iterate the library's events inside `ui.live`, so the library has no TUI code.

```bash
flnd auth login ins
flnd download ins:example_user --compress
flnd sync --compress
flnd target ls
flnd target forget twt:example_handle
flnd media show ins:example_user --when 2026
flnd media show ins:example_user --by month --style fun --music song.mp3
flnd media card ins:example_user --by day --style strip
flnd media album twt:example_handle
```

| today | after |
| --- | --- |
| `download --time A,B --sync --again` | `download --when …` (synced by default) |
| `target ls`, `sync-on`, `sync-off`, `del` | `target ls`, `target forget` |
| `auth login`, `logout`, `ls` | unchanged, with `login <source> [how]` |
| `--from` required on profiles | optional; own content by default |
| PLAN's `flnc` and `flns` binaries | `--compress`, automatic extraction, `flnd media show/card/album` |

### 3.5 Performance
- **History** moves to SQLite: a lookup per file instead of loading the whole JSON at start. Target: 50k-file history, start < 100 ms.
- **`_held`** checks the history row first and touches the disk only once for the candidate path; today it may stat twice.
- **Downloads**:
  - each source's files go through one `worker.Pool(jobs=Source.concurrency)`, paced by the client's token bucket;
  - big files use segmented downloads;
  - video and audio DASH parts download in parallel, then mux.
- **Browser**: one browser per profile for the run, and tabs are reused.

## 4. Compression and extraction, built into flnd

There is no flnc (user, 2026-11-16): "remove the compress, archive, extract command. It just be built in, so things like loi can extract the archive by itself, and all things back by --compress flag." So the downloader carries two steps after a file lands:

```
download → [extract, if an archive] → [compress, with --compress] → history row → Saved event
```

### 4.1 The library
```python
fl = flnd.Flnd(".flnd")
for saved in fl.download("loi:5930", compress=True):   # archives extract, media compress
    saved.path, saved.kind, saved.compressed            # compressed: Shrink(before, after, format) | None
fl.compress("ins:example_user")                         # files saved before; yields Saved too
```
- `compress=True` on `download` and `sync`, and `fl.compress(targets)` for files already saved, all go through one step, `flnd.shrink`.
- The engine's work is `shared.media.compress` (§2.14) plus verification and placement. flnd only decides when to run it and records the result.
- There's no `level`: the quality is always visually lossless (`media.compress(..., level="high")`). The library may pass `level="max"`, but the CLI never does.

### 4.2 Compression, per file
1. **Skip** files that aren't media, `.*.stage` files, files whose history row says `compressed` or `kept` with an unchanged stat, and files a tool can't improve (a video already AV1/HEVC under 0.08 bits per pixel is `kept`).
2. **Plan** from `media.probe`: candidates in their expected order of size (AVIF first for photos), from the table in §2.14.
3. **Encode** the first candidate into `fs.stage`, then **verify** it:
   - it decodes;
   - it has the same dimensions, frames and duration (±0.1 s);
   - images have SSIM ≥ 0.98;
   - it saves ≥ 5%.

   If it saves ≥ 40%, stop there; otherwise try the next candidate and keep the smallest that passes.
4. **Place**:
   1. `fs.touch` copies the original's mtime (flnd's post date).
   2. With the same suffix, the result replaces the original atomically.
   3. With a new suffix, the new file is written, the history row's path moves to it in the same step, and then the old file is removed. `sync` never downloads it again.

   A crash leaves either the old file or the new one, never neither.
5. **Record** on the file's history row: `compressed` (with before, after and format), `kept` (no candidate passed) or `failed` (with the tools string, so a tool upgrade retries it). It is batched with the download's own history write.

- **Concurrency**:
  - downloads keep their pool;
  - images compress on a `worker.Pool(jobs="cpu")` fed as files land;
  - videos compress one at a time (ffmpeg is threaded), with a progress row from `-progress pipe:1`.

  A slow encode never blocks the next download.
- **Targets**: a `sync --compress` over 100k already-compressed files adds < 3 s, since only stats are read. 1,000 JPEGs on 8 cores take < 2 min.

### 4.3 Extraction, automatic
- A source marks an archive with the password it knows: `File(url, password="…")`. loi gets the password from the post, so its `info.md` no longer has to carry it for a person to type.
- After the download, the engine extracts it with `archive.open(path, password=…).extract(...)` into a folder named after the archive, beside it. `archive.Limits` stay on, and a nested archive is extracted once.
- The archive itself:
  - is kept by default;
  - with `--compress`, is removed after a verified extract, and the extracted media are compressed instead, since the archive would only double the space.
- The history row of the archive records `extracted` and the folder, so a rerun doesn't extract again. A failed extract (a wrong password, a damaged archive) keeps the archive, warns, and records `failed`.
- `shared.archive.create` stays in `shared` for any script; flnd has no archive command.

### 4.4 Layout
- `flnd/shrink.py` (compress a file: plan, encode, verify, place) and `flnd/unpack.py` (extract a saved archive). Both are called by the job, and both are under 200 lines (est.).
- The history row gains `state` (`compressed | kept | failed | extracted`), `before`, `format` and `tools`.

## 5. Slideshows, cards and albums (`flnd media`)

flnd turns its media into things to watch, print or share: `fl.show`, `fl.card`, `fl.album` and `fl.plan` in the library, and `flnd media show|card|album` on the CLI (§3.4). There is no separate flns (user, 2026-11-16: "maybe also not keep flns, fold it into flnd"). The user, 2026-11-16: "I can generate slideshow with different animation from cli … like slideshow, create album, create a photobooth card".

Defaults adopted without asking (the user may reverse any): verb `slideshow`; a plain fade, 3 s, no motion; H.264; hardware encoding only with `--hw`; video clips in shows (≤ 4 s, sound ducked under music); no smart crop in the MVP; one bundled OFL font (Inter); PDF/JPG albums first; output in `<home>/made/<target>/<command>/`; modules inside flnd (§5.9).

### 5.1 Scope

| product | what it makes | output | phase |
| --- | --- | --- | --- |
| `slideshow` | photos and short clips with transitions, optional motion, title card, music | `.mp4` | MVP |
| `card` | photobooth strip (1×3, 1×4), 2×2 grid, polaroid, postcard, date captions | `.jpg`/`.png` at 300 dpi | MVP |
| `album` | justified-row pages with dates and a title page | `.pdf`, `.jpg` pages | MVP |
| `collage` | one grid or mosaic of N photos | `.jpg`/`.png` | MVP |
| motion | Ken Burns, zoom, pan (`zoompan`) | option of `slideshow` | later |
| `loop` | a looping animation | `.gif`, `.webp` | later |
| album HTML, smart/face crop, `reel` (scene cuts to music), `recap` (year in review) | | | later |

### 5.2 Tools
**ffmpeg + Pillow only.**
- **ffmpeg**: `xfade` (57 transitions), `zoompan`, the concat demuxer with stream copy, audio fades/mix, every encoder, `-progress pipe:1`.
- **Pillow**: stills, text, EXIF rotation, `draft()` downscale, PDF, WebP.

Homebrew's ffmpeg has no `drawtext` and no `libwebp`, so all text is drawn by Pillow into a transparent PNG and laid on with `overlay`, and WebP goes through Pillow. The transition list is read from the installed ffmpeg (`ffmpeg -h filter=xfade`), so an older build offers only what it has.

Optional later: libvips (`attention` smart crop, huge albums) and OpenCV YuNet (face crop).

Rejected:
- ImageMagick: Pillow covers it in-process.
- ffmpeg-gl-transition: needs a patched ffmpeg build.
- editly / ffmpeg-concat: need Node, and `headless-gl` is hard to build on Apple Silicon.
- MLT: a second video engine.
- dvd-slideshow: unmaintained.
- Remotion / Motion Canvas: need Node and a headless Chromium per render.

### 5.3 The library
```python
fl = flnd.Flnd(".flnd")

show = flnd.Show(transition="random", duration=3.0, fade=0.75, size="1080p")
r = fl.show(paths, "show.mp4", style=show, music="song.mp3")   # Result(path, duration, skipped)
fl.card(paths[:4], "strip.jpg", layout="strip-4", theme=flnd.themes.film)
fl.card(paths[:9], "grid.jpg", layout="grid-3x3")         # collages are card layouts
fl.card(paths[0], "polaroid.jpg", layout="polaroid", caption="{date:%d %b %Y}")
fl.album(paths, "2026.pdf", page="a4", title="2026")
fl.plan(paths, style=show)                                # slides, seconds, pieces cached

# composed with flnd
images = [f.path for f in fl.files.filter(target="ins:example_user", kind="image").sort("date").get()]
fl.show(images, "example_user.mp4")                         # skipped when unchanged
```
- Public: `slideshow`, `card`, `album`, `plan`, `Show`, `Theme`, `themes`, `Result`, `Done`. Four functions, down from a class with 9 members.
  - `collage` folds into `card`'s layouts: `strip-3/4`, `grid-4`, `polaroid`, `postcard`, `grid-NxM` and `mosaic`.
  - `photos`, `Photo` and `Page` go: selection is `fl.files`, and rotation comes from `media.probe`.
- `Show` is a frozen record:
  - `size` (1080p, 4k, square, story, `W×H`), `fps` 30;
  - `duration` 3 s, `fade` 0.75 s;
  - `transition` (an xfade name, `cut` or `random`), `fit` (FILL | FIT | BLUR, default BLUR);
  - `codec` (H264 | HEVC | AV1 | HW), `title`, `clip` 4 s;
  - `seed`, from the inputs.

  `motion` and `crop` come with their later features.
- `Theme` covers the background, frame, text, font and caption. The themes are `plain`, `film`, `polaroid`, `dark` and `pastel`.
- Errors: `flnd.MediaError`, under `shared.BrokenError`; a missing tool is offered for download.

### 5.4 The CLI
The commands are `flnd media show`, `flnd media card` and `flnd media album` (§3.4). Each takes `--by` to group photos (post, day, month, year, all) and `--style` to pick a preset, so one command makes many outputs.
- `show`'s styles map onto `Show`: `classic` (fade, 3 s), `fun` (random transitions, 2 s), `calm` (dissolve, 5 s, 1.5 s fades) and `story` (9:16, blur fit).
- `card`'s styles are its layouts.
- The groups come from flnd's history: a post's files, or the files saved on a day or in a month.
- Every other `Show` field is a library option.

### 5.5 Animations
**Transitions** (`xfade`, `duration=fade`):

| group | names | in `random` |
| --- | --- | --- |
| fades | fade, fadefast, fadeslow, fadeblack, fadewhite, fadegrays, dissolve | fade, fadeblack, dissolve |
| wipes | wipeleft, wiperight, wipeup, wipedown, wipetl, wipetr, wipebl, wipebr | wipeleft, wiperight |
| slides | slideleft, slideright, slideup, slidedown | all |
| smooth | smoothleft, smoothright, smoothup, smoothdown | all |
| cover, reveal | coverleft/right/up/down, revealleft/right/up/down | coverleft, revealright |
| shapes | circleopen, circleclose, circlecrop, rectcrop, radial, vertopen, vertclose, horzopen, horzclose | circleopen, radial, horzopen |
| diagonals | diagtl, diagtr, diagbl, diagbr | diagtl, diagbr |
| slices, wind | hlslice, hrslice, vuslice, vdslice, hlwind, hrwind, vuwind, vdwind | — |
| effects | distance, pixelize, hblur, squeezeh, squeezev, zoomin | zoomin |
| special | `cut`, and `random` (a seeded pick, never the same twice in a row) | — |

**Motion** (`zoompan`, later):
- `zoom-in` is `z=1+0.12*on/d`, centred; `zoom-out` is the reverse;
- `pan-*` holds `z=1.1` and moves `x` or `y` with `on/d`;
- `kenburns` alternates in and out toward the point of interest.

`zoompan` jitters because it rounds to whole pixels, so frames are prescaled to 2× before it. That costs about 4× the pixels, which is why motion is opt-in.

**Text and audio**:
- Title cards and captions are Pillow PNGs, laid on with `overlay` and `fade=t=in:alpha=1`.
- Music:
  - the track loops (`-stream_loop -1`) and is trimmed to the show (`atrim`);
  - `afade` in 1 s and out 2 s, then `loudnorm=I=-16`;
  - `acrossfade` between several tracks.
- Clips keep their sound, ducked with `sidechaincompress` and mixed with `amix`. Without music, an `anullsrc` silent track is added.

### 5.6 Pipeline and performance
`select → probe → normalize (Pool, cached) → pieces (Pool, cached) → concat (copy) → mux audio (copy video)`
1. **Normalize** each photo to one output-size frame with `media.fit`:
   - EXIF transpose, then `draft()` downscale;
   - fill, fit or blur-pad, then crop;
   - cached at `.flnd/cache/media/frames/<digest>.png`, keyed by (path, mtime, size, fit, crop), on `worker.Pool(jobs="cpu")`.
2. **Pieces, not one graph.** One graph holds N decoders open, can't resume, and gets slow past about 30 slides. Instead:
   - each slide is a **hold** piece (`duration − fade`);
   - each pair is a **transition** piece (`fade`, xfade of the tail and the head);
   - each piece is one small ffmpeg run on `worker.Pool(jobs=cpu//2)` with `-threads 2`, with identical encode parameters;
   - pieces are cached at `.flnd/cache/media/pieces/<digest>.mp4`.

   Adding one photo re-renders three pieces.
3. **Concat** with the concat demuxer and `-c copy`.
4. **Mux audio** with `-c:v copy` and `+faststart` into `fs.stage(dest)`. `fs.touch` sets the mtime to the newest input's date.

Encoders:

| encoder | when | settings |
| --- | --- | --- |
| `libx264` | the default | `-preset medium -crf 20 -pix_fmt yuv420p` |
| `h264_videotoolbox` / `hevc_videotoolbox` | `--hw` on macOS | `-q:v 65` (`-tag:v hvc1` for HEVC) |
| nvenc, qsv, vaapi | `--hw` elsewhere, when listed | quality 23 |
| `libsvtav1` | `--codec av1` | `-preset 8 -crf 32` |

- **Progress**: each piece writes `-progress pipe:1`, and `media.render` turns it into `row.advance(Δframes)`. The total frames are known up front, and cached pieces advance at once.
- **Stills**: a pure `layout()` returns boxes, then `draw()` pastes. Album pages render on the pool, and the PDF is written with `save_all=True`.
- **Cache**: `fs.sweep` keeps 30 days.
- **Targets**, on an 8-core Mac:
  - 100 photos at 1080p with fade, cold, in < 60 s with `--hw`;
  - a rerun with one photo added in < 5 s;
  - `flnd media --help` in < 150 ms.

### 5.7 `shared` additions, kept general
- **`media.render(*args, seconds=, progress=)`**: runs ffmpeg with `-progress`, feeding a sink. Its error carries the last stderr lines. `--compress`'s video path uses it too.
- **`media.concat(parts, dest)`**: the concat demuxer with stream copy, staged.
- **`media.fit(src, size, *, mode=, focus=)`** returns a `PIL.Image`.
- **`media.font(name=None)`** returns a `Path` to a system or bundled font.
- **`media.probe`** gains `rotation`, and `media.encoders` lists hardware encoders.

### 5.8 Store
```python
class Done(store.Row):            # a Memo row; key: dest relative to the home, or absolute
    table = "outputs"
    kind: str; inputs: str        # digest of [(path, mtime, size), …]
    options: str; tools: str      # the record's JSON; "ffmpeg 9.0.2; Pillow 11.3"
    count: int; seconds: float; size: int; mtime: int; at: float
```
- A rerun is skipped when `inputs`, `options` and `tools` match and `dest` still has its size and mtime. `--again` forces it.
- Frames and pieces need no rows: the digest is the file name.

### 5.9 Layout
- Inside flnd: `flnd/show.py` (slideshow pieces, concat, audio), `flnd/stills.py` (card and album layouts and drawing), `flnd/themes.py`, and the `media` group in `flnd/cli.py`.
- Outputs are recorded as history rows of kind `made`, keyed by the output path, with the inputs digest, options and tools, so a rerun skips unchanged outputs.
- Pillow joins flnd's dependencies, and goes into a `media` extra for `shared`.

## 6. Steps

Each step is one logical commit as `feiluvnana`, with `check` green, `api.txt` regenerated and its diff read, and a push after it. Mechanical work goes to a Haiku subagent: call-site renames, the snapshot, README edits.

1. `docs:` this PLAN.md; `plan/` removed; AGENTS.md and the docstrings that cite `plan/` point here.
2. `feat:` `shared.Query`; `test_names` gains rules 2–7.
3. `perf:` `store` on SQLite: `Db`, `Table`, `Row`, `rows`, `batch`.
4. `refactor:` / `perf:` one commit per module, in this order: `text`, `clock`, `fs`, `sh`, `worker`, `http`, `cookies`, `selector`, `browser`, `crypto`, `archive`, `prompt`, `cli`, `ui` (without live). Each carries its tests, flnd's call sites, its fake and the snapshot.
5. `feat:` `ui.live` with the three modes, prompts as modals, `Stop.paused`, `Pool.pause`.
6. flnd's sources and engine, simplified (§3), one commit each. A–G touch flnd only and may run alongside step 4:
   - A `refactor:` drop redundant route options (`from_`, route `folder`, mht `discovers`) and the dead `Source.browser` and `params` getattr;
   - B `refactor:` remove mht's author and genre routes, `job._run_discovered` and the cross-source child branch;
   - C `refactor:` check `--from` once, and folder templates at class creation;
   - D `refactor:` one login check and one finder path in the engine;
   - E `feat:` add the loi search route;
   - F `refactor:` keep the early-stop rules in the job only;
   - G `refactor:` allow literal finder patterns and drop `shown`;
   - H `refactor:` site on shared `http` and `browser`: one client per source; `_BrowserPool` and its proxies → one `shared.browser`; `page.watch`, `site.warm`. This comes after step 4;
   - I `refactor:` sources own login through `Auth`, and `auth.py` shrinks to the vault and helpers.
7. `refactor:` flnd's library as `Flnd`: `Run`, `Session`, `Job`, the cursor in `Job` (`site.pages`), `download` yielding `Saved`, `Report.of`, `move`, `Home`, `files`.
8. `perf:` flnd's history and targets on typed `store.Row`s, the `_held` fix, segmented downloads, the import-time test.
9. `feat:` flnd's new CLI (§3.4: `download`, `sync`, the `target`, `auth` and `media` groups, `--when`, sync by default, optional `--from`) on the library and `ui.live`; README.
10. `feat:` `shared.media` with pinned tool specs and self-install on first use, plus `store.Memo`, `shared.Report`, and the `--when`/`--by` parsers in `shared.cli`.
11. `feat:` `--compress` for images: `flnd/shrink.py`, history states, `fl.compress`, `download`/`sync --compress`.
12. `feat:` `--compress` for video and audio.
13. `feat:` automatic extraction: `File.password`, `flnd/unpack.py`, loi passing its password, the archive removed under `--compress`.
14. `docs:` README and AGENTS.md for the new CLI, the UI modes and the API rules.
15. `feat:` `media.render`, `media.concat`, `media.fit`, `media.font`, probe rotation.
16. `feat:` `fl.show` (pieces, transitions, cache, concat, audio) and `fl.plan`; `flnd media show` with `--by` and `--style`.
17. `feat:` `fl.card` (layouts, themes, collages); `flnd media card`.
18. `feat:` `fl.album` PDF; `flnd media album`.
19. `docs:` README for `flnd media`. Later, one commit each: motion, crop, clip ducking, `loop`, album HTML, `reel`, `recap`.

## 7. Verification

- `uv run --no-project scripts/dev.py check` at every commit: lint, pyright 0/0, unittest and the counters.
- `scripts/dev.py bench` before and after each `perf:` commit, run offline: store, walk, worker, ui frames, `probe` cache and the `sync --compress` rerun.
- The tests use the fakes (`sh.fake`, `http.fake`, `clock.Fake`, `prompt.fake`), so there is no network.
- Textual's `run_test()` Pilot covers the keys and the `m` switch in `INLINE` and `FULL`; the `PLAIN` snapshot tests stay.
- A test checks that `Flnd.move` makes `_held` find a renamed file, so it isn't downloaded again.
- `download(compress=True)` on fakes: each file is compressed once, a format change moves the history row with it, and an archive with a password is extracted once.
- By hand, on the authorized test accounts:
  - `uv run flnd download <target> --limit 5` with `--ui inline` and `--ui full`: press `p`, `v`, `f`, `m`, then `q` twice, and check that the terminal is restored. Pipe the same command to a file to check that it falls back to `plain`.
  - On a copy of `.flnd`, run `flnd sync --compress`, then again. The rerun must add < 3 s. Check that mtimes are kept, and that `flnd sync` doesn't download the `.avif`/`.webp` files again.
  - `flnd download loi:<id> --compress` on the authorized account: the archive is extracted with its password and removed, and its photos are compressed.
  - `flnd media show`: render a 20-photo show with `--transition random --music`, add one photo and rerun (only three pieces re-render), and make a `strip-4` card at 300 dpi.
- The media tests fake ffmpeg with `sh.fake` and check filter strings and layout boxes; nothing is rendered for real.
