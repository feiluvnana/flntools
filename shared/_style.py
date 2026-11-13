"""Chainable terminal styles in the shape of Node's chalk: colors, modifiers, gradients and links.

`style` is one empty `Chain`. Each property or method returns a new `Chain` with one more style, so
styles chain and can be kept for reuse; calling a chain paints a string. Painting asks the process
theme (`shared.theme`) for its color depth at call time, so the same chain prints plain text into a
pipe and 24-bit color into a modern terminal. The module wraps no library: it writes SGR and OSC 8
escape sequences itself, and has no `.raw`.

    danger = style.red.bold
    print(danger("disk full"), style.cyan.link("https://example.com")("docs"))
"""

from __future__ import annotations

import functools
import re
import unicodedata
from dataclasses import dataclass, replace

import shared

# `shared.style` is the `style` object: its methods, `Error` among them, are the module's names.
__all__ = ["Error", "strip", "style"]

# SGR 0 turns every style off; a painted string always ends with it, so nothing bleeds into later text.
RESET = "\x1b[0m"
# OSC 8 hyperlinks: `ESC ] 8 ; params ; URL ST` opens one, and the same with an empty URL closes it.
# ST is `ESC \`, the string terminator the spec names; BEL is accepted too, but this module writes ST.
LINK_CLOSE = "\x1b]8;;\x1b\\"
# A CSI sequence is `ESC [`, parameter bytes 0x30-0x3F, intermediate bytes 0x20-0x2F, then one final
# byte 0x40-0x7E. An OSC sequence is `ESC ]` up to BEL or ST. `strip` removes both kinds.
ESCAPE_PATTERN = r"\x1b\[[0-?]*[ -/]*[@-~]|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)"
ESCAPE = re.compile(ESCAPE_PATTERN)
# Colors and links only, leaving cursor movement in place: `Screen.write` uses this when colour is
# off, so colour and link escapes are removed and cursor moves are left alone. `ESCAPE` removes all.
PAINT = re.compile(r"\x1b\[[0-9;:]*m|\x1b\]8;[^\x07\x1b]*(?:\x07|\x1b\\)")

# The 16 ANSI colors in SGR order. Foreground codes are 30-37 for the first eight and 90-97 for the
# bright eight (index + 8), so the names map straight onto `NAMED_COLORS` indices.
_BASE_NAMES = ("black", "red", "green", "yellow", "blue", "magenta", "cyan", "white")
NAMED_COLORS = {
    **{name: index for index, name in enumerate(_BASE_NAMES)},
    **{f"bright_{name}": index + 8 for index, name in enumerate(_BASE_NAMES)},
    "gray": 8,
}
# SGR parameters for each modifier. Each is its own code, so modifiers combine: bold and underline
# together send both.
MODIFIER_CODES = {"bold": "1", "dim": "2", "italic": "3", "underline": "4", "reverse": "7", "strike": "9"}


class Error(shared.Error):
    """The base of every error `shared.style` raises."""


