# log

Phase 1. Depends on [theme](theme.md) and [style](style.md).

[`shared/_log.py`](../../shared/_log.py). Five levels and progress bars. Every write goes through `theme.screen` and `_format_log`.

## Lines

```python
log.debug("raw {n}", n=len(buf))
log.info("saved {path}", path=path)
log.ok("done")
log.warn("retry {n}", n=2)
log.error("failed: {err}", err=err)
log.ok("finished", folders=3)          # "✓ finished  folders=3"
log.info(f"saved {path}")              # no arguments, so no str.format: braces are safe
```

Each level has the signature `(message: str = "", /, *args, theme=None, **fields) -> None`.

- `str.format` runs only when `args` or `fields` are given. An f-string with literal braces passes through unchanged.
- Keyword names the message does not use are added as `k=v`, in the order they were given.
- A bad format string raises the error from `str.format`.
- An empty message prints only the glyph.
- Styling inside a field is kept. `{p-message}` is filled first and the surrounding pieces are painted afterwards.

```text
• saved /tmp/a                         bullet, cyan
✓ done                                 tick, green
! retry 2                              bang, yellow
✗ failed: boom                         cross, red
✓ finished  folders=3                  tick, green
```

`log.level` defaults to `"info"`. The order, quietest first, is `debug`, `info`, `ok`, `warn`, `error`, then `quiet`. A line is shown when its rank is at least `log.level`, and `quiet` shows only `error`. Assigning an unknown name raises `ValueError`.

| Call | Shown when `log.level` is |
| --- | --- |
| `debug` | `debug` |
| `info` | `debug`, `info` |
| `ok` | `debug`, `info`, `ok` |
| `warn` | `debug` through `warn` |
| `error` | every level, including `quiet` |

## Progress

```python
with log.progress("clip.mp4", total=size, show=fmt.bytes) as bar:
    for chunk in response.iter_bytes():
        bar.advance(len(chunk))
    bar.caption = "part 3"

with log.progress("scroll posts") as bar:
    scroll_page_to_end(page, on_step=bar.advance)
```

`log.progress(label, *, total=None, show=str, theme=None)` is a context manager that returns a bar.

- `bar.advance(n=1)` adds to `done`.
- `bar.update(n)` sets `done`. `done` never goes below 0, and a `done` above `total` draws a full bar.
- `bar.total` can be assigned, for when the length arrives late.
- `bar.caption` is the trailing text.
- `bar.label`, `bar.done`, and `bar.finished` are readable.
- `show` turns each number in the count into text: `show=fmt.bytes` prints `1.2 MB/3.0 MB`.

A normal exit, including `return` and `break`, rewrites the line at `ok` and clears the caption. An exception, `KeyboardInterrupt` included, rewrites the line at `error`, keeps the caption, and re-raises. The final line shows the real `done`; a clean exit does not fill the bar.

```text
• clip.mp4 ━━━━━━━━──────────── 1.2 MB/3.0 MB part 3     running
✓ clip.mp4 ━━━━━━━━━━━━━━━━━━━━ 3.0 MB/3.0 MB            clean exit
✗ clip.mp4 ━━━━━━━━──────────── 1.2 MB/3.0 MB part 3     the block raised
• scroll posts ⠙ 12                                       spinner
✓ scroll posts 12                                         spinner, clean exit
```

## Several bars

```python
with log.multi("download") as jobs:
    ins = jobs.add("ins:ada", total=12)
    twt = jobs.add("twt:bob")
    ins.advance()
    ins.caption = "reel 3"
```

```text
• download
• ins:ada ━━────────────────── 1/12 reel 3
• twt:bob ⠙
```

`log.multi(title="", *, theme=None)` draws a title row with one row per job under it.

- `jobs.add(label, *, total=None, show=str)` returns a bar with the same methods as above.
- `bar.finish()` and `bar.fail()` end one row early, at `ok` or `error`.
- `jobs.bars` is the list of bars.
- When the block ends, every unfinished row ends at `ok` after a clean exit, or at `error` if the block raised. The title row follows the block's result.

## The live area

Open bars live at the bottom of the screen. This one rule covers nesting, log lines, and prompts:

- Each open `progress` and each `multi` block is a group of rows in the live area. A bar opened inside another bar adds rows under it, so a run bar, a per-target `multi`, and a per-file bar can all be open together.
- When a `progress` finishes, its final line is written above the live area and its row goes away. A `multi` block stays in the live area until its block ends, and then all its final lines are written together.
- A log line written while bars are open is written above the live area, and the live area is redrawn under it.
- A prompt asked while bars are open clears the live area and asks below the log lines. After the done line, the live area is redrawn and the spinner thread resumes.

On a TTY, rows are redrawn in place with `\r`, cursor-up, and clear-line. Each row is cut to `screen.width` so it never wraps, because a wrapped row would break the cursor-up count. Redraws happen at most every 0.1 s, plus once whenever a row finishes. While a spinner is open, a background thread redraws every 0.1 s so the spinner keeps moving during a blocking call. All writes share the screen's lock.

When the stream is not a TTY, nothing is redrawn. Log lines are written as they come, and each row writes only its final line.

## Per-call theme

`theme=` on a level, on `progress`, or on `multi` uses that theme for the call and leaves the process theme unchanged.

## Tests

- each level, and each threshold including `quiet`;
- fields, and braces in a message with no arguments;
- progress on a pipe and on a fake TTY, both clean exit and exception;
- nested bars, and `multi` with `finish` and `fail`;
- a log line written while bars are open, and rows cut to `width`.
