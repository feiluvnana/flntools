"""Values as short text for a person to read, and text as safe file names.

Each function takes one value and returns a plain `str`: a size in bytes, a duration, a date, a
count with its noun, or a file name every platform accepts. They are pure, so the caller adds
color (`shared.style`) and decides where the text goes. Stdlib only: nothing is wrapped, so there
is no `.raw`.

    log.ok("saved {count}, {size}", count=fmt.count(3, "file"), size=fmt.size(1536))  # 3 files, 1.5 KB
"""

from __future__ import annotations

import datetime as _datetime
import re

import shared

__all__ = ["Error", "count", "date", "duration", "filename", "size"]

BYTE_UNITS = ("B", "KB", "MB", "GB", "TB")
UNSAFE_CHARACTERS = re.compile(r'[\\/*?:"<>|\x00-\x1f\x7f]')
SEPARATOR_RUNS = re.compile(r"[\s_]+")
# Windows refuses these names with any extension, in any case.
RESERVED_NAMES = {"CON", "PRN", "AUX", "NUL"} | {f"{kind}{n}" for kind in ("COM", "LPT") for n in range(1, 10)}


class Error(shared.Error):
    """The base of every error `shared.fmt` raises."""


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


def count(n: int, noun: str, *, plural: str | None = None) -> str:
    """A number with its noun, singular for exactly one: `1 file`, `0 files`, `1,200 files`.

        fmt.count(3, "file")                       # '3 files'
        fmt.count(1, "story", plural="stories")    # '1 story'

    Args:
        n: how many; written with thousands separators.
        noun: the singular noun, such as "file".
        plural: the plural for a noun that doesn't just add "s", such as "stories"; None adds "s".
    """
    if n == 1:
        return f"{n:,} {noun}"
    return f"{n:,} {plural if plural is not None else noun + 's'}"


def date(value: _datetime.date | float, *, pattern: str | None = None) -> str:
    """A date as `YYYY-MM-DD`; a datetime or Unix timestamp as `YYYY-MM-DD HH:MM`.

        fmt.date(datetime.date(2026, 11, 11))       # '2026-11-11'
        fmt.date(stamp, pattern="%d/%m")            # '11/11', in local time

    Args:
        value: a date, a datetime, or a Unix timestamp in seconds, read in local time.
        pattern: a `strftime` pattern; None picks one of the two above by the value's kind.

    Raises:
        TypeError: `value` is a bool: it is an int, but never a timestamp.
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
