# Performance

How fast flnd is, how to measure it, and how a change proves it made things faster.

## Running the benchmarks

```sh
make bench                              # every group, a few minutes
make bench ARGS="net disk"              # only these groups
make bench ARGS=--gate                  # exit 1 on a regression against the baseline
make bench ARGS="db --gate --tolerance 5"
make bench ARGS=--record                # store this run as the new baseline
make bench ARGS=--smoke                 # each group's smallest case once: does it still run?
```

`make bench` runs `tests/bench/run.py` inside the project environment
(`uv run python tests/bench/run.py`). It is never part of `make check`: it takes minutes, and timings are
noisy. Everything is offline: a loopback HTTP server, synthetic data, and scratch folders that
are removed afterwards.

`make check` does run every group in **smoke mode** (`tests/test_bench.py`): each at its
smallest size (a few files, a few rows), each timed once, in a few seconds. Its numbers are
thrown away; it only proves each script still runs against today's code, so a benchmark can't
silently break between two full runs.

The run prints one line per number, writes them all to `tests/bench/last.json` (ignored by
git), and compares each with its **baseline** (the number recorded earlier on the same machine)
and its **target** (what a planned change must reach) in `tests/bench/gates.json`.

## The groups

Each group is one script in `tests/bench/`, run in a fresh interpreter.

| group | what it measures |
| --- | --- |
| `startup` | process start to exit: `python -c pass`, the imports, `flnd --help` (median of 15) |
| `net` | flnd's HTTP client on the run's loop from a loopback server: 1 GiB to disk, 2,000 × 100 KiB one and 8 at once, and the loop's longest stall on a disk throttled to 20 MB/s |
| `disk` | writing 5,000 files of 100 KiB: a plain write, a temp file and rename, a download's part |
| `db` | the history store over 100k rows; SQLite commits per file, per post and per 500 rows |
| `walk` | listing 100,000 files, and `stat` on known paths |
| `cpu` | JSON, a JSON path, HTML parsing, hashing, the loop bridge, path planning, a task handed to the loop |
| `e2e` | a whole run of the real engine on a faked site, with and without latency |
| `browser` | headless Chromium on a synthetic infinite-scroll feed; skipped without Chromium |

Each script's docstring says exactly what it builds and times.

## Reading the numbers

- A time is the **best of 3** runs, because noise only ever adds time. Process starts are the
  **median of 15**. The long runs (`e2e`, `browser`, video encodes) run once.
- Loopback hides latency and bandwidth: `net` shows what the client costs in CPU and per
  request, not what a CDN delivers. `e2e` adds simulated latency.
- Media inputs are synthetic, so compare speeds between runs, never output sizes.
- Run on a quiet machine, on power. Expect a few percent of noise between runs, and up to 10%
  on `net.big_rate`, which depends on the page cache.

## Gates and baselines

`--gate` fails when a number is worse than its baseline by more than the tolerance, 15% by
default. Run it by hand before a performance commit, and quote both runs in the commit message.

After a performance commit lands, run `make bench ARGS=--record`, so the next gate compares
against the new numbers. `--record` keeps every target and only replaces the baselines of the
groups it ran. Baselines only compare on the machine that recorded them; its name, the date and
the commit are in the `recorded` block of `gates.json`. On another machine, record first.

`make check` guards performance too, but with **counts**, never times: requests per run,
`stat` calls per file, database statements per file, and modules imported by `flnd --help`.

## The baseline

Recorded on 2026-11-16 at `1e01feb`, the rebuilt flnd, on an Apple M5 (10 cores, 32 GB, APFS
SSD), macOS 26.2, CPython 3.14.6, with other sessions running (load 4.5 to 6). "Before" is the
baseline recorded at `9d1cf13`, before the rebuild. The numbers a target is set for:

