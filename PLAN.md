# PLAN: the revamp

## 0. Why

The user, 2026-11-16:
- **`shared`** is a toolkit useful to every script: a minimal, elegant API, fast, with good DX.
- **flnd** is a TUI app with three UI modes (plain, inline Textual, full-screen Textual), not log lines.
- **flnc** is a new tool for flnd's output:
  - it compresses images, video and audio as far as it can, changing format (WebP, AVIF) when that is smaller;
  - it archives and extracts with 7zz;
  - it keeps a store of what it has done.
- **flnd and flnc are libraries first**; each CLI is a thin layer.
- **Performance, simplicity and DX** are first-class: code is concise, short and intuitive, and annotations and decorators carry intent where that reads better than options.

Decisions (user, 2026-11-16):
- Textual for the TUI.
- flnc replaces files in place after verifying them.
- The quality is visually lossless with a `level`, and format change is allowed.
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
   nc.files.filter(state="kept").sort("saved", desc=True).limit(20).get()
   nc.files.count
   ```
4. **An action is a verb**: `download`, `sync`, `track`, `forget`, `compress`, `archive`, `extract`, `login`.
5. **One spelling.**
   - There are no aliases and no `try_x` twins.
   - No bool switches the return type. A soft failure is `check=False`, which returns the result with its status (`sh.run`, `http.fetch`).
6. **Options are keyword-only**, at most 4 of them. Past that, split the call into two verbs, use a record, or use a decorator.
7. **Progress goes to a sink, never a bare callback.** A sink is any object with `total` and `advance(n)`; `ui.Row` is one. Pools and transfers also expose `done`/`total` as properties.
8. **Module-level functions are verbs or pure computations** (`text.size(n)`, `clock.now()`). A module can't have plain properties, so rule 2 applies to objects; this is the one exception.
9. **Errors.** Each module has one `<module>.Error`, under `shared.UnsafeError`, `BrokenError`, `RefusedError` or `MissingError`. A message names the thing and what to do: `"7zz: not found; run flnc tools --install"`.
10. **Typed.** Generics flow through: `Query[T]`, `Task[R]`, `Table[T]`, `Result[R]`. Pyright reports 0 errors and 0 warnings, and `reveal_type` tests sit in `tests/typing/`.

### 1.2 Simplicity
- A module reads top to bottom: docstring, imports, constants, entry point, then helpers in call order.
- A docstring is one sentence. The example sits only in the module docstring and is the one in this plan.
- No wrapper class where a function does. No `raw` escape hatch unless a caller needs the library underneath; only `http.Client.raw`, `browser.Page.raw` and `ui.raw` keep it.
- Records are frozen `msgspec.Struct`s: fast, typed, and they encode to JSON.

### 1.3 Performance
- **Import is cheap.** `import shared` loads nothing heavy, and every module is lazy. `flnd --help` and `flnc --help` take < 150 ms, which a test checks with `-X importtime` over a list of heavy modules.
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
- **Change**: new; every collection read in `shared`, flnd and flnc returns one.

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
- **Performance**: `find` caches its result for the life of the process. `version` is cached on disk, keyed by the binary's path and mtime, so flnc doesn't spawn every tool at each start.
- **Change**:
  - `run(cmd: list)` → `run(*args)`.
  - `tool(name, platforms=)` → `find(name, specs=)`.
  - `Result.returncode/stdout/stderr` → `code/out/err`.
  - `capture=False` → `Tool.stream`.
  - New: `version`, `fake`.

### 2.6 `store`: SQLite, typed tables
```python
db = store.open(".flnc/flnc.db")
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
app = cli.App("flnc", help="Compress and archive flnd's output.")

