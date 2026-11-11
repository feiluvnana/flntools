"""Numbers and times as short text. Pure functions; the caller adds color."""

from __future__ import annotations

import datetime as _datetime

BYTE_UNITS = ("B", "KB", "MB", "GB", "TB")


def bytes(n: int) -> str:
    """Bytes in the largest unit under 1024, with one decimal: `1.5 KB`."""
    size = float(n)
    for unit in BYTE_UNITS[:-1]:
        if abs(round(size, 1)) < 1024:
            return f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} {BYTE_UNITS[-1]}"


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
    """A date as `YYYY-MM-DD`; a datetime or Unix timestamp as `YYYY-MM-DD HH:MM`."""
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        value = _datetime.datetime.fromtimestamp(value)
    if pattern is None:
        pattern = "%Y-%m-%d %H:%M" if isinstance(value, _datetime.datetime) else "%Y-%m-%d"
    return value.strftime(pattern)
