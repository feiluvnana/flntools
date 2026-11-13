# speed

Phase 7, step 8. The user asked on 2026-11-13 for **very fast downloads** and a downloader that
is **very strong, learning from IDM**, and approved the proposal below the same day.

flnd has two speeds, and they carry different risks:
- **Listing**: requests to the site itself (instagram.com, x.com, …). This is what gets an account
  checkpointed or blocked, so it is paced.
- **Downloading**: the files from the CDNs (fbcdn.net, twimg.com, …), with no login sent. CDNs
  are built for this, so this is where speed comes from.

## Today

`shared.http.download` already: resumes from a `.part` with `Range` and `If-Range` when the
server sends a validator; splits a file of 16 MB or more into up to 4 fixed ranges on hosts that
passed a live check (`net.SPLIT_HOSTS`); holds 6 connections per host across the process; tries
mirrors in order. flnd's job runs `Source.concurrency` (4) files at once per target, and the
targets of a run one source at a time.

## 1. The downloader, after IDM

In `shared.http.download`, each with its tests. Revised after a performance review on 2026-11-13,
measured on local fakes, found flaws in the first draft (noted below).
1. **Bounded pieces, IDM's way.** A big file is cut into fixed pieces of 2–4 MB, each asked for
   with its own `Range: bytes=a-b`, and the file's connections pull the next piece from one
   queue; a connection that finishes its piece takes another. Every stream ends cleanly at its
   piece's end, so connections stay alive for the next piece and the next file (today the first
   range reads a whole-file 200 and is reset when it stops early, and every split pays a new TLS
   handshake), and no running range is ever cut in half. *First draft:* cutting the largest
   running range in half, which would reset a connection at every cut.
