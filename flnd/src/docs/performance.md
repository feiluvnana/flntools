# Performance

How fast flnd is, how to measure it, what the numbers mean, and how a change proves it made
things faster. The design of the speed-ups is in [PLAN.md](../../PLAN.md).

## Running the benchmarks

```sh
make bench                              # every group, a few minutes
make bench ARGS="net disk"              # only these groups
make bench ARGS=--gate                  # exit 1 on a regression against the baseline
make bench ARGS="db --gate --tolerance 5"
make bench ARGS=--record                # store this run as the new baseline
make bench ARGS=--smoke                 # each group's smallest case once: does it still run?
```

`make bench` runs `tests/bench/run.py` inside the project environment. It is never part of
`make check`: it takes minutes, and timings are noisy. Everything is offline: a loopback HTTP
server, synthetic data, and scratch folders that are removed afterwards.

`make check` runs every group in **smoke mode** (`tests/test_bench.py`): each at its smallest
size, each timed once, in a few seconds. Its numbers are thrown away. It only proves that each
script still runs against today's code, so a benchmark can't silently break between two full
runs.

Each group runs as its own script in a fresh interpreter, so one group's imports and memory
never slow the next. A group that crashes or prints no report makes the run exit 1. A group
that lacks what it needs (Chromium, ffmpeg) is reported as skipped, and that does not fail
the run.

The run prints one line per number and writes them all to `tests/bench/last.json` (ignored by
git). It compares each with its **baseline** (the number recorded earlier on the same machine)
and its **target** (what a change must reach), both in `tests/bench/gates.json`.

## The groups

Each group is one script in `tests/bench/`.

| group | what it measures |
| --- | --- |
| `startup` | process start to exit: `python -c pass`, the imports, `flnd --help` |
| `ui` | the run's screen: its cost in a run, one frame, idle bytes, list open time |
| `net` | the HTTP client on the run's loop, from a loopback server (see below) |
| `disk` | writing 5,000 files of 100 KiB: a plain write, a temp file, a download's part |
| `db` | the history store over 100,000 rows; SQLite commits per file, post and 500 rows |
| `walk` | listing 100,000 files, and `stat` on known paths |
| `cpu` | JSON, a JSON path (`dig`), HTML parsing, hashing, path planning, a loop task |
| `e2e` | a whole run of the real engine on a faked site, with and without latency |
| `browser` | headless Chromium on a synthetic infinite-scroll feed, and warm tabs |

The `net` group downloads 1 GiB to disk; 2,000 files of 100 KiB, one at a time and 8 at once;
the loop's longest stall on a disk throttled to 20 MB/s; a second client on a slow disk; a
per-connection cap; four 256 MiB files at once; and a playlist of 200 HLS segments. The `e2e`
group also syncs 100 targets, and runs while another process holds `flnd.db`'s write lock.
The `browser` group is skipped without Chromium.

Each script's docstring says exactly what it builds and times.

## Reading the numbers

- A time is the **best of 3** runs, because noise only ever adds time. Process starts are the
  **median of 15**. The long runs (`e2e`, `browser`) run once.
- Loopback hides latency and bandwidth: `net` shows what the client costs in CPU and per
  request, not what a CDN delivers. `e2e` adds simulated latency.
- Media inputs are synthetic, so compare speeds between runs, never output sizes.
- Run on a quiet machine, on power. Expect a few percent of noise between runs, and up to 10%
  on `net.big_rate`, which depends on the page cache.

## Gates and baselines

`--gate` fails when a number is worse than its baseline by more than the tolerance, 15% by
default (`--tolerance` changes it). Run it by hand before a performance commit, and quote both
runs in the commit message.

After a performance commit lands, run `make bench ARGS=--record`, so the next gate compares
against the new numbers. `--record` keeps every target and replaces only the baselines of the
groups it ran. A recorded run is refused with `--smoke`, whose numbers mean nothing.

Baselines compare only on the machine that recorded them. The `recorded` block of `gates.json`
names that machine, the date and the commit. On another machine, record first.

`make check` guards performance too, but with **counts**, never times: requests per run,
`stat` calls per file, database statements per file, and modules imported by `flnd --help`.

## Targets and current baselines

