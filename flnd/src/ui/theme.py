"""The theme: every colour and glyph flnd draws with, and when to drop colour or Unicode.

src/docs/cli explains it.
"""

from __future__ import annotations

import os
from functools import lru_cache
from typing import Any

__all__ = ["GLYPHS", "MARKS", "PADDING", "SPINNER", "TOKENS", "box", "color", "glyph",
           "spinner", "style", "tty", "unicode"]

# The colour of each token: the only colours flnd writes.
TOKENS: dict[str, str] = {
    "text": "#D0D0D0",   # body text
    "dim": "#767676",    # hints, keys, borders, zeros
    "focus": "#7D84D6",  # the cursor, titles, bars, spinners
    "good": "#02BF87",   # done, answers
    "bad": "#ED567A",    # failed, errors
    "warn": "#E8C468",   # warnings, holds
}

# Each glyph, then the ASCII a terminal that isn't UTF-8 gets instead.
GLYPHS: dict[str, tuple[str, str]] = {
    "bullet": ("•", "*"),
    "cursor": ("❯", ">"),
    "done": ("✓", "v"),
    "failed": ("✗", "x"),
    "waiting": ("·", "-"),
    "followed": ("●", "o"),
    "unfollowed": ("○", " "),
    "default": ("★", "+"),
    "bar": ("┃", "|"),
    # A log line's marks, as they were before the theme: a warning, a debug line.
    "warn": ("!", "!"),
    "debug": ("·", "."),
    # A progress bar's filled and empty cells, a cut in the middle, and a fold's count.
    "full": ("━", "="),
    "empty": ("─", "-"),
    "more": ("…", "..."),
    "times": ("×", "x"),
    # The arrow keys, as the bottom line of a list or a form names them.
    "updown": ("↑↓", "up/down"),
    "leftright": ("←→", "left/right"),
}

# The mark and the token of each kind of line.
MARKS: dict[str, tuple[str, str]] = {
    "debug": ("debug", "dim"),
    "info": ("bullet", "dim"),
    "ok": ("done", "good"),
    "warn": ("warn", "warn"),
    "error": ("failed", "bad"),
}

# The spinner's frames, then the ASCII ones, shown at `FPS` frames a second.
SPINNER: tuple[tuple[str, ...], tuple[str, ...]] = (
    ("⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏"),
    ("|", "/", "-", "\\"),
)
FPS = 10
# Panels and detail views: Rich's `box.ROUNDED` (`box()`), with this padding.
PADDING = (0, 1)


def box() -> Any:
    """The border of panels, tables and detail views (Rich's `box.ROUNDED`)."""
    from rich import box as boxes

    return boxes.ROUNDED


@lru_cache(maxsize=128)
def style(spec: str) -> str:
    """A Rich style with each token in `spec` replaced by its colour: "bold bad" is bold red.

    Args:
        spec: space-separated tokens and Rich attributes ("bold", "italic"); "" for none.
    """
    return " ".join(TOKENS.get(word, word) for word in spec.split())


def color(stream: Any) -> bool:
    """Whether to colour what goes to `stream`: not with `NO_COLOR`, a dumb terminal, or a pipe.

    Args:
        stream: the file the output goes to.
    """
    if os.environ.get("NO_COLOR"):
        return False
    if os.environ.get("TERM") == "dumb":
        return False
    return tty(stream)


def unicode(stream: Any) -> bool:
    """Whether `stream` can show the Unicode glyphs, from its encoding and the locale.

    Args:
        stream: the file the output goes to.

    Returns:
        True for a UTF-8 stream, except under a Japanese, Korean or Chinese locale. On Windows
        it is True only in a terminal that sets one of its known variables, or under ConEmu.
    """
    encoding = getattr(stream, "encoding", None) or ""
    if not encoding and tty(stream):
        encoding = "utf-8"
    if encoding.lower().replace("-", "").replace("_", "") != "utf8":
        return False
    locale = os.environ.get("LC_ALL") or os.environ.get("LANG") or ""
    if locale.lower().startswith(("ja", "ko", "zh")):
        return False
    modern = ("WT_SESSION", "TERM", "ANSICON", "TERM_PROGRAM")
    if os.name == "nt" and not any(os.environ.get(k) for k in modern):
        return os.environ.get("ConEmuANSI") == "ON"
    return True


def glyph(name: str, fancy: bool) -> str:
    """The glyph called `name`: its Unicode form when `fancy`, else its ASCII one."""
    return GLYPHS[name][0 if fancy else 1]


def spinner(at: float, fancy: bool) -> str:
    """The spinner's frame at `at` seconds: Unicode when `fancy`, else ASCII."""
    frames = SPINNER[0 if fancy else 1]
    return frames[int(at * FPS) % len(frames)]


def tty(stream: Any) -> bool:
    """Whether `stream` is a terminal; False for a pipe, a file or a closed stream."""
    try:
        return bool(stream.isatty())
    except (AttributeError, OSError, ValueError):
        return False