| metric | before | rebuilt | target |
| --- | ---: | ---: | ---: |
| `startup.help` (`flnd --help`) | 162 ms | 23.4 ms | ≤ 40 ms, met |
| `net.big_rate` (1 GiB to disk) | 1.76 GiB/s | 2.40 GiB/s | ≥ 4 GiB/s, **not met** |
| `net.small_8_at_once` (2,000 × 100 KiB) | 927 ms | 617 ms | ≤ 300 ms, **not met** |
| `net.stall_20mbs` (the loop's longest stall, 20 MB/s disk) | 54 ms | 9.4 ms | < 100 ms, met |
| `disk.part_write` (per file) | 255 µs | 123 µs | ≤ 130 µs, met |
| `db.point_read` | 2.39 µs | 1.96 µs | ≤ 2.2 µs, met |
| `db.commit_per_post` (per file) | 8.1 µs | 13.1 µs | ≤ 15 µs, met |
| `e2e.sync_nothing_new_100k` (a sync of 100k files, nothing new) | 4 ms | 5 ms | ≤ 0.3 s, met |
| `e2e.latency_500_posts` (150 ms a page, 40 ms a file) | 7.16 s | 4.85 s | ≤ 5.5 s, met |
| `browser.scroll_default` (the synthetic feed, human-like) | 26.0 s | 14.2 s | ≤ 16 s, met |
| `browser.scroll_fast` (the same feed, fast) | — | 2.0 s | ≤ 3 s, met |
| `cpu.html_meta` (a 2 MiB Meta-style page) | 18.0 ms | 6.7 ms | ≤ 8 ms, met |
| `ui.frame_1000` (a frame with 1,000 targets and files) | 1.56 ms | 1.62 ms | ≤ 5 ms, met |
| `ui.sample_core` (live stats of a busy run, share of a core) | 0.155% | 0.148% | ≤ 1%, met |

The two missed targets, and where their time goes, are under "Targets not met". Three numbers
with no target are slower than before the rebuild, the price of `flnd.db`'s real columns and
indexes over the old stores of JSON values: `db.insert` (1.13 to 2.30 µs a row: 15 columns,
and the `(target, post_id)` index alone costs 0.5 µs), `db.prefix_scan_10k` (4.2 to 15.6 ms:
each of the 10,000 rows comes back as 20 columns, which is 10 of those ms; the index walk and
row lookups are under 2) and `db.commit_per_post` (above, still within its target).

`gates.json` has the rest: 77 numbers in all.

## Network

Every transfer runs on the run's asyncio loop through curl_cffi (`flnd._net`): libcurl's multi
interface, HTTP/2, Chrome's TLS fingerprint. Measured A/B against the httpx client it replaced,
on the M5 above while other work ran (load about 8), the median of 3 alternating runs of
`make bench ARGS=net`:

| metric | httpx, 8 threads | curl_cffi on the loop |
| --- | ---: | ---: |
| `net.big_rate` (1 GiB to disk) | 1.62 GiB/s | 3.02 GiB/s |
| `net.small_8_at_once` (2,000 × 100 KiB, 8 at once) | 744 ms | 615 ms |
| `net.small_1_at_once` (the same, one at a time) | 997 ms | 798 ms |
| `net.stall_20mbs` (longest stall of the loop, disk at 20 MB/s) | — | 54 ms |

How a file is written, and why:

- **Bytes go to one writer thread, 4 MiB at a time, and are hashed beside it.** curl hands
  the loop 16 KiB chunks; they are gathered, and each full 4 MiB piece is written on the
  `flnd-writer` thread while the `flnd-hasher` thread adds it to the file's SHA-256 (for
  dedupe and `check`), both while the loop goes on. Measured with the writes on the loop, four
  files onto a disk throttled to 20 MB/s held the loop up for 1.2 s at a time; on the writer
  thread, 15 ms. The last piece of a file, when it fits the part's 1 MiB buffer, is copied
  there on the loop and reaches the disk when the part lands, so a small file costs no hop to
  the writer at all.
- **Why 4 MiB and two threads.** SHA-256 runs at about 3.3 GiB/s on one M5 core, so hashing
  after each write on the writer thread capped one transfer at about 1.9 GiB/s. And every hop
  to a thread waits for the GIL when it comes back, which takes up to the switch interval
  (5 ms) while the loop runs Python: a thread hashing 1 MiB pieces beside a busy Python thread
  managed 0.13 GiB/s. Hashing beside the write, and handing off 4 MiB instead of 1 MiB, took
  `net.big_rate` from 1.91 to 2.35 GiB/s (median of 3 alternating runs).
- **A transfer waits for the disk with at most 8 MiB in memory.** curl_cffi queues every chunk
  curl receives, with no bound; on a disk slower than the network a whole file would wait
  there. Once 8 MiB of a transfer wait (4 MiB before the pieces grew), flnd pauses it
  (`curl_easy_pause`) until the writer has taken them down to half, and TCP holds the server
  back meanwhile. Measured at 4 MiB with two 64 MiB files onto a disk throttled to 20 MB/s: at
  most 4.0 MiB waited per transfer, against 62.9 MiB unpaused, in the same time (7.3 s against
  7.2 s).
