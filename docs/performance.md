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
| `net` | flnd's HTTP client from a loopback server: 1 GiB to disk, 2,000 × 100 KiB on 1 and 8 threads |
| `disk` | writing 5,000 files of 100 KiB, from a plain write up to `fs.write` |
| `db` | the history store over 100k rows; SQLite commits per file, per post and per 500 rows |
| `walk` | listing 100,000 files, and `stat` on known paths |
| `cpu` | JSON, a JSON path, HTML parsing, hashing, the loop bridge, path planning, the worker pool |
| `e2e` | a whole run of the real engine on a faked site, with and without latency |
| `browser` | headless Chromium on a synthetic infinite-scroll feed; skipped without Chromium |
| `media` | Pillow's AVIF, WebP and JPEG encoders; ffmpeg's video encoders, skipped without ffmpeg |

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
| `net.small_8_threads` (2,000 × 100 KiB) | 927 ms | ≤ 300 ms |
| `disk.fs_write` (per file) | 255 µs | ≤ 130 µs |
| `db.point_read` | 2.39 µs | ≤ 2.2 µs |
| `db.commit_per_post` (per file) | 8.1 µs | ≤ 15 µs |
| `e2e.sync_nothing_new` | 4 ms (4,000 files) | ≤ 0.3 s (100k files) |
| `e2e.latency_500_posts` (150 ms a page, 40 ms a file) | 7.16 s | ≤ 5.5 s |
| `browser.scroll_default` (the synthetic feed) | 26.0 s | ≤ 16 s human-like, ≤ 3 s fast |
| `cpu.html_meta` (a 2 MiB Meta-style page) | 18.0 ms | ≤ 8 ms |
| compressing 1,000 JPEGs | not built yet | ≤ 2 min |
| `sync --compress` over 100k done files | not built yet | ≤ 3 s |

`gates.json` has the rest: 68 numbers in all.