The table is what `tests/bench/gates.json` holds: every number with a target, its baseline as
recorded on 2026-11-18 at commit `e128f92` (Apple M5, 10 cores, CPython 3.14.6), and whether the
baseline meets the target. A baseline is the last recorded measurement, not a fresh one.

| metric | unit | target | baseline | state |
| --- | --- | ---: | ---: | --- |
| `startup.help` | ms | ≤ 60 | 35.2 | met |
| `ui.overhead` | % | ≤ 3 | 16.5 | not met |
| `ui.frame` | ms | ≤ 1 | 0.55 | met |
| `ui.idle_bytes` | B | 0 | 654,248 | not met |
| `ui.open` | ms | ≤ 250 | 100.5 | met |
| `net.big_rate` | GiB/s | ≥ 4 | 2.47 | not met |
| `net.small_8_at_once` | ms | ≤ 300 | 476.5 | not met |
| `net.stall_20mbs` | ms | < 100 | 9.4 | met |
| `net.two_disks` | % | ≤ 10 | 3,915.9 | not met |
| `net.per_conn_limit` | x | ≥ 4 | 0.997 | not met |
| `net.big_4_at_once` | x | ≥ 2.5 | 1.215 | not met |
| `net.hls_200` | x | ≥ 6 | 0.978 | not met |
| `disk.part_write` | µs | ≤ 130 | 112.1 | met |
| `db.point_read` | µs | ≤ 2.2 | 1.84 | met |
| `db.commit_per_post` | µs | ≤ 15 | 12.5 | met |
| `cpu.html_meta` | ms | ≤ 8 | 6.67 | met |
| `e2e.sync_nothing_new_100k` | s | ≤ 0.3 | 0.0055 | met |
| `e2e.latency_500_posts` | s | ≤ 5.5 | 4.87 | met |
| `e2e.sync_100_targets` | s | ≤ 3.84 | 15.4 | not met |
| `e2e.db_contended` | ms | ≤ 100 | 2,104 | not met |
| `browser.scroll_default` | s | ≤ 16 | 14.6 | met |
| `browser.scroll_fast` | s | ≤ 3 | 2.0 | met |
| `browser.warm_4` | s | ≤ 0.091 | 0.278 | not met |

By this table, 12 of the 23 targets are met and 11 are not. `gates.json` holds 70 numbers in
all; the other 47 have no target and are gated only against their baseline. The sections
below give what is known about each missed target.

## Earlier measurements (2026-11-16)

Before the table above, the numbers were written down in the page at each change. The first
full set was recorded on 2026-11-16 at commit `1e01feb`, the rebuilt flnd, on an Apple M5
(10 cores, 32 GB, APFS SSD), macOS 26.2, CPython 3.14.6, with other sessions running (load 4.5
to 6). "Before" is the baseline recorded at `9d1cf13`, before the rebuild. The target column is
the target at that time; `gates.json` above is the current one.