@dataclass(frozen=True)
class Chain:
    """A reusable style. Calling it paints a string for the process theme's color depth.

    Colors are tuples: ("ansi", 0-15) for the named colors, ("index", 0-255)
    for the 256-color palette, and ("rgb", (r, g, b)) for hex and rgb values.

    A chain is frozen: every method returns a new one, so a chain can be shared between threads
    and reused without one caller's style leaking into another's.
    """

    # The foreground color, or None for the terminal's default.
    fg: tuple | None = None
    # The background color, or None for the terminal's default.
    bg: tuple | None = None
    # The per-character colors of a foreground gradient; empty when there is none.
    fg_ramp: tuple = ()
    # The per-character colors of a background gradient; empty when there is none.
    bg_ramp: tuple = ()
    # The modifier names in use, as keys of `MODIFIER_CODES`.
    modifiers: frozenset = frozenset()
    # The URL an OSC 8 hyperlink opens, set by `link(url)`; None paints no link.
    href: str | None = None

    # `shared.style` is a Chain, not a module, so the module's error is reached through it.
    # Not a field: it has no annotation.
    Error = Error

    # Only reached for names that are not found normally, so the color and modifier names are
    # resolved here rather than declared one by one.
    def __getattr__(self, name: str) -> Chain:
        if name in NAMED_COLORS:
            return replace(self, fg=("ansi", NAMED_COLORS[name]))
        if name.startswith("bg_") and name[3:] in NAMED_COLORS:
            return replace(self, bg=("ansi", NAMED_COLORS[name[3:]]))
        if name in MODIFIER_CODES:
            return replace(self, modifiers=self.modifiers | {name})
        raise AttributeError(f"style has no property {name!r}")

    def __call__(self, text: object) -> str:
        return self.paint(str(text), current_depth())

    def color(self, value: str | int, *, background: bool = False) -> Chain:
        """This style with a color set, as a new chain.

            style.color("#fa0").color(208, background=True)

        Args:
            value: a 256-color index (0-255), a hex string such as "#fa0" or "#ffaa00", or
                "rgb(r, g, b)"; see `parse_color`.
            background: True sets the background color; False, the default, sets the text color.

        Returns:
            A new `Chain`; this one is unchanged.

        Raises:
            ValueError: `value` is not a color this module understands.
        """
        color = parse_color(value)
        return replace(self, bg=color) if background else replace(self, fg=color)

    def gradient(self, *stops: str | int, background: bool = False) -> Chain:
        """This style with a gradient across its characters, as a new chain.

            print(style.gradient("#f00", "#00f")("hello"))

        Each character gets the next color between the first stop and the last, so the gradient
        follows the text, not the line. Newlines take no color, and a line ends with a reset.

        Args:
            *stops: at least two colors, in the forms `color` accepts, from first to last.
            background: True paints the gradient on the background; False, the default, on the text.

        Returns:
            A new `Chain`; this one is unchanged.

        Raises:
            ValueError: fewer than two stops are given, or a stop is not a color.
        """
        if len(stops) < 2:
            raise ValueError("a gradient needs at least two stops")
        ramp = tuple(parse_color(stop) for stop in stops)
        return replace(self, bg_ramp=ramp) if background else replace(self, fg_ramp=ramp)

    def link(self, url: str) -> Chain:
        """This style, also making the text an OSC 8 hyperlink that opens `url`.

            print(style.cyan.link("https://example.com")("docs"))

        Args:
            url: what the link opens; a terminal without OSC 8 support shows the text alone.

        Returns:
            A new `Chain`; with no color (depth "none") it paints the text with no link either.
        """
        return replace(self, href=url)

    @staticmethod
    def strip(text: str) -> str:
        """Remove ANSI and OSC 8 sequences."""
        return strip(text)

    def paint(self, text: str, depth: str) -> str:
        """Paint `text` with this style for a color depth, and return the escaped string.

        Args:
            text: the text to paint; it may already contain escapes from other styles.
            depth: the color depth the terminal supports: "none" (no colour at all, so `text`
                comes back unchanged), "8" (the 16 ANSI colors), "256" (the 256-color palette),
                or "16m" (24-bit RGB).

        Returns:
            The painted text. With no style at all it is `text` itself, so painting costs nothing.
        """
        if depth == "none":
            return text
        static_codes = self._static_codes(depth)
        if self.fg_ramp or self.bg_ramp:
            body = self._paint_ramp(text, depth, static_codes)
        elif static_codes:
            opener = f"\x1b[{';'.join(static_codes)}m"
            # An inner reset would end this style early, so the style is reopened after it,
            # except after a final one, which already closes the text.
            inner = text.replace(RESET, RESET + opener)
            closed = inner.endswith(RESET + opener)
            body = opener + (inner[:-len(opener)] if closed else inner + RESET)
        else:
            body = text
        if self.href:
            body = f"\x1b]8;;{self.href}\x1b\\{body}{LINK_CLOSE}"
        return body

    def _static_codes(self, depth: str) -> list[str]:
        """The SGR parameters for this style's modifiers and its single colors, for a depth.

        Modifiers come first; a gradient's colors are per character and are left to `_paint_ramp`.
        """
        codes = [code for name, code in MODIFIER_CODES.items() if name in self.modifiers]
        if self.fg and not self.fg_ramp:
            codes.append(color_code(self.fg, depth, "fg"))
        if self.bg and not self.bg_ramp:
            codes.append(color_code(self.bg, depth, "bg"))
        return codes

    def _paint_ramp(self, text: str, depth: str, static_codes: list[str]) -> str:
        """Paint `text` one grapheme at a time, each with the next color of the gradient.

        Escape sequences already in the text are passed through and take no step, so colors
        from other styles never shift the gradient.
        """
        units = graphemes(text)
        # Only visible graphemes take a step: newlines and escapes are skipped, so the gradient
        # spans the visible characters alone.
        steps = sum(1 for unit in units if unit != "\n" and not unit.startswith("\x1b"))
        fg_colors = ramp_colors(self.fg_ramp, steps) if self.fg_ramp else None
        bg_colors = ramp_colors(self.bg_ramp, steps) if self.bg_ramp else None
        painted, step = [], 0
        for unit in units:
            # A reset before each newline stops the gradient's last color from running onto the
            # next line, where the terminal would otherwise keep painting.
            if unit == "\n" or unit.startswith("\x1b"):
                painted.append(RESET + unit if unit == "\n" else unit)
                continue
            codes = list(static_codes)
            if fg_colors:
                codes.append(color_code(fg_colors[step], depth, "fg"))
            if bg_colors:
                codes.append(color_code(bg_colors[step], depth, "bg"))
            painted.append(f"\x1b[{';'.join(codes)}m{unit}")
            step += 1
        return "".join(painted) + RESET


