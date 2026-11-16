# PLAN: flnd, one fast package

The user's brief, 2026-11-16, gathered from one session:
- **One package, flnd.** It is a library plus a thin CLI. There is no `shared` toolkit, and anything flnd needs lives inside flnd.
- **Maximum performance, measured first.** Native-backed libraries are welcome. Cython, mypyc and Nuitka get evaluated. Python stays the language.
- **A nice, easy API**, above all the source API. Sources can also be added at runtime, as plugins.
- **Robust error handling** end to end.
- **Every good feature**, ranked, with an MVP cut.
- **Built in:** compression, archive extraction, and slideshows, cards and albums.
- **The Makefile instead of `scripts/`.**
- **Type hints for readers, not a checking gate.**
- **Extensive comments and docs for human readers** (user, 2026-11-16: "the codebase should have extensive comment and docs for human to read").
- **Everything is open.** The old API, CLI and data format don't constrain this design.

This is the plan for the rebuild. Its steps are the commits of section 15, but nothing is built until the user approves it; section 16 lists the decisions to confirm first.

## Contents
1. Summary
2. Measured today
3. Where the time goes
4. Architecture: one package
5. Performance design, area by area
6. The library API
7. The source API and plugins
8. Errors
9. The CLI
10. Storage and the home folder
11. Compression, extraction and media
12. Features, ranked
13. Dependencies, native code and compilation
14. Tooling: the Makefile, tests, benchmarks, AGENTS.md
15. Migration: ranked commits with gates
16. Open questions, with recommended defaults

## 1. Summary
**The bottlenecks.** I measured every layer on this machine (section 2). Python's CPU speed is not what makes flnd slow:
- the engine's own work is about **0.5 ms per file**, or 2,000 files a second;
- the real costs are **waiting**:
  - the browser's scroll loop takes **26 s** for a feed that delivers its data in about 2 s;
  - downloads run 4 at a time where 16 finish **2.5× sooner**;
  - startup takes **173 ms**, of which **150 ms** is eager imports;
  - per-file disk bookkeeping costs **276 µs**, where a plain write and rename costs 113 µs;
  - a pure-Python HTTP stack moves small files **5× slower** than libcurl.

**The design in brief:**
- **One package, three layers:**
  - `flnd`, the public library: `Flnd`, events, errors;
  - `flnd.source`, the plugin API: `Source`, `Site`, `Post`, `File`;
  - private internals under `flnd/_*`.
  The CLI (`flnd/cli.py`) imports only the public library.
- **Network:** libcurl through `curl_cffi`, on one asyncio loop with HTTP/2. It sends Chrome's TLS fingerprint, so sites that refuse httpx stop needing the in-page fetch fallback. Concurrency is adaptive per host.
- **Browser:** async Playwright on the same loop, with no thread hop per call. Scrolling is driven by events and ends on the site's own "no more" signal, not by waiting out 10 s of idle.
- **Disk:**
  - writes go straight to `<dest>.part`, then a rename;
  - one home lock replaces a lock file per file;
  - history writes go through a group-committing writer;
  - one `flnd.db` holds real columns and indexes.
- **Startup:** argparse, and commands load only what they use. Built-in sources come from a static manifest. `flnd --help` drops from 173 ms to about 25 ms.
- **CPU:** msgspec for JSON, selectolax for HTML, and SHA-256 computed while streaming (hardware-accelerated at 3.3 GiB/s) for dedupe.
- **Rejected, with numbers:**
  - Cython and mypyc save 3–17% on a 13 µs per-file path;
  - a Rust extension has no hot path left to speed up;
  - free-threaded 3.14t runs CPU work 3.6× faster, but httpx crashes on it. It comes later.
- **Plugins:** a Python file in `<home>/sources/`, an installed package (the `flnd.sources` entry point), or `fl.add(MySource)`. Each is validated at load time, versioned (`api = 1`) and isolated. Authors get a test kit, `flnd.testing`.
- **Errors:** one hierarchy. Every failure is classified as retryable, permanent, login, or fatal for the run. A failure report says what failed, why, and how to retry; Ctrl-C has three stages. A user error never prints a traceback.
- **Docs for humans:** guides in `docs/`, full docstrings on every module and function, and comments that explain why and how wherever the code isn't obvious. A test keeps them in step with the code (section 14.6).
- **Size:** the 6,366 lines of `shared` and the 11,124 lines of flnd become about 12,000 lines in one package (estimated, section 4.4). The wrappers, generic `Query`, `Sink` and Protocols written for pyright, and the error trees and fakes that existed only to make `shared` stand alone, all go.

**Expected effect on a real run.** Take a 1,000-post Instagram profile, 2,000 files, and a CDN latency of about 100 ms:
- **Today:** about 60–70 s. Listing takes about 34 s (40 scroll steps at 0.6 s, then 10 s of idle), and the downloads take about 25 s at 8 at a time.
- **Proposed:** about 15–20 s. Listing paced by the site takes about 15 s, and adaptive downloads at up to 32 per host take about 6 s, overlapped with the listing.

This is a model built from the measured parts, not a measurement; commit 15.7 sets a benchmark gate for it.

## 2. Measured today
**Machine:**
- Apple M5 (10 cores), 32 GB of memory, an APFS SSD;
- macOS 26.2;
- CPython 3.14.6 from uv;
- the code at `01cfdad`, the head of main when this was written.

**Method:**
- Each number is the best of 3 to 5 runs, or the median of 15 for process start times. Every number in a table was measured; estimates are marked "est." in the text.
- Network tests run on loopback, against an asyncio server that answers with `sendfile` and supports keep-alive and `Range`. Loopback shows CPU and per-request overhead; it hides latency and bandwidth, so latency is simulated where it matters.
- The scripts live in the session's scratchpad (`bench/*.py`). Section 14.4 moves them into `tests/bench/`.

### 2.1 Startup
| what | median |
| --- | ---: |
| `python -c pass` | 12.4 ms |
| `import shared` (lazy) | 21.5 ms |
| `import flnd` / `import flnd.source` / `import flnd.cli` | 148 / 152 / 151 ms |
| `flnd --help` | 173 ms (196 ms in a second session) |
| argparse with 2 commands, `--help` | 22.4 ms |
| cyclopts with 2 commands, `--help` (it renders through Rich) | 82.6 ms |

The cumulative import times under `import flnd.cli` (`-X importtime`):
- `flnd.engine` takes 117.7 ms, pulled in by `flnd/__init__`;
- questionary and prompt_toolkit take 29 ms, through `flnd.options` and then `shared._prompt`;
- httpx takes 19 ms;
- the seven sources take 16 ms, with fbk alone at 13 ms through parsel/lxml;
- cyclopts takes 15 ms;
- flnd's own `job`, `targets` and `site` take about 40 ms of their own.

Import cost of each candidate, including the interpreter's 12 ms: argparse 16 ms, msgspec 24, pycurl 24, selectolax 25, httpx 42, parsel 43, curl_cffi 57, playwright 57, questionary 60, niquests 79.

### 2.2 HTTP on loopback
| client | 1 GiB to disk | 2,000 × 100 KiB, 1 thread | … 8 threads | … one event loop |
| --- | ---: | ---: | ---: | ---: |
| httpx 0.28, raw stream | 389 ms (2.63 GiB/s) | 606 ms | 658 ms | — |
| `shared.http.download` (flnd today) | 538 ms (1.90 GiB/s) | 1,131 ms | 1,009 ms | — |
| niquests 3.21 | 276 ms (3.72 GiB/s) | 697 ms | 544 ms | — |
| curl_cffi 0.17, `content_callback` | 226 ms (4.53 GiB/s) | 447 ms | 520 ms | 249–270 ms (`AsyncSession`, 8 to 32 at once) |
| pycurl, an easy handle per thread | 224 ms (4.57 GiB/s) | 357 ms | 180 ms | — |
| pycurl `CurlMulti`, one thread | 220 ms (4.65 GiB/s) | — | — | **127 ms** |
| curl CLI (for scale) | 405 ms, process start included | | | |

Notes:
- `shared.http.download` splits a file of 64 MiB or more into 4 parallel ranges. On loopback that costs 26% (1.95 against 2.63 GiB/s); its value is against servers that throttle each connection.
- **Per-file overhead.** `download` costs about 0.55 ms per file against 0.30 ms for a raw stream. That is about 2,800 Python function calls per file, plus a lock file opened and unlinked, 5 `stat` calls and 8 `lstat` calls.
- **httpx doesn't scale with threads** (606 ms on 1 thread, 658 ms on 8). libcurl releases the GIL, so pycurl scales 2× with threads and 5× on a single multi handle.
- **curl_cffi doesn't scale with threads either**, even with a Session per thread (436–514 ms on 1 to 16 threads). Its asyncio session does scale, to 2.4× httpx.
- **flnd's own client is HTTP/1.1.** `flnd/common/net.py:client` builds a plain `httpx.Client` without `http2=True` and swaps it into `http.Client` through private attributes (`site.py:111-113`), so the `http2=True` of `shared.http`'s default client never applies to flnd's traffic.

