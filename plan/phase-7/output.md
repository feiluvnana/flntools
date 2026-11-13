# output

Phase 7, steps 0 and 6. How a run looks: by default, a few live status lines; with `-v`, the same
status lines and every line of progress and log as well (user, 2026-11-13).

## Built in step 0

### Columns

`shared.theme.draw_progress` draws a bar with a total in fixed columns, the label last, so every
name starts at one column whatever its length:

```
✓ ━━━━━━━━━━━━━━━━━━━━           45.4 KB posts/2026-11-11_122168984774665018.jpg
✓ ━━━━━━━━━━━━━━━━━━━━            2.2 MB videos/2026-11-09_2964864390550364.mp4
• ━━━━━━━━━━──────────    7.3 MB/14.6 MB videos/2026-08-20_2125341001354648.mp4
✗ ━━━━━━──────────────    1.1 MB/3.9 MB videos/2026-09-24_1418040356948308.mp4 timed out
```

- Glyph, 20 bar cells, the count right-aligned in 17 cells (`COUNT_CELLS`: `999.9 MB/999.9 MB`),
  then the label and caption. A longer line is cut at its end by the live area.
- A line that ended at ok shows its total alone; a failed one keeps `count/total`, so how far it
  got stays on screen.
- A spinner (no total) keeps its label first: `• scroll posts ⠋ 12`.

### Skips

- Each skipped file is a **debug** line naming the file that holds it:
  `· skip posts/122168984888665018.jpg: already saved as posts/2026-11-11_122168984888665018.jpg`.
- At info, each category says once how many it skipped:
  `• fbk:example.page: photos: 131 files already saved`.
- `flnd download -v` (`--verbose`) shows debug lines, and so will `flnd sync -v` from step 6
  (user, 2026-11-13: same rule as `download`). `-v` is how a run is shown, not how a target is
  downloaded, so `sync` still changes no setting.

## Status lines

Step 6. Today every finished file prints a line, so a run of 500 files is 500 lines of scrollback,
and what is happening right now (how many running, shrinking, failing) can't be seen.

### Without `-v`

The live area shows **one status line per running target and category**. A category that ends
leaves its line in the scrollback as its summary. Nothing is printed per file.

```
✓ fbk:example.page  posts    164 found   150 saved   12 skipped   2 failed   24.1 MB  1m 12s
⠹ fbk:example.page  photos   140 found     9 saved  131 skipped    3.4 MB
                             4 downloading  1 shrinking
```

- **The first line** is the state glyph (spinner, `✓` or `✗`), the target in bold, the category
  dim, then the counts in fixed-width slots, so lines of several targets line up too: found,
  saved, linked ([links](links.md)), skipped, failed, then the bytes saved and the time taken.
- **The second line**, only while something runs: downloading and shrinking ([shrink](shrink.md)), and a rate-limit or block wait as its countdown (`waiting 4m 10s: Facebook says
  you've been temporarily blocked`).
- **Colors** follow what needs the eye: downloading cyan, shrinking magenta, saved green, linked
  blue, skipped and zero counts dim grey, failed red, the bytes and time dim. A count is drawn in
  its color only when above zero.
- **Warnings and errors still print in full** above the live area: a file that failed, with why;
  a rate limit; a refused login. They are what a person must read.
- **The source's own "N files found" lines become debug**, since the status line carries found.
- **A narrow terminal** drops the words and keeps the glyphs and numbers
  (`↓4 ⟳1 ✓150 ↷12 ✗2`), then the bytes and time; it never wraps.
- **Not a terminal** (a pipe or file): no live area; each category's summary line and the final
  total, plus warnings and errors.
- `Complete. 159 files` stays the last line, with the run's totals.

### With `-v`

The **same status lines**, and every line of progress and log as well:
- **in the live area**, under each status line, the files running now, as today's rows (as many
  as the source downloads at once);
- **in the scrollback**, every finished file in the [columns](#columns) above; every skip, dim,
  with the file that holds it: `↷ posts/122168984888665018.jpg → posts/2026-11-11_122168984888665018.jpg`;
  every link made (`⛓ reels/… → ../videos/…`); every shrink (`⟳ videos/….mp4 14.6 MB → 6.1 MB`);
  and the source's own debug lines (pages read, replays, scrolls).

These are the lines every run's log file holds anyway, `-v` or not ([layout](layout.md#run-logs)).

Sizes are dim and names in normal weight, so the names stand out.

### How it is built

- **`shared.log`** gains a status row: a row whose text is drawn by a callback each frame, that
  isn't printed per change, and that stays in the scrollback when it ends. The live area already
  redraws only changed rows on one thread (Phase 6 [ui](../phase-6/ui.md)).
- **flnd's job** counts per category: found, downloading, shrinking, saved, linked, skipped,
  failed, bytes. Download threads and the encode pool change the counts under the job's lock.
- **The per-file rows** are made only under `-v`, so without it the live area holds only status lines.
- **Glyphs** get ASCII fallbacks in `theme.glyphs` like every other (`↓` `v`, `⟳` `~`, `↷` `>`,
  `⛓` `=`), and none of them is ambiguous-width.

`sync` takes `-v` with the same rule (user, 2026-11-13).