def strip(text: str) -> str:
    """Remove ANSI and OSC 8 sequences."""
    return ESCAPE.sub("", text)


def current_depth() -> str:
    """The color depth of the process theme right now: "none", "8", "256" or "16m".

    Returns "none" while no theme exists, so nothing is colored before the theme is set up.
    """
    # theme imports style, so the process theme is looked up at call time.
    from shared import _theme

    process_theme = getattr(_theme, "theme", None)
    return process_theme.color if process_theme is not None else "none"


def parse_color(value: str | int) -> tuple:
    """Turn a color the caller wrote into a color tuple: a palette index, or an RGB triple.

    Args:
        value: an int from 0 to 255 (a 256-color index; a bool is refused), a hex string "#rgb",
            "#rrggbb" or "#rrggbbaa", or "rgb(r, g, b)" or "rgba(r, g, b, a)" with 0-255 channels.
            The alpha of a hex or rgba value is read but not used, since terminals have no alpha.

    Returns:
        ("index", n) for a palette index, or ("rgb", (r, g, b)) for the other forms.

    Raises:
        ValueError: `value` is in none of these forms.
    """
    if isinstance(value, int) and not isinstance(value, bool):
        if 0 <= value <= 255:
            return ("index", value)
    elif isinstance(value, str):
        text = value.strip().lower()
        hex_match = re.fullmatch(r"#([0-9a-f]{3}|[0-9a-f]{6}|[0-9a-f]{8})", text)
        if hex_match:
            digits = hex_match[1]
            # "#fa0" is shorthand for "#ffaa00": each digit is doubled.
            if len(digits) == 3:
                digits = "".join(digit * 2 for digit in digits)
            # The first three pairs are red, green and blue; an eight-digit value's alpha pair is dropped.
            return ("rgb", tuple(int(digits[i:i + 2], 16) for i in (0, 2, 4)))
        rgb_match = re.fullmatch(r"(rgba?)\(([^)]*)\)", text)
        if rgb_match:
            channels = [channel.strip() for channel in rgb_match[2].split(",")]
            expected = 4 if rgb_match[1] == "rgba" else 3
            # Only whole numbers: "50%" and decimals are refused, so a percentage is never misread.
            if len(channels) == expected and all(c.isdigit() and int(c) <= 255 for c in channels[:3]):
                return ("rgb", tuple(int(c) for c in channels[:3]))
    raise ValueError(f"not a color: {value!r}")