### 2.3 Disk
5,000 files of 100 KiB each, per file:

| how | µs per file |
| --- | ---: |
| `open` + `write` | 62.8 |
| write to a temp file, then `os.replace` | 112.9 |
| `fs.write` (stage + lock file + replace) | 276.2 |
| `fs.stage` + write | 266.8 |
| temp file + `os.link` + `unlink` | 289.5 |
| `fs.write(replace=False)` (stage + lock + link) | 444.4 |
| `fs.write(sync=True)` | 289.5 |

Two things stand out:
- the per-file lock costs **160 µs**, more than the write itself;
- `fsync` on macOS costs almost nothing because it doesn't reach the platter. Durability there needs `fcntl(F_FULLFSYNC)`.

### 2.4 Store and file walks
Over a table of 100k rows:

| operation | today | raw sqlite3 + msgspec |
| --- | ---: | ---: |
| 100k inserts in one transaction | 0.86 µs/row | 0.67 µs/row |
| point read | 2.4–2.6 µs | 2.05 µs (1.12 µs batched `IN (…)`) |
| `key in table` | 2.08 µs | — |
| a prefix scan of 10k rows | 4.0 ms | — |
| `filter(size=5)` pushed down (`json_extract` over every row) | 15.7 ms | an indexed column: µs (est.) |
| `redate`'s rows for one source of 7 (after `b3c859d`) | 6 ms, down from 62 ms | |

Walking 100k files: `fs.walk` 38 ms, `os.scandir` 32 ms, `os.walk` 33 ms. `Path.rglob` with `is_file()`, which `engine._folder_stats` uses, takes 287 ms. `fs.walk` with `entry.stat()` takes 215 ms, and `os.stat` on a known path costs 1.44 µs.

### 2.5 CPU
| work | time |
| --- | ---: |
| `worker.Pool(8)`, a no-op task | 3.8 µs (ThreadPoolExecutor: 2.7 µs) |
| `ui` row `advance` | 0.07 µs plain, 0.51 µs with Rich live |
| decode a 0.85 MiB feed page | `json` 2.4 ms · msgspec 1.2 ms · orjson 1.05 ms · msgspec typed 0.43 ms |
| encode the same page | `json` 2.1 ms · msgspec 0.34 ms · orjson 0.19 ms |
| `selector.parse(obj).jmespath("…edges[].node")` | 649 µs, against 19 µs of plain dict access |
| parse a 2 MiB Meta-style page and read its JSON `<script>` blocks | parsel 18.1 ms · selectolax (lexbor) 6.9 ms |
| the same page, 20k `img[src]` | parsel 33.0 ms · selectolax 9.9 ms |
| hash, per MiB | sha256 302 µs (3.3 GiB/s) · sha1 301 · blake2b 691 · md5 1,092 · crc32 23 · blake3 408 on 1 thread and 58 on all · xxh3_128 52 |
| `job.place` (templates, names, dates) | 13.2 µs per file |
| `text.filename` | 4.3 µs |
| `day()` | 0.55 µs |

### 2.6 A whole run on a faked site
This is the real engine, the real clock and the source's real pacing. A `MockTransport` serves pages of 50 posts with 2 files each, every file 100 KiB, with simulated latency.

| run | wall time |
| --- | ---: |
| 2,000 posts, no latency, no pace | 2.01 s, or **0.50 ms per file** |
| 2,000 posts, no latency, pace 0.5 s (the default) | 19.6 s: the pace alone |
| 500 posts, 150 ms per page, 40 ms per file, 4 jobs | 12.7 s |
| … 8 jobs | 7.0 s |
| … 16 jobs | **5.0 s**, which is the pacing floor of 10 pages × 0.5 s |
| a sync with nothing new | 0.16 s |

What this shows:
- listing already overlaps downloading;
- the limits are download concurrency first, then the pace;
- engine overhead is not a limit.

### 2.7 Browser
Headless Chromium through `shared.browser`, on a synthetic infinite-scroll page that loads 30 batches of 10 items, each batch arriving in 50 ms:

| step | time |
| --- | ---: |
| launch | 682 ms |
| new tab and load | 92 ms |
| one `evaluate` round trip | 0.43 ms |
| `page.document` (HTML into a Selection) | 1.58 ms |
| in-page `fetch` | 1.06 ms (`http.fetch`: 0.83 ms) |
| **`scroll()` to the end, `idle=10` (the default)** | **26.0 s** for about 2 s of data |
| `scroll()`, `idle=2` | 19.2 s |
| close | 1.05 s |

Each scroll step costs about 0.6 s whether data is arriving or not:
- at the bottom, the scroller waits up to `SCROLL_WAIT` (3 s) for progress;
- then it pauses `SCROLL_PAUSE` (0.3 s), and jiggles if nothing moved (another 0.3 s);
- the end of a feed then costs the full `idle` window.

### 2.8 Media
The input is a synthetic 10 s 1080p30 clip (`testsrc2`), so the sizes are not representative; treat the speeds as relative.

| encode | time |
| --- | ---: |
| x264 medium, CRF 23 | 1.44 s |
| x264 veryfast, CRF 23 | 0.67 s |
| x265 medium, CRF 28 | 3.73 s |
| h264_videotoolbox, `q:v 65` | 1.22 s, 33% larger than x264 medium |
| hevc_videotoolbox, `q:v 65` | 1.31 s |
| SVT-AV1 preset 8, CRF 35 | 1.56 s |
| a 12 MP JPEG to AVIF (SVT) / to JPEG q5 | 0.30 s / 0.06 s |

Homebrew's ffmpeg has no `libwebp`. One `ffprobe` spawn costs 15.6 ms.

### 2.9 Compiled Python and free-threading
| | today | Cython 3.3 (no annotations used) | mypyc |
| --- | ---: | ---: | ---: |
| `job.place`, per file | 13.2 µs | 10.6–10.9 µs (−17%) | — |
| `text.filename` | 4.34 µs | 4.10 µs | 4.21 µs (−3%) |
| `day()` | 0.55 µs | 0.52 µs | — |

On free-threaded 3.14t:
- pure-Python CPU work on 8 threads runs **3.6× faster** (313 ms against 88 ms; on the GIL build, 324 ms against 310 ms);
- **httpx crashes** with 4 or more threads, a race in httpcore's pool (`TypeError: '>' not supported between instances of 'float' and 'NoneType'` in `has_expired`). It reproduced 3 times out of 3.

## 3. Where the time goes
| area | cost today | why | fix (section) | gain |
| --- | --- | --- | --- | --- |
| browser feeds | 0.6 s per scroll step + 10 s idle per tab | fixed sleeps; the end detected by silence | event-driven scroll ended by the site's cursor (5.4) | 26 s → ≤ 3 s on the synthetic feed (gate) |
| download concurrency | 4–8 per source | a fixed `Source.concurrency` | adaptive per host, up to 32 per CDN host (5.1) | 12.7 s → ~5 s at 40 ms latency (measured with 16 jobs) |
| pacing | 0.5 s per API request | conservative defaults | per-source pace, adaptive on 429 and blocks (5.1) | site-bound; no change in the worst case |
| startup | 173 ms | eager imports | argparse, lazy commands, a source manifest (5.6) | → ~25 ms (gate ≤ 40 ms) |
| HTTP stack | 0.55 ms per file and GIL-bound | pure-Python httpx | curl_cffi on the loop (5.1) | 2.4× today, 4.5× the 1 GiB rate of today's `shared.http` |
| disk bookkeeping | 276 µs per file | a lock file per write, a stage wrapper | `.part` + rename under one home lock (5.2) | → ~115 µs |
| the in-page fetch fallback (ins) | a browser round trip per API call, serialized through the tab | Instagram refuses httpx's TLS fingerprint | a Chrome fingerprint via curl_cffi (5.1) | API calls leave the tab (est.) |
| HTML parsing | 18 ms per Meta page; 10 ms of import | parsel/lxml | selectolax (5.3) | 2.6× |
| JSON paths | 649 µs per query | jmespath over dicts | plain access plus a tiny `dig` (5.3) | 30× (small in absolute terms) |
| compiled Python | 13 µs per file | — | none (13.3) | ≤ 17% of 13 µs: not worth it |

