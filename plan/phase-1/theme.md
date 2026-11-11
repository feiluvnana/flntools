# theme

Phase 1. Depends on [style](style.md).

[`shared/_theme.py`](../../shared/_theme.py). The look of everything `shared` prints: glyphs, templates, color depth, and the screen.

## Glyphs

Glyphs are grouped into namespaces rather than flat names. `select`, `check`, and `bar` are groups, and `spin` is a tuple of frames. Assigning an unknown name raises `AttributeError`.

```python
theme.glyphs.bullet = "•"
theme.glyphs.tick = "✓"
theme.glyphs.bang = "!"
theme.glyphs.cross = "✗"
theme.glyphs.dot = "·"
theme.glyphs.pointer = "›"

theme.glyphs.select.yes = "●"
theme.glyphs.select.no = "○"
theme.glyphs.check.yes = "✔"
theme.glyphs.check.no = "·"
theme.glyphs.bar.full = "━"
theme.glyphs.bar.empty = "─"
theme.glyphs.spin = ("⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏")
```

| Glyph | Used by |
| --- | --- |
| `bullet` `tick` `bang` `cross` `dot` | the mark for `log.info`, `log.ok`, `log.warn`, `log.error`, `log.debug` |
| `pointer` | the cursor on the active prompt row |
| `select.yes` `select.no` | single choice: chosen, not chosen |
| `check.yes` `check.no` | multiple choice: checked, not checked |
| `bar.full` `bar.empty` | a line bar when the total is known |
| `spin` | a progress line with no total |

`theme.unicode = False` switches to the ASCII set below. A glyph you assign yourself always wins over that set.

```python
bullet="*" tick="+" bang="!" cross="x" dot="." pointer=">"
select.yes="(*)" select.no="( )"
check.yes="[x]" check.no="[ ]"
bar.full="=" bar.empty="-"
spin=("|", "/", "-", "\\")
```

## Templates

`theme.templates` is the one place a printed line is described. Each kind of print has one template, and assigning a template changes every later draw of that kind.

A template is a string, or `Styled` pieces joined with `+`. A styled piece is painted after the slots are filled, and a missing slot is an empty string. A template can contain two kinds of slot:

- **`{g-name}` reads a glyph.** `{g-tick}` reads `theme.glyphs.tick`, and `{g-bar.full}` reads `theme.glyphs.bar.full`.
- **`{p-name}` is a part the method fills.** For example, `{p-bar}` is filled after the method has built the progress bar.

A caller's message does not use this syntax. In `log.info("saved {path}", path=path)` the message is formatted with `str.format` first, and the finished text arrives in the template as `{p-message}`.

```python
theme.templates.log.info = style.cyan("{g-bullet}") + " {p-message}{p-fields}"
theme.templates.log.ok = style.green("{g-tick}") + " {p-message}{p-fields}"
theme.templates.log.warn = style.yellow("{g-bang}") + " {p-message}{p-fields}"
theme.templates.log.error = style.red("{g-cross}") + " {p-message}{p-fields}"
theme.templates.log.debug = style.dim("{g-dot}") + " {p-message}{p-fields}"

theme.templates.progress.run = style.cyan("{g-bullet}") + " {p-label} {p-bar} {p-count} {p-caption}"
theme.templates.progress.ok = style.green("{g-tick}") + " {p-label} {p-bar} {p-count} {p-caption}"
theme.templates.progress.error = style.red("{g-cross}") + " {p-label} {p-bar} {p-count} {p-caption}"

theme.templates.prompt.question = style.white("{p-message}")
theme.templates.prompt.input = style.magenta("{p-cursor}") + " {p-value}"
theme.templates.prompt.placeholder = style.magenta("{p-cursor}") + " " + style.bright_black("{p-value}")
theme.templates.prompt.choice = style.magenta("{p-cursor}") + " {p-mark} {p-label}"
theme.templates.prompt.done = style.green("{g-tick}") + " {p-message}"
theme.templates.prompt.error = style.red("{g-cross}") + " {p-message}"
theme.templates.prompt.keys = style.dim("{p-binding} {p-action}")

theme.templates.help.section = style.bold("{p-title}")
theme.templates.help.usage = style.magenta("  {p-usage}")
theme.templates.help.row = style.magenta("{p-label}") + "  " + style.dim("{p-help}")
theme.templates.help.epilog = style.dim("{p-text}")
theme.templates.help.path = style.cyan.underline.url("{p-path}")("{p-path}")
theme.templates.help.error = style.red("{g-cross}") + " {p-message}"
```

After the slots are filled, runs of spaces left by empty slots collapse to one space, and trailing spaces are removed. A spinner line with no count and no caption therefore never ends in spaces.

## The parts

