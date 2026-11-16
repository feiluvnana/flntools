# PLAN: flnd, one fast package

The user's brief, 2026-11-16, gathered from one session:
- **One package, flnd.** It is a library plus a thin CLI. There is no `shared` toolkit, and anything flnd needs lives inside flnd.
- **Maximum performance, measured first.** Native-backed libraries are welcome. Cython, mypyc and Nuitka get evaluated. Python stays the language.
- **A nice, easy API**, above all the source API. Sources can also be added at runtime, as plugins.
- **Robust error handling** end to end.
- **Every good feature**, ranked, with an MVP cut.
- **Built in:** compression and archive extraction. Slideshows, cards and albums were removed on 2026-11-16, at the user's request.
- **A simple, feature-rich architecture, with sources that are straightforward to write**, reviewed against gallery-dl, yt-dlp, instaloader and twscrape. [ARCHITECTURE.md](ARCHITECTURE.md) explains the design for readers; this plan keeps the measurements, the dependencies and the order of the work.
- **The Makefile instead of `scripts/`.**
- **Type hints for readers, not a checking gate.**
- **Extensive comments and docs for human readers** (user, 2026-11-16: "the codebase should have extensive comment and docs for human to read").
- **Everything is open.** The old API, CLI and data format don't constrain this design.

This is the plan for the rebuild, and its steps are the commits of section 15. The user approved it on 2026-11-16, taking the recommended answer to every question in section 16.

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
11. Compression and extraction
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
- **Network:** libcurl through `curl_cffi`, on one asyncio loop with HTTP/2. It sends Chrome's TLS fingerprint, which may let Instagram's API calls leave the in-page fetch fallback (a live check decides). Concurrency is adaptive per host.
- **Browser:** async Playwright on the same loop. Sources drive it through a few `Site` verbs, so no proxy object wraps Playwright. Scrolling is driven by events and ends on the site's own "no more" signal, not by waiting out 10 s of idle.
- **Disk:**
  - writes go straight to `<dest>.part`, which is also the file's lock, then a rename;
  - no lock file per file, and two runs can still share a home safely;
  - history rows commit once per post (about 15 µs a row, measured), with no writer thread;
  - one `flnd.db` holds real columns and indexes.
- **Startup:** argparse, and commands load only what they use. Built-in sources come from a static manifest. `flnd --help` drops from 173 ms to about 25 ms.
- **CPU:** msgspec for JSON, selectolax for HTML, and SHA-256 computed while streaming (hardware-accelerated at 3.3 GiB/s) for dedupe.
- **Rejected, with numbers:**
  - Cython and mypyc save 3–17% on a 13 µs per-file path;
  - a Rust extension has no hot path left to speed up;
  - free-threaded 3.14t runs CPU work 3.6× faster, but flnd's CPU work is small and its native dependencies need free-threaded wheels first. It comes later;
  - pycurl's `CurlMulti` is 2× curl_cffi on loopback, which is 62 µs per file of CPU, invisible at real latency.
- **Plugins:** a Python file in the user's plugin folder (`~/.config/flnd/sources/`, never inside a home), an installed package (the `flnd.sources` entry point), or `fl.add(MySource)`. Each is validated at load time and versioned (`api = 1`), and a broken one disables only itself. Authors get a test kit, `flnd.testing`.
- **Errors:** one hierarchy. Every failure is classified as retryable, permanent, login, or fatal for the run. A failure report says what failed, why, and how to retry; Ctrl-C has three stages. A user error never prints a traceback.
- **Docs for humans:** guides in `docs/`, full docstrings on every module and function, and comments that explain why and how wherever the code isn't obvious. A test keeps them in step with the code (section 14.6).
- **Size:** the 6,366 lines of `shared` and the 11,124 lines of flnd become about 13,000 lines in one package (estimated, section 4.4), before the prose that section 14.6 adds. The wrappers, generic `Query`, `Sink` and Protocols written for pyright, and the error trees and fakes that existed only to make `shared` stand alone, all go.

**Expected effect on a real run.** Take a 1,000-post Instagram profile, 2,000 files, and a CDN latency of about 100 ms:
- **Today:** about 60–70 s. Listing takes about 34 s (40 scroll steps at 0.6 s, then 10 s of idle), and the downloads take about 25 s at 8 at a time.
- **Planned:** about 20–25 s with the default human-like scroll, which takes about 18 s for 40 steps (a 0.4 s median pause plus the response wait); about 10 s with `scroll = "fast"`. Adaptive downloads at up to 32 per host take about 6 s, overlapped with the listing.

This is a model built from the measured parts, not a measurement; commits 15.12 and 15.13 set benchmark gates for it.

## 2. Measured today
**Machine:**
- Apple M5 (10 cores), 32 GB of memory, an APFS SSD;
- macOS 26.2;
- CPython 3.14.6 from uv;
- the code at `01cfdad`, the head of main when this was written.

**Method:**
- Each number is the best of 3 to 5 runs, or the median of 15 for process start times. Every number in a table was measured; estimates are marked "est." in the text.
- Network tests run on loopback, against an asyncio server that answers with `sendfile` and supports keep-alive and `Range`. Loopback shows CPU and per-request overhead; it hides latency and bandwidth, so latency is simulated where it matters.
- The scripts live in the sessions' scratchpads: the plan's in `bench/*.py`, and the review's three (`commit_bench.py`, `avif_bench.py`, `bridge_bench.py`) beside them. Commit 15.1 moves them all into `tests/bench/` (section 14.4).

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

What a commit costs, per row, over 5,000 rows of `files` (WAL, one index; same machine and Python, one run each, `commit_bench.py`):

| commit | `synchronous=NORMAL` | `synchronous=FULL` |
| --- | ---: | ---: |
| one per row | 14.5 µs | 39.0 µs |
| one per 50 rows | 2.9 µs | 3.4 µs |
| one per 500 rows | 2.2 µs | 2.0 µs |

Walking 100k files: `fs.walk` 38 ms, `os.scandir` 32 ms, `os.walk` 33 ms. `Path.rglob` with `is_file()`, which `engine._folder_stats` uses, takes 287 ms. `fs.walk` with `entry.stat()` takes 215 ms, and `os.stat` on a known path costs 1.44 µs.

### 2.5 CPU
| work | time |
| --- | ---: |
| `worker.Pool(8)`, a no-op task | 3.8 µs (ThreadPoolExecutor: 2.7 µs) |
| a thread calling a coroutine on the loop thread (`run_coroutine_threadsafe(…).result()`, 20,000 calls, `bridge_bench.py`) | 39.5 µs |
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

Pillow 13.0's wheels encode AVIF (libavif), WebP and JPEG (libjpeg-turbo) in-process, with no tool to install. On a synthetic 12 MP image (a gradient and noise, so again, compare speeds, not sizes; same machine and Python, best of one run each, `avif_bench.py`):

| encode | one image | four on four threads |
| --- | ---: | ---: |
| AVIF, quality 60, speed 6 | 0.12 s | 0.42 s (libavif already uses every core) |
| AVIF, quality 60, speed 8 | 0.16 s | 0.46 s |
| WebP, quality 80, method 4 | 0.77 s | 0.85 s (scales with threads) |
| JPEG, quality 80, optimized | 0.07 s | 0.25 s (doesn't scale with threads) |

`pyoxipng` ships oxipng as a wheel, so PNG optimisation needs no tool either.

### 2.9 Compiled Python and free-threading
| | today | Cython 3.3 (no annotations used) | mypyc |
| --- | ---: | ---: | ---: |
| `job.place`, per file | 13.2 µs | 10.6–10.9 µs (−17%) | — |
| `text.filename` | 4.34 µs | 4.10 µs | 4.21 µs (−3%) |
| `day()` | 0.55 µs | 0.52 µs | — |

On free-threaded 3.14t:
- pure-Python CPU work on 8 threads runs **3.6× faster** (313 ms against 88 ms; on the GIL build, 324 ms against 310 ms);
- **httpx crashes** with 4 or more threads, a race in httpcore's pool (`TypeError: '>' not supported between instances of 'float' and 'NoneType'` in `has_expired`). It reproduced 3 times out of 3. The plan drops httpx, so what matters is whether the new stack (curl_cffi, Playwright, msgspec, selectolax, cryptography) ships free-threaded wheels.

## 3. Where the time goes
| area | cost today | why | fix (section) | gain |
| --- | --- | --- | --- | --- |
| browser feeds | 0.6 s per scroll step + 10 s idle per tab | fixed sleeps; the end detected by silence | event-driven scroll ended by the site's cursor (5.4) | 26 s → ≤ 16 s human-like, ≤ 3 s fast, on the synthetic feed (gates) |
| download concurrency | 4–8 per source | a fixed `Source.concurrency` | adaptive per host, up to 32 per CDN host (5.1) | 12.7 s → ~5 s at 40 ms latency (measured with 16 jobs) |
| pacing | 0.5 s per API request | conservative defaults | per-source pace, adaptive on 429 and blocks (5.1) | site-bound; no change in the worst case |
| startup | 173 ms | eager imports | argparse, lazy commands, a source manifest (5.6) | → ~25 ms (gate ≤ 40 ms) |
| HTTP stack | 0.55 ms per file and GIL-bound | pure-Python httpx | curl_cffi on the loop (5.1) | 2.4× today, 4.5× the 1 GiB rate of today's `shared.http` |
| disk bookkeeping | 276 µs per file | a lock file per write, a stage wrapper | `.part` + rename, the part locked by its own descriptor (5.2) | → ~115 µs |
| the in-page fetch fallback (ins) | a browser round trip per API call, serialized through the tab | Instagram refuses flnd's client; the code suspects the TLS fingerprint (`ins.py:465`, unproven) | a Chrome fingerprint via curl_cffi (5.1), checked live first | API calls leave the tab, if the live check passes |
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
  _db.py           flnd.db: schema, one transaction per post, queries (was shared/_store + paths.table + targets)
  _files.py        .part/rename and the part's lock, sweep, walk, mtime (was shared/_fs)
  _loop.py         the run's asyncio loop thread and the sync bridge (was shared/_worker's Loop)
  _html.py         selectolax helpers + `dig` for JSON paths (was shared/_selector)
  _tools.py        finding, downloading and verifying ffmpeg and 7zz, the only outside tools (was shared/_sh)
  _shrink.py       --compress: plan, encode (Pillow in-process for images, ffmpeg for video), verify, place
  _unpack.py       automatic archive extraction (was shared/_archive)
  _media.py        ffmpeg probes, and ffmpeg runs with progress (joining DASH tracks, compressing video)
  _ui.py           full-screen, inline and plain progress (Rich, lazy), prompts (input/getpass)
  _stats.py        Stats snapshots: counters from the run, psutil for RAM and CPU (lazy)
  _text.py _time.py                file names, templates, days, sizes; the clock and its fake
  _plugins.py      discovery, validation and versioning of external sources, and containing their failures