@app.command
def compress(paths: list[Path] = [], *, to: To = To.AUTO, level: Level = "high", dry: bool = False):
    """Compress images, video and audio in place.

    Args:
        to: The format to write; auto picks the smallest that passes.
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

### 3.1 The library
```python
import flnd

fl = flnd.Flnd(".flnd")
report = fl.download("ins:example_user", only=["reels"], time="2026-01-01,", limit=50)
report.ok, report.files, report.failed
fl.preview("twt:example_handle")             # list[Item]: what download would take, nothing saved
fl.track("ins:example_user"); fl.untrack("ins:example_user"); fl.forget("ins:example_user")
fl.sync()                                    # every tracked target
fl.targets.filter(source="ins", sync=True).get()
fl.files.filter(target="ins:example_user").sort("date").get()
fl.move(old, new)                            # keep history in step when a file is renamed
fl.login("ins", using="chrome:Default")      # or Path("cookies.txt"), or "qr"
fl.logout("ins"); fl.logins.get()
with fl.connect("ins") as site:              # a Site for scripting
    site.fetch("https://www.instagram.com/api/...").json
flnd.resolve("https://x.com/example_handle") # Target
```

| before | after |
| --- | --- |
| `Engine` and the module-level `download`/`open` | `Flnd(home)` |
| `download(..., dry=, sync=)` | `download`, `preview`, `track` |
| `target_ls`/`target_sync`/`target_del` | `targets`, `track`/`untrack`, `forget` |
| `auth_ls`, `logins`, `cookie_logins` | `logins` (`Query[Login]`) |
| `login(code, method, browser=, profile=, path=)` | `login(code, using=)` |
| `TargetInfo`, `AuthInfo`, `TargetReport` | `Target`, `Login`, `Result` |
| `Site.try_fetch` | `Site.fetch(check=False)` |
| `Post.filed(category)` | `Post.replace(category=)` |

### 3.2 Writing a source
```python
class Example(Source):
    """example.com: a user's posts and avatar."""
    key, folder = "exm", "{user}"

    @target("https://example.com/{user}", "exm:{user}")
    def profile(self, site: Site, user: str) -> dict:
        return site.fetch(f"https://example.com/api/{user}").json

    @profile.category(newest_first=True)
    def posts(self, site: Site, profile: dict) -> Iterator[Post]:
        for page in site.pages(f"/api/{profile['id']}/posts", cursor="next"):
            for p in page["items"]:
                yield Post(p["id"], [File(p["url"])], date=p["time"])
```
- `site.pages(url, cursor=)` replaces `pages(fetch, cursor)`; the cursor is a key or a function.
- `site.fetch`, `site.page`, `site.state`, `site.secrets`, `site.info/warn` and `site.back_off` stay.

### 3.3 The CLI
- `flnd download <targets…> [--from] [--time] [--limit] [--dry] [--sync] [--again]`
- `flnd sync`, `flnd targets [--source] [--synced]`, `flnd track|untrack|forget <targets…>`
- `flnd login <src> [--using]`, `flnd logout <src…>`, `flnd logins`
- Global: `--ui`, `-v`, `--home`.

`download`/`sync` run inside `ui.live`. The engine's `Board` writes `ui` rows only, so the library has no TUI code.

### 3.4 Performance
- **History** moves to SQLite: a lookup per file instead of loading the whole JSON at start. Target: 50k-file history, start < 100 ms.
- **`_held`** checks the history row first and touches the disk only once for the candidate path; today it may stat twice.
- **Downloads**:
  - each source's files go through one `worker.Pool(jobs=Source.concurrency)`, paced by the client's token bucket;
  - big files use segmented downloads;
  - video and audio DASH parts download in parallel, then mux.
- **Browser**: one browser per profile for the run, and tabs are reused.

## 4. flnc

### 4.1 The library
```python
import flnc

nc = flnc.Flnc(".flnd")                         # the store: .flnd/metadata/flnc.db
report = nc.compress(".flnd/media/ins", to="auto", level="high", jobs="cpu")
report.saved, report.files, report.failed
nc.preview(".flnd/media/ins")                   # list[Plan]: file, candidates, expected action
nc.archive("photos/", dest="photos.7z", password="pw", split="2g")   # list[Path]
nc.extract("a.zip", "b.7z", passwords=["pw"], remove=True)          # list[Path]
nc.files.filter(state="failed").get()
nc.saved                                        # bytes saved, all time
nc.forget(".flnd/media/ins/example_user")       # process again next time
nc.tools                                        # Query[Tool]: name, path, version, missing
nc.install("caesiumclt", "avifenc", "oxipng", "7zz")
```

### 4.2 The CLI
- `flnc compress [paths…] [--to keep|auto|avif|webp|jpeg|png] [--level high|max] [--jobs N] [--dry] [--again]`; the default path is `<home>/media`.
- `flnc archive <paths…> [-o] [--password] [--split] [--level]`
- `flnc extract <archives…> [--into] [--password…] [--rm]`
- `flnc files [--state] [--under]`, `flnc forget <paths…>`, `flnc tools [--install]`
- Global: `--ui`, `-v`, `--home`.

### 4.3 The store
```python
class File(store.Row):
    table = "files"
    state: State            # compressed | kept | failed
    src_size: int; src_format: str
    size: int; mtime: int; format: str
    level: str; tools: str  # e.g. "avifenc 1.1.1"
    at: float