2. **Stall detection, from MDM** (misterdownloadmanager's measured rules): a connection with no
   first byte after twice the file's first time-to-first-byte (at least 20 ms) gives its piece to
   another; after 2 s, a connection slower than a quarter of **the best speed any connection of
   this file has sustained** reconnects, when it has more than 2 s of work left at its own pace.
   Measuring against the best seen, not the others, still works when every connection is on a slow
   path (MDM: 32 MiB in about 2 s instead of 14 with one request in three slow). Nothing is shed
   while the disk is the bottleneck. *First draft:* 10 s of silence or a tenth of the others for
   15 s, which leaves a tail piece holding a file back.
3. **Adaptive connections.** Each host starts at 4; while the host's throughput rises with one
   more, another is added, up to **8 per host** (was 6). When a host refuses a connection while
   others are receiving, its cap becomes the most connections seen working, after two such
   refusals, not one; a cap is forgotten after 10 minutes, so a long `sync` probes again (MDM).
4. **Smaller split threshold**: 4 MB instead of 16, as most videos are 2–15 MB; **up to 8
   connections** on one file, within the host's free ones.
5. **HTTP/2 for small files only.** Photos and small files share one HTTP/2 connection per CDN
   (httpx `http2=True`, `h2` in the `http` extra) instead of a handshake each; **a split file goes
   over HTTP/1.1**, since HTTP/2 multiplexes every stream onto one TCP connection, and 8 pieces
   would share one congestion window. A host that misbehaves on HTTP/2 falls back for the run.
   *First draft:* HTTP/2 for everything.
6. **One client per run** with a pool sized to the hosts' limits, so files reuse connections.
   *Dropped from the first draft:* 1 MB writes and saving `.part.json` every 8 MB; measured,
   `download` already runs at 1.88 GB/s against 1.94 GB/s for a raw copy, with 64 KB chunks and
   no fsync, so they would add code and gain nothing.
7. **Videos first.** Within a target, videos are started before photos, so the run doesn't end
   with one big video alone; the job knows a file's kind from its extension. *First draft:*
   biggest first, which does nothing, as only tlg knows a file's size before asking.
8. **Speed in the status line**: each target's MB/s and the run's, from the bytes written in the
   last 5 s ([output](output.md#status-lines)).
9. **CDN throttles don't slow the listing.** Today one `Backoff` per source serves its API calls
   and its CDN downloads (`site.py`, `net.Transport`): a 429 from fbcdn.net pauses the listing and
   every download, and doubles the source's learned listing gap (up to 8×, saved). The site and
   its CDNs get a back-off each, and only the site's teaches the pacer. Adaptive connections
   (item 3) probe the CDN's limit on purpose, so this comes with them.
10. **DASH video and audio at once**, and kept across a merge retry; today they download one
    after the other and a failed merge deletes both.
11. **Retry once at once.** A dropped connection is retried straight away, then after 1 s,
    doubling to 30 s; most drops are one-offs (MDM). A CDN's `Retry-After` is kept up to 300 s.
12. **Pieces stay in order on disk**: the piece queue hands out pieces from the front, so a part
    file is never extended far past its written end (NTFS zero-fills everything before such a write).
13. **The server's hash, when it sends one** (`Digest`, `Repr-Digest`, `Content-MD5`,
    `x-goog-hash`), is checked on the finished file, catching corrupt bytes the size and media
    checks miss (loi's archive hosts send these).
14. **Room on disk** is checked before a big file: its size plus 128 MB must be free, and a file
    of 4 GiB or more is refused on FAT32 with a message, before any byte is written.

## 2. More at once

- **8 files at once per target** (`Source.concurrency` 4 → 8; it becomes the engine constant
  [lean](lean.md) E2 asked for).
- **Several targets at once, one per source**: with [lean](lean.md) E5's one run, an Instagram
  target and a Facebook target download side by side; each source keeps its own pace, so no site
  sees more requests than before.
- **Range splitting on more CDNs**: `cdninstagram.com` and `tiktokcdn.com` join `SPLIT_HOSTS`
  after a live check by hand (206, ranges identical), as the others passed.
- **Telegram**: `cryptg` in the `telegram` extra (Telethon uses it by itself for a large decryption
  speed-up), and 3 files at once, each flood wait honoured as it is today.

## 3. Less work around the downloads

Measured on local fakes (a 10-core Mac, ffmpeg 9) on 2026-11-13:
- **`sync` stops decoding the whole library.** `repair.repair` fully checks every saved file on
  every sync, before its first request: a Pillow decode per photo (4 ms) and two ffmpeg runs over
  every packet per video (34 ms for 5.5 MB), one file at a time. 40k photos and 10k videos cost
  about 10–20 minutes, and every byte read from disk. After: the history row keeps the file's
  size and modified time after postprocessing; a file whose size and time match is trusted, and
  the full check runs on a file that changed, plus a rolling 1/30 of the rest each sync, so bit
  rot is still found within a month of syncs. The checks run on a thread pool, and repair
  overlaps the listing instead of coming before it.
- **`--shrink` skips encodes that can't win.** An encode at `mid` that comes out bigger is thrown
  away after costing its whole time (a CDN clip at CRF 23 re-encoded slower and bigger in the
  test). A video whose bitrate is already at or below the level's target is left as it is; for
  the rest, a 4 s slice is encoded first and the whole file only when the slice came out at
  least 10% smaller. Videos stay one encode at a time (four at once were only 9% faster).
- **Photos shrink on a pool**: `compress.apply` re-encodes saved photos one at a time (29 ms
  each, 10k in about 5 minutes); Pillow releases the GIL, so a pool of 8 threads.
- **Telegram resumes and splits**: a failed file restarts from 0 today (`tlg` opens `dest` with
  "wb"); it resumes from a part with `iter_download(offset=…)`, and a big file fetches its parts on
  several connections (as FastTelethon does). `cryptg` first: without it, decryption runs at
  21 MB/s on the one loop every Telegram download shares.

Checked and fine: history reads and writes (0.04 ms a write, WAL), the live area (one thread, 10
frames a second, idle when nothing changes), the job's waits, startup (150 ms, heavy libraries
already imported lazily), the check after a download (192 bytes read), photo encode settings.

## 4. Listing pace

| Site | Today | After | Why |
| --- | --- | --- | --- |
| ins | floor 1.0 s; 275 GraphQL per 600 s, 75 `/api/v1/` per 660 s | **floor 0.5 s**, same budgets | the budgets are Instaloader's measured ceiling and stay; the floor only spaces a burst while a window has room |
| twt | floor 0.5 s, and X's rate headers | **floor 0.2 s** | the headers stop flnd exactly at the limit |
| fbk | floor 1.5 s | **floor 0.8 s** | no published limit; the learned speed doubles on a throttle and is remembered, so the first block teaches it |
| tik | floor 1.0 s | 1.0 s | the browser path is the bottleneck, not the pace |
| loi | 0.5 s | 0.5 s | no data |

The learned speed ([pace](../phase-6/pace.md)) is unchanged: it starts at the floor, doubles on
each throttle, eases back after 50 clean requests, and is saved per source.

**Resuming without a validator** (user, 2026-11-13; it relaxes Phase 6's redesign B6). A part
whose server sent no `ETag` or `Last-Modified` is dropped today; after this, it is resumed once its
last 64 bytes, fetched again, equal what is on disk (MDM's overlap check), and dropped when they
don't. A drop at 90% then costs 10%, not the whole file. Such a file still isn't split, since
its pieces couldn't be checked against each other.

## Sources

misterdownloadmanager (`crates/engine/src/download.rs`, `planner.rs`, `hosts.rs`, `verify.rs`,
`sparse.rs`, `disk.rs`); Instaloader's `RateController` (275 GraphQL per 10 min; 200 per query and 75 other per 11 min);
gallery-dl's defaults (Instagram 6–12 s between requests; no delay for Facebook, X or TikTok);
X's rate-limit headers (`x-rate-limit-*`, 15-minute windows); Telethon's FAQ (`cryptg`; flood
waits are the ceiling, parallel transfers reach them sooner). Checked 2026-11-13.

## Tests

Fakes only (`httpx.MockTransport`, a fake clock): a big file is fetched in bounded pieces whose
connections are reused; a stalled piece goes to another connection and its bytes are kept; a
host's connections grow while throughput rises and shrink on a 429; a 4 MB file splits and a 3 MB
one doesn't; a split file goes over HTTP/1.1 and photos over HTTP/2, which falls back to 1.1;
videos start before photos; a CDN 429 doesn't slow the listing; DASH streams download together; a
`sync` trusts unchanged files and checks changed ones and its rolling share; a video already at
the target bitrate isn't encoded; a Telegram file resumes; each source's floor is the table's.
