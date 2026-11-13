# bench: measuring performance without the network

Part of [Phase 8](README.md). [speed](speed.md) sets a target for each change; this file is how
they are measured and kept: a benchmark suite with no network, its baselines, and how a target
stays met.

Everything here is **added** and needs the user's OK; the suite itself was the draft's idea.

## 1. How it runs

```
uv run --no-project scripts/dev.py bench              # every scenario, a table, about 3 minutes
uv run --no-project scripts/dev.py bench D2 K1        # some scenarios
uv run --no-project scripts/dev.py bench --check      # fail when a scenario misses its target
```

- **Download scenarios** use a server on `127.0.0.1` (an ephemeral port), started by the suite and
  stopped after it: `tests/bench/server.py`, the stdlib's `ThreadingHTTPServer` with keep-alive,
  `Range`, `ETag`, `Last-Modified` and per-scenario faults; HTTP/2 scenarios use `hypercorn` over
  the same handlers, from a `bench` dependency group, so nothing joins `dev`. It drives
  `shared.http` and flnd's job with a fake site whose files point at it (question 9).
- **Everything else** runs in process on generated data: media made with Pillow and the bundled
  ffmpeg, a `kv` store of 100,000 entries, a fake screen.
- Every saved file is compared byte for byte, so a fast wrong answer fails.
- **No real site, no network, no cookie.** Nothing leaves the machine.
- **Not in `check`**: timings are noisy. `check` runs the
  [counts](tests.md#6-counts-that-stand-in-for-speed).

## 2. Scenarios

| # | Scenario | Measures | Target ([speed](speed.md)) |
| --- | --- | --- | --- |
| D1 | `http.download` of one 1 GB file | throughput, CPU s per GB | ≥ 90% of a raw loopback copy |
| D2 | 32 MB, piece connections 60 ms to first byte, the first request 10 ms | requests, wall time | 16 requests; within 10% of an even network |
| D3 | 1,000 photos of 100 KB through flnd's job over HTTP/2 | wall time, CPU per file | ≤ 1.1 × aria2c at the same concurrency |
| D4 | 20 videos of 50 MB at once | throughput, peak memory | memory ≤ 64 MB over idle |
| D5 | 64 MB cut at 90%; then resumed with another width | bytes fetched again | ≤ one piece + 64 B; = bytes missing |
| D6 | a host refusing past 5 connections, 200 files | failed files, the cap reached | none failed; cap 5 within two refusals |
| D7 | 20 transfers, then the second Ctrl-C | time until all stopped | ≤ 1 s |
| D8 | a slow progress callback (1 ms) on an 8-connection file | throughput | unchanged within 5% |
| K1 | `kv` `items()` of 4,000; `clear()` of 2,000; 1,000 writes | ms | ≤ 1.5; < 2; < 3 (and 0.008 ms a write with NORMAL) |
| L1 | a frame of 40 rows, unchanged and all changed | µs per frame | < 20; < 150 |
| L2 | `log.info` written, `debug` dropped, dropped with a record open | µs | ≤ 3; 0.3; 6 |
| M1 | `media.open` of a 150-frame GIF and WebP | ms | < 3 each |
| M2 | `check` of a 5 MB video | ms | about 18 |
| M3 | `frames(count=9)` of a 2-minute and a 20-minute 1080p clip | s | < 0.5 for both |
| M4 | `compress` of a 26 MB animated GIF | s | −50% from the first run |
| W1 | `worker.map` of 20,000 trivial tasks, with and without `timeout` | µs per task | ≤ 8; no extra thread per try |
| A1 | `archive.entries` of a 4-part split zip | temporary bytes written | 0 |
| S1 | `Selection.match` on a 2.4 MB page | ms | ≈ the bare regex |
| F1 | flnd `sync` re-dating with 100,000 history entries | ms per target | < 5 |
| F2 | flnd release of 20,000 undated posts | ms | < 10 |
| F3 | flnd repair's walk of 20,000 files | ms | ≤ 50 |

## 3. Baselines, in the same run

A download target is a **ratio against something measured in the same run on the same machine**,
so it means the same on any machine, and no machine's name or numbers enter the repo:

| Baseline | Used by | How |
| --- | --- | --- |
| a raw copy | D1, D4 | a socket read into a file, one connection, 1 MB buffers |
| aria2c | D3 | `aria2c -j 8 -x 1` over the same files; skipped, with a note, when not installed |
| the even network | D2 | D2's file with every wait 60 ms |

In-process scenarios (K, L, M, W, A, S, F) use absolute bounds, set generously against today's
numbers on the slowest machine the user runs, as their improvements are 5× or more.

## 4. What a run prints

```
scenario  measure          now        baseline   ratio   target   ok
D1        throughput       1.86 GB/s  1.94 GB/s  0.96    ≥ 0.90   ✓
D2        requests         16         16         1.00    = 16     ✓
M1        GIF open         2.1 ms     —          —       < 3 ms   ✓
…
```

(The numbers above show the layout, not a measurement.)

## 5. Targets and how they are kept

1. The suite runs first on the code as Phase 7 leaves it: the starting numbers, kept in the
   commit message that adds the suite.
2. `tests/bench/targets.json` keeps each scenario's target (a ratio with 15% tolerance, or a
   bound); `bench --check` compares a run with it.
3. A commit that touches a hot path of [speed](speed.md) runs `bench --check` before it is made,
   and carries the table in its message.
4. Raising or lowering a target is its own commit that says why.

## Risks

- **aria2c missing**: D3's ratio is skipped; its absolute bound still runs.
- **The loopback server as the bottleneck**: it streams from memory, and the raw-copy baseline
  shows the ceiling, so a ratio near 1.0 means "as fast as this machine goes".
- **Laptops throttle**: each scenario runs three times and keeps the median.

## Left out

Benchmarks against real CDNs (live checks, by hand if ever); spinning-disk targets (a hand run
decides whether a per-disk limit is worth adding).