```
- The key is the path relative to the home, or absolute outside it.
- What happens to a file depends on its row:
  - a file whose stat matches its row is skipped without being read;
  - `kept` isn't tried again;
  - `failed` is tried again with `--again`, or when the `tools` string changes;
  - a higher level reprocesses the file.
- An extract is recorded in its own table, by the archive's path and stat.

### 4.4 The pipeline, per file
1. **Scan** with `fs.walk`. Skip `.*.part` files, files changed in the last 10 minutes, and rows whose stat matches.
2. **Plan** from `media.probe`: list the candidates in their expected order of size (AVIF first for photos).
3. **Encode** the first candidate into `fs.stage`, then verify it:
   - it decodes;
   - it has the same dimensions, frames and duration (±0.1 s);
   - images have SSIM ≥ 0.98 at `high` or ≥ 0.97 at `max`;
   - it saves ≥ 5%.

   If it saves ≥ 40%, stop there; otherwise try the next candidate and keep the smallest that passes.
4. **Place**:
   1. `fs.touch` copies the original's mtime, since flnd dates files by post.
   2. With the same suffix, the result replaces the original atomically.
   3. With a new suffix, the new file is written, then `Flnd.move(old, new)` runs so `flnd sync` doesn't download it again, then the old file is removed.

   A crash leaves either the old file or the new one, never neither.
5. **Record** the row, in a `db.batch()` every 100 files.

### 4.5 Performance
- **Concurrency**: images run on a `worker.Pool(jobs="cpu")`. Video runs one at a time, since ffmpeg is already threaded, with its progress row fed from `-progress pipe:1`.
- **Fewer encodes**: an early stop at ≥ 40% saved, and candidates ordered by expected win, mean about 1.3 encodes per image instead of 3.
- **SSIM** runs on images downscaled to ≤ 1024 px.
- **Skipping**: a video already AV1/HEVC under 0.08 bits per pixel is `kept` without an encode.
- **Targets**:
  - a rerun over 100k processed files finishes in < 3 s (stat only);
  - 1,000 JPEGs on 8 cores finish in < 2 min.

### 4.6 Layout
- A top-level `flnc/`: `__init__` (`Flnc`), `cli`, `compress`, `archive`, `files`.
- `flnc = "flnc.cli:app"` in `pyproject.toml`.
- It imports `shared`, the stdlib and flnd's public API only, which `test_layers` checks.

## 5. Steps

Each step is one logical commit as `feiluvnana`, with `check` green, `api.txt` regenerated and its diff read, and a push after it. Mechanical work goes to a Haiku subagent: call-site renames, the snapshot, README edits.

1. `docs:` this PLAN.md; `plan/` removed; AGENTS.md and the docstrings that cite `plan/` point here.
2. `feat:` `shared.Query`; `test_names` gains rules 2–7.
3. `perf:` `store` on SQLite: `Db`, `Table`, `Row`, `rows`, `batch`.
4. `refactor:` / `perf:` one commit per module, in this order: `text`, `clock`, `fs`, `sh`, `worker`, `http`, `cookies`, `selector`, `browser`, `crypto`, `archive`, `prompt`, `cli`, `ui` (without live). Each carries its tests, flnd's call sites, its fake and the snapshot.
5. `feat:` `ui.live` with the three modes, prompts as modals, `Stop.paused`, `Pool.pause`.
6. `refactor:` flnd's library as `Flnd`: records, `move`, `site.pages`, the source surface.
7. `perf:` flnd's history on SQLite, the `_held` fix, segmented downloads, the import-time test.
8. `feat:` flnd's CLI on the library and `ui.live`; README.
9. `feat:` `shared.media`, with pinned tool specs.
10. `feat:` flnc: library, store, image pipeline, CLI.
11. `feat:` flnc video and audio.
12. `feat:` flnc archive and extract.
13. `docs:` README and AGENTS.md for flnc, the UI modes and the API rules.

## 6. Verification

- `uv run --no-project scripts/dev.py check` at every commit: lint, pyright 0/0, unittest and the counters.
- `scripts/dev.py bench` before and after each `perf:` commit, run offline: store, walk, worker, ui frames, `probe` cache and flnc rerun.
- The tests use the fakes (`sh.fake`, `http.fake`, `clock.Fake`, `prompt.fake`), so there is no network.
- Textual's `run_test()` Pilot covers the keys and the `m` switch in `INLINE` and `FULL`; the `PLAIN` snapshot tests stay.
- A test checks that `Flnd.move` makes `_held` find a renamed file, so it isn't downloaded again.
- By hand, on the authorized test accounts:
  - `uv run flnd download <target> --limit 5` with `--ui inline` and `--ui full`: press `p`, `v`, `f`, `m`, then `q` twice, and check that the terminal is restored. Pipe the same command to a file to check that it falls back to `plain`.
  - On a copy of `.flnd/media`, run `flnc compress --dry`, then for real, then again. The rerun must skip everything in < 3 s. Check that mtimes are kept, and that `flnd sync` doesn't download the `.avif`/`.webp` files again.
  - `flnc archive`/`extract` round trip, with a password, a split and a sibling `info.md`.