docs/              guides for people: architecture, library, CLI, writing a source, errors, storage, performance, one page per site (14.6)
examples/          a runnable script and a 40-line plugin with its test
```
A leading `_` marks a private module. Only `flnd`, `flnd.source` and `flnd.testing` are public: the API snapshot lists those three, and nothing else is promised.

### 4.2 Public library, internals, CLI
- **`import flnd`** gives a script `Flnd`, `open`, `Run`, `Target`, `Saved`, `Failed`, `Skipped`, `Report`, `Login`, `Record` (a saved file's row) and the errors. `File` belongs to `flnd.source` alone, where it means a file to download: one word, one meaning. The heavy parts load lazily: `__getattr__` on the package imports `_engine` on first use.
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
| `site.py`'s browser proxies (`_wrap_raw`, a thread hop per DOM call; 184 lines) | sync Playwright on another thread | async Playwright on the loop, behind a few `Site` verbs (5.4) |
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
| new: plugins, testing, compress, extract | — | ~1,800 |
| total | 17,490 | ~13,000 |
| frames per file download, call to disk | 15 | ~6 |

The total shrinks even though compression, extraction, media and plugins are new; the wrappers and parallel layers pay for them. The planned figures count code. Section 14.6's docstrings and comments come on top, by design, so a fair comparison counts code lines (no docstrings, comments or blank lines) on both sides.

## 5. Performance design, area by area
For each area: the change, the evidence behind it, the risk, and what it does to the API.

### 5.1 Network
- **One client: curl_cffi on the run's asyncio loop.**
  - Its `AsyncSession` drives libcurl's multi interface. Measured on 2,000 small files, it is 2.4× httpx, and 4.5× faster than today's per-file path; on 1 GiB, 4.53 GiB/s against 2.63.
  - It speaks HTTP/2, multiplexing a host's transfers over one connection, and reuses DNS, TLS sessions and connections per host. HTTP/3 is opt-in in libcurl (no automatic `Alt-Svc` upgrade), so it stays off.
  - `impersonate="chrome"` sends Chrome's TLS and HTTP/2 fingerprint. Instagram refuses flnd's client today and flnd falls back to sending its API calls from the page (`ins.py:465`), which costs a browser round trip per API call and serializes those calls through one tab. The code's own comment only says Instagram *may* refuse httpx's fingerprint: the fallback triggers on any refused or walled answer. So the fallback stays until a live check on the authorized test account shows the API calls pass from curl_cffi.
  - `AsyncSession(max_clients=10)` is the default and caps the whole session. flnd sets `max_clients` to the sum of its per-host caps, or adaptive concurrency stops at 10.
  - Risk: curl_cffi ships its own libcurl (curl-impersonate), so updates, security fixes included, follow its releases. Wheels exist for macOS, Linux and Windows on x86-64 and arm64.
  - Risk on Windows: Playwright needs the Proactor loop (it starts a driver subprocess), and the Proactor loop has no `add_reader`. curl_cffi then starts a selector thread and warns (`curl_cffi/aio.py`), so every socket event hops threads. Commit 15.9's gate includes a Windows run.
  - API: none; `Site.fetch` keeps its shape.
- **Not pycurl.** On loopback, `CurlMulti` is 2× curl_cffi (127 ms against 249 ms for 2,000 files), but:
  - the difference is 62 µs of CPU per file. At 100 ms of latency, 32 transfers at once make 320 files a second, so curl_cffi's extra cost is about 2% of one core;
  - the benchmark drives `CurlMulti` with a blocking `perform`/`select` loop, which can't share the asyncio loop with Playwright and Telethon. On the loop, pycurl needs the same Python socket callbacks that cost curl_cffi its time;
  - it would mean a second libcurl in the process with no Chrome fingerprint, and a second retry and cookie path.

  Revisit only if a real run's profile shows the loop thread busy more than half the time.
- **Disk writes on the loop.** The transfer callback writes each chunk on the loop thread. A write to the page cache takes microseconds, but a slow disk (a USB drive, a network share) would stall the browser and every transfer with it. Commit 15.9's gate includes a disk throttled to 20 MB/s. If the loop stalls for more than 100 ms there, writes move to a writer thread with 1 MiB buffers, which costs about 40 µs per MiB in thread hops.
- **Two pools per source:**
  - *pages* (the source's own domains): the token bucket, `pace` from the source and the config;
  - *media* (everything else, CDNs): no pace, adaptive concurrency **per host** (AIMD). It starts at 8 and halves on a 429, a 503, a reset or a timeout. It adds 1 per 10 clean transfers, up to 32, but only while the host's throughput still rises. More transfers on a saturated link only make each file slower and hold more memory.

  Evidence: on the e2e run, 4 jobs took 12.7 s and 16 took 5.0 s, which is the pace floor. Nothing above 16 is measured yet, and loopback can't show it; the real-CDN check of commit 15.12 sets the cap. Risk: some CDNs throttle bursts, which AIMD answers. A ban isn't a 429, so the cap stays conservative. API: `Source.concurrency` is replaced by an optional cap, `Source.limits = {"cdn.example": 4}`.
- **Pacing never wastes idle time.**
  - The bucket refills by elapsed time (one token deep), so a slow listing never waits again.
  - A `Retry-After` or `x-rate-limit-reset` sets the bucket.
  - A 429 or a block halves the rate until 20 clean requests.
- **Overlap.** Listing feeds the download queue as posts arrive. The queue has a bound of 4× concurrency, which applies backpressure to the listing, so memory stays flat.
- **Segmented downloads** stay for files of 64 MiB or more on servers with `Accept-Ranges`, now on the loop with no threads. The 26% loopback loss came from threads and truncation, but whether segmenting pays on the real CDNs is unknown: gate it on a real-CDN benchmark, default on only if it wins (section 16). Over HTTP/2, segments are streams on one connection, so they help only against a per-stream throttle.
- **DASH** video and audio download in parallel, then ffmpeg muxes them with `-c copy`.

### 5.2 Disk
- **Write to `<dest>.part`, then `os.replace` into place.** No lock file per file, and no stage wrapper: 276 µs becomes about 115 µs.
- **The part is its own lock.** The downloader opens `<dest>.part` and takes a non-blocking `fcntl.flock` (`msvcrt.locking` on Windows) on that descriptor: one system call, no extra file.
  - A second run that finds a part locked skips that file as "being saved by another run", and the first run writes the row.
  - So two runs can share a home: one terminal syncing while another downloads, or cron firing during a manual run. A home-wide lock would make the second run wait for the first to finish.
  - SQLite already serializes the database's writers, the migration included (it runs in one `BEGIN IMMEDIATE`).
  - Part names keep today's rule: a name that would pass the file system's limit (255 bytes) is shortened, as `fs._stage_name` does now.
- **Resume:**
  - `<dest>.part` is kept on failure;
  - a later run sends `Range: bytes=<size>-` with `If-Range: <etag>`;
  - a 416 or a changed ETag restarts the file from 0 (today this fails for good);
  - Telegram's per-part offsets go in a `.part.json` beside the part, flushed before it is written (as fixed in `8522f68`).
- **The history row is written after the rename**, in the post's transaction (5.3). A crash between the rename and the row leaves a file without a row. A rename is atomic, so a file at its final path is always whole: `flnd check` and the next sync adopt it by its `(target, post, file)` key, so nothing downloads twice.
- **No fsync per file**, since media can be downloaded again. The database relies on SQLite in WAL mode with `synchronous=NORMAL`, so a power cut can lose the last commits but never corrupts the file. On macOS, `PRAGMA fullfsync` and `checkpoint_fullfsync` make SQLite's own syncs reach the disk (`fsync` there stops at the drive's cache). An `F_FULLFSYNC` on one descriptor at the end of a run would only flush that one file, so the plan doesn't use one for media.
- **Preallocation** (`F_PREALLOCATE` or `posix_fallocate`) applies only to segmented downloads, which write at offsets. Not measured; low priority.
- **mtime:** `os.utime(fd)` sets the post's date on the open part just before it closes, and the rename keeps it. There is no second stat. Windows can't rename a file Python still has open, so setting it after the rename wouldn't work there.
- **Walks** use one `scandir` with the `DirEntry` stats it already has. `_folder_stats` moves to `flnd.db`'s sizes, so it never walks: 287 ms per 100k files becomes 0.

### 5.3 CPU and data
- **`flnd.db`, one SQLite file with real columns** (section 10):
  - `files` is indexed on `(target, post_id)`, which also serves lookups by `target` alone, and on `hash` and `path`. Point reads go through prepared statements: 2.5 µs becomes about 2 µs, and batched `IN` lookups for a page of posts about 1.1 µs.
  - Filters run on columns, not `json_extract`: 15.7 ms becomes µs.
- **One transaction per post, and no writer thread.** Measured (2.4): a commit per row costs 14.5 µs, and per 50 rows 2.9 µs a row. A post's files (1 to 20) commit together once the last one lands, so the cost per file is 3–15 µs, under 3% of the engine's 0.5 ms. A 250 ms group-commit thread would save at most 12 µs per file. In return it would need a second connection, a gap in which other connections can't see rows that are already written, and a crash window to explain. The commit runs on the loop thread. The only slow statement is SQLite's automatic WAL checkpoint, every 1,000 pages, so commit 15.11's benchmark writes enough rows to include several.
- **Hashing while streaming.** `hashlib.sha256` runs over each chunk as it is written. It runs at 3.3 GiB/s on the M5's SHA instructions; on an x86 machine without SHA-NI it's nearer 0.5 GiB/s (est.), still faster than the link. That makes cross-target dedupe and `flnd check` cheap, with no extra dependency: blake3 and xxhash are faster but buy nothing at network speed.
- **JSON: msgspec everywhere.** Decoding is 2× faster than `json`, and 4× with typed structs on hot API shapes where the source wants them. orjson brings nothing more.
- **HTML: selectolax (lexbor)**, 2.6–3.3× faster than parsel, and its import is 25 ms against 43 ms. Embedded JSON blocks are found with `css('script[type="application/json"]')` and decoded with msgspec. selectolax has no XPath. The one XPath in the sources, TikTok's visible text (`tik.py:39`), becomes `strip_tags(["script", "style"])` and then `text()`.
- **JSON paths: `dig(obj, "data.user.edges[].node")`**, about 60 lines in `flnd.source`. It replaces jmespath and python-jsonpath, and is 30× faster than today's `selector.jmespath` (649 µs against 19 µs of plain access). It covers what the sources use today, and nothing more:
  - dotted keys and `[]` to flatten a list (`data.highlights.edges[].node`);
  - `a || b`, the first path that finds something (`itemList || storyList`, in tik and fbk);
  - `..` with a test, a search at any depth (`..[__typename=TimelineAppCollectionItem]`, fbk's one JSONPath).
- **Threads, asyncio and processes:**
  - one asyncio loop thread runs network, browser and Telegram;
  - each source's listing runs on its own thread, so a plugin author writes plain code (section 7);
  - CPU-heavy media work runs in ffmpeg subprocesses, and image encodes run in Pillow on a small thread pool, a bounded number at once (5.5);
  - pure-Python CPU work stays small: 0.5 ms per file in all.

### 5.4 Browser
- **Async Playwright on the run's loop, behind a few `Site` verbs.** A sync category can't call async Playwright objects directly, and wrapping each one for it is what `site.py:599-782`'s proxies do today. So a source never gets a Playwright object:
  - `Site` has the browser verbs the sources use today: `open(url)`, `wait(css=…, url=…)`, `scroll(until=…)`, `feed(match)`, `document()` (the HTML as a parsed tree), `evaluate(js)` and `click(css)`;
  - each verb is one bridge call to the loop, measured at 39.5 µs (`run_coroutine_threadsafe`), small against the 0.43 ms CDP round trip;
  - an `async def` category may also use `site.tab`, the raw Playwright `Page`. It is outside the source API's version promise, since Playwright's API isn't flnd's to keep stable.

  So the proxies go, and the plugin API doesn't depend on Playwright's API.
- **One browser per run**, launched on first need (682 ms) and reused by every target and source. Each target gets its own tab, at most 6 open. The pre-warmed next tab stays.
- **Event-driven scroll**, replacing `SCROLL_WAIT`, `SCROLL_PAUSE` and the jiggle:
  1. register the feed's response filter (the source names it);
  2. scroll by one viewport;
  3. wait for the next matching response *or* DOM growth, up to 3 s;
  4. read the cursor from the response, and stop when the site says there is no more (`has_next_page: false`, an empty page, or a repeated cursor);
  5. only if neither a response nor growth comes, keep a short idle window (2 s, not 10 s).

  Gates, on the synthetic feed of 30 batches (26 s today): ≤ 16 s with `scroll = "human"`, the default (30 steps × 0.4 s median pause, plus the responses and the end); ≤ 3 s with `scroll = "fast"`.
- **Human-like scrolling stays the default, and is a setting.** The user asked for scrolling that looks human, to be flagged less as a bot (2026-11-11), so it stays the default:
  - `scroll = "human"`: mouse-wheel events (`page.mouse.wheel`) in uneven steps, not `scrollTo`, which no person produces. Between steps there's a lognormal pause with a 0.4 s median, longer when new media just appeared, and now and then a small scroll back up.
  - `scroll = "fast"` has no pauses. The config sets it per source.
  - At most 2 tabs per site scroll at once with `human`, since many tabs scrolling for one account at once is not how a person browses.

  The speed-up doesn't depend on the pace: it comes from never waiting on silence and stopping on the site's own signal. With `human`, the scroll is the slow part, and that is the price of looking human. The word is `scroll`, not `pace`, because `pace` already means the seconds between page requests (5.1).
- **Response capture over DOM scraping** stays the rule. DOM reads remain only where a site gives nothing else.

### 5.5 Media
- **Encoders:**
  - **Photos:** Pillow in-process (2.8): AVIF at 0.12 s for 12 MP, against 0.30 s for avifenc, with no spawn and no tool to install. The fallbacks are JPEG through libjpeg-turbo with `optimize=True`, and WebP. PNG goes through `pyoxipng`. Pillow's wheels carry every one of these codecs, so photos need no outside tool at all.
  - **Video:** SVT-AV1 preset 8 is about as fast as x264 medium (1.56 s against 1.44 s) and much smaller at equal quality. x265 is the fallback. The preset and the codec are chosen by commit 15.18's measurement on real downloads (section 11), not on `testsrc2`.
  - **Hardware encoders** are not faster on M-series for this work (1.2–1.3 s) and give larger files, so they aren't used.
- **Parallelism:**
  - libavif already spreads one AVIF encode over every core (four at once take 3.5× one), so AVIF runs 2 at a time;
  - WebP scales with threads, so it runs one per core on threads;
  - JPEG doesn't scale with threads, and it is the fast fallback, so it runs 2 at a time;
  - videos encode one at a time with `-threads 0`.

  A slow encode never blocks downloads: a separate bounded queue runs after the history row.
- **Probe once.** One `ffprobe -show_streams -of json` per file at 15.6 ms each, cached in `flnd.db` by `(path, size, mtime)`. Pillow probes images in-process.

### 5.6 Startup
- **argparse in `cli.py`** (22 ms for `--help`, against cyclopts' 83 ms). Help text comes from each command function's docstring. No Rich in `--help`.
- **Lazy everything.** `flnd/__init__` defines `__getattr__`, so `import flnd` costs about 1 ms (est.). The CLI imports `flnd` only when a command runs.
- **A source manifest.** `flnd/sources/_manifest.py` lists each built-in source's code, domains and route patterns. `make manifest` generates it and a test checks it. Resolving `ins:example_user` imports only `flnd.sources.ins`; a URL is matched against the manifest first, and plugin files are imported only when no built-in matches (7.2).
- **Gates:** `flnd --help` ≤ 40 ms (est. 25); `flnd download` start to first request ≤ 150 ms with curl_cffi loaded (est. 110).

### 5.7 UI
- **Plain mode** writes one line per finished file or event, with no Rich (0.07 µs per update).
- **Inline mode** draws a Rich `Live` at 10 frames a second, coalesced. Row updates only set numbers (0.5 µs each), and one frame redraws them all. Rich is imported only in this mode.
- **Full screen is the default on a terminal** (user, 2026-11-16: "the default should be altscreen, with good ui and good info"). It is Rich's `Live(screen=True)` on the alternate screen, drawn at 10 frames a second from the same coalesced rows, so it costs what inline mode costs. Rich is already a dependency. Textual would bring scrolling and widgets, but it is used only if its measured import and frame cost stay under the gates below.
  - **Layout:** a header (totals, throughput, elapsed, ETA when knowable); a targets panel (category, page, posts seen, early-stop state); an active downloads panel (progress, host, speed); a system panel (browser tabs and RAM, flnd's RAM and CPU, ffmpeg and Pillow jobs, free disk); and a tail of warnings and waits, each with its reason ("example.com asked to wait 3 min").
  - **Keys:** `q` stops gracefully (the first Ctrl-C stage), `p` pauses and resumes. A small reader thread puts the terminal in cbreak mode (`termios`, or `msvcrt` on Windows) and restores it on exit.
  - **On exit** the terminal is restored and the summary report is printed to the normal screen, so scrollback keeps it. The three Ctrl-C stages show on screen.
  - **Not a terminal**, `--json` or `--ui plain` give plain lines; `--ui inline` keeps the inline view.
- **Stats are events too** (section 6): the full screen, `--json` and a script read the same `Stats` snapshots, so the UI has no private way to look inside a run.
- **Gates:** `flnd --help` no slower (the UI loads lazily); a frame with 1,000 rows ≤ 5 ms at 10 fps; stats sampling < 1% of one core.

## 6. The library API
```python
import flnd