## 4. Architecture: one package
### 4.1 Layout
```
flnd/
  __init__.py      the public library: Flnd, open, events, errors, Target, Saved, Report (lazy attributes)
  __main__.py      python -m flnd
  cli.py           the thin CLI: argparse, one function per command, imports only `flnd`
  source.py        the source API: Source, target, Site, Post, File, Login, errors a source raises
  testing.py       the test kit for source authors: FakeSite, collect, check
  errors.py        the hierarchy (section 8)
  sources/         built-in sources (fbk, ins, loi, mht, tik, tlg, twt) + _manifest.py
  _engine.py       Flnd's run: resolve, plan, jobs, events, the failure report
  _job.py          one target: listing, early stops, dedupe, placing, dating
  _download.py     the downloader: the queue, per-host concurrency, resume, verify, place
  _net.py          curl_cffi sessions: pace, retry, fingerprint, cookies (was shared/_http + flnd/common/net)
  _browser.py      async Playwright: launch, tabs, response capture, event-driven scroll (was shared/_browser + site.py proxies)
  _cookies.py      reading cookies from Chrome, Firefox and Safari, cookies.txt
  _vault.py        encrypted secrets (AES-GCM) and the keychain key (was shared/_crypto + common/auth)
  _db.py           flnd.db: schema, a group-commit writer, queries (was shared/_store + paths.table + targets)
  _files.py        .part/rename, the home lock, sweep, walk, mtime (was shared/_fs)
  _loop.py         the run's asyncio loop thread and the sync bridge (was shared/_worker's Loop)
  _html.py         selectolax helpers + `dig` for JSON paths (was shared/_selector)
  _tools.py        finding, downloading and verifying ffmpeg, 7zz and others (was shared/_sh)
  _shrink.py       --compress: plan, encode, verify, place
  _unpack.py       automatic archive extraction (was shared/_archive)
  _media.py        probe, render with progress, concat, fit
  _show.py _stills.py _themes.py   slideshows, cards and albums
  _ui.py           plain and inline progress (Rich, lazy), prompts (input/getpass)
  _text.py _time.py                file names, templates, days, sizes; the clock and its fake
  _plugins.py      discovery, validation, versioning and isolation of external sources
docs/              guides for people: architecture, library, CLI, writing a source, errors, storage, performance, one page per site (14.6)
examples/          a runnable script and a 40-line plugin with its test
```
A leading `_` marks a private module. Only `flnd`, `flnd.source` and `flnd.testing` are public: the API snapshot lists those three, and nothing else is promised.