def color_code(color: tuple, depth: str, layer: str) -> str:
    """The SGR parameters that put a color on one layer, for a color depth.

    Args:
        color: a color tuple from `parse_color`.
        depth: "8" for a terminal with the 16 ANSI colors; anything else uses the 256-color
            palette, or 24-bit RGB when `depth` is "16m" and the color is an RGB triple.
        layer: "fg" for the text color, "bg" for the background.

    Returns:
        The parameters without the surrounding escape, such as "31" or "38;5;208".
    """
    kind, value = color
    # On an 8-color terminal an RGB or palette color is mapped to the nearest ANSI color, since the
    # 256-color codes are not part of the 8-color set this depth asks for.
    if kind == "ansi" or depth == "8":
        index = value if kind == "ansi" else nearest_ansi(color_rgb(color))
        # SGR 30-37 and 40-47 are the first eight colors; 90-97 and 100-107 are the bright eight,
        # reached by adding 60 to the base.
        base = 30 if layer == "fg" else 40
        return str(base + index if index < 8 else base + 60 + index - 8)
    # SGR 38 starts a foreground color and 48 a background one. The next parameter says the form:
    # 5 is a 256-color index, and 2 is 24-bit RGB as `2;r;g;b`.
    prefix = "38" if layer == "fg" else "48"
    if kind == "rgb" and depth == "16m":
        return f"{prefix};2;{value[0]};{value[1]};{value[2]}"
    return f"{prefix};5;{nearest_index(value) if kind == 'rgb' else value}"


# The usual xterm values of the 16 ANSI colors. Terminals set their own palette, so these are only
# an approximation, used to find which ANSI color an RGB value is nearest to.
ANSI_RGB = (
    (0, 0, 0), (205, 0, 0), (0, 205, 0), (205, 205, 0),
    (0, 0, 238), (205, 0, 205), (0, 205, 205), (229, 229, 229),
    (127, 127, 127), (255, 0, 0), (0, 255, 0), (255, 255, 0),
    (92, 92, 255), (255, 0, 255), (0, 255, 255), (255, 255, 255),
)
# The four greys of the 16 ANSI colors, darkest first: black, bright black, white, bright white.
ANSI_GREYS = (0, 8, 7, 15)
# The hued colors: the 16 minus the greys. `nearest_ansi` picks among these for a colored input.
ANSI_HUES = tuple(index for index in range(16) if index not in ANSI_GREYS)
# The six levels of the 6x6x6 color cube in the 256-color palette (indices 16-231).
CUBE_LEVELS = (0, 95, 135, 175, 215, 255)


