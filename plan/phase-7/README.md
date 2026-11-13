# Phase 7: honest listings and a calm screen

Phase 6 is complete. Phase 7 comes from a live `flnd download` of a Facebook test page on
2026-11-13, whose log the user read. They asked four things: why files were skipped, why the log
didn't line up, that every file have its date, and that a run show what it is doing at a glance,
with every line still there under `-v`. They then asked for `.flnd/` to be laid out as media,
metadata and a log of each run.

| Problem | Where it's solved |
| --- | --- |
| the log doesn't line up: each bar starts after a name of a different length | [output](output.md#columns): fixed columns, the name last (**built**, step 0) |
| hundreds of `skip` lines, without saying why | [output](output.md#skips): one line per category at info; each skip at debug, naming the file that holds it (**built**, step 0) |
| Facebook's feed lists one photo twice, dated and undated, so files get either name | [dates](dates.md#1-one-listing-memory-in-the-job): one listing memory in the job, for every source |
| some photos have no date | [dates](dates.md#2-every-file-dated): each source's `date` hook, then an estimate; `sync` dates saved files |
| `--image`, `--video`, `--audio` ask for formats and levels most people don't want to choose | [shrink](shrink.md): one `--shrink` flag, a balanced compress in each file's own format |
| downloads should be very fast, the downloader as strong as IDM | [speed](speed.md): dynamic segmentation, stall detection, adaptive connections, HTTP/2, 8 files at once, sources side by side, lower listing floors |
| bugs found by a review: `sync` can't restore lost files, a failed carousel file never retried, X marks failed listings complete | [fixes](fixes.md): five bugs, each with a test that fails first |
| `sync` decodes the whole library every run; encodes that can't win; one back-off for site and CDN | [speed](speed.md#3-less-work-around-the-downloads) |
| `shared` makes flnd think: rebuilt fetches, side channels for headers, error text matched, no way to stop a download | [shared](shared.md): `shared` as a black box |
| files flnd never gets (X's older tweets, a second CDN host) | [fixes](fixes.md#gaps-found-by-comparing-with-gallery-dl), from gallery-dl |
| flnd's code could be leaner | [lean](lean.md): proposals from two audits, about 425 lines |
| a file found in two tabs (reels and videos, photos and posts) is in only one folder | [links](links.md) |
| `.flnd/` mixes media and stores, and a run leaves no record to audit |  [layout](layout.md): `.flnd/media/`, `.flnd/metadata/`, `.flnd/log/` with every run's log |
| a run prints a line per file; it should show what it is doing at a glance | [output](output.md#status-lines): live status lines by default, and every line as well under `-v` |

## Order

Each step ends with `check` green and is committed on its own.
0. [output › columns and skips](output.md#built-in-step-0): the column layout, per-category skip
   lines, `download -v`. **Built** (`9df3e36`).
1. [layout](layout.md): `media/`, `metadata/` and `log/`, and a log of every run at debug. **Built**, with lean's E9.
2. [shrink](shrink.md): `--shrink` replaces `--image`, `--video` and `--audio`. **Built**.
3. [fixes](fixes.md): five bugs from the 2026-11-13 review.
4. [dates › one listing memory](dates.md#1-one-listing-memory-in-the-job): in the job, for every
   source: each key once, one key → date map, undated posts held until their category ends.
5. [dates › every file dated](dates.md#2-every-file-dated): a source's `date` hook, then an
   estimate; `sync` dates files saved without a date.
6. [links](links.md): a file found again in another tab's folder is linked there.
7. [shared](shared.md): `shared` as a black box: `http.fetch` used, `download` and `goto` return
   their headers, downloads stoppable, errors caught by name, private imports gone.
8. [speed](speed.md): the downloader after IDM and MDM, more at once, less work around the
   downloads, and lower listing floors.
9. [output › status lines](output.md#status-lines): live status lines, colors, `-v` adding every
   line under them.

The layout comes first, since every later step writes paths and history rows, and the run log
records what the later steps add. `--shrink` comes next, since the new `metadata/` means no old
setting needs reading. The fixes come before the steps that build on the job and `sync`.
`shared` comes before speed, whose downloader builds on `download`'s new return and `stop`. Step 9
builds on step 6's counts (linked files are counted) and step 8's speeds, and steps 4–5 change
what it counts as skipped. Step 9 also gives every line its colour
and the running rows their motion ([output](output.md#colors-and-motion)).

## Decisions

All decided by the user on 2026-11-13:
1. **Progress lines draw the name last**, after fixed columns: glyph, bar, count ([output](output.md#columns)).
2. **Skips are summed per category at info**; each skip is a debug line with the file that holds it ([output](output.md#skips)).
3. **Every file has a date.** For a Facebook photo with none in any listing: the photo's own page,
   then the nearest dated neighbour, marked as estimated ([dates](dates.md#2-every-file-dated)).
4. **A file found in a second tab is a symlink there** ([links](links.md)).
5. **Without `-v`, a run shows live status lines** (how many files are downloading, shrinking,
   saved, skipped, failed); **with `-v`, the same status lines and every line** of progress and
   log as well ([output](output.md#status-lines)).
6. **`.flnd/` holds `media/` (renamed from `downloads/`), `metadata/` (every store) and `log/`
   (a log of each run, kept 30 days)**, with no migration of an old `.flnd/` ([layout](layout.md)).
7. **`sync` dates files saved without a date**: it finds them in the history, asks the site for
   their info and renames them ([dates](dates.md#files-already-saved-without-a-date)).
8. **Symlinks, listing memory and dating apply to every source**, not Facebook only.
9. **`sync -v`**, with the same rule as `download -v` ([output](output.md#skips)).
10. **flnd's code is made leaner**, under the same conventions ([lean](lean.md)); every low-risk
    proposal is taken.
11. **`--shrink` replaces `--image`, `--video` and `--audio`**: a balanced compress of every
    photo, video and audio file, in its own format, saved for the target ([shrink](shrink.md)).
12. **[lean](lean.md)'s three calls, as recommended**: tik's direct API path deleted, one retry layer (E1), one run grouped by source (E5).
13. **Very fast downloads** ([speed](speed.md)): the downloader learns from IDM, 8 files at once,
    sources side by side, and listing floors ins 0.5 s, twt 0.2 s, fbk 0.8 s.
14. **Every log line gets a sensible colour; running rows animate** ([output](output.md#colors-and-motion)).
15. **The bugs and the performance findings of the 2026-11-13 reviews are fixed** ([fixes](fixes.md), [speed](speed.md#3-less-work-around-the-downloads)); `sync` trusts unchanged files and fully checks changed ones plus a rolling 1/30 of the rest.
16. **`shared` is a black box**: flnd doesn't think for it ([shared](shared.md)).
17. **The screen is pleasant, not flashy**: muted colours, one soft motion per running line, a still scrollback ([output](output.md#colors-and-motion)).
18. **What gallery-dl and misterdownloadmanager do better is taken** where it fits ([fixes](fixes.md#gaps-found-by-comparing-with-gallery-dl), [speed](speed.md)).
19. **`shared`'s public API is judged by usefulness**: unused is not dead, and only aliases go ([shared](shared.md)); `media`'s API stays whole.
20. **A part resumes without a validator** after a 64-byte overlap check ([speed](speed.md#4-listing-pace)); **an interrupted listing restarts from its cursor**; **story music, quoted tweets' media and card images** are saved with their posts ([fixes](fixes.md#gaps-found-by-comparing-with-gallery-dl)).

3 and 7 reverse Phase 6 [simplify §3](../phase-6/simplify.md#3-no-history-migrations)'s "no
rename when the real date is found": the user's later decision.

No decision is open.