| metric | before | rebuilt | target then |
| --- | ---: | ---: | ---: |
| `startup.help` (`flnd --help`) | 162 ms | 23.4 ms | ≤ 40 ms |
| `net.big_rate` (1 GiB to disk) | 1.76 GiB/s | 2.40 GiB/s | ≥ 4 GiB/s |
| `net.small_8_at_once` (2,000 × 100 KiB) | 927 ms | 617 ms | ≤ 300 ms |
| `net.stall_20mbs` (the loop's longest stall, 20 MB/s disk) | 54 ms | 9.4 ms | < 100 ms |
| `disk.part_write` (per file) | 255 µs | 123 µs | ≤ 130 µs |
| `db.point_read` | 2.39 µs | 1.96 µs | ≤ 2.2 µs |
| `db.commit_per_post` (per file) | 8.1 µs | 13.1 µs | ≤ 15 µs |
| `e2e.sync_nothing_new_100k` (a sync of 100k files, nothing new) | 4 ms | 5 ms | ≤ 0.3 s |
| `e2e.latency_500_posts` (150 ms a page, 40 ms a file) | 7.16 s | 4.85 s | ≤ 5.5 s |
| `browser.scroll_default` (the synthetic feed, human-like) | 26.0 s | 14.2 s | ≤ 16 s |
| `browser.scroll_fast` (the same feed, fast) | — | 2.0 s | ≤ 3 s |
| `cpu.html_meta` (a 2 MiB Meta-style page) | 18.0 ms | 6.7 ms | ≤ 8 ms |
| `ui.frame_1000` (a frame with 1,000 targets and files) | 1.56 ms | 1.62 ms | ≤ 5 ms |
| `ui.sample_core` (live stats of a busy run, share of a core) | 0.155% | 0.148% | ≤ 1% |

Three numbers with no target were slower after the rebuild, the price of `flnd.db`'s real
columns and indexes over the old stores of JSON values: `db.insert` (1.13 to 2.30 µs a row: 15
columns, and the `(target, post_id)` index alone costs 0.5 µs), `db.prefix_scan_10k` (4.2 to
15.6 ms: each of the 10,000 rows comes back as 20 columns, which is 10 of those ms; the index
walk and row lookups are under 2) and `db.commit_per_post` (above).

## Network

Every transfer runs on the run's asyncio loop through curl_cffi (`flnkit.net`): libcurl's multi
interface, HTTP/2, Chrome's TLS fingerprint. The numbers in this section were recorded when each
change was measured, on the M5 above. The A/B against the httpx client that it replaced used the
median of 3 alternating runs of `make bench ARGS=net`, while other work ran (load about 8):

| metric | httpx, 8 threads | curl_cffi on the loop |
| --- | ---: | ---: |
| `net.big_rate` (1 GiB to disk) | 1.62 GiB/s | 3.02 GiB/s |
| `net.small_8_at_once` (2,000 × 100 KiB, 8 at once) | 744 ms | 615 ms |
| `net.small_1_at_once` (the same, one at a time) | 997 ms | 798 ms |
| `net.stall_20mbs` (longest stall of the loop, disk at 20 MB/s) | — | 54 ms |

How a file is written, and why:

- **Bytes go to their disk's writer thread, 4 MiB at a time.** curl hands the loop 16 KiB
  chunks; they are gathered, and each full 4 MiB piece is written on the `WRITE` thread of the
  device the file's folder is on (`os.stat(folder).st_dev`, one thread per device, in the
  scheduler), while the loop goes on. A slow disk holds up only its own files. The
  `net.two_disks` figure went from about 5,800 % to 19 to 25 % when this landed. A download no
  longer hashes as it is written: dedupe hashes a file only when a saved one has its size, as a
  `HASH` job on the hasher pool, one thread per core. Measured with the writes on the loop, four
  files onto a disk throttled to 20 MB/s held the loop up for 1.2 s at a time; on the writer
  thread, 15 ms. The last piece of a file, when it fits the part's 1 MiB buffer, is copied there
  on the loop and reaches the disk when the part lands, so a small file costs no hop to the
  writer at all.
- **Why 4 MiB and two threads.** SHA-256 runs at about 3.3 GiB/s on one M5 core, so hashing
  after each write on the writer thread capped one transfer at about 1.9 GiB/s. Every hop to a
  thread also waits for the GIL when it comes back, which takes up to the switch interval (5 ms)
  while the loop runs Python: a thread hashing 1 MiB pieces beside a busy Python thread managed
  0.13 GiB/s. Hashing beside the write, and handing off 4 MiB instead of 1 MiB, took
  `net.big_rate` from 1.91 to 2.35 GiB/s (median of 3 alternating runs).
- **A transfer waits for the disk with at most 8 MiB in memory.** Every chunk curl receives
  waits in the transfer's queue, which has no bound. On a disk slower than the network, a whole
  file would wait there. Once 8 MiB of a transfer wait (4 MiB before the pieces grew), flnd
  pauses it (`curl_easy_pause`) until the writer has taken them down to half, and TCP holds the
  server back meanwhile. Measured at 4 MiB with two 64 MiB files onto a disk throttled to
  20 MB/s: at most 4.0 MiB waited per transfer, against 62.9 MiB unpaused, in the same time (7.3 s
  against 7.2 s).
- **A part's open and rename run on worker threads.** Each is a file-system call of 50 to 150
  µs, more on a busy disk; on the loop they would run one at a time.
- **Small files are bound by the loop's own work.** At the baseline the loop thread was busy for
  0.53 s of the 0.61 s that `net.small_8_at_once` took. Kept curl handles bring it to 0.48 s
  (see "Targets not met" below).
- `net.stall_20mbs` charges every write of a part as a disk write at 20 MB/s, the last piece's
  copy into memory included, so it overstates a real slow disk.
