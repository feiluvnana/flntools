"""Chainable terminal styles in the shape of Node's chalk."""

from __future__ import annotations

import functools
import re
import unicodedata
from dataclasses import dataclass, replace

RESET = "\x1b[0m"
LINK_CLOSE = "\x1b]8;;\x1b\\"
ESCAPE_PATTERN = r"\x1b\[[0-?]*[ -/]*[@-~]|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)"
ESCAPE = re.compile(ESCAPE_PATTERN)
# Colors and links only, leaving cursor movement in place.
PAINT = re.compile(r"\x1b\[[0-9;:]*m|\x1b\]8;[^\x07\x1b]*(?:\x07|\x1b\\)")

_BASE_NAMES = ("black", "red", "green", "yellow", "blue", "magenta", "cyan", "white")
NAMED_COLORS = {
    **{name: index for index, name in enumerate(_BASE_NAMES)},
    **{f"bright_{name}": index + 8 for index, name in enumerate(_BASE_NAMES)},
    "gray": 8,
}
MODIFIER_CODES = {"bold": "1", "dim": "2", "italic": "3", "underline": "4", "reverse": "7", "strike": "9"}
RAINBOW_STOPS = ("#ff0000", "#ffff00", "#00ff00", "#00ffff", "#0000ff", "#ff00ff")


@dataclass(frozen=True)
class Chain:
    """A reusable style. Calling it paints a string for the process theme's color depth.

    Colors are tuples: ("ansi", 0-15) for the named colors, ("index", 0-255)
    for the 256-color palette, and ("rgb", (r, g, b)) for hex and rgb values.
    """

    fg: tuple | None = None
    bg: tuple | None = None
    fg_ramp: tuple = ()
    bg_ramp: tuple = ()
    modifiers: frozenset = frozenset()
    link: str | None = None

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
        color = parse_color(value)
        return replace(self, bg=color) if background else replace(self, fg=color)

    def gradient(self, *stops: str | int, background: bool = False) -> Chain:
        if len(stops) < 2:
            raise ValueError("a gradient needs at least two stops")
        ramp = tuple(parse_color(stop) for stop in stops)
        return replace(self, bg_ramp=ramp) if background else replace(self, fg_ramp=ramp)

    def rainbow(self, text: object = None, /, *, background: bool = False) -> Chain | str:
        chain = self.gradient(*RAINBOW_STOPS, background=background)
        return chain if text is None else chain(text)

    def url(self, target: str) -> Chain:
        return replace(self, link=target)

    @staticmethod
    def strip(text: str) -> str:
        """Remove ANSI and OSC 8 sequences."""
        return strip(text)

    def paint(self, text: str, depth: str) -> str:
        """Paint text for a color depth: "none", "8", "256", or "16m"."""
        if depth == "none":
            return text
        static_codes = self._static_codes(depth)
        if self.fg_ramp or self.bg_ramp:
            body = self._paint_ramp(text, depth, static_codes)
        elif static_codes:
            opener = f"\x1b[{';'.join(static_codes)}m"
            # An inner reset would end this style early, so the style is reopened after it.
            body = opener + text.replace(RESET, RESET + opener) + RESET
        else:
            body = text
        if self.link:
            body = f"\x1b]8;;{self.link}\x1b\\{body}{LINK_CLOSE}"
        return body

    def _static_codes(self, depth: str) -> list[str]:
        codes = [code for name, code in MODIFIER_CODES.items() if name in self.modifiers]
        if self.fg and not self.fg_ramp:
            codes.append(color_code(self.fg, depth, "fg"))
        if self.bg and not self.bg_ramp:
            codes.append(color_code(self.bg, depth, "bg"))
        return codes

    def _paint_ramp(self, text: str, depth: str, static_codes: list[str]) -> str:
        units = graphemes(text)
        steps = sum(1 for unit in units if unit != "\n" and not unit.startswith("\x1b"))
        fg_colors = ramp_colors(self.fg_ramp, steps) if self.fg_ramp else None
        bg_colors = ramp_colors(self.bg_ramp, steps) if self.bg_ramp else None
        painted, step = [], 0
        for unit in units:
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
    # theme imports style, so the process theme is looked up at call time.
    from shared import _theme

    process_theme = getattr(_theme, "theme", None)
    return process_theme.color if process_theme is not None else "none"