### 4.2 Public library, internals, CLI
- **`import flnd`** gives a script `Flnd`, `open`, `Target`, `Saved`, `Failed`, `Skipped`, `Report`, `Login`, `File` (a saved file's record) and the errors. The heavy parts load lazily: `__getattr__` on the package imports `_engine` on first use.
- **`flnd.source`** is what a plugin imports (section 7).
- **`flnd.testing`** is what a plugin's tests import.
- **The CLI** imports `flnd` only. It turns flags into calls and events into progress lines, which `tests/test_layers.py` checks. A command needs no internals, so whatever the CLI can do, a script can do too.

### 4.3 What goes now that nothing must stay general
| removed | why it existed | replacement |
| --- | --- | --- |
| `shared/__init__` and its 16 namespaces, `_bundler` | a toolkit for future tools | internal modules |
| `Query`, `Rows`, `Plan`, `Sink` | generic reads and progress for any script | keyword filters on the few reads flnd has (`fl.files(target=…, kind=…)`); progress is the event stream |
| the error trees of 16 modules × `UnsafeError`/`BrokenError`/`RefusedError`/`MissingError` | one tree per module so `shared` stood alone | one hierarchy (section 8) |
| `http.Client` wrapping httpx, `_make_client` reaching into `_raw`/`_send`/`_call` | a general client | `_net.Session` owning curl_cffi directly |
| `site.py`'s browser proxies (`_wrap_raw`, a thread hop per DOM call; 184 lines) | sync Playwright on another thread | async Playwright on the loop |
| `selector` (parsel + python-jsonpath) | a general scraper | `_html.py` (~80 lines est.) |
| `prompt` (questionary) | rich prompts for any script | `input`/`getpass` with numbered choices (~60 lines est.) |
| `cli` (cyclopts wrapper) | a general CLI framework | argparse in `cli.py` |
| `worker.Pool` (sinks, pause, results), `clock` Protocols | general pools | the downloader's own queue on the loop; `_time.Fake` for tests |
| `tests/typing/` and the overloads and Protocols written for pyright | the type-checking gate | annotations for readers (section 14.3) |
| `scripts/dev.py` (484 lines) | portable tasks | the Makefile + `tests/api_snapshot.py` |
| `flnd/common/`, `flnd/sources/common/` | layering | `_net`, `_files`, and helpers in `flnd.source` |

### 4.4 Size
| | today | planned (est.) |
| --- | ---: | ---: |
| `shared/` | 6,366 | 0 |
| flnd engine side | 7,095 | ~4,800 |
| sources (`flnd/sources/`) | 4,029 | ~3,400: `Site.pages`, the early stops and wall detection move into the engine |
| internals that were `shared` | — | ~3,000 |
| new: plugins, testing, compress, extract, media | — | ~2,500 |
| total | 17,490 | ~13,700 |
| frames per file download, call to disk | 15 | ~6 |

The total shrinks even though compression, extraction, media and plugins are new; the wrappers and parallel layers pay for them.

## 5. Performance design, area by area
For each area: the change, the evidence behind it, the risk, and what it does to the API.

### 5.1 Network
- **One client: curl_cffi on the run's asyncio loop.**
  - Its `AsyncSession` drives libcurl's multi interface. Measured on 2,000 small files, it is 2.4× httpx, and 4.5× faster than today's per-file path; on 1 GiB, 4.53 GiB/s against 2.63.
  - It speaks HTTP/2 and HTTP/3 where the server offers them, and reuses DNS, TLS sessions and connections per host.
  - `impersonate="chrome"` sends Chrome's TLS and HTTP/2 fingerprint. Instagram refuses httpx's fingerprint today (`ins.py:466`, the "sending its API calls from the page" fallback), which costs a browser round trip per API call and serializes those calls through one tab.
  - Risk: curl_cffi ships its own libcurl (curl-impersonate), so updates follow its releases. Wheels exist for macOS, Linux and Windows on x86-64 and arm64.
  - API: none; `Site.fetch` keeps its shape.
- **Later, pycurl's `CurlMulti` for bulk media**, only if the real-CDN gate (15.7) shows the downloader CPU-bound. On loopback it is another 2× over curl_cffi (127 ms against 249 ms for 2,000 files), but loopback exaggerates CPU cost: over real networks both are far below link speed.
- **Two pools per source:**
  - *pages* (the source's own domains): the token bucket, `pace` from the source and the config;
  - *media* (everything else, CDNs): no pace, adaptive concurrency **per host** (AIMD). It starts at 8, adds 1 per 10 clean transfers up to 32, and halves on a 429, a 503, a reset or a timeout.

  Evidence: on the e2e run, 4 jobs took 12.7 s and 16 took 5.0 s, which is the pace floor. Risk: some CDNs throttle bursts, which AIMD answers. API: `Source.concurrency` is replaced by an optional cap, `Source.limits = {"cdn.example": 4}`.
- **Pacing never wastes idle time.**
  - The bucket refills by elapsed time (one token deep), so a slow listing never waits again.
  - A `Retry-After` or `x-rate-limit-reset` sets the bucket.
  - A 429 or a block halves the rate until 20 clean requests.
- **Overlap.** Listing feeds the download queue as posts arrive. The queue has a bound of 4× concurrency, which applies backpressure to the listing, so memory stays flat.
- **Segmented downloads** stay for files of 64 MiB or more on servers with `Accept-Ranges`, now on the loop with no threads. The 26% loopback loss came from threads and truncation, but whether segmenting pays on the real CDNs is unknown: gate it on a real-CDN benchmark, default on only if it wins (section 16).
- **DASH** video and audio download in parallel, then ffmpeg muxes them with `-c copy`.

### 5.2 Disk
- **Write to `<dest>.part`, then `os.replace` into place.** No lock file per file, and no stage wrapper: 276 µs becomes about 115 µs. One **home lock** (`<home>/.lock`, `fcntl.flock` or `msvcrt.locking`) is taken per run with a timeout and a message ("another flnd run holds `<home>`; waiting…").
- **Resume:**
  - `<dest>.part` is kept on failure;
  - a later run sends `Range: bytes=<size>-` with `If-Range: <etag>`;
  - a 416 or a changed ETag restarts the file from 0 (today this fails for good);
  - Telegram's per-part offsets go in a `.part.json` beside the part, flushed before it is written (as fixed in `8522f68`).
- **The history row is written after the rename**, through the group-commit writer (5.3). A crash between the rename and the row leaves a file without a row. `flnd check` and the next sync adopt it by its size and its `(target, id)` key, so nothing downloads twice.
- **No fsync per file**, since media can be downloaded again. The database relies on SQLite in WAL mode with `synchronous=NORMAL`. On macOS, `F_FULLFSYNC` is used only at the end of a run (one call). Disk durability isn't the bottleneck (section 2.3).
- **Preallocation** (`F_PREALLOCATE` or `posix_fallocate`) applies only to segmented downloads, which write at offsets. Not measured; low priority.
- **mtime:** `os.utime` sets the post's date right after the rename, from the open file (`os.utime(fd)` where supported), with no second stat.
- **Walks** use one `scandir` with the `DirEntry` stats it already has. `_folder_stats` moves to `flnd.db`'s sizes, so it never walks: 287 ms per 100k files becomes 0.

### 5.3 CPU and data
- **`flnd.db`, one SQLite file with real columns** (section 10):
  - `files` is indexed on `target`, `hash` and `(target, post_id)`, and point reads go through prepared statements: 2.5 µs becomes about 2 µs, and batched `IN` lookups for a page of posts about 1.1 µs.
  - Filters run on columns, not `json_extract`: 15.7 ms becomes µs.
- **The group-commit writer.** One thread owns the write connection and commits every 250 ms or every 500 rows. A row is visible to reads on the same connection at once, and the writer is flushed at the end of each target and on Ctrl-C. Risk: a power cut loses at most 250 ms of rows; their files are adopted by `check` (5.2).
- **Hashing while streaming.** `hashlib.sha256` runs over each chunk as it is written, at 3.3 GiB/s hardware-accelerated, faster than any link. That makes cross-target dedupe and `flnd check` cheap, with no extra dependency: blake3 and xxhash are faster but buy nothing at network speed.
- **JSON: msgspec everywhere.** Decoding is 2× faster than `json`, and 4× with typed structs on hot API shapes where the source wants them. orjson brings nothing more.
- **HTML: selectolax (lexbor)**, 2.6–3.3× faster than parsel, and its import is 25 ms against 43 ms. Embedded JSON blocks are found with `css('script[type="application/json"]')` and decoded with msgspec.
- **JSON paths: `dig(obj, "data.user.edges[].node")`**, about 40 lines in `flnd.source`. It replaces jmespath and python-jsonpath, and is 30× faster than today's `selector.jmespath` (649 µs against 19 µs of plain access).
- **Threads, asyncio and processes:**
  - one asyncio loop thread runs network, browser and Telegram;
  - each source's listing runs on its own thread, so a plugin author writes plain code (section 7);
  - CPU-heavy media work runs in ffmpeg subprocesses, a bounded number at once;
  - pure-Python CPU work stays small: 0.5 ms per file in all.

### 5.4 Browser
- **Async Playwright on the run's loop.** No proxy objects and no thread hop per DOM call (`site.py:599-782` goes). A source's sync code calls `site.page.…` through the loop bridge (`run_coroutine_threadsafe`, about 30 µs est.), which is small against the 0.43 ms CDP round trip.
- **One browser per run**, launched on first need (682 ms) and reused by every target and source. Each target gets its own tab, at most 6 open. The pre-warmed next tab stays.
- **Event-driven scroll**, replacing `SCROLL_WAIT`, `SCROLL_PAUSE` and the jiggle:
  1. register the feed's response filter (the source names it);
  2. scroll by one viewport;
  3. wait for the next matching response *or* DOM growth, up to 3 s;
  4. read the cursor from the response, and stop when the site says there is no more (`has_next_page: false`, an empty page, or a repeated cursor);
  5. only if neither a response nor growth comes, keep a short idle window (2 s, not 10 s).

  Gate: the synthetic feed takes ≤ 3 s, down from 26 s.
- **Human-like pacing stays, and is a setting.** The user asked for human-like scrolling earlier. The default is `pace="human"`: jittered scroll steps, a lognormal pause with a 0.4 s median between steps, and occasional upward jiggles. `pace="fast"` uses no pauses, and the config sets it per source. Risk: faster scrolling could trip bot detection on some sites, so the default stays human, and the gain still comes from never waiting on silence.
- **Response capture over DOM scraping** stays the rule. DOM reads remain only where a site gives nothing else.

### 5.5 Media
- **Encoders:**
  - **Photos:** AVIF through SVT (0.30 s for 12 MP), with the JPEG fallback through Pillow or mozjpeg.
  - **Video:** SVT-AV1 preset 8 is about as fast as x264 medium (1.56 s against 1.44 s) and much smaller at equal quality. x265 is the fallback.
  - **Hardware encoders** are not faster on M-series for this work (1.2–1.3 s) and give larger files, so they come only with `--hw`, for long shows.
- **Parallelism:** photos encode on all cores, one ffmpeg or Pillow job per core. Videos encode one at a time with `-threads 0`. A slow encode never blocks downloads: a separate bounded queue runs after the history row.
- **Probe once.** One `ffprobe -show_streams -of json` per file at 15.6 ms each, cached in `flnd.db` by `(path, size, mtime)`. Pillow probes images in-process.
- **Shows** render in cached pieces (holds and transitions, about 3 s each) on a process pool, then concatenate with `-c copy`. Adding one photo re-renders three pieces.

### 5.6 Startup
- **argparse in `cli.py`** (22 ms for `--help`, against cyclopts' 83 ms). Help text comes from each command function's docstring. No Rich in `--help`.
- **Lazy everything.** `flnd/__init__` defines `__getattr__`, so `import flnd` costs about 1 ms (est.). The CLI imports `flnd` only when a command runs.
- **A source manifest.** `flnd/sources/_manifest.py` lists each built-in source's code, domains and route patterns. `make manifest` generates it and a test checks it. Resolving `ins:example_user` imports only `flnd.sources.ins`; a URL is matched against the manifest first.
- **Gates:** `flnd --help` ≤ 40 ms (est. 25); `flnd download` start to first request ≤ 150 ms with curl_cffi loaded (est. 110).

### 5.7 UI
- **Plain mode** writes one line per finished file or event, with no Rich (0.07 µs per update).
- **Inline mode** draws a Rich `Live` at 10 frames a second, coalesced. Row updates only set numbers (0.5 µs each), and one frame redraws them all. Rich is imported only in this mode.
- **Full screen (Textual) comes later.** It is a separate consumer of the same event stream.

## 6. The library API
```python
import flnd

fl = flnd.open("~/Media/flnd")              # Flnd(home); creates the layout on first write

run = fl.download("ins:example_user", only=["reels"], when="2026", compress=True)
for event in run:                           # Saved | Skipped | Failed | Progress, as they happen
    match event:
        case flnd.Saved(path=path, post=post): print(path, post.date)
        case flnd.Failed(error=e): print(e.why, e.retry)
print(run.report)                           # counts, sizes, failures; also when iteration stopped early

fl.sync()                                   # every followed target; also yields events
fl.preview("twt:example_handle")            # what download would take: list[Item], nothing written
fl.targets(source="ins")                    # list[Target]: followed targets with counts and last sync
fl.files(target="ins:example_user", kind="image", since="2026-01")   # list[File] from flnd.db
fl.forget("twt:example_handle")             # stop following; files stay
fl.check(fix=True)                          # history against disk: missing, unknown, wrong size
fl.retry()                                  # the last run's retryable failures
fl.login("ins", "chrome")                   # how: chrome | firefox | safari | cookies.txt | qr | ...
fl.logins()                                 # list[Login]: state, expiry
fl.add(MySource)                            # a source for this Flnd, from a script
fl.on("saved", lambda e: ...)               # hooks: saved, failed, target_done, run_done
with fl.site("ins") as site:                # a logged-in Site for scripting
    site.fetch("https://www.instagram.com/api/v1/...").json
fl.show(fl.files(target="ins:example_user", kind="image"), "show.mp4", style="fun")
fl.card(paths, "strip.jpg", layout="strip-4")
fl.album(paths, "2026.pdf")
```
- Options are keyword-only, with no more than five on a call. `download` takes `only`, `when`, `limit`, `compress` and `dry`; the rest lives in the config (section 10).
- Events are frozen msgspec structs, so they encode to JSON for `--json` output and for hooks.
- Errors raised to a script are `flnd.Error` subclasses (section 8). A failed file is an event, not an exception, unless the script asks for `strict=True`.
- `run.stop()` asks the run to stop gracefully, the same as a first Ctrl-C.

## 7. The source API and plugins
### 7.1 Writing a source
```python
from flnd.source import Source, Site, Post, File, target, cookies

class Example(Source):
    """example.com: a user's posts and photos, one post by URL, and a search."""

    code = "exm"                       # what targets start with: exm:<user>
    domains = ("example.com",)         # links, cookies, pacing
    api = 1                            # the source API version this was written for
    login = cookies("session")         # logged in when this cookie is there; flnd fetches it from a browser
    pace = 0.5                         # seconds between page requests (the config can override)
    folder = "{user}/{category}"       # where files go; checked when the class is made

    @target("exm:{user}", "example.com/{user}")
    def profile(self, site: Site, user: str) -> dict:
        """A profile: what every category below receives."""
        return site.fetch(f"https://api.example.com/users/{user}").json

    @profile.category(newest_first=True)
    def posts(self, site: Site, profile: dict):
        """The profile's posts, newest first."""
        for page in site.pages(f"https://api.example.com/users/{profile['id']}/posts", cursor="next"):
            for p in page["items"]:
                yield Post(p["id"], [File(u) for u in p["urls"]], date=p["time"], caption=p.get("text"))

    @target(r"example.com/p/{id:\w+}")
    def post(self, site: Site, id: str):
        """One post by its link."""
        yield Post(id, [File(site.fetch(f"https://api.example.com/posts/{id}").json["url"])])

    @target("exm?{words}")
    def search(self, site: Site, words: str):
        """A search: yields targets, each downloaded like a profile."""
        for hit in site.pages("https://api.example.com/search", params={"q": words}, cursor="next"):
            yield from (self.target(user=u["name"]) for u in hit["users"])
```
- **A source declares what it is.** Its hooks are a few named methods:
  - `@target` routes;
  - `@<route>.category` for the parts of a target;
  - optional `login(auth, how)` and `connect(site)` (async allowed) for custom flows, as Telegram needs.
- **What a source gets from `Site`:**
  - `fetch` (paced, retried, fingerprinted);
  - `pages(url, cursor=…)`, where the engine keeps and resumes the cursor;
  - `page` (the browser tab), `feed(match)` (captured responses while scrolling, section 5.4) and `html(text)`;
  - `dig`, `state` (per-source key-value), `warn`, and `wait(seconds, reason)`.
- **What a source never does:** touch the disk, the history, retries or concurrency.
- **The engine owns the early stops** (5 saved in a row on sync, 5 older than `--when` in a row) and wall detection, through `Site.fetch`/`page` raising `LoginWall` or `Blocked`. The four copies in ins, tik, twt and fbk go.
- **A post** can carry `caption`, `author`, `tags`, `comments` and `extra`, which feed the sidecars and search (section 12).
- **Sync or async.** A category may be `def` or `async def`. Sync code runs on the listing thread, and async code runs on the loop. Both get the same `Site`.
- **Mistakes are caught when the class is made.** A bad pattern, an unknown folder field, a missing `code`, a duplicate route, or a category on a route that yields posts each raise `SourceError` naming the class, the attribute and the fix, e.g. `exm.folder: unknown field {usr}; fields are user, id, date, category, …`.

### 7.2 Plugins at runtime
- **Discovery, in order:**
  1. the built-ins;
  2. installed packages declaring `[project.entry-points."flnd.sources"] exm = "flnd_example:Example"`;
  3. `<home>/sources/*.py` and `<home>/sources/<package>/`;
  4. `fl.add(cls)` in a script.

  A later one with the same `code` is refused with a message, never silently shadowed.
- **Loading is lazy.** Entry points and files are scanned for their `code` and `domains` without importing them, from a `# flnd: code=exm domains=example.com` header line or the entry point's name. A plugin is imported when one of its targets is resolved or when `flnd sources` runs. `--help` never imports one.
- **Versioning.** The source API has a major version, `flnd.source.API = 1`. A plugin declares `api = 1`.
  - A plugin built for a newer major version is refused: `exm needs source API 2; this flnd has 1: update flnd`.
  - One built for an older major version is refused with a link to the migration notes.
  - Minor additions stay compatible.
- **Isolation.** These are in-process boundaries, not separate processes:
  - an import or validation error disables that source alone, with a warning and the file and line;
  - an exception inside a listing fails that target alone, and is classified (section 8) with the source named;
  - a plugin can't block the loop: sync code runs on the listing thread, and an async category that doesn't yield for 10 s gets a warning;
  - process isolation is listed as later (section 12).
- **The test kit (`flnd.testing`):**
  - `FakeSite(json=…, html=…, pages=…, feeds=…)` answers by URL;
  - `collect(Example, "exm:example_user", json=FIXTURES)` returns what would be saved: paths, keys and dates;
  - `check(Example)` validates the class and resolves every example in its route docstrings;
  - a `fake_clock`;
  - `examples/plugin/` is a 40-line plugin with its test.
- **The CLI:** `flnd sources` lists every source with its origin (built-in, package or file), version and login state, and the reason a plugin was disabled. `flnd ls` and `flnd logins` include plugin sources.

## 8. Errors
### 8.1 The hierarchy
```
flnd.Error                      every error flnd raises; .why (one line), .retry (how to fix or retry), .target
├── UsageError                  bad target, flag or config value → exit 2, no traceback
├── LoginError                  not logged in, expired, refused   → that source stops; others go on (exit 3 at the end)
├── SiteError                   the site answered, but not usefully
│   ├── Blocked                 rate limited or temporarily blocked: retried after its wait, then reported
│   ├── NotFound                deleted, private, or never existed: recorded as gone, not retried
│   └── Changed                 the response no longer has the expected shape: reported with a saved sample
├── NetworkError                DNS, TLS, timeout, reset: retried with backoff; then a failure
├── DiskError                   no space, permission, name too long: the run stops cleanly
├── ToolError                   ffmpeg, 7zz and the like: missing (with an install offer) or failed (last stderr lines)
├── SourceError                 a source or plugin is broken: load, validation, or a bug in a listing
└── Stopped                     Ctrl-C: not a failure; exit 130
```

### 8.2 The policy
| failure | retry | then |
| --- | --- | --- |
| a timeout, a reset, a 5xx | 4 tries with jittered exponential backoff (1, 2, 4, 8 s), on the same mirror and then the next | a `Failed` event; kept for `retry` |
| a 429 or a block page | the `Retry-After` wait or the site's reset (up to 1 h), then the adaptive rate | `Blocked` after 3 rounds |
| a 404 or 410, a private item | none | `gone` in `flnd.db`; never asked again unless `--again` |
| a 401 or 403 on the source's domains, a login wall | one cookie refresh from the browser | `LoginError` for that source |
| a truncated transfer | resume from the `.part` | restart from 0 on a 416 or a new ETag |
| a parse failure | none | `Changed`, the response saved to `logs/<run>/samples/` |
| disk full | none | stop listing, finish the in-flight files' rows, exit 1 with the folder named |

- **No data is lost:**
  - a file is visible only after its rename;
  - a row is written only after its file;
  - parts and offsets survive a failure, so resume is exact;
  - the database is WAL;
  - every crash leaves either the old state or the new one.
- **The failure report.**
  - Each run writes `logs/<run-id>.json`: every failure with its target, post, URL (redacted), class, `why`, `retry` and attempts.
  - The end of a run prints a short summary:
    ```
    ✗ 3 files failed: 2 network (retry: flnd retry), 1 gone. Details: flnd status
    ```
  - `flnd status` shows the last run, and `flnd retry` replays its retryable failures.
- **Ctrl-C has three stages:**
  1. the first stops listing and finishes the in-flight downloads (showing "stopping…, Ctrl-C again to stop now");
  2. the second cancels the transfers, keeps the parts, flushes the rows and closes the browser;
  3. the third exits at once.

  Every stage leaves a resumable state, and `Stopped` exits 130.
- **No tracebacks for user errors.** `UsageError`, `LoginError`, `DiskError` and `ToolError` print one line and what to do. An unexpected exception prints one line plus the path of a crash file with the traceback (`logs/crash-<run>.txt`); `-v` prints it inline.
- **Secrets never reach a message.** URLs are redacted (user info, query and fragment), headers are scrubbed by name, and cookies are never logged. A test greps every log and error message produced by the test suite for the fixtures' secret values.

## 9. The CLI
The user's current leanings were the `target`/`auth`/`media` groups, `--from`, `--when`, `--by`, `--style` and `--compress`, with inline UI, `.flnd` and quiet output as defaults. They are kept where good. Groups are flattened, because each verb is unique and shorter to type.

```
flnd download <targets…> [--only CATS | --all] [--when WHEN] [--limit N] [--compress] [--dry]
flnd sync [targets…] [--compress]
flnd ls [targets…] [--json]          followed targets: files, size, last sync, failures
flnd forget <targets…>               stop following; files stay
flnd status                          the last run: what failed and why
flnd retry                           retry the last run's retryable failures
flnd check [--fix]                   history against disk: missing, unknown, wrong size
flnd login <source> [how]            how: chrome | firefox | safari | cookies.txt | qr | phone
flnd logout <source>
flnd logins                          each source's login and its expiry
flnd sources                         built-in and plugin sources, versions, why one is disabled
flnd show  <targets|paths…> [--by post|day|month|year|all] [--style classic|fun|calm|story] [--music FILE] [--when WHEN]
flnd card  <targets|paths…> [--by post|day|month] [--style strip|grid|polaroid|postcard] [--when WHEN]
flnd album <targets|paths…> [--by month|year|all] [--when WHEN]

global: --home DIR (default: $FLND_HOME, else ./.flnd) · --ui plain|inline (default: inline on a terminal) · -v · --json
```
- **`--only` replaces `--from`.** "From" read like a source or a date. `--only reels,posts` says what it does, and `--all` replaces `--from all`. The default is the target's own content.
- **`--when`** takes `2026`, `2026-06`, `2026-06-15`, `2026-01..2026-03` or `30d`.
- **`download` follows by default**, and `sync` updates everything followed.
- **Archives extract by themselves.** `--compress` shrinks files as they land, and on `sync` it also shrinks files saved earlier.
- **`--json`** turns the event stream into JSON lines, for scripts and cron.
- **Exit codes:** 0 ok, 1 some files failed, 2 usage, 3 login needed, 130 stopped.

## 10. Storage and the home folder
```
<home>/
  flnd.db            files, posts, targets, cursors, logins (metadata only), runs, failures, made, probes
  secrets.db         the encrypted vault: cookies and tokens per source (AES-256-GCM, key in the OS keychain)
  config.toml        defaults and per-source settings (tomllib, stdlib)
  media/<source>/<target>/…   downloaded files; the folder templates decide below
  made/<target>/<show|card|album>/…
  sources/           plugin files and packages
  cache/             probes, show pieces, frames (swept after 30 days)
  logs/              run reports, samples of changed responses, crash files
  .lock              the home lock
```
- **`files`:** `key PRIMARY KEY, target, source, post_id, path, size, mtime, date, kind, hash, state (saved|gone|compressed|kept|extracted|failed), before, format, tools, sidecar`, indexed on `target`, `hash`, `(target, post_id)` and `path`.
- **`targets`:** `key, source, value, follow, complete, cursors (JSON), last_sync, files, bytes`.
- **`failures`:** `run, key, target, url, class, why, retry, attempts, at`.

**`config.toml`:**
```toml
[defaults]
ui = "inline"
compress = false
when = ""            # e.g. "1y" to bound every download

[source.ins]
pace = 1.0
scroll = "human"     # or "fast"
proxy = "socks5://127.0.0.1:1080"
limits = { "cdninstagram.com" = 8 }
```

**Migrating an existing `.flnd` home: recommended.** History is what stops a sync from downloading everything again.
- On first open, `flnd.db` is built from `metadata/history.db`, `targets.db` and `state.db`, which are SQLite tables of JSON values (or JSON stores, which are converted first).
- The migration takes about 1 s per 100k rows (est., from the measured 0.86 µs insert and 0.72 µs read per row).
- Files stay where they are, and rows keep their paths.
- The vault is re-encrypted with the same key.
- The converter is about 150 lines and is deleted one release later.

## 11. Compression, extraction and media
- **`--compress`** is one step after a file lands:
  `download → [extract, if an archive] → [compress] → row → Saved`.
  1. *Plan* the candidates from the probe:
     - JPEG goes to AVIF, then mozjpeg, then WebP;
     - PNG goes to oxipng, then lossless WebP for flat art, then AVIF for photos;
     - animated GIF goes to animated WebP;
     - video goes to SVT-AV1 10-bit preset 6, with Opus at 96k, `+faststart`, and the metadata copied; x265 is used when SVT-AV1 is missing;
     - audio goes to Opus.
  2. *Encode* into `<dest>.part`.
  3. *Verify*:
     - the result decodes;
     - its dimensions, frames and duration match, within ±0.1 s;
     - an image keeps SSIM ≥ 0.98;
     - it saves at least 5%.

     If a candidate saves 40% or more, stop there; otherwise keep the smallest that passed.
  4. *Place*: same suffix, replace; new suffix, write the new file and move the row in one transaction, then remove the old file.
  5. *Record* `compressed`, `kept` or `failed`, with the tools string, so a tool upgrade retries `failed` and `kept`.
- **Tools** come from PATH or are offered for download: ffmpeg, avifenc, oxipng, caesiumclt, 7zz. Each is pinned by checksum and cached in `~/.cache/flnd/tools`. Pillow is a dependency of the `media` extra.
- **Archives extract by themselves.**
  - A source may mark a file `File(url, password=…)`; loi does.
  - The archive extracts beside itself into a folder of its name. The limits on size, ratio and file count stay, links are refused, and `--` comes before paths (as fixed in `daa2354`).
  - The archive is kept, unless `--compress` is on, in which case it is removed after a verified extract.
  - A wrong password or a damaged archive keeps the archive and records `failed`.
- **Slideshows, cards and albums** (`flnd show`, `card`, `album`) use ffmpeg and Pillow only:
  - `xfade` transitions, read from the installed ffmpeg;
  - Pillow-drawn titles and captions, laid on with `overlay`;
  - music looped, faded and normalised to −16 LUFS;
  - clips ducked under the music.

  Shows render in cached pieces on a process pool (5.5). Cards and albums lay out boxes with a pure function, then draw them on the pool; albums go to PDF through `save_all`. Outputs are recorded in `made`, so a rerun skips any whose inputs are unchanged. Styles:
  - `classic`: fade, 3 s;
  - `fun`: random transitions, 2 s;
  - `calm`: dissolve, 5 s;
  - `story`: 9:16 with a blurred fit.

  Card layouts are `strip-3/4`, `grid-NxM`, `polaroid`, `postcard` and `mosaic`.

## 12. Features, ranked
Value and cost are on a scale of 1 to 3. The MVP is what the first release ships.

| # | feature | value | cost | API sketch | cut |
| ---: | --- | :---: | :---: | --- | :---: |
| 1 | download, sync, follow, forget, ls | 3 | — | section 9 | MVP |
| 2 | logins from Chrome, Firefox and Safari, cookies.txt, QR and phone; expiry warnings | 3 | 1 | `flnd login ins chrome` | MVP |
| 3 | the failure report, `status`, `retry` | 3 | 1 | `fl.retry()` | MVP |
| 4 | plugins (file, entry point, `fl.add`) + the test kit | 3 | 2 | section 7 | MVP |
| 5 | `--compress` + automatic extraction | 3 | 2 | section 11 | MVP |
| 6 | config file: per-source pace, limits, proxy, scroll style | 3 | 1 | `config.toml` | MVP |
| 7 | `check [--fix]`: integrity and repair, adopting orphans | 3 | 1 | `fl.check(fix=True)` | MVP |
| 8 | dedupe across targets by content hash (hard-link or skip) | 2 | 1 | `[defaults] dedupe = "link"` | MVP |
| 9 | metadata sidecars: post JSON, captions | 2 | 1 | `[defaults] sidecars = true` → `<file>.json` | MVP |
| 10 | hooks and events for scripts; `--exec CMD` per saved file | 2 | 1 | `fl.on("saved", fn)`; `--exec 'cmd {path}'` | MVP |
| 11 | `--dry` as a diff: new, already saved, gone | 2 | 1 | `fl.preview()` | MVP |
| 12 | `show`, `card`, `album` | 2 | 3 | section 11 | MVP |
| 13 | storage stats (`ls` sizes per target and source, what compression saved) | 2 | 1 | `flnd ls` columns | MVP |
| 14 | `--json` events for scripting and cron | 2 | 1 | section 9 | MVP |
| 15 | schedules: `flnd sync --every 6h`, plus a generator for a launchd, systemd or Task Scheduler unit | 2 | 2 | `flnd schedule 6h` | later |
| 16 | notifications (macOS, ntfy, webhook) on new files or a login expiring | 2 | 1 | `[notify] url = …` | later |
| 17 | search over saved posts (SQLite FTS5 on captions, authors, tags) | 2 | 2 | `flnd find "beach 2026"` | later |
| 18 | export and import of follows | 1 | 1 | `flnd ls --json > f`; `flnd download $(…)` | later |
| 19 | comments capture where the site gives them | 1 | 2 | `Post.comments`; `--comments` | later |
| 20 | a resumable queue across runs (pending files kept in `flnd.db`) | 2 | 2 | automatic | later |
| 21 | proxy rotation and per-source quotas (loi's daily VIP quota, generalised) | 1 | 2 | `[source.x] quota = "15/day"` | later |
| 22 | a library browser: a local web page or a Textual TUI over `flnd.db` and the media | 2 | 3 | `flnd browse` | later |
| 23 | full-screen Textual UI | 1 | 2 | `--ui full` | later |
| 24 | process isolation for untrusted plugins | 1 | 2 | `[plugins] isolate = true` | later |
| 25 | motion (Ken Burns), loops (GIF/WebP), recaps, smart crop | 1 | 3 | `Show(motion=…)` | later |
| 26 | free-threaded build, if the dependencies support it | ? | 1 | — | later |

## 13. Dependencies, native code and compilation
### 13.1 Dependencies
| package | role | verdict | why |
| --- | --- | --- | --- |
| `curl_cffi` | the HTTP client | **adopt** | 2.4× small files on the loop, 1.7× large files, HTTP/2 and 3, a Chrome fingerprint; replaces httpx, h2, httpcore and anyio |
| `playwright` | the browser | keep | the only practical way to drive Chrome; async API from now on |
| `msgspec` | JSON, records, events | keep | 2–4× `json` |
| `selectolax` | HTML | **adopt** | 2.6–3.3× parsel; replaces parsel, lxml, cssselect, w3lib, python-jsonpath and jmespath |
| `cryptography` | the vault | keep | AES-GCM |
| `rich` | inline progress | keep, lazy | imported only for the inline UI |
| `Pillow` | stills, cards, albums, image probes | adopt (the `media` extra) | in-process, no ImageMagick |
| `telethon` + `cryptg` + `qrcode` | Telegram | keep (the `telegram` extra) | |
| `httpx`, `parsel`, `python-jsonpath` | | **remove** | replaced above |
| `questionary` | prompts | **remove** | 29 ms of import; `input`/`getpass` cover numbered choices |
| `cyclopts` | the CLI | **remove** | 83 ms for `--help` against argparse's 22 ms |
| `filelock` | locks | **remove** | one home lock is about 30 lines with `fcntl`/`msvcrt` |
| `orjson` | | no | msgspec decodes as fast; one JSON library |
| `niquests` | | no | fast on large files, slower on small ones; 67 ms import |
| `pycurl` | bulk downloads | **later** | 2× curl_cffi on loopback with `CurlMulti`; only if the real-CDN gate shows the downloader CPU-bound |
| `rnet` | | no | its 0.x blocking API failed to import as documented; too young |
| `blake3`, `xxhash` | hashing | no | SHA-256 is hardware-accelerated at 3.3 GiB/s, above any link |
| `uvloop` | | no | the loop isn't a bottleneck, and there's no Windows support (not measured) |
| `pillow-simd`, `pyvips` | | no / later | pillow-simd lags CPython releases; pyvips for huge albums later |

Direct runtime dependencies drop from 13 to 10:
- the core holds 6: curl_cffi, playwright, msgspec, selectolax, cryptography and rich;
- the `telegram` extra holds telethon, cryptg and qrcode;
- the `media` extra holds Pillow.

The installed tree loses about 20 transitive packages (est.).

### 13.2 A Rust extension (PyO3 and maturin)
**Not now.** After the changes above, no CPU hot path remains:
- the engine costs 0.5 ms per file, of which 13 µs is `place`;
- every heavy loop is already C: libcurl, sqlite, msgspec, lexbor, hashlib, ffmpeg.

The cost of an extension would be maturin builds, wheels for 5 or more platforms in CI, and slower contributor setup. **Revisit if** a measured profile shows ≥ 20% of a real run's CPU in pure-Python flnd code.

### 13.3 Cython, mypyc and Nuitka
**None.**
- Cython compiled `job.py`, `source.py` and `_text.py` unchanged, and saved 17% on `place` (13.2 to 10.7 µs per file) and 0–5% elsewhere. mypyc saved 3% on `filename`. Both speed up the slice that is already the smallest.
- Plugins would still load against a compiled core (both keep Python-level classes), but every platform would need compiled wheels, and debugging would be harder.
- Nuitka only matters for distribution. PyInstaller keeps building the one-file binary; switch to Nuitka only if PyInstaller's start time (unpacking) becomes a complaint.

### 13.4 Free-threaded CPython 3.14t
**Later.** It runs CPU-parallel Python 3.6× faster, but httpx crashes on it today, and the native dependencies (curl_cffi, Playwright's driver, cryptography, msgspec, selectolax) need free-threaded wheels. flnd's CPU-heavy work is in subprocesses anyway. Revisit when every dependency ships `cp314t` wheels, with the e2e benchmark as the gate.

## 14. Tooling
### 14.1 The Makefile replaces `scripts/`
```make
UV  ?= uv
PY  ?= 3.14
RUN  = $(UV) run --python $(PY)

setup:    ; $(UV) sync --python $(PY) --all-extras
test:     ; $(RUN) python -m unittest discover -s tests $(ARGS)
lint:     ; $(RUN) ruff check flnd tests
check: lint test
api:      ; $(RUN) python tests/api_snapshot.py --write
manifest: ; $(RUN) python -m flnd.sources._manifest --write
bench:    ; $(RUN) python tests/bench/run.py $(ARGS)
docs:     ; $(RUN) --group dev pdoc flnd flnd.source flnd.testing -o build/docs
try:      ; $(RUN) flnd --home .try $(ARGS)
dist:     ; $(RUN) --group dist pyinstaller --onefile --name flnd flnd/__main__.py
clean:    ; $(RUN) python -c "import shutil; [shutil.rmtree(p, True) for p in ('build', 'dist', '.try')]"
.PHONY: setup test lint check api manifest bench docs try dist clean
```
- **Each recipe is one `uv` command**, so it runs the same in bash, zsh, PowerShell and cmd. There is no `rm`, no `&&` chain and no shell globbing.
- **Windows** has no make by default. The README says to `winget install ezwinports.make`, or use the `make` that ships with Git for Windows, and lists each recipe's command for anyone without make. `dev.py` existed partly for Windows; one-command recipes cover that.
- **`scripts/` is removed.** The API snapshot generator moves to `tests/api_snapshot.py`, which `tests/test_api.py` imports to compare the snapshot. `load_dev()` goes from `test_names.py`, `test_api.py` and `test_packaging.py`, and `test_dev.py` is deleted.

### 14.2 Tests
- **`shared`'s tests fold into flnd's,** by the new internal module: `test_net.py`, `test_browser.py`, `test_db.py`, `test_files.py` and so on. Tests of wrapper-only behaviour go: `Query`, `Sink`, the error trees, the generality of `http.fake`.
- **Fakes:**
  - `flnd.testing` is public (FakeSite and the rest);
  - a network fake at the curl_cffi session (a route table answering requests) replaces `httpx.MockTransport`;
  - the clock fake stays.
- **`tests/data/api.txt`** lists `flnd`, `flnd.source` and `flnd.testing` only, and `make api` regenerates it.
- **`test_names.py`** shrinks to the naming rules a reader feels: no `get_`/`to_`/`try_` prefixes, verbs for actions, at most 5 keyword options. Its allowlist starts empty.
- **`test_layers.py`** checks three things:
  - the CLI imports only `flnd`;
  - built-in sources import only `flnd.source`, the stdlib and their own helpers;
  - no public module imports a heavy module at import time (`-X importtime` against a list).
- **`tests/test_docs.py`** keeps the docs in step with the code (14.6).
- **`tests/typing/` and pyright are removed**, and so is `type` from `check`. Ruff (lint, about 50 ms) catches the mistakes a type checker found most often: undefined names, unused imports, bugbear patterns.

### 14.3 Type hints
- Annotate the public API (`flnd`, `flnd.source`, `flnd.testing`) for editors and readers; plugin authors get completion.
- Annotate internals where an annotation explains something.
- No overloads or Protocols written only for a checker, and no `TypeVar` gymnastics.

### 14.4 Benchmarks (`make bench`)
- `tests/bench/` holds the scripts from section 2 as one runner:
  - `startup` (help, import);
  - `http` (loopback server, 1 GiB, 2,000 files);
  - `disk` (5,000 writes);
  - `db` (100k rows);
  - `walk`;
  - `cpu` (JSON, HTML, hash);
  - `e2e` (the faked site with latency and pace);
  - `browser` (the synthetic feed; skipped without Chromium);
  - `media` (skipped without ffmpeg).
- `make bench` prints a table and writes `tests/bench/last.json`.
- **`make bench ARGS=--gate`** compares against `tests/bench/gates.json` and fails beyond a 15% tolerance. It runs by hand before a performance commit, not in `check`, because timing is noisy.
- **Regression guards in `check` use counts**, not times:
  - requests per run in the e2e fake;
  - `stat` calls per downloaded file (≤ 2);
  - database statements per file (≤ 2);
  - scroll steps per synthetic feed;
  - modules imported by `flnd --help`.

### 14.5 AGENTS.md
Rewrite it for the plan:
- the layout for one package and the public/private split;
- `make` targets instead of `scripts/dev.py`, and no pyright gate;
- the writing rules of section 14.6, replacing "a docstring is one sentence" and "more code than prose";
- keep the commit format, privacy, example handles and the low-cost-model rule;
- add "a performance commit states its benchmark before and after".

### 14.6 Documentation for human readers
The user, 2026-11-16: "the codebase should have extensive comment and docs for human to read." Anyone who opens a file should understand what it does, why it is built that way, and how it fits with the rest, without asking anyone or reading the git history. This replaces the old rules: one-sentence docstrings, "more code than prose", and comments only for why.

**Three layers:**
1. **Guides in `docs/`**, written for people:
   - `README.md`: an index, and where to start for a user, a script author, a source author and a contributor;
   - `architecture.md`: the layout of section 4, one download traced from the CLI to the disk, and the threads and the loop, with a diagram;
   - `library.md`: every public call, its options, its events and a runnable example;
   - `cli.md`: every command, flag, default and exit code, with examples;
   - `writing-a-source.md`: a tutorial that builds the example source step by step, then packages it as a plugin and tests it with `flnd.testing`;
   - `errors.md`: each error class, what causes it and what to do;
   - `storage.md`: the home layout, `flnd.db`'s tables and columns, and the migration from an old `.flnd`;
   - `performance.md`: section 2's numbers, how to run `make bench`, and why each performance choice was made;
   - `sources/<code>.md`: per site, what it downloads, how login works, its quirks with the date they were seen, the endpoints it uses, and its limits.
2. **Docstrings on every module, class, function and method**, private ones included unless the body is a few obvious lines:
   - **A module docstring** says what the module holds, who calls it, what it calls, its main flow in numbered steps, and any rule a change must keep (threads, locks, ordering, invariants). There is no length cap.
   - **A function docstring** in Google style: a summary line; a paragraph on behaviour and edge cases; `Args:`, `Returns:`, `Raises:`; and an `Example:` on public names. Editors show these, and they are the API reference.
3. **Comments** explain *why*, and also *how* wherever the code isn't obvious at a glance:
   - a site quirk, with the date it was seen;
   - a magic number and where it comes from;
   - a lock, the thread a call runs on, the order two writes must keep;
   - a platform branch;
   - the steps of a long algorithm, as numbered comments;
   - how a failure is handled, and why it is safe.

   Section comments split a long module into parts. A comment never restates the line under it, and never cites a plan step or a commit (those change); it may point to a `docs/` page.

**Style.** Plain English and short sentences; examples use the example handles only (`example_user`, `example.com`, …). Every term is defined where it first appears, and the docs keep a glossary: target, source, route, category, post, file, event, home.

**Kept in step with the code:**
- a change of behaviour updates its docstrings and `docs/` page in the same commit;
- `tests/test_docs.py` checks that:
  - every module, public class and public function has a docstring, and a public function documents each argument;
  - every `python` block in `docs/*.md` runs against the fakes, like a doctest;
  - every CLI flag that argparse knows appears in `docs/cli.md`, and every `docs/` link resolves;
  - every built-in source has its `docs/sources/<code>.md`.
- `make docs` builds an HTML API reference with pdoc, a dev-only dependency, into `build/docs/`.

**Cost.** There is more prose to maintain, which is what the test above is for. Comments also make files longer, but a reader's time is the scarcer resource.

## 15. Migration: ranked commits with gates
Each commit is one logical change with `make check` green, and it documents what it touches: its docstrings, its comments and its `docs/` page. A performance commit quotes `make bench` before and after in its message. Mechanical moves go to a low-cost model.

| # | commit | gate |
| ---: | --- | --- |
| 15.1 | `chore:add make bench and the benchmark suite` (section 2's scripts in `tests/bench/`) | baseline recorded in `gates.json` |
| 15.2 | `chore:replace scripts with the makefile and drop the type gate` (14.1–14.3) | `make check` green without pyright |
| 15.2a | `docs:add the docs folder, the glossary and the docs test` (14.6) | `tests/test_docs.py` runs; the gaps it lists drive 15.16 |
| 15.3 | `refactor:fold shared into flnd's private modules` (moves only; imports updated) | tests green; `make bench` unchanged ±5% |
| 15.4 | `perf:start the cli with argparse, lazy imports and a source manifest` | `flnd --help` ≤ 40 ms (from 173) |
| 15.5 | `perf:run network and browser on one asyncio loop with curl_cffi` | 2,000 × 100 KiB ≤ 300 ms (from 1,009); 1 GiB ≥ 4 GiB/s (from 1.9) |
| 15.6 | `perf:write downloads to a part and rename under one home lock` | ≤ 130 µs of disk bookkeeping per file (from 276) |
| 15.7 | `perf:adapt download concurrency per host` | e2e 500 posts at 40 ms ≤ 5.5 s at the default (from 12.7) |
| 15.8 | `perf:drive feeds by responses and end on the site's cursor` | synthetic feed ≤ 3 s (from 26) |
| 15.9 | `refactor:store history, targets and runs in one flnd.db with columns` + the home migration | point read ≤ 2.2 µs; sync of 100k with nothing new ≤ 0.3 s |
| 15.10 | `perf:parse html with selectolax and paths with dig` | Meta page ≤ 8 ms (from 18) |
| 15.11 | `feat:classify every failure, report it and add status and retry` | tests per class; no traceback in any usage-error test |
| 15.12 | `feat:load sources from files, packages and scripts, with the test kit` | the example plugin's tests pass; a broken plugin disables only itself |
| 15.13 | `feat:compress media and extract archives as files land` | 1,000 JPEGs on 8 cores ≤ 2 min; `sync --compress` over 100k done files ≤ 3 s |
| 15.14 | `feat:make slideshows, cards and albums` | 100 photos at 1080p ≤ 60 s cold; one added ≤ 5 s |
| 15.15 | `feat:add check, dedupe, sidecars, hooks and json events` | per-feature tests |
| 15.16 | `docs:document every module, function and source for human readers` (14.6) | `tests/test_docs.py` passes with an empty allowlist |
| 15.17 | `docs:rewrite the readme for the rebuilt flnd` | every README example runs in `test_docs` |

The order puts the biggest measured wins first (15.4–15.8). 15.3 is a pure move, so every later commit's diff stays readable.

## 16. Open questions, with recommended defaults
1. **Flat verbs** (`flnd ls`, `flnd login`, `flnd show`) or the groups (`flnd target ls`, `flnd auth login`, `flnd media show`)? *Recommended: flat*, because every verb is unique.
2. **`--only` instead of `--from`?** *Recommended: yes*, with `--all`.
3. **Scroll default `human` or `fast`?** *Recommended: `human`* with the event-driven loop; `fast` per source in the config.
4. **Default concurrency per CDN host?** *Recommended: start at 8, adapt up to 32.*
5. **Segmented downloads by default?** *Recommended: on for files of 64 MiB or more only after a real-CDN benchmark shows a gain*; off until then.
6. **Migrate old `.flnd` homes?** *Recommended: yes, automatically on first open,* for one release.
7. **Dedupe across targets: hard-link, skip or keep both?** *Recommended: hard-link* (no extra space, both paths stay true). Skip on file systems without hard links.
8. **Sidecars on by default?** *Recommended: off*; on in the config.
9. **The home default:** `./.flnd` (today) or `~/flnd`? *Recommended: `$FLND_HOME`, else `./.flnd`.* That keeps today's behaviour and suits cron.
10. **pycurl as a second client for bulk media?** *Recommended: later,* gated on a real-CDN benchmark.
11. **Ruff as the only lint?** *Recommended: yes*, with a small rule set.
12. **Process isolation for plugins?** *Recommended: later*; in-process isolation first.