- Since the save runs as jobs, `net.stall_20mbs` reads 4 to 25 ms, where it read under 2. It is
  not blocking I/O: measured, no write, open or flush of a part runs on the loop. It is the
  garbage collector's pauses (6.7 to 17.7 ms) and the scheduling of the save's extra threads,
  so it varies from run to run, and stays far under the 100 ms target.

## Targets not met

Two network targets are missed on the recorded baselines, and the profiling below explains
where the time goes. The targets stay as they are.

**`net.big_rate`: 2.47 GiB/s recorded, against ≥ 4.** The page's history records 4.7 to 4.8
GiB/s after the hash moved off the transfer's path. That run is not in `gates.json`, and the
recorded baseline there is 2.47 GiB/s, so measure again with `make bench ARGS=net` before
quoting either number. What the profiling found, with one 1 GiB file and one transfer:

- every byte of a download is hashed (SHA-256, for dedupe and `check`), and one stream can't be
  hashed faster than one core runs SHA-256: 3.3 GiB/s on the M5 (`cpu.hash_sha256`, 297 µs a
  MiB). So with the hash on the transfer's path, 4 GiB/s can't be reached by one transfer,
  however it is arranged;
- without the hash (measured by patching it out), the same transfer runs at 3.2 GiB/s with 1
  MiB pieces and 4.6 GiB/s with 4 MiB pieces and a 256 KiB curl buffer. The loop alone, with no
  write at all, reaches 4.8 GiB/s (0.19 s of its CPU per GiB);
- with the hash on the hasher thread beside the write, the transfer waits for the hash. In 0.43
  s for 1 GiB the hasher is busy 0.33 s, and every piece it takes waits for the GIL when it comes
  back, while the loop runs Python. Pipelining the pieces, a larger log of waiting bytes (16 and
  64 MiB), 8 MiB pieces and a shorter switch interval (0.5 ms) each moved it by under 5%;
- what would meet the target is a hash that is not on the transfer's path: a faster or parallel
  hash, or hashing after the file lands. That changes what `flnd.db` stores or when dedupe can
  run. The current code does the second for the size-matched case: `hash` is set only for files
  whose size another saved file has, and dedupe hashes both then.

**`net.small_8_at_once`: 476 ms recorded, against ≤ 300** (0.48 s in the page's history, 0.61 s
at the baseline). 2,000 files of 100 KiB, 8 at once:

- the loop thread is the bottleneck: at the baseline, 0.53 s of its CPU for 0.61 s of wall time;
- the client alone (`Client.fetch` of the same files, no part) took 0.30 s at the baseline. Per
  request, curl_cffi's request layer then cost about 150 µs of the loop's time: 15 `setopt` and
  15 `getinfo` calls through cffi (about 55 µs), re-applying Chrome's fingerprint to the reused
  handle, and parsing the answer's headers (about 50 µs);
- a part adds about 115 µs of the loop's time per file: two hops to a worker thread (15 µs each,
  measured by hopping to a no-op), the last piece's copy into the part's buffer and its hash
  (about 30 µs), and the transfer's own steps (the range request, the meter, the sink). The
  file-system calls themselves (open, lock, write, rename, close: about 110 µs a file, a rename
  into one folder serialized by APFS) run on the worker threads, beside the loop;
- the `Cookie` header is no longer worked out per request. The session caches, per origin
  (scheme, host and port), the cookies it may send, and the header itself when they are all for
  `Path=/`. Any change to the jar (a `Set-Cookie`, `Session.add`, a cookie set or cleared on
  `session.jar`) or a cookie expiring drops the cache. With 20 cookies for the host, the header
  went from 52 µs a request to 0.3 µs. The `net` group sends no cookie, so it moved within noise
  (`small_8_at_once` 622 and 644 ms before, 638 and 605 ms after, alternating);
- curl's handles are driven directly (`flnkit.net._Curls`), not through curl_cffi's
  `AsyncSession`. A handle is set up once (the fingerprint, the timeouts, the redirects, the
  proxy) and kept. A GET sets only its URL, its headers and its callbacks, and reads back only
  its status and URL. A request with another method or a body sets those too, and its handle is
  set up afresh after it. A test sends the same requests both ways to a loopback server and
  compares them byte for byte. Alternating `make bench ARGS=net` runs, before and after:
  `small_8_at_once` 641 and 619 ms before, 480 and 477 ms after; `small_1_at_once` 997 and 943
  ms before, 820 and 721 ms after; `big_rate` unchanged (2.38 to 2.42 GiB/s before, 2.39 to 2.46
  after, three alternating pairs of the big file alone);