with flnd.open("~/Media/flnd") as fl:       # Flnd(home); creates the layout on first write
    run = fl.download("ins:example_user", only=["reels"], when="2026", compress=True)
    for event in run:                       # Saved | Skipped | Failed | Progress, as they happen
        match event:
            case flnd.Saved(path=path, post=post): print(path, post.date)
            case flnd.Failed(error=e): print(e, e.hint)
    print(run.report)                       # counts, sizes, failures; also when iteration stopped early

    report = fl.sync().wait()               # every followed target; wait() runs it and returns the Report
    planned = list(fl.download("twt:example_handle", dry=True))   # Planned events: new, saved or gone; nothing written
    fl.targets(source="ins")                # list[Target]: followed targets with counts and last sync
    fl.files(target="ins:example_user", kind="image", since="2026-01")   # list[Record] from flnd.db
    fl.follow("twt:example_handle", only=["posts"]).wait()   # record it for sync, then download it
    fl.unfollow("twt:example_handle")       # stop following; files and history stay
    fl.check(fix=True)                      # history against disk: missing, unknown, wrong size
    fl.retry().wait()                       # the last run's retryable failures, as a new run
    fl.login("ins", "chrome")               # how: chrome | firefox | safari | cookies.txt | qr | ...
    fl.logins                               # list[Login]: state, expiry (a read, so a property)
    fl.add(MySource)                        # a source for this Flnd, from a script
    with fl.site("ins") as site:            # a logged-in Site for scripting
        site.fetch("https://www.instagram.com/api/v1/...").json
