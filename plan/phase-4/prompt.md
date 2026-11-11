# prompt look

Phase 4, step 4, together with [terminal](terminal.md), which replaces templates with `theme.palette` and drawers. The default prompt look follows huh. No new dependencies.

The default prompt look follows [huh](https://github.com/charmbracelet/huh), Charm's form library for Go, and its default `ThemeCharm`:

- a thick bar down the left of the open question;
- a bold indigo title;
- a fuchsia `>` cursor;
- green for the chosen option;
- buttons for yes/no questions;
- a help line of keys under every question.

Only the defaults change. `theme.palette`, `theme.glyphs` and `prompt.keymap` stay the public knobs.

## Palette

Colors are huh's dark palette. The numbers are xterm 256-color indexes. The first column is the `theme.palette` name (see [terminal](terminal.md#templates-are-deleted)). `cream` and `button` are added to the palette for the buttons. `ok`, `chosen` and `error` are the same colors that log lines use.

| Name | Color | Used for |
| --- | --- | --- |
| `title` | `#7571F9` (indigo) | the title |
| `cursor` | `#F780E2` (fuchsia) | the `>` cursor, the active button background |
| `chosen` (and `ok`) | `#02BF87` (green) | a chosen or checked option; `ok` draws the done tick |
| `error` | `#ED567A` (red) | the error mark and message |
| `cream` | `#FFFDF5` | text on the active button |
| `text` | `252` | options and typed text |
| `muted` | `243` | the description, the `•` of an unchecked option |
| `faint` | `238` | the left bar, placeholder text |
| `button` | `237` | the inactive button background |
| `key` / `hint` | `241` / `239` | the key and its action in the help line |

**Depth `"8"` maps to the 16 ANSI colors** (`30`–`37` and `90`–`97`), not only the first 8. Greys (`232`–`255`, and hex values whose channels are within 10 of each other) are mapped by lightness, never by hue:
- under 15 % lightness → black;
- under 60 % → bright black (`90`);
- under 85 % → white (`37`);
- above that → bright white (`97`).

Without this rule, `237`–`241` became black (invisible on a dark terminal) and `243` became yellow. The rule lives in `style`, so every theme benefits.

## Frames

Each picture shows the characters drawn. The notes on the right give the colors.

### input

```python
prompt.input("Name", description="Your account name", placeholder="ada")
```

```text
┃ Name                                  indigo, bold
┃ Your account name                     muted
┃ > ada                                 > fuchsia; ada faint until the user types
enter submit                            key 241, action 239
```

### secret

```text
┃ Token
┃ > •••
enter submit
```

### choice

```text
┃ Source
┃ > tik                                 > fuchsia; tik green
┃   fbk                                 normal
┃   ins
┃   twt
↑/k up • ↓/j down • enter submit
```

A single choice shows no `●` / `○` marks; the cursor and the green label say which row is chosen, as in huh.

### choices

```text
┃ Sources
┃ > ✓ tik                               checked: ✓ and label green
┃   • fbk                               unchecked: • muted, label normal
┃   ✓ ins                               checked without the cursor: still green
┃   • twt
↑/k up • ↓/j down • space toggle • enter submit
```

Color follows the **checked state**, and the cursor only adds the `>`. An unchecked row under the cursor stays normal, so it never looks chosen.

### confirm

```text
┃ Deploy?
┃
┃   Yes      No                         " No " cream on fuchsia; " Yes " normal on 237
←/h left • →/l right • y yes • n no • enter submit
```

With `theme.color == "none"` (`NO_COLOR`, pipes, `TERM=dumb`), color can't show which button is active, so the drawer marks it with brackets:

```text
┃   Yes    [ No ]
```

### error

```text
┃ Name *                                * red
┃ >
┃ * Enter a name.                       red
enter submit
```

### done

The frame and its help line are replaced by one line, as today:

```text
✓ Name: sam
✓ Deploy? No
✓ Sources: tik, ins
```

## Layout rules

- **The bar is on every frame line**, including each line of a multi-line description, and is never on the help line.
- **Descriptions wrap** to `screen.width - 2` columns, on word boundaries, before the bar is added. Nothing is cut off.
- **Long lists scroll.** A frame is never taller than `screen.height - 1` rows. When a `choice` or `choices` list doesn't fit, it shows a window of rows around the cursor, with a faint `↑ N more` / `↓ N more` row at the cut edges. `Screen.height` comes from [fixes › log](fixes.md#log); the same cap keeps the frame's cursor-up erase correct.
- **Padding is exact.** Drawers build each piece directly, so the button's `" No "` keeps both spaces inside its background. The old space tidying trimmed the right one.

## The help line

- **On by default.** `keys` defaults to `True`, and `keys=False` hides the line for one question.
- **One setting hides it everywhere:** `prompt.keys = False` is the module-wide default that `keys=` overrides per question.
- **Key names are glyphs.** The `glyphs.keys` group maps key names to what the line shows: `up` → `↑`, `down` → `↓`, `left` → `←`, `right` → `→`. Other keys are shown by name.
- **The separator is a glyph.** `glyphs.keysep` is `•`, or `|` in ASCII, so `theme.unicode = False` has no `•` left.
- **Typing questions list only keys that aren't typed.** In `input`, `secret`, and any `filter=True` question, letters are typed as text. So the line shows `↑ up • ↓ down • enter submit`, without `k` or `j`.
- **`accept` is renamed `submit`**, matching huh. Assigning or reading `prompt.keymap.accept` raises `AttributeError("prompt.keymap.accept was renamed to submit")`.

| Question | Help line |
| --- | --- |
| `input`, `secret` | `enter submit` |
| `confirm` | `←/h left • →/l right • y yes • n no • enter submit` (`up`/`down` are no longer listed) |
| `choice` | `↑/k up • ↓/j down • enter submit` |
| `choices` | `↑/k up • ↓/j down • space toggle • enter submit` |
| `choice` / `choices` with `filter=True` | the same, without the letter keys |

## API changes

- **`description=""`** on every question. When `cli` asks for a missing value, the parameter's help line becomes the description.
- **Glyphs.**
  - New: `border` (`┃`, ASCII `|`), `keysep` (`•`, ASCII `|`), and `keys.up`, `keys.down`, `keys.left`, `keys.right` (arrows; in ASCII the key name).
  - Changed: `pointer` becomes `>`, `check.yes` becomes `✓` (ASCII `[x]`), `check.no` becomes `•` (ASCII `[ ]`).
  - `select.yes` / `select.no` stay, for themes that want marks.
- **Alignment.** Rows without the cursor are indented by the display width of `glyphs.pointer`, so a theme that sets `pointer` to `->` still lines up.
- **Color follows the checked state in `choices`.** The drawer colors a `choices` row's label from its checked state, and the cursor only adds the pointer. This fixes the review's finding that an unchecked row under the cursor looked chosen.
- **Buttons without color.** When `theme.color == "none"`, the active button is drawn as `[ No ]` and the inactive one as `  Yes  `, so the active one is visible without color.
- **Errors.** While an error is shown, the title gets a red ` *`, and the message is drawn under the input as `* message`.

## Light terminals

huh picks a light or dark palette from the terminal background. flntools does not detect the background. The defaults are huh's dark values, which stay readable on most light themes except the `238` bar and placeholder. A script on a light terminal sets `theme.palette.faint` to a darker grey. Background detection (OSC 11) is [out of scope](../README.md#out-of-scope).

## Tests

- Each frame above, through a fake `Screen` at `color="none"`: exact characters, a bar on every frame line, and no bar on the help line.
- Each frame at `"16m"`: the title is indigo and bold, color follows the checked state in `choices`, and the active button has a fuchsia background.
- The exact button string, padding included, at `"16m"` and at `"none"`. At `"none"` the active button is the bracketed one.
- At depth `"8"`, no default palette color paints black text or a black background, and `243` maps to bright black.
- ASCII: `|` bar, `>` pointer, `[x]` / `[ ]`, ` | ` separators, and key names in place of arrows; no non-ASCII character appears.
- A `pointer` of `->` keeps rows aligned.
- `keys` defaults to `True`; `keys=False` hides the line; `prompt.keys = False` hides it everywhere.
- With `filter=True`, and in `input`, the help line lists no letter keys.
- A multi-line description gets the bar on every line, and a long one wraps at `width - 2`.
- A `choice` list taller than `height - 1` scrolls with the cursor, shows the `↑ N more` / `↓ N more` rows, and redraws cleanly.
- `confirm`: `left`/`right` move between buttons, `y`/`n` answer at once, and `enter` submits the active button.
- `prompt.keymap.accept` raises `AttributeError` with the rename message.
- `cli` passes help lines as descriptions when it asks for missing values.
- The done line replaces the frame and the help line, and the live area is redrawn afterwards.