def parse_color(value: str | int) -> tuple:
    if isinstance(value, int) and not isinstance(value, bool):
        if 0 <= value <= 255:
            return ("index", value)
    elif isinstance(value, str):
        text = value.strip().lower()
        hex_match = re.fullmatch(r"#([0-9a-f]{3}|[0-9a-f]{6}|[0-9a-f]{8})", text)
        if hex_match:
            digits = hex_match[1]
            if len(digits) == 3:
                digits = "".join(digit * 2 for digit in digits)
            return ("rgb", tuple(int(digits[i:i + 2], 16) for i in (0, 2, 4)))
        rgb_match = re.fullmatch(r"(rgba?)\(([^)]*)\)", text)
        if rgb_match:
            channels = [channel.strip() for channel in rgb_match[2].split(",")]
            expected = 4 if rgb_match[1] == "rgba" else 3
            if len(channels) == expected and all(c.isdigit() and int(c) <= 255 for c in channels[:3]):
                return ("rgb", tuple(int(c) for c in channels[:3]))
    raise ValueError(f"not a color: {value!r}")


def color_code(color: tuple, depth: str, layer: str) -> str:
    """The SGR parameters that put a color on one layer: "fg" or "bg"."""
    kind, value = color
    if kind == "ansi" or depth == "8":
        index = value if kind == "ansi" else nearest_ansi(color_rgb(color))
        base = 30 if layer == "fg" else 40
        return str(base + index if index < 8 else base + 60 + index - 8)
    prefix = "38" if layer == "fg" else "48"
    if kind == "rgb" and depth == "16m":
        return f"{prefix};2;{value[0]};{value[1]};{value[2]}"
    return f"{prefix};5;{nearest_index(value) if kind == 'rgb' else value}"


ANSI_RGB = (
    (0, 0, 0), (205, 0, 0), (0, 205, 0), (205, 205, 0),
    (0, 0, 238), (205, 0, 205), (0, 205, 205), (229, 229, 229),
    (127, 127, 127), (255, 0, 0), (0, 255, 0), (255, 255, 0),
    (92, 92, 255), (255, 0, 255), (0, 255, 255), (255, 255, 255),
)
ANSI_GREYS = (0, 8, 7, 15)  # black, bright black, white, bright white: darkest first
ANSI_HUES = tuple(index for index in range(16) if index not in ANSI_GREYS)
CUBE_LEVELS = (0, 95, 135, 175, 215, 255)


def color_rgb(color: tuple) -> tuple:
    kind, value = color
    if kind == "rgb":
        return value
    if value < 16:
        return ANSI_RGB[value]
    if value < 232:
        cube = value - 16
        return (CUBE_LEVELS[cube // 36], CUBE_LEVELS[cube // 6 % 6], CUBE_LEVELS[cube % 6])
    gray = 8 + (value - 232) * 10
    return (gray, gray, gray)


def nearest_ansi(rgb: tuple) -> int:
    """The closest of the 16 ANSI colors.

    Greys go by lightness, never by hue: by distance, the dark greys a prompt uses
    came out black (invisible on a dark terminal) and 243 came out yellow.
    """
    if max(rgb) - min(rgb) <= 10:
        lightness = (max(rgb) + min(rgb)) / 2 / 255
        return ANSI_GREYS[sum(lightness >= edge for edge in (0.15, 0.60, 0.85))]
    return min(ANSI_HUES, key=lambda index: _distance(rgb, ANSI_RGB[index]))


@functools.lru_cache(maxsize=4096)
def nearest_index(rgb: tuple) -> int:
    return min(range(16, 256), key=lambda index: _distance(rgb, color_rgb(("index", index))))


def ramp_colors(stops: tuple, steps: int) -> list[tuple]:
    """One ("rgb", ...) color per step, interpolated from the first stop to the last."""
    points = [color_rgb(stop) for stop in stops]
    colors = []
    for step in range(steps):
        position = (step / (steps - 1) if steps > 1 else 0) * (len(points) - 1)
        segment = min(int(position), len(points) - 2)
        fraction = position - segment
        start, end = points[segment], points[segment + 1]
        colors.append(("rgb", tuple(round(a + (b - a) * fraction) for a, b in zip(start, end))))
    return colors


_UNIT = re.compile(ESCAPE_PATTERN + "|.", re.S)


def graphemes(text: str) -> list[str]:
    """Split text into escape sequences and graphemes (a base character plus its marks)."""
    units: list[str] = []
    for unit in _UNIT.findall(text):
        previous = units[-1] if units else ""
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