- **A part's open and rename run on worker threads.** Each is a file-system call of 50 to 150
  µs, more on a busy disk; on the loop they would run one at a time.
- **Small files are bound by the loop's own work.** The loop thread is busy for 0.53 s of the
  0.61 s that `net.small_8_at_once` takes. The client alone (`Client.fetch` of the same 2,000
  files, no part) takes 0.30 s, all of it the loop's CPU; the part's path adds the rest (see
  "Targets not met" below).
- `net.stall_20mbs` charges every write of a part as a disk write at 20 MB/s, the last piece's
  copy into memory included, so it overstates a real slow disk.

## Targets not met

Two network targets are still missed on this machine, after the profiling below. The targets
stay as they were; the numbers are from the recorded baseline.

**`net.big_rate`: 2.2 to 2.4 GiB/s, against ≥ 4.** One 1 GiB file, one transfer:
- every byte of a download is hashed (SHA-256, for dedupe and `check`), and one stream can't be
  hashed faster than one core runs SHA-256: 3.3 GiB/s on the M5 (`cpu.hash_sha256`, 297 µs a
  MiB). So with the hash, 4 GiB/s can't be reached by one transfer, however it is arranged;
- without the hash (measured by patching it out), the same transfer runs at 3.2 GiB/s with 1
  MiB pieces and 4.6 GiB/s with 4 MiB pieces and a 256 KiB curl buffer: the loop alone, with no
  write at all, reaches 4.8 GiB/s (0.19 s of its CPU per GiB);
- with the hash on the hasher thread beside the write, the transfer waits for the hash: in 0.43
  s for 1 GiB the hasher is busy 0.33 s, and every piece it takes waits for the GIL when it
  comes back, while the loop runs Python. Pipelining the pieces (handing off piece n + 1 while
  n is hashed), a larger log of waiting bytes (16 and 64 MiB), 8 MiB pieces and a shorter switch
  interval (0.5 ms) each moved it by under 5%;
- what would meet it is a hash that isn't on the transfer's path (a faster or parallel hash, or
  hashing after the file lands), which changes what `flnd.db` stores or when dedupe can run.

**`net.small_8_at_once`: 0.61 s, against ≤ 0.3.** 2,000 files of 100 KiB, 8 at once:
- the loop thread is the bottleneck: 0.53 s of its CPU for 0.61 s of wall time;
- the client alone (`Client.fetch` of the same files, no part) takes 0.30 s, which is already
  the target. Per request, curl_cffi's request layer costs about 150 µs of the loop's time: 15
  `setopt` and 15 `getinfo` calls through cffi (about 55 µs), re-applying Chrome's fingerprint
  to the reused handle, and parsing the answer's headers (about 50 µs);
- a part adds about 115 µs of the loop's time per file: two hops to a worker thread (15 µs
  each, measured by hopping to a no-op), the last piece's copy into the part's buffer and its
  hash (about 30 µs), and the transfer's own steps (the range request, the meter, the sink).
  The file-system calls themselves (open, lock, write, rename, close: about 110 µs a file, a
  rename into one folder serialized by APFS) run on the worker threads, beside the loop;
- moving the last piece's write and hash to the landing thread, or the part's calls onto the
  loop, made it no faster (the second, 5% slower). Meeting the target needs a lighter request
  path than curl_cffi's `AsyncSession`: driving libcurl's handles directly with only the
  options a request needs.

## Concurrency per host

Each media host's cap adapts (`flnd._net.Cap`, flnd/docs/architecture "Network"): it starts at 8
transfers at once, halves on a 429, a 503, a reset or a timeout, and grows by one every 10
clean transfers, up to 32, while the host's throughput still rises. The `net` group holds its
caps fixed (`Limits(cap=8, most=8)`), so its numbers don't move with the adaptation.

Throughput is measured by Little's law: the cap times the bytes over the seconds the window's
transfers took. A first version took bytes over the window's wall time; with only 10 transfers
a window, that rate swung with where in their run the window closed, and on the e2e run the cap
stalled at 9 to 10. With durations it climbs to about 27 there, and holds where a step brings
nothing.

`e2e.latency_500_posts` (500 posts, 150 ms a page, 40 ms a file, the default pace of 0.5 s),
A/B on the M5 above while other work ran (load about 5), the median of 5 alternating runs:

| caps | seconds |
| --- | ---: |
| 8 at once per host (before) | 6.01 |
| 8, adapting to 32 | 4.87 |

