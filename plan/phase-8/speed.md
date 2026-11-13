# speed: performance, module by module

Part of [Phase 8](README.md). For each module: the hot paths, what is slow and why, the change,
and a target that [bench](bench.md) measures. Numbers were measured on 2026-11-13 on a 10-core
Mac (Python 3.14, ffmpeg 9) with local scripts on fakes, at `37147c1`, unless marked *reasoned*.
Nothing here repeats Phase 7's speed step, which is built.

A target is kept by `dev.py bench --check` as a ratio or bound
([bench](bench.md#5-targets-and-how-they-are-kept)); where a count stands in for time, `check`
asserts it on every run ([tests](tests.md#6-counts-that-stand-in-for-speed)).

## The biggest wins

| # | Where | Now | After |
| --- | --- | --- | --- |
| S1 | `http.download`: piece stall rule judged against a warm first request | 4.1× slower (69 requests for 16) when pieces open cold connections | within 10% of an even network |
| S2 | `media.open` of an animated WebP / GIF | 340 ms / 83 ms | < 3 ms each |
| S3 | `log`'s drawing thread while a question is open | one core at 100% ([L1](bugs.md#log-theme-prompt-cli-fmt)) | idle |
| S4 | flnd `sync`'s re-dating scan per target with a date hook | 0.99 s per target at 100k history entries | < 5 ms |
| S5 | `kv`: `items()`, `clear()`, writes | 9.7 ms / 299 ms per 2,000 / 0.030 ms | 0.8 ms / < 2 ms / 0.008 ms |
| S6 | `media` check of a video (every download, every repair check) | 31 ms, two ffmpeg runs | about 18 ms, one run |
| S7 | `media.frames(count=9)` | grows with length (1.1 s for 2 min, ~33 s for an hour) | grows with the count: < 0.5 s for 9 at any length |
| S8 | `worker` per task | 22 µs (35 µs with `timeout`) | ≤ 8 µs, no extra thread per try |
| S9 | the DASH merge retry and a refused stream | a whole stream fetched again | 0 bytes fetched twice ([F7](bugs.md#flnd)) |

## http and download

| Hot path | Now, and why | Change | Target |
| --- | --- | --- | --- |
| Piece stalls (`_download.py:1107-1130`) | A piece's first byte is compared with 2× the *first* request's, which came over a warm HTTP/2 connection; pieces open cold HTTP/1.1 ones, so healthy pieces are given away and asked again, each closed stream costing a new connection. 32 MB with a 10 ms first wait and 60 ms piece waits: 69 requests, 1.36 s, against 16 requests, 0.33 s, when every wait is 60 ms. | Judge against the median first-byte time seen on piece connections, at least 0.5 s. | 16 requests and within 10% of the even case |
| A resume with another width ([H6](bugs.md#http-download-pace)) | the rest fetched twice: 63 MB for 38 | the fix | bytes served = bytes missing |
| Progress under the pieces' lock (`_download.py:883-889`) | `progress` runs per chunk under `_Pieces.lock` (4,097 calls per 256 MB), so a slow callback serialises 8 connections | call it outside the lock, at most 10 times a second | a 1 ms callback leaves 8-connection throughput unchanged |
| A stop watcher per download (`_download.py:347, 375`) | a thread per file whenever `stop` is given (flnd always gives it), polling every 0.25 s | one watcher per stop event, shared | 0 extra threads per file |
| The server's hash (`_hash_check`) | the whole file read again after it is written: 0.2 s per 512 MB | hash while writing, on one connection with no resume | no second read |
| Per-chunk CPU, fetch overhead | 5 GB/s one connection, 2.7 GB/s pieces, in memory; `Client.fetch` 54 µs against httpx's 28 µs | none | — |

## kv

| Hot path | Now, and why | Change | Target |
| --- | --- | --- | --- |
| `items()`, `values()` | inherited: N+1 queries; 9.7 ms for 4,000 entries | one `SELECT` | ≤ 1.5 ms for 4,000 |
| `clear()` | inherited `popitem` loop, a key snapshot per pass: quadratic, 299 ms for 2,000 | one `DELETE` | < 2 ms for 2,000 |
| `update()` | one transaction per key | one transaction, `executemany` | 1,000 keys < 3 ms |
| every write | `synchronous=FULL` under WAL: 0.030 ms a set | `synchronous=NORMAL` (WAL keeps the store whole; a power cut may lose the last commits) (question 5) | 0.008 ms a set |

## log, theme

| Hot path | Now, and why | Change | Target |
| --- | --- | --- | --- |
| The drawing thread during a question ([L1](bugs.md#log-theme-prompt-cli-fmt)) | 1.8 million ticks a second | the fix | 0 ticks |
| A frame of 40 rows, nothing changed | 90 µs: the cache key calls each row's `show()` twice | key on the raw count, total, caption, outcome, tick, width, colour | < 20 µs |
| A frame of 40 rows, all changed | 457 µs, mostly `theme.fit` (7–9 µs a row) | fast path: an ASCII row that fits is returned as it is (0.26 µs measured) | < 150 µs |
| `theme.paint` | parses the palette value every call: 1.5–3.3 µs | cache the parsed style per value (0.02 µs measured) | `render` 3 → 1 µs |
| `log.info` written / `debug` dropped / dropped with a record open | 4.2 / 0.5 / 7.5 µs | cache a template's field names; write the record outside the screen lock | ≤ 3 / 0.3 / 6 µs |
| Stripping colour | twice: `render`, then `Screen.write` | once, in `Screen.write` | — |

## media

| Hot path | Now, and why | Change | Target |
| --- | --- | --- | --- |
| `open` of an animated GIF (150 frames) | 83 ms: counting frames and reading delays seeks every frame, and a GIF seek decodes | read the delays from the GIF's control blocks | < 3 ms |
| `open` of an animated WebP | 340 ms: `durations()` loads each frame | read durations from the ANMF chunk headers | < 3 ms |
| `check` of a video | 31 ms: a probe (17.5 ms) and a copy pass (~14 ms), two processes (10.5 ms floor each) | one ffmpeg run that copies to null and yields the probe's summary from the same stderr | about 18 ms |
| `frames(at=…)`, `frames(count=…)` | the `select` filter decodes the whole video: 1.1 s for 9 frames of a 2-minute 1080p clip, against 0.32 s for 9 seeks | one input seek per frame | < 0.5 s for 9 frames, any length |
| An animated `compress` | every frame decoded twice (delays, then the conversion): 537 ms for a 26 MB GIF | one pass | −50% |
| Checked and fine | sniff 0.01 ms; open a JPEG 0.1 ms; a photo's compress is encode-bound | — | — |

## worker, archive

| Hot path | Now, and why | Change | Target |
| --- | --- | --- | --- |
| Per task | 22 µs against `ThreadPoolExecutor`'s 3 µs: one condition, `notify_all` on every change wakes every runner and the caller; 0.25 s polling | separate conditions for room and finished | ≤ 8 µs |
| With `timeout=` | 35 µs and an extra thread per try | one deadline thread for the pool | no extra thread per try |
| Split archives | every `ls` and `unzip` copies all parts into a temporary file first | a seekable reader chained over the parts | `ls` reads only the index; no temporary copy |
| RAR | one `unrar p` process per file | one `unrar x` into the staging folder | one process per archive |

## browser, selector

| Hot path | Now, and why | Change | Target |
| --- | --- | --- | --- |
| `Selection.script(pattern=)` | 19.4 ms on a 2.4 MB page against 0.33 ms for the same regex: it serialises the parsed tree again | search the source text kept on the selection | ≈ the regex's time |
| A paced page's cookies, copied on every `fetch` while a browser is open | *reasoned*: a browser round trip and a jar rebuilt per request | one cookie jar shared live by the browser and the client ([api › http](api.md#http)) | 0 browser round trips per plain fetch |

## flnd

| Hot path | Now, and why | Change | Target |
| --- | --- | --- | --- |
| `Ledger.redate` under `sync` (`job.py:496`) | 0.99 s per target with a date hook, at 100k entries: every entry of every site decoded, then filtered by folder | take the target's entries, which repair already indexed | < 5 ms per target |
| Re-dating asks again every sync | *reasoned*: each undated or estimated file asks its site on every sync, 2,000 files × fbk's 0.8 s floor ≈ 27 minutes of paced requests | remember `asked: <day>`; ask again after 30 days (question 6) | requests per sync follow new undated files only |
| `Ledger._neighbour` on release (`job.py:536-546`) | quadratic: 0.2 s for 4,000 undated posts, about 3 s at 20,000 | one forward and one backward pass | < 10 ms at 20,000 |
| Repair's folder walks (`repair.py:126-145, 196-198`) | four `rglob` walks with 2–3 stats a file: 238 ms per 20,000 files against 45 ms for one walk | one `os.scandir` walk for parts, sidecars, links and sizes | ≤ 50 ms per 20,000 |
| `targets.db` per target | `complete` and `keep_cursors` each copy and write the row: two commits | one write | one commit per target |
| Checked and fine | per-file history reads and writes, the job's counts, JSONPath and JMESPath compiling (cached) | — | — |