```
- **`Flnd` holds resources**: the loop thread, the browser, the database connections. So it is a context manager, and `fl.close()` does the same as leaving the `with`. Leaving it stops any run still going, like a first Ctrl-C, and waits for it.
- **A `Run` starts when it is first iterated or waited on**, like a generator. One nobody starts warns with a `ResourceWarning` when it's dropped, the way a coroutine nobody awaited does.
  - Iterating yields events as they happen, and `wait()` runs it to the end and returns the `Report`. `run.report` holds the counts so far.
  - The listing waits while the script handles an event, so events never pile up in memory.
  - `run.stop()` asks the run to stop gracefully, the same as a first Ctrl-C.
  - From asyncio code, `async for event in run` and `await run` do the same without blocking the caller's loop, since the work already runs on flnd's own loop.
- **Options are keyword-only, with no more than five on a call.** `download` takes `only`, `when`, `limit`, `compress` and `dry`; the rest lives in the config (section 10).
- **`dry=True` is the preview.** `--dry` and `dry=True` are one spelling for one thing; there is no separate `preview` call.
- **No hooks.** A script reacts to events by iterating, and the CLI runs a command per saved file with `--exec` (section 9). A second callback path (`fl.on("saved", …)`) would mean two ways to do one thing, keyed by strings a typo silently breaks.
- **Events are frozen msgspec structs**, so they encode to JSON for `--json` output.
- **Live stats** (user, 2026-11-16: "flnd should have api to know about how many browser tab, ram usage,… also to show on the ui"). While a run is listened to, it yields a `Stats` event once a second, and `run.stats` holds the latest snapshot (`None` before the first). A `Stats` holds:
  - **run:** elapsed, files saved, skipped and failed, bytes, current throughput, ETA when the total is known;
  - **downloads:** files in flight, per host the concurrency and its adaptive cap, queue depth, parts being resumed;
  - **listing,** per target: category, page, posts seen, early-stop state;
  - **pacing:** per source, the next allowed request and why it waits (a site's `Retry-After`, a back-off, the human pace);
  - **browser:** tabs open against the cap, and the RSS of the Chromium process tree;
  - **process:** flnd's RSS and CPU, ffmpeg and Pillow jobs running, free disk on the home's volume.

  The RAM and CPU of a process tree need `psutil` (13.1); it is imported only when stats are first sampled. Sampling runs on the loop, at most once a second, and only while someone iterates the run, so a `wait()` with no reader samples nothing.
- **Errors** raised to a script are `flnd.Error` subclasses (section 8). A failed file is an event, not an exception. A script that wants strictness checks `report.failed`, which keeps `download` at five options.

## 7. The source API and plugins
### 7.1 Writing a source
```python
from flnd.source import Source, Site, Post, File, Target, target, cookies

class Example(Source):
    """example.com: a user's posts and photos, one post by URL, and a search."""

    code = "exm"                       # what targets start with: exm:<user>
    domains = ("example.com",)         # links, cookies, pacing
    api = 1                            # the source API version this was written for
    login = cookies("session")         # logged in when this cookie is there; flnd reads it from a browser
    pace = 0.5                         # seconds between page requests: the default, which the config can override
    folder = "{target}/{category}"     # the default; checked when the class is made

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
        """A search: yields targets, each downloaded as if it had been typed."""
        for hit in site.pages("https://api.example.com/search", params={"q": words}, cursor="next"):
            yield from (Target(f"exm:{u['name']}") for u in hit["users"])
