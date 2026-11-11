# terminal

Phase 4, step 4, together with [prompt](prompt.md). This trims the terminal layer (`style`, `theme`, `log`, `prompt`, `cli`) from about 2,140 lines to about 1,750, by deleting machinery that nothing uses. Built: 1,726 code lines before this step (by the code-line counter), 1,553 after. What flnd does use stays.

## Templates are deleted

**Deleted:**
- `theme.templates`;
- the `{g-…}` / `{p-…}` template language;
- `Styled` and `Template` in `style`, the objects that delayed styling until slots were filled;
- the space tidying;
- template overrides.

That machinery let any line be restyled, even with gradients across filled slots. flnd never restyled a line, and it caused two of the review's bugs: buttons lost their padding, and in `choices` the color followed the cursor.

**What remains in `theme`:**

```python
theme.glyphs.tick = "✓"                       # glyph namespaces, with the ASCII fallback, as today
theme.palette.title = "bold #7571F9"          # named colors
theme.paint("ok", "done")                     # text in a palette color; on="cursor" adds a background
theme.drawers.log = lambda theme, record: ... # replace the drawer for one kind of line
theme.color = "16m"; theme.unicode = True; theme.screen = Screen(...)
with theme.scope(color="none"): ...
```

| `theme.palette` name | Default | Used for |
| --- | --- | --- |
| `info`, `ok`, `warn`, `error`, `debug` | cyan, `#02BF87`, yellow, `#ED567A`, dim | log glyphs, progress rows by state, and the prompt's done tick and errors. `ok` and `error` use huh's green and red, so log lines and prompts match |
| `title` | `bold #7571F9` (indigo) | prompt titles, help section titles |
| `cursor` | `#F780E2` (fuchsia) | the prompt pointer, the active button |
| `chosen` | `#02BF87` (green) | chosen and checked options |
| `text` | `252` | options, typed text |
| `muted` | `243` | descriptions, unchecked marks, help text |
| `faint` | `238` | the prompt bar, placeholders |
| `key`, `hint` | `241`, `239` | the keys line |
| `label` | magenta | help labels, usage |
| `cream`, `button` | `#FFFDF5`, `237` | the active button's text, and the inactive button's background ([prompt](prompt.md)) |

Each palette value is anything `style.color` accepts, or style names and colors such as `"dim"`, `"bold red"` or `"bold #7571F9"`. Assigning an unknown name raises `AttributeError`, and a value that isn't a style raises `ValueError`. There is no `link` entry: nothing in the help model marks paths (see [theme](#theme)), so nothing would use it.

**Drawers.** Each kind of output is drawn by a short function that reads `theme.palette` and `theme.glyphs` directly. There are four kinds, kept in `theme.drawers`: `log` (a log line), `progress` (a progress row), `prompt` (a whole question frame, built from small private helpers for the input line, the option rows, the buttons and the keys line) and `help`. `theme.draw(kind, value)` calls one. Restyling means changing the palette or the glyphs. A script that wants something stranger replaces the drawer for that kind, which is a plain function of `(theme, value)`.

## style

Removed, because nothing uses them:
- `double_underline`, `curly_underline`, `dotted_underline` and `dashed_underline`;
- `underline_color`, `overline`, `blink` and `hidden`.

What stays:
- the named colors and the `bg_` forms;
- `bold`, `dim`, `italic`, `underline`, `strike` and `reverse`;
- `color()` with hex, rgb and 256-color values, and the depth mapping;
- `gradient` and `rainbow`;
- `url` and `strip`.

Depth `"8"` maps to the 16 ANSI colors, with greys mapped by lightness to black, bright black or white, never by hue. Without this, the review found `237`–`241` came out as black (invisible) and `243` came out as yellow.

## theme

Removed:
- `templates`;
- `branch`, which only served the per-call `theme=` that `log` and `prompt` also drop. `cli` draws help for stdout's color depth inside `theme.scope(color=...)`;
- the epilog path detection in help. Nothing marks paths in the help model yet, so the epilog is drawn `muted`, like the help text.

## log

Removed:
- the per-call `theme=` (`theme.scope` covers it);
- **`**fields` appended as `k=v`.** flnd always formats its messages. A keyword that the message doesn't use now raises `TypeError` instead of being appended. `str.format` itself ignores unused keywords, so `log` checks them, for every level, so a misspelled field fails even on a hidden `debug` line.

`progress`, `multi` and the thread-safe live area stay as they are, with the height cap from [fixes](fixes.md#log). `bar.update(done, total=None)` takes an optional total, so library progress callbacks (Telethon's `(current, total)`) plug in directly.

## cli

Removed parameter forms that nothing uses:
- `nargs` as an integer, and fixed-size tuple positionals;
- the "rest of the line" `tuple[str, ...]`; `tuple` annotations are refused altogether, and `list[...]` is the one many-values form;
- `Enum` choices (`Literal` covers it);
- `hidden=`;
- the `-v`/`-q` verbosity flags (`App(verbosity=)`);
- the `@app` single-handler form and `@app.command(default=True)`, because `cli.run(fn)` covers one-command programs. `app()` takes no function;
- the `prepare(parser)` subclass hook (a method, so it is simply gone rather than a `TypeError`);
- **prompt mode**: `App(promptmode=)`, the whole-program menu, and the parameter-by-parameter walk through a command. Bare `flnd` prints help;
- **`ask=`** on `cli.opt` and `cli.arg`, which only served prompt mode. A command that needs to ask something, such as `flnd login`, asks with `prompt` in its own code.

With prompt mode gone, `cli` no longer imports `prompt` at all. `_run_promptmode`, the generated questions (`ask_generated`) and the equivalent-command-line logger (`command_line`) are deleted from `_cli.py` and `_params.py`. Asking is the script's job: it calls `prompt.input`, `prompt.choice` and the rest where it needs an answer, and those keep their own non-terminal rule (`PromptError`, or the default inside `prompt.defaults()`).

Added, both general:

| Addition | For |
| --- | --- |
| `date` and `datetime` as built-in conversions (ISO format), with `DATE` / `DATETIME` as the default metavar; a bad value is a usage error naming the format: `argument --since: invalid date: 'yesterday' (use YYYY-MM-DD)` | flnd's `--since` and `--until` |
| comma lists for list options: `--kind photo,video` equals `--kind photo --kind video`, empty pieces are skipped, and each piece is checked on its own | flnd's `--from` |

Fixed: **a long flag given in `cli.opt` replaces the parameter's own name.** `categories: Annotated[..., cli.opt("--from")]` was also accepted as `--categories`, and help showed `--from, --categories`. Now only `--from` exists. A short flag (`cli.opt("-o")`) still adds to the name's `--output`.

## Tests

- **No leftovers:** no template, `Styled`, `Template` or `branch` remains (import checks). The removed style attributes raise `AttributeError`.
- **Palette:** a palette change restyles log lines, prompt frames and help, checked at `16m` against expected escapes.
- **Depth `"8"`:** no default palette color maps to black or yellow.
- **log:** a message with an unused keyword raises `TypeError`; `bar.update(5, 10)`.
- **cli:**
  - `date` options;
  - comma lists, including a bad piece;
  - a given long flag replaces the name (`--from`, not `--categories`);
  - every removed form raises `TypeError` at decoration;
  - `import shared._cli` doesn't import `shared._prompt` (checked in a subprocess with a bare `shared` package, since `shared/__init__.py` imports every terminal module).
- **Unchanged:** all existing help screens, log lines and prompt frames render as before (prompt frames as in [prompt](prompt.md)).
