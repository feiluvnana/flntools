"""Values as short text, and text as safe file names. Pure functions; the caller adds color."""

from __future__ import annotations

import datetime as _datetime
import re

BYTE_UNITS = ("B", "KB", "MB", "GB", "TB")
UNSAFE_CHARACTERS = re.compile(r'[\\/*?:"<>|\x00-\x1f\x7f]')
SEPARATOR_RUNS = re.compile(r"[\s_]+")
# Windows refuses these names with any extension, in any case.
RESERVED_NAMES = {"CON", "PRN", "AUX", "NUL"} | {f"{kind}{n}" for kind in ("COM", "LPT") for n in range(1, 10)}


def size(n: int) -> str:
    """Bytes in the largest unit under 1024, with one decimal: `1.5 KB`.

    >>> fmt.size(1536)
    '1.5 KB'
    """
    amount = float(n)
    for unit in BYTE_UNITS[:-1]:
        if abs(round(amount, 1)) < 1024:
            return f"{amount:.1f} {unit}"
        amount /= 1024
    return f"{amount:.1f} {BYTE_UNITS[-1]}"


def duration(seconds: float) -> str:
    """`250ms` under a second, `12.4s` under a minute, then two units: `1h 1m`."""
    if round(seconds * 1000) < 1000:
        return f"{round(seconds * 1000)}ms"
    if round(seconds, 1) < 60:
        return f"{seconds:.1f}s"
    whole = round(seconds)
    units = ((whole // 3600, "h"), (whole % 3600 // 60, "m"), (whole % 60, "s"))
    nonzero = [f"{amount}{unit}" for amount, unit in units if amount]
    return " ".join(nonzero[:2])


def date(value: _datetime.date | float, pattern: str | None = None) -> str:
    """A date as `YYYY-MM-DD`; a datetime or Unix timestamp as `YYYY-MM-DD HH:MM`.

    A bool raises TypeError: it is an int, but never a timestamp.
    """
    if isinstance(value, bool):
        raise TypeError("fmt.date() takes a date, datetime, or Unix timestamp, not a bool")
    if isinstance(value, (int, float)):
        value = _datetime.datetime.fromtimestamp(value)
    if pattern is None:
        pattern = "%Y-%m-%d %H:%M" if isinstance(value, _datetime.datetime) else "%Y-%m-%d"
    return value.strftime(pattern)


def filename(text: str, *, fallback: str = "untitled", maxlen: int = 200) -> str:
    """Text as a file or folder name that is safe on Windows, macOS, and Linux.

    >>> fmt.filename("Summer: 2026 / day 1?")
    'Summer_2026_day_1'

    The result is at most `maxlen` bytes of UTF-8, and `fallback` when nothing is left.
    """
    name = UNSAFE_CHARACTERS.sub("_", text)
    name = SEPARATOR_RUNS.sub("_", name).strip("._ ")
    name = name.encode("utf-8")[:maxlen].decode("utf-8", "ignore").strip("._ ")
    if not name:
        return fallback
    # Windows ignores the extension when it checks a reserved name, so "nul.txt" is refused too.
    base, dot, rest = name.partition(".")
    if base.upper() in RESERVED_NAMES:
        name = f"{base}_{dot}{rest}"
    return name
