# Performance

How fast flnd is, how to measure it, and how a change proves it made things faster.

## Running the benchmarks

```sh
make bench                              # every group, a few minutes
make bench ARGS="net disk"              # only these groups
make bench ARGS=--gate                  # exit 1 on a regression against the baseline
make bench ARGS="db --gate --tolerance 5"
make bench ARGS=--record                # store this run as the new baseline
```

`make bench` runs `tests/bench/run.py` inside the project environment
(`uv run python tests/bench/run.py`). It is never part of `make check`: it takes minutes, and timings are
noisy. Everything is offline: a loopback HTTP server, synthetic data, and scratch folders that
are removed afterwards.

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
| `media` | Pillow's AVIF, WebP and JPEG encoders; the compress step over 1,000 JPEGs; `sync --compress` over 100k done files; ffmpeg's video encoders, skipped without ffmpeg |

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

Recorded on 2026-11-16 at `9d1cf13`, on an Apple M5 (10 cores, 32 GB, APFS SSD), macOS 26.2,
CPython 3.14.6. The numbers a planned change is gated on:

| metric | baseline | target |
| --- | ---: | ---: |
| `startup.help` (`flnd --help`) | 162 ms | ≤ 40 ms |
| `net.big_rate` (1 GiB to disk) | 1.76 GiB/s | ≥ 4 GiB/s |
| `net.small_8_at_once` (2,000 × 100 KiB) | 927 ms | ≤ 300 ms |
| `net.stall_20mbs` (the loop's longest stall, 20 MB/s disk) | (new) | < 100 ms |
| `disk.part_write` (per file) | 255 µs | ≤ 130 µs |
| `db.point_read` | 2.39 µs | ≤ 2.2 µs |
| `db.commit_per_post` (per file) | 8.1 µs | ≤ 15 µs |
| `e2e.sync_nothing_new` | 4 ms (4,000 files) | ≤ 0.3 s (100k files) |
| `e2e.latency_500_posts` (150 ms a page, 40 ms a file) | 7.16 s | ≤ 5.5 s |
| `browser.scroll_default` (the synthetic feed) | 26.0 s | ≤ 16 s human-like, ≤ 3 s fast (`scroll_fast`); see "Browser feeds" |
| `cpu.html_meta` (a 2 MiB Meta-style page) | 18.0 ms | ≤ 8 ms |
| `media.jpegs_1000` (compressing 1,000 JPEGs) | 23.2 s (measured under load, below) | ≤ 2 min |
| `media.sync_compress_100k` (`sync --compress` over 100k done files) | 0.44 s | ≤ 3 s |

`gates.json` has the rest: 68 numbers in all.

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

- **Bytes go to one writer thread, a MiB at a time.** curl hands the loop 16 KiB chunks; they
  are gathered, and each full MiB is written on the `flnd-writer` thread while the loop goes
  on. Measured with the writes on the loop, four files onto a disk throttled to 20 MB/s held the
  loop up for 1.2 s at a time; on the writer thread, 15 ms. The last piece of a file, under a
  MiB, is copied into the part's own buffer on the loop and reaches the disk when the part
  lands, so a small file costs no hop to the writer at all.
- **A transfer waits for the disk with at most 4 MiB in memory.** curl_cffi queues every chunk
  curl receives, with no bound; on a disk slower than the network a whole file would wait
  there. Once 4 MiB of a transfer wait, flnd pauses it (`curl_easy_pause`) until the writer
  has taken them down to 2 MiB, and TCP holds the server back meanwhile. Measured with two
  64 MiB files onto a disk throttled to 20 MB/s: at most 4.0 MiB waited per transfer, against
  62.9 MiB unpaused, in the same time (7.3 s against 7.2 s).
- **A part's open and rename run on worker threads.** Each is a file-system call of 50 to 150
  µs, more on a busy disk; on the loop they would run one at a time.
- **Small files are bound by the disk bookkeeping**, not the client: the client alone fetches
  2,000 × 100 KiB in about 0.4 s on the same machine, and a part's open, lock and rename add
  the rest. A rename into one folder is serialized by the file system.
- `net.stall_20mbs` charges every write of a part as a disk write at 20 MB/s, the last piece's
  copy into memory included, so it overstates a real slow disk.

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

## Compression

What `--compress` costs and saves ([media.md](media.md)). Measured on 2026-11-16 on the
Apple M5 above, with other work running, so read the times as upper bounds:

| number | what | result |
| --- | --- | ---: |
| `media.jpegs_1000` | the whole compress step (plan, encode, SSIM check, place, record) over 1,000 synthetic 1080×1350 JPEGs at quality 90, on the photo lane's 2 threads | 23.2 s, every file compressed to AVIF, 76% smaller |
| `media.sync_compress_100k` | open the store, read 100,000 history rows of done files once, and queue none | 0.44 s |

A synthetic photo (a gradient, shapes and noise) compresses far better than a real download,
which the site has already compressed: these savings say nothing about real files, only the
times do. One AVIF encode takes about 0.02 s for a 1080×1350 photo and 0.11 s for a 12 MP one
(`media.image_avif_speed_8`), so 1,000 full-size camera photos take several times longer.

### Real samples (hand check)

The defaults (the candidates, AVIF quality 60, SVT-AV1 CRF 35 preset 8, and which kinds are left
alone) are confirmed on real downloads: about 100 photos and 20 videos from the authorized test
accounts, kept in a folder outside the repo. The script copies each sample, compresses the copy
exactly as `--compress` would, and prints one row per kind: files, compressed / kept / failed,
median and total saving, the lowest and median SSIM, and the time per file. It prints no file
names.

```sh
make media-real ARGS="~/flnd-samples"                # videos to AV1, the default
make media-real ARGS="~/flnd-samples --video hevc"   # videos to HEVC
```

Results: not measured yet. A kind whose median saving comes out under 15% is left alone by
default; the table marks it.

Whether AV1 plays on the user's own devices (Photos and QuickTime on the iPhone and Mac) is
checked by hand, with a file `--compress` made, before AV1 stays the default.
