# style

Phase 1. Reads `theme.color` when a chain is called (a lazy import, because `theme` imports `style`). Otherwise it stands alone, so build it first.

[`shared/_style.py`](../../shared/_style.py). A chain in the shape of Node's chalk. A chain is a value. Calling it paints a string. When `theme.color == "none"`, the call returns the text unchanged.

```python
style.red("failed")
style.red.bold("failed")
style.bold.underline("name")
style.dim.cyan("path")
style.bg_white.black(" flnd ")

style.color("#F25D94")("hello")
style.color("rgb(80, 250, 123)")("hello")
style.color(141)("hello")
style.color("#F25D94").bold("hello")
style.color("#1a1b26", background=True).white("panel")

style.url("https://example.com")("docs")
style.cyan.underline.url(".flnd/cookies/ins.txt")(".flnd/cookies/ins.txt")

style.gradient("#F25D94", "#8BE9FD")("flnd")
style.gradient("#F25D94", "#BD93F9", "#8BE9FD")("flnd")
style.bold.gradient("#F25D94", "#8BE9FD")("flnd")
style.gradient("#1a1b26", "#44475A", background=True).white("flnd")
style.rainbow("flnd")
style.rainbow(background=True).black("flnd")

style.blink("wait")
style.hidden("secret")
style.overline("title")
style.double_underline("name")
style.curly_underline("name")
style.dotted_underline("name")
style.dashed_underline("name")
style.underline_color("#F25D94")("path")
style.curly_underline.underline_color("#F25D94")("path")
```

## Named properties

The named properties are the short form of the same chain.

- **Foreground:** `black`, `red`, `green`, `yellow`, `blue`, `magenta`, `cyan`, `white`, `gray`, and each `bright_` form (`bright_red`, …).
- **Background:** `bg_` plus each of those names (`bg_red`, `bg_bright_black`, …).
- **Modifiers:** `bold`, `dim`, `italic`, `underline`, `double_underline`, `curly_underline`, `dotted_underline`, `dashed_underline`, `overline`, `strike`, `reverse`, `blink`, `hidden`.

Rules for combining them:

- Order does not matter: `style.red.bold` and `style.bold.red` paint the same way.
- A later color on the same layer replaces an earlier one.
- When a chain has two underline shapes, the later one wins.
- A chain is reusable.
- Named colors are the terminal's own palette colors (`31`, `91`, …) at every depth, so they follow the user's terminal theme. `style.color("#ff0000")` is an exact color, mapped down only on `"8"` and `"256"`.

## Colors and gradients

`style.color(value, *, background=False)` returns a chain. `value` can be:

- a hex string: `#RGB`, `#RRGGBB`, or `#RRGGBBAA`;
- an `rgb(r, g, b)` or `rgba(r, g, b, a)` string;
- an int from `0` to `255`, meaning a color in the 256-color palette.

`background=True` paints that color behind the text. A value the parser does not recognize raises `ValueError`.

`style.gradient(*stops, background=False)` gives each grapheme its own color, interpolated from the first stop to the last.

- At least two stops are required, and each takes the same values as `style.color`.
- A flat `color()` on the same layer loses to the gradient.
- Newlines are not colored and do not take a step.
- In a template, the ramp is painted after `{g-*}` and `{p-*}` are filled, across the finished text.

`style.rainbow(*, background=False)` is a gradient whose stops sweep the hue: red, yellow, green, cyan, blue, magenta.

`style.url(target)` makes a link. The visible text is the string passed when the chain is called. The terminal gets an OSC 8 hyperlink. When `theme.color == "none"`, only the visible text is written.

`style.underline_color(value)` sets the underline color. It turns on a single underline when the chain has no underline shape.

## Color depth

| `theme.color` | Colors | Attributes (underline shapes, overline, blink, hidden) |
| --- | --- | --- |
| `"none"` | none; text unchanged | none |
| `"8"` | mapped to the nearest of the 8 ANSI colors | written |
| `"256"` | hex and rgb mapped to the nearest palette index | written |
| `"16m"` | hex and rgb sent as written | written |

Gradient steps and `underline_color` are mapped the same way as `style.color`.

## What a call returns

Calling a chain returns a `Styled`. A `Styled` is a `str` subclass that already holds the painted text, so `print`, f-strings, and `log` messages all use it as ordinary text. It also remembers the raw text and the chain.

`+` between a `Styled` and a `str` builds a template: a list of pieces that `theme` paints later. This is how `style.green("{g-tick}") + " {p-message}"` can fill the slots first and paint them afterwards, which a gradient needs. Outside a template, nobody needs to know about this.

```python
danger = style.red.bold
log.error("{msg}", msg=danger("disk full"))
```

`style.strip(text) -> str` removes ANSI and OSC 8 sequences. `style` has no padding, borders, widths, or alignment. It does not turn numbers or times into text; that is `fmt`'s job.

## Tests

- every named property, and chain order;
- `color` parsing, including `ValueError`;
- each color depth, gradient steps, and rainbow;
- `url` with and without color;
- underline shapes and `underline_color`;
- `strip`, and `Styled` used both as a `str` and in a template.