| Part | Value |
| --- | --- |
| `{p-message}` | the formatted message |
| `{p-fields}` | empty, or `"  k=v"` pairs for keywords that the message did not use |
| `{p-bar}` | the 20-cell bar built from `glyphs.bar`, one `glyphs.spin` frame for a running spinner, or empty for a finished spinner |
| `{p-count}` | `done/total` with a total; `done` for a spinner once `done > 0`; each number passed through the bar's `show` |
| `{p-caption}` | the bar's caption |
| `{p-cursor}` | `glyphs.pointer` on the active row, and spaces of the same width on the others, so rows line up |
| `{p-mark}` | `select.yes` or `select.no` for one choice; `check.yes` or `check.no` for many |
| `{p-binding}` | the keys for one action, joined with `/` |
| `{p-action}` | that action's name |

The keys line draws `prompt.keys` once per action and joins the pieces with two spaces.

An epilog word that starts with `./`, `~/`, `/`, or `.` and also contains `/` is drawn with `help.path` instead of `help.epilog`. That makes the path a cyan, underlined link while the words around it stay dim. Words like `and/or` or `Twitter/X` stay dim.

## Color, unicode, and the screen

```python
theme.color = "16m"       # "none" | "8" | "256" | "16m"; anything else raises ValueError
theme.unicode = True
```

`theme.color` defaults to `"none"` when stderr is not a TTY, `NO_COLOR` is set, or `TERM` is `dumb`. Otherwise it is `"16m"` when `COLORTERM` is `truecolor` or `24bit`, and `"256"` on any other TTY. `theme.unicode` defaults to `True` when stderr's encoding is UTF-8.

`theme.screen` is the terminal that `log` and `prompt` use. Tests replace it. `Screen` lives in [`shared/_screen.py`](../../shared/_screen.py) and is re-exported from `shared._theme`.

| Field | Default |
| --- | --- |
| `out` | `sys.stderr`, looked up at write time |
| `inp` | the terminal itself, not stdin: `/dev/tty` on POSIX and `CONIN$` on Windows, opened on the first key read |
| `tty` | `out.isatty()`; controls whether progress redraws in place |
| `interactive` | `tty`, and the terminal can be opened for keys; controls whether prompts may ask |
| `width` | the terminal width, read at draw time |
| `readkey()` | one key name (`"a"`, `"enter"`, `"up"`, `"ctrl+c"`, …), read from `inp` in raw mode with `termios` on POSIX and `msvcrt` on Windows |
| `write(text)` | writes under a lock, and strips ANSI first when `theme.color == "none"` |

Keys are read from the terminal rather than stdin, the same way `fzf` reads them. This leaves stdin free to carry data. `cat list.txt | flnd download` reads its targets from stdin and can still ask for a cookie file in the terminal. Under cron or CI there is no terminal to open, so `interactive` is `False` and nothing is asked.

```python
from io import StringIO
from shared._theme import Screen

theme.screen = Screen(out=StringIO(), inp=StringIO(""), width=80, tty=False, interactive=True)
theme.screen.readkey = iter(["down", "enter"]).__next__
```

On Windows, the first write turns on VT processing so that ANSI codes work in the old console.

## Scope and branch

```python
with theme.scope(color="none", unicode=False):
    log.info("plain")

plain = theme.branch(color="none")
log.info("plain", theme=plain)
```

`scope(**changes)` applies the changes for the length of the block and restores the previous values on exit. `branch(**changes)` returns a separate `Theme` copy and leaves the process theme as it was. Both accept `color`, `unicode`, and `screen`. Glyphs and templates on a branch are copies, so changing them does not touch the process theme.

## Private drawers

`theme._format_log(record)`, `theme._format_help(model)`, and `theme._format_prompt(view)` turn a model into text. They are private: callers change the look through `glyphs`, `templates`, and `style`, and never by assigning drawer functions. Tests may call the drawers; application code does not.

```python
record.level     # "debug" | "info" | "ok" | "warn" | "error"
record.message   # {p-message}
record.fields    # {p-fields}
record.bar       # None, or Bar(label, done, total, caption, finished, show)
```

- A record with a bar uses `templates.progress.<run|ok|error>`. Any other record uses `templates.log.<level>`.
- `HelpModel` has `prog`, `description`, `usage`, `arguments`, `options`, `commands`, and `epilog`. `arguments`, `options`, and `commands` are `(label, help)` pairs.
- `PromptView` has `kind`, `message`, `value`, `options`, `index`, `checked`, `error`, `keys`, and `done`.

## Tests

- glyph namespaces and the ASCII set;
- every template slot, and spaces collapsing;
- the epilog path rule, including `and/or`;
- `color` validation;
- `scope` restoring the old values, and `branch` leaving the process theme alone.
