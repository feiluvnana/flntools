# output

Phase 7, steps 0 and 9. How a run looks: by default, a few live status lines; with `-v`, the same
status lines and every line of progress and log as well (user, 2026-11-13).

## Built in step 0

### Columns

`shared.theme.draw_progress` draws every progress line the same way: glyph, 20 bar cells, the
label, then the count and caption, one space apart (user, 2026-11-13: the size last, no wide gap):

```
✓ ━━━━━━━━━━━━━━━━━━━━ posts/2026-11-11_122168984774665018.jpg 45.4 KB
✓ ━━━━━━━━━━━━━━━━━━━━ videos/2026-11-09_2964864390550364.mp4 2.2 MB
• ━━━━━━━━━━────────── videos/2026-08-20_2125341001354648.mp4 7.3 MB/14.6 MB
• ⠋                    posts/2026-09-30_example_12.jpg 12.0 KB
✗ ━━━━━━────────────── videos/2026-09-24_1418040356948308.mp4 1.1 MB/3.9 MB timed out
```

- The bars and the names start at one column on every line; the size follows its name.
- A line that ended at ok shows its total alone; a failed one keeps `count/total`, so how far it
  got stays on screen.
- **A download whose size the server didn't send** (no `Content-Length`) has no total: its
  spinner sits in the bar's column while it runs, and it ends with full cells like any other, so
  no line is left without a bar.

### Skips

- Each skipped file is a **debug** line naming the file that holds it:
  `· skip posts/122168984888665018.jpg: already saved as posts/2026-11-11_122168984888665018.jpg`.
- At info, each category says once how many it skipped:
  `• fbk:example.page: photos: 131 files already saved`.
- `flnd download -v` (`--verbose`) shows debug lines, and so will `flnd sync -v` from step 9
  (user, 2026-11-13: same rule as `download`). `-v` is how a run is shown, not how a target is
  downloaded, so `sync` still changes no setting.

## Status lines

Step 9. Today every finished file prints a line, so a run of 500 files is 500 lines of scrollback,
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
- **Colors** follow what needs the eye, muted except where it matters: downloading cyan, shrinking magenta, saved green, linked
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

### Colors and motion

The user said on 2026-11-13 that the screen is **too white and hard to look at, especially under
`-v`**: every log line needs a sensible color and behaviour, and running rows may animate. Rules:
- **Less white.** Plain text is the exception, kept for what a person must read: a file's name,
  a warning's or error's message. Everything else is toned down:
  - **debug lines** (`-v`) are faint grey, glyph and all, so the detail sits behind the status;
  - **info lines** are muted, with only their subject in normal weight (the target, the category);
  - **a path** draws its folder dim and its file name normal; **sizes, counts and times** dim;
  - **the target** (`fbk:example.page`) bold, **the category** in its own muted accent.
- **Levels keep their meaning**: ok green, warn yellow, error red, each painting its glyph and its
  subject; the message's detail after a colon is dimmer than its head.
- **Skips** (`↷`) and links (`⛓`) dim grey and blue: seen, not read.
- **Calm, not flashy** (user, 2026-11-13: "nice aesthetic, but don't be too flashy"). Motion
  only where something is running, and only one kind at a time on a line:
  - a running bar's filled cells carry a soft **gradient** (two neighbouring shades of the accent
    in 24-bit colour, a short 256-colour ramp otherwise) that drifts slowly along them;
  - the spinner steps through the accent's shades;
  - a waiting row (rate limit, block) is steady yellow, with its countdown; no pulsing;
  - counts don't animate; they change as they change.
  A row that ends stops moving and keeps one flat colour (green bar at ok, red at error), so the
  scrollback is still. No blinking, no bold colour blocks, no emoji.
- **Cheap and safe**: the animation runs on the live area's one drawing thread at its frame rate
  (Phase 6 [ui](../phase-6/ui.md)), only for rows on screen; `NO_COLOR`, `TERM=dumb`, a pipe or a
  file get no colour and no motion; 8-colour terminals get flat colours; the run log is plain text.
- **In `shared`**: the palette gains the roles above (`faint`, `muted`, `accent`, `size`, `path`…)
  and a gradient drawer in `theme`, so flnd names roles, never colours. Each role keeps an ASCII
  and no-colour form, and `tests/test_theme.py` pins each depth.

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