- what is left, about 0.48 s, is mostly the part's path above and the loop's own work per
  transfer (asyncio's callbacks, curl's socket actions, the client's retry and cap). Moving the
  last piece's write and hash to the landing thread, or the part's calls onto the loop, made it
  no faster (the second, 5% slower). Going under 300 ms needs the transfers spread over more than
  one loop. That was tried and dropped; see below.

**The other missed targets** have no profiling written up yet. Each is a measured baseline in
`gates.json` and a target in the table above:

- `net.two_disks` (3,915.9 %, against ≤ 10): what a second client writing to a slow disk adds to
  a fast target's downloads;
- `net.per_conn_limit` (0.997x, against ≥ 4) and `net.hls_200` (0.978x, against ≥ 6): the
  recorded ratios are about 1x, so the speed-up these two gates ask for is not there yet;
- `net.big_4_at_once` (1.2x, against ≥ 2.5): four 256 MiB downloads, each hashed;
- `ui.overhead` (16.5 %, against ≤ 3) and `ui.idle_bytes` (654,248 B, against 0): the screen's
  share of a run, and what it writes while nothing changes;
- `e2e.sync_100_targets` (15.4 s, against ≤ 3.84 s): a sync of 100 followed targets with nothing
  new. The target is a quarter of the earlier baseline;
- `e2e.db_contended` (2,104 ms, against ≤ 100 ms): the loop's longest stall while another process
  holds `flnd.db`'s write lock for 2 s;
- `browser.warm_4` (0.278 s, against ≤ 0.091 s): starting four warm tabs beside a target's own.

## Transfer lanes: tried and dropped

A trial ran the transfers in N lanes: N worker processes, each with its own loop and its own curl
session. It was measured and dropped. The code has no lane processes.

- `net.small_8_at_once` with 4 lanes: 386 ms, against a goal of 300 (599 ms before).
- `net.small_1_at_once` with 4 lanes: 1,291 ms, 84% slower than 700 ms.
- It helped `net.stall_20mbs` (10.6 ms to 1.0 ms) and `net.big_4_at_once` (1.5x to 2.7x).

The lanes worked around the per-file cost on the loop rather than removing it. They also added
processes, so Ctrl-C had to reach each one, and they needed the cookies handed to each child.
The work now goes to the per-file cost on the loop itself (see "The next step").

## The next step

This is the plan, not built. The next step cuts the per-file cost on the loop, then re-measures:

1. Profile `net.small_1_at_once` end to end, and find where each file's loop time goes.
2. Batch the store's commits, so that fewer files pay for a commit each.
3. Move the hashing, and any syncing, onto a thread off the loop. Today no file is `fsync`ed on
   its own (see [storage.md](storage.md)), so only what the profile shows is worth moving.
4. Write the sidecar in the post's own transaction.
5. Drop the repeated `stat` and rename calls on each file's path.

Then run `make bench ARGS=net` and `make bench ARGS=db`, compare with the table above, and record.

Lanes come back only if `net.small_8_at_once` is still well over 300 ms after this, and the loop
is shown to be CPU-bound at realistic rates. Even then, a lane starts only when 4 or more file
jobs are waiting.

## Concurrency per host

Each media host's cap adapts (`jobs.Cap`, [architecture.md](architecture.md#network)). It starts
at 8 transfers at once (`CAP`), halves on a 429, a 503, a reset or a timeout, and grows by one
every 10 clean transfers (`STEP`), up to 32 (`MOST`). A step that does not raise the host's
throughput by half of what one more place adds is undone, and the cap holds for a while before it
probes again. The `net` group holds its caps fixed (a limit of 8 for its host, `Capped`), so its
numbers don't move with the adaptation.

Throughput is measured by Little's law: the cap times the bytes over the seconds the window's
transfers took. A first version took bytes over the window's wall time. With only 10 transfers a
window, that rate swung with where in their run the window closed, and on the e2e run the cap
stalled at 9 to 10. With durations it climbs to about 27 there, and holds where a step brings
nothing.

`e2e.latency_500_posts` (500 posts, 150 ms a page, 40 ms a file, the default pace of 0.5 s), A/B
on the M5 above while other work ran (load about 5), the median of 5 alternating runs:

| caps | seconds |
| --- | ---: |
| 8 at once per host (before) | 6.01 |
| 8, adapting to 32 | 4.87 |

The listing's 10 pages, half a second apart, take about 4.65 s, so the run now ends about 0.2 s
after its last page. The `net` group is unchanged within noise (`small_8_at_once` 602 to 607 ms
before and after).

### The real-CDN check

Loopback can't show where a real CDN stops giving more for more transfers at once, so the
ceiling of 32 is confirmed by hand with `tests/bench/cdn_real.py`. Its output contains no URL,
file name or host, only numbers, so it can be pasted here.

1. Collect the URLs of about 200 files on one CDN host from the authorized test accounts (the
   browser's network panel shows them), one per line, in a file outside the repo, such as
   `~/flnd-cdn.txt`.
2. Run `make cdn-real ARGS="--urls ~/flnd-cdn.txt --rounds 3"`. Add
   `--header "Referer: https://www.example.com/"` with the site's own address when its CDN needs
   one (Facebook's and Instagram's do), and `--caps 8,16,24,32,48` to look further. The default
   caps are 8, 16, 24 and 32.
3. It downloads the list through flnd's client at each fixed cap, then once with the adaptive
   default (`--no-adaptive` leaves it out), alternating over the rounds. It prints the median
   seconds and MiB/s per cap, the failures, and the cap each run ended at.
4. Pick the smallest cap past which MiB/s stops rising by more than a few per cent. If it is well
   under 32, lower `flnd.engine.jobs.MOST`. If a run ended below the cap it asked for, the host
   was overloaded there.

## Disk

What writing one downloaded file costs beyond the write itself, per file of 100 KiB, measured
A/B on the M5 above while other work ran (the median of 5 alternating runs of the best of 3):

| how | µs per file |
| --- | ---: |
| `open` + `write` (the floor) | 70.8 |
| before parts: a stage file, a lock file, a replace | 303.4 |
| a download's part: open and lock, write, set the time, one `fstat`, rename | 126.2 |

The lock file cost more than the write itself. A part needs one `open` and one `flock`, and the
history row takes the file's size and time from the `fstat` before the rename, so nothing stats
the file again. A part that a failed run left costs one `stat` more, to check that no other run
landed it meanwhile. `tests/test_fs.py` counts these calls, so a change that adds one fails
`make check`.

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

Each old step cost about 0.6 s whether data came or not (a 3 s wait for growth at the bottom, a
0.3 s pause and a jiggle), and the end cost the whole idle window. Now a step waits only for the
next matching response or the page growing, and the read ends on the last batch's own "no more".
With `human`, nearly all of what is left is the pauses a person would make: 29 steps at a median
of 0.4 s each. `fast` is the 30 answers of 50 ms each, plus the scrolling.

## HTML and JSON paths

Pages are parsed by selectolax (lexbor, a C parser with its own CSS engine) through
`flnkit.html`, and JSON paths are walked by `dig`, which replaced parsel (lxml), jmespath and
python-jsonpath. Measured A/B against the old reads on the M5 above while other work ran (load
about 5), the median of 9 alternating rounds, each the best of 3:

| metric | parsel / jmespath | selectolax / `dig` |
| --- | ---: | ---: |
| `cpu.html_meta` (a 2 MiB Meta-style page: parse, then its JSON blocks) | 18.1 ms | 6.6 ms |
| `cpu.html_images` (the same page: parse, then 20,000 `img[src]`) | 32.1 ms | 10.8 ms |
| `cpu.dig` (`edges[].node` over 1,500 posts; plain dict access is 18 µs) | 238 µs | 46 µs |

- **The old `html_meta` read only the blocks' text.** The new one also decodes the six that hold
  `__bbox`, as Facebook and Instagram do, and is still 2.7× faster.
- **`dig` compiles a path once** and caches it, and walks the commonest shape (`a[].b`) in one
  comprehension. The old 682 µs baseline went through parsel's JSON selectors. jmespath alone
  is the 238 µs above.
- **Neither parser loads at startup.** `flnkit.html` imports selectolax on the first parse and
  msgspec on the first decode, so `flnd --help` and `import flnd.plugin` load neither.