```
[ARCHITECTURE.md](ARCHITECTURE.md) §6 walks through writing a source step by step; this section lists the rules.
- **Only `code`, `domains`, `api` and one route are required.** Everything else has a default: `pace` 0.5 s, `folder = "{target}/{category}"`, `filename = "{post}_{num}"`. A template may use `{source}`, `{target}`, `{category}`, `{post}`, `{num}`, `{date}` and any field of the post.
- **`newest_first=True`** on a route or category says the listing comes newest first, so a sync may stop at old posts. Without it, a sync reads the whole listing, which is slower but never wrong. The source declares this, so the engine doesn't guess.
- **`Target(text)`** takes any target, from any source. A search yields its results this way, and a post that links to another site hands the link on. This is gallery-dl's `Queue`, and it is the one way to make a target: there is no `self.target(...)`.
- **A post's files may be a function** (`Post(id, lambda: fetch_pages(id))`) that runs only if the post is new, so a source never fetches the pages of a post flnd already has. Today's sources do this, and it stays.
- **`File(url, audio=…)`** downloads a video and its separate audio track at once and joins them with ffmpeg without re-encoding. This replaces the shared DASH helper. **`File(url, password=…)`** marks an archive.
- **A source declares what it is.** Its hooks are a few named methods:
  - `@target` routes;
  - `@<route>.category` for the parts of a target;
  - an optional `connect(site)` (async allowed), for a site with its own client, as Telegram has.
- **`login` is one attribute**, never also a method: one word, one meaning.
  - `cookies("session")` covers the cookie sites: logged in when the cookie is there, read from a browser or a cookies.txt.
  - A custom flow (Telegram's QR code and phone number) is a `Login` subclass with `start(site, how)` and `check(site)`, assigned the same way: `login = TelegramLogin()`.
- **What a source gets from `Site`:**
  - `fetch` (paced, retried, fingerprinted);
  - `pages(url, *, params=…, cursor=…, send=…)`, where the engine keeps and resumes the cursor:
    - `cursor` finds the next cursor in a page: a `dig` path (`"next"`, `"data.user.media.page_info.end_cursor"`), or a function from the page to the cursor, or `None` to stop, for a site whose last page still carries one (`has_next_page: false`);
    - `send` says where the cursor goes in the next request: a query parameter's name (the default is `"cursor"`), or a function from the cursor to the params, for GraphQL `variables` JSON;
    - the listing stops on a missing or repeated cursor, or an empty page;
  - the browser verbs of section 5.4: `open`, `wait`, `scroll`, `feed(match)` (captured responses while scrolling), `document`, `evaluate`, `click`, and `site.tab` in async code;
  - `html(text)`, `dig`, `state` (per-source key-value), `warn`, and `pause(seconds, reason)`. That is `pause`, not `wait`, because `wait` already means waiting for the page (`site.wait(css=…)`) or for a run (`run.wait()`).
- **What a source never does:** touch the disk, the history, retries or concurrency.
- **The engine owns the early stops** (5 saved in a row on sync, 5 older than `--when` in a row, on a `newest_first` listing; `stop_after` per source in the config, like gallery-dl's `skip = "abort:N"`) and wall detection, through `Site`'s calls raising `LoginError` or `Blocked` (section 8). The four copies in ins, tik, twt and fbk go.
- **Cursors are resumed only where that is right.** A full download that stopped part-way (the target isn't `complete`) resumes from its last cursor. A sync starts from the newest page, since that is where new posts are.
- **A post** can carry `caption`, `author`, `tags`, `comments` and `extra`, which feed the sidecars and search (section 12). `date` takes a `datetime`, Unix seconds or an ISO 8601 string; the engine parses it once, so no source carries its own date parser.
- **Sync or async.** A category may be `def` or `async def`. Sync code runs on the listing thread, and async code runs on the loop. Both get the same `Site`.
- **Mistakes are caught when the class is made.** A bad pattern, an unknown folder field, a missing `code`, a duplicate route, or a category on a route that yields posts each raise `SourceError` naming the class, the attribute and the fix, e.g. `exm.folder: unknown field {usr}; fields are source, target, category, post, num, date, user`.

### 7.2 Plugins at runtime
- **Discovery, in order:**
  1. the built-ins;
  2. installed packages declaring `[project.entry-points."flnd.sources"] exm = "flnd_example:Example"`;
  3. the user's plugin folder: `$FLND_SOURCES`, else `~/.config/flnd/sources/` (`%APPDATA%\flnd\sources\` on Windows), holding `<code>.py` files and `<code>/` packages;
  4. `fl.add(cls)` in a script.

  A later one with the same `code` is refused with a message, never silently shadowed.
- **Never from a home.** The home defaults to `./.flnd` in the current folder, which may be a folder someone else made: a cloned repository, an unpacked archive, a shared drive. Loading `<home>/sources/*.py` would run whatever code that folder holds the moment flnd starts there. A plugin is code that runs with the user's logins, so it is installed the way any code is: into the user's own folder, or with pip.
- **Loading is lazy, with nothing declared twice.** A file plugin's code is its file name (`exm.py` is `exm`), and an entry point's code is its name. `ins:…` style targets therefore find their plugin without importing anything. A link needs the plugin's route patterns, so plugin files are imported only when no built-in route matches it, and when `flnd sources` runs. Importing a small file takes about a millisecond. `--help` never imports one.
- **Versioning.** The source API has a major version, `flnd.source.API = 1`. A plugin declares `api = 1`.
  - A plugin built for a newer major version is refused: `exm needs source API 2; this flnd has 1: update flnd`.
  - One built for an older major version is refused with a link to the migration notes.
  - Minor additions stay compatible.
- **Containment, not isolation.** A plugin runs in flnd's process. A broken plugin can't take the run down:
  - an import or validation error disables that source alone, with a warning and the file and line;
  - an exception inside a listing fails that target alone, and is classified (section 8) with the source named;
  - sync code runs on the listing thread, so it can't block the loop, and an async category that doesn't yield for 10 s gets a warning.

  It is not a security boundary, and the docs say so: a plugin can do anything the user can. Process isolation wouldn't change that, because a plugin needs a logged-in `Site`, and a `Site` can send the user's cookies anywhere the plugin asks. Process isolation also costs a round trip per call. So it isn't planned (section 12, #24). The safeguard is where plugins load from (above).
- **The test kit (`flnd.testing`):**
  - `FakeSite(json=…, html=…, pages=…, feeds=…)` answers by URL;
  - `collect(Example, "exm:example_user", json=FIXTURES)` returns what would be saved: paths, keys and dates;
  - `check(Example)` validates the class and resolves every example in its route docstrings;
  - a `fake_clock`;
  - `examples/plugin/` is a 40-line plugin with its test.
- **The CLI:** `flnd sources` lists every source with its origin (built-in, package or file), version and login state, and the reason a plugin was disabled. `flnd ls` and `flnd login` include plugin sources.

## 8. Errors
### 8.1 The hierarchy
```
flnd.Error                      every error flnd raises; str(e) says what went wrong in one line,
│                               .hint what to do (a command when there is one), .target, .retryable
├── UsageError                  bad target, flag or config value → exit 2, no traceback
├── LoginError                  not logged in, expired, refused   → that source stops; others go on (exit 3 at the end)
├── SiteError                   the site answered, but not usefully
│   ├── Blocked                 rate limited or temporarily blocked: retried after its wait, then reported
│   ├── NotFound                deleted, private, or never existed: recorded as gone, not retried
│   └── Changed                 the response no longer has the expected shape: reported with a saved sample
├── NetworkError                DNS, TLS, timeout, reset: retried with backoff; then a failure
├── DiskError                   no space, permission, name too long: the run stops cleanly
├── ToolError                   ffmpeg, 7zz and the like: missing (with an install offer) or failed (last stderr lines)
└── SourceError                 a source or plugin is broken: load, validation, or a bug in a listing

flnd.Stopped(KeyboardInterrupt) Ctrl-C or run.stop(): not a failure, and not an Error; exit 130
```
- **The message is the error's `str()`.** A separate `.why` would say the same thing twice. The fix is `.hint`, not `.retry`, because `retry` is already a verb: `fl.retry()` and `flnd retry`.
- **`Stopped` is not an `Error`.** `except flnd.Error` must never swallow a Ctrl-C. Deriving from `KeyboardInterrupt` lets code that already handles Ctrl-C handle it too.
- **`.retryable` is set by the policy below, not implied by the class.** A `NetworkError` from a DNS name that doesn't exist won't heal on a retry, and a 403 may be a login wall or a block.

### 8.2 The policy
| failure | retry | then |
| --- | --- | --- |
| a timeout, a reset, a 5xx | 4 tries with jittered exponential backoff (1, 2, 4, 8 s), on the same mirror and then the next | a `Failed` event; kept for `retry` |
| a 429 or a block page | the `Retry-After` wait or the site's reset (up to 1 h), then the adaptive rate | `Blocked` after 3 rounds |
| a 404 or 410, a private item | none | `gone` in `flnd.db`; never asked again unless `--again` |
| a 401 or 403 on the source's domains, a login wall | one cookie refresh from the browser | `LoginError` for that source |
| a truncated transfer | resume from the `.part` | restart from 0 on a 416 or a new ETag |
| a parse failure | none | `Changed`, the response saved to `logs/<run>/samples/`, scrubbed (below) |
| disk full | none | stop listing, finish the in-flight files' rows, exit 4 with the folder named |

- **No data is lost:**
  - a file is visible only after its rename;
  - a row is written only after its file;
  - parts and offsets survive a failure, so resume is exact;
  - the database is WAL;
  - every crash leaves either the old state or the new one.
- **The failure report.**
  - Each failure is a row of `flnd.db`'s `failures` table: its target, post, file, class, message, hint, attempts and the redacted URL. That is the only record, so the report and `retry` can't disagree. `flnd status --json` prints it for other tools.
  - The end of a run prints a short summary:
    ```
    ✗ 3 files failed: 2 network (retry: flnd retry), 1 gone. Details: flnd status
    ```
  - `flnd status` shows the last run.
  - `flnd retry` lists each failed post again and downloads the files that failed. It works from `(target, post, file)`, not the stored URL. That URL is redacted, and a CDN's signed URL expires within hours anyway.
- **Ctrl-C has three stages:**
  1. the first stops listing and finishes the in-flight downloads (showing "stopping…, Ctrl-C again to stop now");
  2. the second cancels the transfers, keeps the parts, flushes the rows and closes the browser;
  3. the third exits at once.

  Every stage leaves a resumable state, and `Stopped` exits 130.
- **No tracebacks for user errors.** `UsageError`, `LoginError`, `DiskError` and `ToolError` print one line and what to do. An unexpected exception prints one line plus the path of a crash file with the traceback (`logs/crash-<run>.txt`), and exits 4; `-v` prints it inline.
- **Secrets never reach a message.**
  - URLs are redacted (user info, query and fragment), headers are scrubbed by name, and cookies are never logged.
  - A saved sample of a changed response is scrubbed too: values under keys that look like secrets (`token`, `csrf`, `session`, `cookie`, `auth`, `password`, `key`) become `"…"`. A login response is never saved. Samples are swept with the logs after 30 days.
  - A test greps every log, sample and error message produced by the test suite for the fixtures' secret values.

## 9. The CLI
The user's current leanings were the `target` and `auth` groups, `--from`, `--when` and `--compress`, with inline UI, `.flnd` and quiet output as defaults. They are kept where good, except the UI: on 2026-11-16 the user made full screen the default. The `media` group (`show`, `card`, `album`, with `--by` and `--style`) was removed on 2026-11-16. Groups are flattened, because each verb is unique and shorter to type, as in git, uv and cargo. Flat verbs must not differ by a letter, so `login` with no source lists the logins, and there is no `logins`.

```
flnd download <targets…> [--only CATS] [--when WHEN] [--limit N] [--compress] [--dry] [--exec CMD]
flnd follow <targets…> [--only CATS] [--when WHEN] [--compress]   follow, and download what's there now
flnd sync [targets…] [--compress] [--exec CMD]                    what's new for followed targets
flnd unfollow <targets…>             stop following; files and history stay
flnd ls [targets…] [--json]          followed targets: files, size, last sync, failures
flnd status [--json]                 the last run: what failed and why
flnd retry                           retry the last run's retryable failures
flnd check [--fix]                   history against disk: missing, unknown, wrong size
flnd login [source] [how]            how: chrome | firefox | safari | cookies.txt | qr | phone; no source: list logins
flnd logout <source>
flnd sources                         built-in and plugin sources, their categories, versions, why one is disabled

global: --home DIR (default: $FLND_HOME, else ./.flnd) · --ui full|inline|plain (default: full on a terminal) · -v · --json
```
- **`--only` replaces `--from`.** "From" read like a source or a date, and `--only reels,posts` says what it does.
  - Without it, a target gets every category, other people's content included. The user decided this on 2026-11-12, after trying the account's own content as the default.
  - So there is no `--all`: leaving `--only` out already means everything.
  - `--only` on a single-post target is a usage error before the run, as decided then.
- **`--when`** takes `2026`, `2026-06`, `2026-06-15`, `2026-01..2026-03` or `30d`.
- **Following is a verb, not a side effect** (section 16, question 13).
  - `download` takes what's there once. `follow` records the targets with their options, then downloads them like `download`. `sync` brings every followed target, or the ones named, up to date. `unfollow` stops.
  - A one-off download never grows every later sync, and a single post or a search's results can't be followed by accident.
  - This keeps the user's opt-in design of 2026-11-13 (`download --sync`), as a verb.
- **Archives extract by themselves.** `--compress` shrinks files as they land, and on `sync` it also shrinks files saved earlier.
- **`--exec CMD`** runs a command per saved file, without a shell. The command is split once with `shlex`, and `{path}`, `{target}` and `{post}` are substituted inside each argument. The same values are also in `FLND_PATH`, `FLND_TARGET` and `FLND_POST`. A file name comes from the site, so it must never reach a shell.
- **`--json`** turns the event stream into JSON lines, for scripts and cron. It implies `--ui plain` and never prompts.
- **Settings resolve in one order:** a flag, then the environment (`FLND_HOME`, `FLND_SOURCES`), then `config.toml`, then the default.
- **Exit codes:** 0 ok, 1 some files failed, 2 usage, 3 login needed, 4 flnd stopped on an error (disk full, a missing tool, a crash), 130 stopped by Ctrl-C. A cron job can tell "some files failed" from "flnd itself broke".

## 10. Storage and the home folder
```
<home>/
  flnd.db            files, posts, targets, cursors, logins (metadata only), runs, failures, probes
  secrets.db         the encrypted vault: cookies and tokens per source (AES-256-GCM)
  secret.key         the vault's key (mode 0600), unless the config moves it to the OS keychain
  config.toml        defaults and per-source settings (tomllib, stdlib)
  media/<source>/<target>/…   downloaded files; the folder templates decide below
  cache/             ffmpeg probes and anything else safe to delete (swept after 30 days)
  logs/              each run's log, scrubbed samples of changed responses, crash files (swept after 30 days)
```
There is no `sources/` (plugins load from the user's folder, 7.2) and no `.lock` (each part locks itself, 5.2).
- **The key stays in a file by default**, as today (`metadata/secret.key`). A keychain is safer on a desktop, but a cron job on macOS, or a headless Linux box without a Secret Service, often can't reach one, and unattended `sync` is a main use. `[vault] key = "keychain"` moves it there, and the README says to keep the home as private as the logins in it.
- **`files`:** `key PRIMARY KEY, target, source, post_id, path, size, mtime, date, kind, hash, state, shrink, unpacked, before, format, tools, sidecar`, indexed on `(target, post_id)`, `hash` and `path`.
  - `state` is where the file stands with the site: `saved`, `gone` or `failed`.
  - `shrink` is what `--compress` did: empty, `compressed`, `kept` (nothing smaller passed) or `failed`.
  - `unpacked` is set on an archive once extracted.

  These are three questions, so they are three columns. One `state` holding `saved|gone|compressed|kept|extracted|failed` would have to drop one answer to record another.
- **`targets`:** `key, source, value, follow, options (JSON: only, when, compress), complete, cursors (JSON), last_sync, files, bytes`.
- **`failures`:** `run, key, target, post_id, file, url (redacted), class, message, hint, attempts, at`.

**`config.toml`:**
```toml
[defaults]
ui = "full"          # full | inline | plain; plain when not a terminal
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
- The old files are read, never changed or deleted, so the previous flnd still runs on that home if the user goes back. `flnd.db` records that the migration ran, and the README says when the old files can be removed.
- The converter is about 150 lines and is deleted one release later.

## 11. Compression and extraction
- **The steps after a file lands** run in one fixed order (ARCHITECTURE.md §8), each on or off by its flag or setting:
  `download (hashing) → dedupe → unpack → compress → date → place → record → sidecar → exec → Saved`.
  A fixed order means steps can't be combined in an order that breaks, such as compressing a file before dedupe has linked it. It is gallery-dl's post-processor chain without the freedom to reorder, and it isn't a plugin API until a real need appears.
- **`--compress`** is the compress step:
  1. *Plan* the candidates from the probe. Every image codec is Pillow's, in-process (2.8):
     - JPEG goes to AVIF, then an optimized JPEG (libjpeg-turbo), then WebP;
     - PNG goes to oxipng (`pyoxipng`), then lossless WebP for flat art, then AVIF for photos;
     - animated GIF goes to animated WebP;
     - video goes to SVT-AV1 10-bit through ffmpeg, with Opus at 96k, `+faststart`, and the metadata copied. The preset is the one commit 15.18 measures; preset 8 was the measured one (2.8). x265 is used when SVT-AV1 is missing, or when the config asks for it (below);
     - audio goes to Opus.
  2. *Encode* into `<dest>.part`.
  3. *Verify*:
     - the result decodes;
     - its dimensions, frames and duration match, within ±0.1 s;
     - an image keeps SSIM ≥ 0.98. Pillow has no SSIM, so this is ffmpeg's `ssim` filter, one spawn per image (15.6 ms or more, about a tenth of an AVIF encode);
     - it saves at least 5%.

     If a candidate saves 40% or more, stop there; otherwise keep the smallest that passed.
  4. *Place*: same suffix, replace; new suffix, write the new file and move the row in one transaction, then remove the old file. A file hard-linked by dedupe is compressed once, and the result is linked at every path that shared its hash, in the same transaction.
  5. *Record* `compressed`, `kept` or `failed` in `shrink`, with the tools string, so a tool upgrade retries `failed` and `kept`.
- **Measured on real downloads before the defaults are fixed.** The originals from these sites are already compressed, so re-encoding loses a generation of quality, and it replaces the original.
  - `testsrc2` and a synthetic photo can't show what that buys. Commit 15.18 measures savings and SSIM on 100 photos and 20 videos from the authorized test accounts, kept outside the repo.
  - A kind of file whose median saving is under 15% is left alone by default.
- **AV1 doesn't play everywhere.** Apple devices decode it in hardware only from 2023 (A17 Pro, M3), and older iPhones and Macs may not play it in Photos or QuickTime (est.; checked on the user's devices before the default is fixed). `[compress] video = "hevc"` picks x265 instead. The default is section 16, question 14.
- **Tools.** Images need no tool, since Pillow and `pyoxipng` are dependencies. Only ffmpeg (video, probes, DASH joins) and 7zz (archives) come from PATH or are offered for download. Each download is pinned by checksum and cached in `~/.cache/flnd/tools`. Two tools on 6 platforms is a far smaller supply chain to keep pinned than five.
- **Archives extract by themselves.**
  - A source may mark a file `File(url, password=…)`; loi does.
  - The archive extracts beside itself into a folder of its name. The limits on size, ratio and file count stay, links are refused, and `--` comes before paths (as fixed in `daa2354`).
  - The archive is kept, unless `--compress` is on, in which case it is removed after a verified extract.
  - A wrong password or a damaged archive keeps the archive and records `failed`.
## 12. Features, ranked
Value and cost are on a scale of 1 to 3. The MVP is what the first release ships.

| # | feature | value | cost | API sketch | cut |
| ---: | --- | :---: | :---: | --- | :---: |
| 1 | download, follow, sync, unfollow, ls | 3 | — | section 9 | MVP |
| 2 | logins from Chrome, Firefox and Safari, cookies.txt, QR and phone; expiry warnings | 3 | 1 | `flnd login ins chrome` | MVP |
| 3 | the failure report, `status`, `retry` | 3 | 1 | `fl.retry()` | MVP |
| 4 | plugins (file, entry point, `fl.add`) + the test kit | 3 | 2 | section 7 | MVP |
| 5 | `--compress` + automatic extraction | 3 | 2 | section 11 | MVP |
| 6 | config file: per-source pace, limits, proxy, scroll style | 3 | 1 | `config.toml` | MVP |
| 7 | `check [--fix]`: integrity and repair, adopting orphans | 3 | 1 | `fl.check(fix=True)` | MVP |
| 8 | dedupe across targets by content hash (hard-link or skip) | 2 | 2 (compress and `check` must keep the links) | `[defaults] dedupe = "link"` | MVP |
| 9 | metadata sidecars: post JSON, captions | 2 | 1 | `[defaults] sidecars = true` → `<file>.json` | MVP |
| 10 | events for scripts; `--exec CMD` per saved file, with no shell | 2 | 1 | `for event in run`; `--exec 'cmd {path}'` | MVP |
| 11 | `--dry` as a diff: new, already saved, gone | 2 | 1 | `fl.download(…, dry=True)` | MVP |
| 12 | `show`, `card`, `album` | — | — | — | removed (user, 2026-11-16) |
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
| 23 | the full-screen UI as the default on a terminal (user, 2026-11-16) | 3 | 2 | `--ui full` | MVP |
| 28 | live stats: tabs, RAM, CPU, throughput, waits, as `Stats` events and `run.stats` (user, 2026-11-16) | 3 | 1 | `run.stats` | MVP |
| 24 | process isolation for untrusted plugins | 1 | 2 | — | no: a plugin needs a logged-in `Site`, so a process boundary wouldn't protect the logins (7.2) |
| 25 | motion, loops, recaps, smart crop | — | — | — | removed with `show` (user, 2026-11-16) |
| 27 | a fallback to yt-dlp for single video links no source handles | 2 | 1 | a separate `flnd-ytdlp` plugin package | later (ARCHITECTURE.md §12) |
| 26 | free-threaded build, if the dependencies support it | ? | 1 | — | later |

## 13. Dependencies, native code and compilation
### 13.1 Dependencies
| package | role | verdict | why |
| --- | --- | --- | --- |
| `curl_cffi` | the HTTP client | **adopt** | 2.4× small files on the loop, 1.7× large files, HTTP/2, a Chrome fingerprint; replaces httpx, h2, httpcore and anyio |
| `playwright` | the browser | keep | the only practical way to drive Chrome; async API from now on |
| `msgspec` | JSON, records, events | keep | 2–4× `json` |
| `selectolax` | HTML | **adopt** | 2.6–3.3× parsel; replaces parsel, lxml, cssselect, w3lib, python-jsonpath and jmespath |
| `cryptography` | the vault | keep | AES-GCM |
| `rich` | the full-screen and inline UI | keep, lazy | imported only by those two UIs |
| `psutil` | RAM and CPU of flnd and the browser's process tree, for `Stats` | **adopt** | the stdlib can't measure a child process tree portably; a wheel on every platform; imported on the first sample |
| `Pillow` | image compression (AVIF, WebP, JPEG) and image probes | **adopt**, in the core | in-process, no avifenc, mozjpeg or caesiumclt; imported only when used |
| `pyoxipng` | PNG optimisation | **adopt**, in the core | oxipng as a wheel, so no tool download |
| `telethon` + `cryptg` + `qrcode` | Telegram | keep (the `telegram` extra) | |
| `httpx`, `parsel`, `python-jsonpath` | | **remove** | replaced above |
| `questionary` | prompts | **remove** | 29 ms of import; `input`/`getpass` cover numbered choices |
| `cyclopts` | the CLI | **remove** | 83 ms for `--help` against argparse's 22 ms |
| `filelock` | locks | **remove** | the part's own lock is a few lines with `fcntl`/`msvcrt` |
| `orjson` | | no | msgspec decodes as fast; one JSON library |
| `niquests` | | no | fast on large files, slower on small ones; 67 ms import |
| `pycurl` | bulk downloads | **no** | 2× curl_cffi on loopback, but only from a blocking loop; 62 µs per file is noise at real latency; a second libcurl with no fingerprint (5.1) |
| `rnet` | | no | its 0.x blocking API failed to import as documented; too young |
| `blake3`, `xxhash` | hashing | no | SHA-256 is hardware-accelerated at 3.3 GiB/s, above any link |
| `uvloop` | | no | the loop isn't a bottleneck, and there's no Windows support (not measured) |
| `pillow-simd`, `pyvips` | | no | pillow-simd lags CPython releases; pyvips was for albums, which are gone |
| `gallery-dl` (as a library) | site code | **no** | GPL-2.0 would bind flnd's license; synchronous, on `requests` (ARCHITECTURE.md §2) |
| `yt-dlp` | video links no source knows | **later, as a plugin** | public domain, so reusable; kept out of the core (feature 27) |

Direct runtime dependencies drop from 13 to 12:
- the core holds 9: curl_cffi, playwright, msgspec, selectolax, cryptography, rich, psutil, Pillow and pyoxipng. Compression is built in, so its codecs are too; each is imported only when used, so they don't slow startup;
- the `telegram` extra holds telethon, cryptg and qrcode.

The installed tree loses about 20 transitive packages (est.). The dev group holds ruff, pdoc and pyright (14.3).

### 13.2 A Rust extension (PyO3 and maturin)
**Not now.** After the changes above, no CPU hot path remains:
- the engine costs 0.5 ms per file, of which 13 µs is `place`;
- every heavy loop is already C: libcurl, sqlite, msgspec, lexbor, hashlib, ffmpeg.

The cost of an extension would be maturin builds, wheels for 5 or more platforms in CI, and slower contributor setup. **Revisit if** a measured profile shows ≥ 20% of a real run's CPU in pure-Python flnd code.

### 13.3 Cython, mypyc and Nuitka
**None.**
- Cython compiled `job.py`, `source.py` and `_text.py` unchanged, and saved 17% on `place` (13.2 to 10.7 µs per file) and 0–5% elsewhere. mypyc saved 3% on `filename`. Both speed up the slice that is already the smallest.
- Cython without type declarations is its worst case. With them, `place` might run 3× faster (est.), but that would save about 9 µs of a file's 0.5 ms. The verdict holds at any plausible speed-up.
- Plugins would still load against a compiled core (both keep Python-level classes), but every platform would need compiled wheels, and debugging would be harder.
- Nuitka only matters for distribution. PyInstaller keeps building the one-file binary; switch to Nuitka only if PyInstaller's start time (unpacking) becomes a complaint.

### 13.4 Free-threaded CPython 3.14t
**Later.** It runs CPU-parallel Python 3.6× faster. But the native dependencies (curl_cffi, Playwright's driver, cryptography, msgspec, selectolax, Pillow) need free-threaded wheels, and flnd's CPU-heavy work runs in subprocesses and C anyway. httpx's crash on it no longer matters, since httpx goes. Revisit when every dependency ships `cp314t` wheels, with the e2e benchmark as the gate.

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
docs:     ; $(RUN) pdoc -d google flnd flnd.source flnd.testing -o build/docs
type:     ; $(RUN) pyright flnd/__init__.py flnd/source.py flnd/testing.py
try:      ; $(RUN) flnd --home .try $(ARGS)
dist:     ; $(RUN) --group dist pyinstaller --onefile --name flnd flnd/__main__.py
clean:    ; $(RUN) python -c "import shutil; [shutil.rmtree(p, True) for p in ('build', 'dist', '.try')]"
.PHONY: setup test lint check api manifest bench docs type try dist clean
```
- **Each recipe is one `uv` command**, so it runs the same in bash, zsh, PowerShell and cmd. There is no `rm`, no `&&` chain and no shell globbing.
- **`PY` is the version the recipes use**, 3.14 by default. `requires-python` stays `>=3.11`, so `make check PY=3.11` runs before a release, so the declared floor is a tested promise.
- **`docs` passes `-d google`**, so pdoc renders the `Args:`, `Returns:` and `Raises:` sections as sections, not as plain text.
- **Ruff's rule set is small and named** in `pyproject.toml`: `F` (undefined and unused names), `E9` (syntax), `B` (bugbear), `UP` (old syntax for the floor version) and `I` (import order).
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
- **`tests/typing/` goes, and pyright leaves `check`.** Ruff (lint, about 50 ms) catches the mistakes a type checker found most often: undefined names, unused imports, bugbear patterns.

### 14.3 Type hints
- Annotate the public API (`flnd`, `flnd.source`, `flnd.testing`) for editors and readers; plugin authors get completion.
- Annotate internals where an annotation explains something.
- No overloads or Protocols written only for a checker, and no `TypeVar` gymnastics.
- **The package ships `py.typed`**, so a plugin author's editor and checker use these hints.
- **`make type` runs pyright on the three public modules only.** It is informational: not in `check`, never a gate, as the brief asks. A wrong hint on the public API misleads every plugin author's editor, which is worse than no hint, so the reader-facing hints get an occasional check.

### 14.4 Benchmarks (`make bench`)
- `tests/bench/` holds the scripts from section 2 as one runner:
  - `startup` (help, import);
  - `http` (loopback server, 1 GiB, 2,000 files);
  - `disk` (5,000 writes);
  - `db` (100k rows, and commits per row, per post and per 500 rows, past several WAL checkpoints);
  - `walk`;
  - `cpu` (JSON, HTML, hash, the loop bridge);
  - `e2e` (the faked site with latency and pace);
  - `browser` (the synthetic feed with `scroll` human and fast; skipped without Chromium);
  - `media` (Pillow's encoders always; ffmpeg's skipped without ffmpeg).
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
   - `ARCHITECTURE.md`, at the repository's root (written 2026-11-16): the parts, one download traced from the CLI to the disk, the threads and the loop, and writing a source; `docs/README.md` links to it;
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
   - **`Args:` says what each argument means**: its unit, its range, what `None` does. It doesn't restate the type, since the annotation already says that, and two copies of a fact drift apart.
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
  - every `python` block in `docs/*.md`, and every `Example:` in a public docstring, runs against the fakes, like a doctest, so an example can't rot;
  - every CLI flag that argparse knows appears in `docs/cli.md`, and every `docs/` link resolves;
  - every built-in source has its `docs/sources/<code>.md`.
- `make docs` builds an HTML API reference with pdoc, a dev-only dependency, into `build/docs/`.

**Cost.** There is more prose to maintain, which is what the test above is for. Comments also make files longer, but a reader's time is the scarcer resource.

## 15. Migration: ranked commits with gates
Each commit is one logical change with `make check` green, and it documents what it touches: its docstrings, its comments and its `docs/` page. A performance commit quotes `make bench` before and after in its message. Mechanical moves go to a low-cost model.

| # | commit | gate |
| ---: | --- | --- |
| 15.1 | `chore:add make bench and the benchmark suite` (section 2's scripts in `tests/bench/`) | baseline recorded in `gates.json` |
| 15.2 | `docs:add the docs folder, the glossary and the docs test` (14.6) | `tests/test_docs.py` runs, with an allowlist of today's gaps that may only shrink |
| 15.3 | `refactor:fold shared into flnd's private modules` (moves only; imports updated) | tests and pyright green; `make bench` unchanged ±5% |
| 15.4 | `chore:replace scripts with the makefile and drop the type gate` (14.1–14.3) | `make check` green without pyright; `make type` runs |
| 15.5 | `refactor:raise one error hierarchy and classify every failure` (8.1, 8.2) | a test per class and per policy row; no traceback in any usage-error test |
| 15.6 | `feat:add the library api with runs and events` (6) | every CLI command expressible through it; an unstarted `Run` warns |
| 15.7 | `perf:rebuild the cli on argparse over the library, with a source manifest` (9, 5.6) | `flnd --help` ≤ 40 ms (from 173); every flag in `docs/cli.md`; an exit code test per class |
| 15.8 | `refactor:run the browser on one asyncio loop behind site verbs` (5.4) | browser bench no slower; the sources' browser tests green; a run on Windows |
| 15.9 | `perf:fetch with curl_cffi on the loop` (5.1) | 2,000 × 100 KiB ≤ 300 ms (from 1,009); 1 GiB ≥ 4 GiB/s (from 1.9); a run on Windows; the loop never stalls over 100 ms on a disk throttled to 20 MB/s |
| 15.10 | `perf:write downloads to a locked part and rename` (5.2) | ≤ 130 µs of disk bookkeeping per file (from 276); two runs on one home save each file once |
| 15.11 | `refactor:store history, targets and runs in one flnd.db, and migrate old homes` (10) | point read ≤ 2.2 µs; ≤ 15 µs of commits per file; sync of 100k with nothing new ≤ 0.3 s; the old files unchanged after the migration |
| 15.12 | `perf:adapt download concurrency per host` (5.1) | e2e 500 posts at 40 ms ≤ 5.5 s at the default (from 12.7); a real-CDN check on the test accounts sets the cap |
| 15.13 | `perf:drive feeds by responses and end on the site's cursor` (5.4) | synthetic feed ≤ 16 s human-like, ≤ 3 s fast (from 26) |
| 15.14 | `refactor:move the seven sources to source api 1` (7.1) | each source's fixture tests green; a live run per source, by hand; the Instagram in-page fallback checked live |
| 15.15 | `perf:parse html with selectolax and paths with dig` (5.3) | Meta page ≤ 8 ms (from 18) |
| 15.16 | `feat:add status, retry and the failure report` (8.2) | `retry` works from `(target, post, file)`; a test that samples are scrubbed |
| 15.17 | `feat:load sources from the plugin folder, packages and scripts, with the test kit` (7.2) | the example plugin's tests pass; a broken plugin disables only itself; nothing loads from a home |
| 15.18 | `feat:compress media and extract archives as files land` (11) | 1,000 JPEGs ≤ 2 min; `sync --compress` over 100k done files ≤ 3 s; savings and SSIM on the real samples recorded in `docs/performance.md` |
| 15.19 | `feat:add check, dedupe, sidecars, exec and json events` | per-feature tests; `--exec` never starts a shell |
| 15.20 | `feat:add live stats and the full-screen ui` (5.7, 6; features 23 and 28) | `Stats` events and `run.stats` from a faked run, each field tested; `flnd --help` no slower; a frame with 1,000 rows ≤ 5 ms; sampling < 1% of one core; the terminal restored after `q`, Ctrl-C and a crash; the look checked by hand on a real terminal |
| 15.21 | `docs:document every module, function and source for human readers` (14.6) | `tests/test_docs.py` passes with an empty allowlist |
| 15.22 | `docs:rewrite the readme for the rebuilt flnd` | every README example runs in `test_docs` |

Why this order:
- **15.3 is a pure move**, so every later commit's diff stays readable. Pyright leaves only after it (15.4), so the type checker still guards the biggest move.
- **The errors and the library API come before the CLI and the network.** The CLI is built on the library (15.7), and the network layer raises the new errors (15.9), so neither is written twice.
- **The biggest measured wins follow** (15.7–15.13). 15.8 and 15.9 split the old single "one loop with curl_cffi" step: the loop and the browser move first, the HTTP client second. Each can then be benchmarked, and reverted, alone.
- **15.14 changes all seven sources at once**, because source API 1 breaks every source together, and there are no aliases to bridge it. Its mechanical parts go to a low-cost model, and the live runs are done by hand.
- **Live stats and the full screen come after the features** (15.20), because they report on every part: browser tabs, per-host caps, waits, compression jobs. Until then 15.7's CLI keeps the inline and plain views.
- **Each commit documents what it touches**, and 15.21 covers whatever no commit touched.

## 16. Decisions
The user took every recommended answer below on 2026-11-16. They are decisions now, not open questions.

1. **Flat verbs** (`flnd ls`, `flnd login`, `flnd follow`) or the groups (`flnd target ls`, `flnd auth login`)? *Recommended: flat*, because every verb is unique. `login` with no source lists the logins, so no two verbs differ by one letter.
2. **`--only` instead of `--from`?** *Recommended: yes.* Without it a target gets every category, as the user decided on 2026-11-12, so there is no `--all`.
3. **Scroll default `human` or `fast`?** *Recommended: `human`*, as the user asked on 2026-11-11, with the event-driven loop; `fast` per source in the config.
4. **Default concurrency per CDN host?** *Recommended: start at 8, and adapt up to 32 while throughput still rises.* The real-CDN check of 15.12 confirms the cap.
5. **Segmented downloads by default?** *Recommended: on for files of 64 MiB or more only after a real-CDN benchmark shows a gain*; off until then.
6. **Migrate old `.flnd` homes?** *Recommended: yes, automatically on first open,* for one release, reading the old files and never changing them.
7. **Dedupe across targets: hard-link, skip or keep both?** *Recommended: hard-link* (no extra space, both paths stay true). Skip on file systems without hard links.
8. **Sidecars on by default?** *Recommended: off*; on in the config.
9. **The home default:** `./.flnd` (today) or `~/flnd`? *Recommended: `$FLND_HOME`, else `./.flnd`.* That keeps today's behaviour and suits cron.
10. **Ruff as the only lint, and pyright only as an informational `make type`?** *Recommended: yes*, with the rule set of 14.1.
11. **Where do plugin files load from?** *Recommended: the user's folder (`$FLND_SOURCES`, else `~/.config/flnd/sources/`), never `<home>/sources/`.* A home is often the current folder, and code there may not be the user's own (7.2).
12. **The vault's key: a file (today) or the OS keychain?** *Recommended: the file by default*, with the keychain as a setting, so unattended syncs keep working (10).
13. **Following: a verb, or a side effect of `download`?** *Recommended: the verb*, `flnd follow`. It keeps the user's opt-in design of 2026-11-13, and a one-off download never grows every later sync (9).
14. **The video codec for `--compress`: AV1 or HEVC?** *Recommended: AV1*, with `[compress] video = "hevc"` for devices that can't play AV1, confirmed on the user's own devices during 15.18. The user's 2026-11-11 rule, H.265 only when asked, fits this.
15. **Pillow and pyoxipng in the core install, with no `media` extra?** *Recommended: yes.* Compression is built in, and both are imported only when used (13.1).
16. **One `Source` class per site, with routes and categories**, rather than gallery-dl's class per kind of URL? *Recommended: one class* (ARCHITECTURE.md §2, §6).
17. **A fixed chain of steps after each file**, rather than a plugin API for steps? *Recommended: fixed, for now* (§11).
18. **A yt-dlp fallback for single video links no source handles?** *Recommended: later, as a separate plugin package*, so the core's dependencies stay as they are (feature 27).