The listing's 10 pages, half a second apart, take about 4.65 s, so the run now ends about
0.2 s after its last page. The `net` group is unchanged within noise (`small_8_at_once` 602 to
607 ms before and after).

### The real-CDN check

Loopback can't show where a real CDN stops giving more for more transfers at once, so the
ceiling of 32 is confirmed by hand with `tests/bench/cdn_real.py`:

1. Collect the URLs of about 200 files on one CDN host from the authorized test accounts (the
   browser's network panel shows them), one per line, in a file outside the repo, such as
   `~/flnd-cdn.txt`.
2. Run `make cdn-real ARGS="--urls ~/flnd-cdn.txt --rounds 3"`. Add
   `--header "Referer: https://www.example.com/"` with the site's own address when its CDN
   needs one (Facebook's and Instagram's do), and `--caps 8,16,24,32,48` to look further.
3. It downloads the list through flnd's client at each fixed cap, then once with the adaptive
   default, alternating over the rounds, and prints the median seconds and MiB/s per cap, the
   failures, and the cap each run ended at. It prints no URL, name or host.
4. Pick the smallest cap past which MiB/s stops rising by more than a few per cent. If it is
   well under 32, lower `flnd.site.MEDIA_MOST`; if a cap ended below the one asked, the host
   overloaded there.

## Disk

What writing one downloaded file costs beyond the write itself, per file of 100 KiB, measured
A/B on the M5 above while other work ran (the median of 5 alternating runs of the best of 3):

| how | µs per file |
| --- | ---: |
| `open` + `write` (the floor) | 70.8 |
| before parts: a stage file, a lock file, a replace | 303.4 |
| a download's part: open and lock, write, set the time, one `fstat`, rename | 126.2 |

The lock file cost more than the write itself. A part needs one `open` and one `flock`, and
the history row takes the file's size and time from the `fstat` before the rename, so nothing
stats the file again. A part that a failed run left costs one `stat` more, to check that no
other run landed it meanwhile. `tests/test_fs.py` counts these calls, so a change that adds one
fails `make check`.

## Browser feeds

Reading the synthetic feed of 30 batches to its end (`browser.scroll_default` and
`browser.scroll_fast`), measured A/B on the M5 above on 2026-11-16 while other work ran: the
median of 3 alternating runs of each.

| how | seconds |
| --- | ---: |
| before: fixed sleeps, the end found by 10 s of silence (`idle=10`, the old default) | 25.17 |
| before, with `idle=2` | 18.35 |
| driven by responses, ended by the cursor, `scroll = "human"` (the default) | 14.14 |
| the same with `scroll = "fast"` | 1.99 |

Each old step cost about 0.6 s whether data came or not (a 3 s wait for growth at the bottom,
a 0.3 s pause and a jiggle), and the end cost the whole idle window. Now a step waits only for
the next matching response or the page growing, and the read ends on the last batch's own "no
more". With `human`, nearly all of what is left is the pauses a person would make: 29 steps at
a median of 0.4 s each. `fast` is the 30 answers of 50 ms each, plus the scrolling.

## HTML and JSON paths

Pages are parsed by selectolax (lexbor, a C parser with its own CSS engine) through
`flnd._html`, and JSON paths walked by `dig`, which replaced parsel (lxml), jmespath and
python-jsonpath. Measured A/B against the old reads on the M5 above while other work ran (load
about 5), the median of 9 alternating rounds, each the best of 3:

| metric | parsel / jmespath | selectolax / `dig` |
| --- | ---: | ---: |
| `cpu.html_meta` (a 2 MiB Meta-style page: parse, then its JSON blocks) | 18.1 ms | 6.6 ms |
| `cpu.html_images` (the same page: parse, then 20,000 `img[src]`) | 32.1 ms | 10.8 ms |
| `cpu.dig` (`edges[].node` over 1,500 posts; plain dict access is 18 µs) | 238 µs | 46 µs |

- **The old `html_meta` read only the blocks' text**; the new one also decodes the six that
  hold `__bbox`, as Facebook and Instagram do, and is still 2.7× faster.
- **`dig` compiles a path once** and caches it, and walks the commonest shape (`a[].b`) in one
  comprehension. The old 682 µs baseline went through parsel's JSON selectors; jmespath alone
  is the 238 µs above.
- **Neither parser loads at startup**: `flnd._html` imports selectolax on the first parse and
  msgspec on the first decode, so `flnd --help` and `import flnd.source` load neither.
