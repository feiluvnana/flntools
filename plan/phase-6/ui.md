# ui

Phase 6, step 8. The user named three terminal problems on 2026-11-12: the download rows blink, a prompt shows a stray white block, and a log line colors only its glyph. They also asked for the live area to be smart about [performance](#performance). Each is fixed in `shared` (`log`, `theme`, `prompt`), so every command gets it.

## The download rows blink

**Why.** `LiveArea` (`shared/_log.py`) redraws by erasing its whole area and then writing every row again: `\r`, cursor up N, `\x1b[J` (clear to the end of the screen), then the rows. Between the clear and the rows, the terminal can paint an empty area, which is the blink. It happens up to 10 times a second (`REDRAW_INTERVAL = 0.1`), and more often than that, because:
- `open` and every `write_lines` (each finished file's line) redraw with `force=True`, past the interval;
- a small photo's row is added, filled and removed within a few frames, so rows keep appearing and disappearing, and every row below moves up and down with them;
- the terminal's cursor is visible, and jumps between the area's top and bottom on every frame.

**Fix:**
1. **Overwrite, don't erase.** A frame moves the cursor to the area's top and writes each row followed by `\x1b[K` (clear to the end of *that line*). It clears below with `\x1b[J` only when the new frame has fewer rows. No moment shows an empty area.
2. **Synchronized output.** Each frame is wrapped in `\x1b[?2026h` … `\x1b[?2026l`, so terminals that support it (iTerm2, kitty, WezTerm, Windows Terminal, recent GNOME Terminal) show the whole frame at once. Others ignore the sequence.
3. **Hide the cursor** while a live area is open (`\x1b[?25l`), and show it again when the last area closes, before a prompt, and at exit, Ctrl-C included (an `atexit` hook and the `KeyboardInterrupt` path both write `\x1b[?25h`).
4. **One redraw per interval, always.** `open`, `close` and `write_lines` mark the area dirty, and the next tick draws it. Only the final frame, when the last area closes, is drawn at once. Lines written above the area (finished files, warnings) go out in the same write as the next frame.
5. **Rows that finish fast are never drawn.** A file's row appears only once it has been running for 0.5 s. One that finishes sooner writes only its final line, if any. So a burst of small photos gives no flicker of rows at all.
6. **A steady height.** A target's group shows at most `concurrency` file rows (the ones running), under its own row, which counts the rest: `ins:example_user · 120 files · 4 running · 31 queued`. The area's height changes only when a target starts or ends.

## A white block in prompts

**Why.** A prompt's frame (`_prompt.py`, `Frame.draw`) leaves the terminal's cursor at the end of its last line, the keys line, so a blinking white block shows after the hints, or after the options of a choice. The input line draws its own pointer (`>`) but no cursor where the text goes, so the only cursor on screen is in the wrong place.

**Fix:**
1. **Hide the terminal cursor while a frame is drawn**, and show it again in the frame's `end` and on any exit, as for the live area.
2. **The input line draws its own cursor** where the next character goes: a reverse-video cell, as huh does (`theme.paint("cursor", " ", on=…)`, or `_` without color). A secret prompt draws it after its mask.
3. **Glyphs of ambiguous width.** `•`, `●`, `○`, `✓`, `✗` and `┃` are "ambiguous" in Unicode: two cells wide in a CJK locale and in some Windows fonts. `display_width` counts them as 1, so the frame's lines wrap and its cursor-up erase then misses a line, which leaves stray characters behind. When the locale is CJK (`LANG` or `LC_ALL` starting with `ja`, `ko` or `zh`), or on Windows' legacy console, the theme uses the ASCII glyphs.

## Whole log lines in their level's color

Today `draw_log` paints only the glyph: `! ins: waiting 30 s` has a yellow `!` and default text. From now on, **the level's color is the line's default color**, from the glyph to the end:

| Level | Line color |
| --- | --- |
| `warn` | yellow, the whole line |
| `error` | `#ED567A`, the whole line |
| `ok` | `#02BF87`, the whole line |
| `debug` | dim, the whole line |
| `info` | cyan, the whole line (decided 2026-11-12) |

- **Styles in the message win.** A part of the message that has its own style (`style.bold(name)`, a path painted `label`) keeps it. After it, the line goes back to the level's color, not to the terminal's default: the drawer re-applies the level's color after every reset (`\x1b[0m`) inside the message. So `log.warn("{file} failed", file=style.bold(name))` gives a bold file name and the rest in yellow.
- **Palette names stay the same** (`theme.palette.warn` and so on), so a theme that changes a level's color changes its whole line.
- Without color (`theme.color == "none"`, `NO_COLOR`, or a pipe) nothing changes: lines are plain text.
- Progress rows keep their colored glyph only, because the bar has its own colors.

## Performance

The live area should cost almost nothing, however many files run. Today every `advance` from a download takes the screen lock, and at most every 0.1 s it re-renders every row: each row is painted, cut to width with `fit` (a regex over the whole row) and written, even the ones that didn't change. A target downloading 4 files sends about 40 `advance` calls a second per file, so the drawing does more work than the downloads' own bookkeeping. The live area becomes cheap in five ways:

1. **Updates only mark, never draw.** `ProgressBar.advance` and `.update` add to a counter and set a dirty flag. No lock, no rendering: one integer write that a download thread can afford on every chunk. Drawing happens in one place, the area's tick.
2. **One drawing thread, and only while something changes.** One daemon thread per screen wakes on a dirty flag (a `threading.Event`), at most every 0.1 s, and sleeps without waking when nothing is dirty. A spinner keeps it at 10 frames a second only while a spinner row is on screen. The thread starts with the first area and ends with the last, so an idle `log.info` script never starts it.
3. **Only changed rows are rendered and written.**
   - Each row keeps the last string it drew. It is rendered again only when what it shows changed: its bar's filled cells (20 per bar), its number as displayed (`3.1 MB`, not every byte), its caption or its label.
   - A frame writes only the rows that differ from the screen, each with a cursor move (`\x1b[<n>B`) and `\x1b[K`, and leaves the others alone. A frame where nothing visible changed writes nothing at all. For a 2 GB file, that is about 200 redraws of its row (one per 0.5% at 20 cells and 0.1 s), not 20,000.
4. **Each frame is one write and one flush**, built in a list and joined, inside the synchronized-output pair ([above](#the-download-rows-blink)). Lines written above the area wait for the next tick and go out in the same write.
5. **A slow terminal skips frames instead of queueing them.** When a write takes longer than the tick (an SSH session, a slow Windows console), the next frame is dropped, and the one after shows the latest state. Output never falls behind the downloads, and the download threads never wait for the terminal.

And outside a terminal (a pipe, a log file, CI) there is no live area at all, as today. The thread never starts, and only the final lines are written.

### Measured, not guessed

`tests/test_log.py` gets a small benchmark-style test on the fake screen and the fake clock:
- 4 bars advanced 10,000 times each over 10 fake seconds produce at most 100 frames;
- a frame where no bar crossed a cell boundary or changed its shown number writes 0 bytes;
- `advance` never takes the screen lock (checked with a lock that fails the test if acquired).

By hand, once, on macOS Terminal, iTerm2, Windows Terminal and an SSH session: flnd's CPU while downloading a 2 GB video and 200 small photos is under 2% for drawing, measured with the drawing thread's own timer.

## Tests

Tests use the fake `theme.screen`, as `tests/test_log.py` and `tests/test_prompt.py` do now:
- a frame never contains `\x1b[J` before its rows, and every row ends with `\x1b[K`; a frame is wrapped in `?2026h` / `?2026l`;
- 50 files finishing within 0.1 s on the fake clock produce at most two frames, and no row for a file that ran under 0.5 s;
- the cursor is hidden while an area or a prompt is open, and shown after each closes and after a `KeyboardInterrupt`;
- an input prompt's frame ends with its cursor cell after the typed text;
- with `LANG=ja_JP.UTF-8`, the theme picks the ASCII glyphs;
- `log.warn("{x} failed", x=style.bold("a"))` renders yellow, then bold `a`, then yellow again for ` failed`; with color off it renders `! a failed`.