def color_rgb(color: tuple) -> tuple:
    """The RGB triple a color stands for: the value itself for "rgb", or a table value for a palette index.

    Args:
        color: a color tuple from `parse_color`.

    Returns:
        A `(r, g, b)` tuple of 0-255 values. For an ANSI color this is the approximation in `ANSI_RGB`.
    """
    kind, value = color
    if kind == "rgb":
        return value
    if value < 16:
        return ANSI_RGB[value]
    if value < 232:
        # Indices 16-231 are the cube: base-6 digits of (index - 16) give the red, green and blue levels.
        cube = value - 16
        return (CUBE_LEVELS[cube // 36], CUBE_LEVELS[cube // 6 % 6], CUBE_LEVELS[cube % 6])
    # Indices 232-255 are 24 evenly spaced greys, from 8 to 238 in steps of 10.
    gray = 8 + (value - 232) * 10
    return (gray, gray, gray)


def nearest_ansi(rgb: tuple) -> int:
    """The closest of the 16 ANSI colors.

    Greys go by lightness, never by hue: by distance, the dark greys a prompt uses
    came out black (invisible on a dark terminal) and 243 came out yellow.

    Args:
        rgb: a `(r, g, b)` triple of 0-255 values.

    Returns:
        An index from 0 to 15.
    """
    # Channels within 10 levels of each other are a grey, not a hue. Greys are matched by lightness.
    if max(rgb) - min(rgb) <= 10:
        lightness = (max(rgb) + min(rgb)) / 2 / 255
        # The three cut-points split the lightness range into the four greys, darkest first.
        return ANSI_GREYS[sum(lightness >= edge for edge in (0.15, 0.60, 0.85))]
    return min(ANSI_HUES, key=lambda index: _distance(rgb, ANSI_RGB[index]))


# Painting a gradient asks for the same colors again and again, and the search over the 240 colors
# of 16-255 is the slow part, so the answers are cached.
@functools.lru_cache(maxsize=4096)
def nearest_index(rgb: tuple) -> int:
    """The palette index from 16 to 255 that is closest to an RGB triple.

    Only the cube and the greys are searched; the 16 ANSI colors are never returned, because
    their values depend on the terminal's own palette.

    Args:
        rgb: a `(r, g, b)` triple of 0-255 values.

    Returns:
        An index from 16 to 255.
    """
    return min(range(16, 256), key=lambda index: _distance(rgb, color_rgb(("index", index))))


def ramp_colors(stops: tuple, steps: int) -> list[tuple]:
    """One ("rgb", ...) color per step, interpolated from the first stop to the last.

    Args:
        stops: the gradient's color tuples, first to last; at least two.
        steps: how many colors to make, one per visible character. 0 gives an empty list; 1 gives
            the first stop alone.

    Returns:
        `steps` color tuples, each `("rgb", (r, g, b))`, with rounded channel values.
    """
    points = [color_rgb(stop) for stop in stops]
    colors = []
    for step in range(steps):
        # Spread the steps evenly over the stop-to-stop segments; `position` runs from 0 to the last stop.
        position = (step / (steps - 1) if steps > 1 else 0) * (len(points) - 1)
        # The last step lands exactly on the final stop, so the segment is clamped to the last pair.
        segment = min(int(position), len(points) - 2)
        fraction = position - segment
        start, end = points[segment], points[segment + 1]
        colors.append(("rgb", tuple(round(a + (b - a) * fraction) for a, b in zip(start, end))))
    return colors


# One unit is an escape sequence or any single character. `re.S` lets "." match a newline too.
_UNIT = re.compile(ESCAPE_PATTERN + "|.", re.S)


def graphemes(text: str) -> list[str]:
    """Split text into escape sequences and graphemes (a base character plus its marks)."""
    units: list[str] = []
    for unit in _UNIT.findall(text):
        previous = units[-1] if units else ""
        # A unit joins the one before it when it is a combining mark, a variation selector (text or
        # emoji presentation), a skin-tone modifier, or follows a zero-width joiner, so that one
        # visible emoji or accented letter is one grapheme and gets one color.
        joins = (
            unicodedata.combining(unit)
            or unit in "‍︎️"
            or "\U0001f3fb" <= unit <= "\U0001f3ff"
            or previous.endswith("‍")
        )
        if joins and previous and previous != "\n" and not previous.startswith("\x1b"):
            units[-1] += unit
        else:
            units.append(unit)
    return units


def _distance(a: tuple, b: tuple) -> int:
    return sum((x - y) ** 2 for x, y in zip(a, b))


style = Chain()
