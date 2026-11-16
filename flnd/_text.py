"""Values as short text for a person to read, and safe file names and URLs."""

from __future__ import annotations

import re
import unicodedata
from urllib.parse import urlsplit, urlunsplit

from flnd import _base

__all__ = ["Error", "count", "duration", "filename", "fold", "redact", "size"]

BYTE_UNITS = ("B", "KB", "MB", "GB", "TB")
UNSAFE_CHARACTERS = re.compile(r'[\\/*?:"<>|\x00-\x1f\x7f-\x9f]')
SEPARATOR_RUNS = re.compile(r"[\s_]+")
# Windows refuses these names with any extension, in any case.
RESERVED_NAMES = {"CON", "PRN", "AUX", "NUL", "CONIN$", "CONOUT$"} | {
    f"{kind}{suffix}"
    for kind in ("COM", "LPT")
    for suffix in (*range(1, 10), "\u00b9", "\u00b2", "\u00b3")
}


class Error(_base.Error):
    """The base of every error `flnd._text` raises."""


def filename(name: str, *, fallback: str = "untitled", max_bytes: int = 200) -> str:
    """Turn text into a file name safe on Windows, macOS, and Linux."""
    cleaned = "".join(ch for ch in name if unicodedata.category(ch) != "Cf")
    cleaned = UNSAFE_CHARACTERS.sub("_", cleaned)
    cleaned = SEPARATOR_RUNS.sub("_", cleaned).strip("._ ")
    cleaned = cleaned.encode("utf-8")[:max_bytes].decode("utf-8", "ignore").strip("._ ")
    if not cleaned:
        return fallback
    base, dot, rest = cleaned.partition(".")
    if base.upper() in RESERVED_NAMES:
        cleaned = f"{base}_{dot}{rest}"
        if dot and len(cleaned.encode("utf-8")) > max_bytes:
            cleaned = (
                cleaned.encode("utf-8")[:max_bytes].decode("utf-8", "ignore").strip("._ ")
            )
    return cleaned


def redact(url: str) -> str:
    """Strip userinfo, query, and fragment from a URL; never raises, even on a broken one."""
    try:
        parts = urlsplit(url)
        if not parts.netloc:
            return urlunsplit((parts.scheme, "", parts.path, "", ""))
        host = parts.hostname or ""
        if ":" in host:
            host = f"[{host}]"
        netloc = f"{host}:{parts.port}" if parts.port is not None else host
        return urlunsplit((parts.scheme, netloc, parts.path, "", ""))
    except ValueError:
        return _rough_redact(url)


def size(n: float) -> str:
    """Format bytes in the largest unit under 1024 with one decimal place."""
    amount = float(n)
    for unit in BYTE_UNITS[:-1]:
        if abs(round(amount, 1)) < 1024:
            return f"{amount:.1f} {unit}"
        amount /= 1024
    return f"{amount:.1f} {BYTE_UNITS[-1]}"


def duration(seconds: float) -> str:
    """Format seconds as `250ms` under a second, `12.4s` under a minute, or two units."""
    if round(seconds * 1000) < 1000:
        return f"{round(seconds * 1000)}ms"
    if round(seconds, 1) < 60:
        return f"{seconds:.1f}s"
    whole = round(seconds)
    units = ((whole // 3600, "h"), (whole % 3600 // 60, "m"), (whole % 60, "s"))
    nonzero = [f"{amount}{unit}" for amount, unit in units if amount]
    return " ".join(nonzero[:2])


def count(n: float, unit: str, *, plural: str | None = None) -> str:
    """Format a number with its unit, using singular only for exactly one."""
    if n == 1:
        return f"{n:,} {unit}"
    return f"{n:,} {plural if plural is not None else unit + 's'}"


def fold(text: str) -> str:
    """Normalize Unicode, strip combining marks, casefold, and collapse whitespace."""
    norm = unicodedata.normalize("NFKD", text.replace("đ", "d").replace("Đ", "D"))
    stripped = "".join(ch for ch in norm if not unicodedata.combining(ch))
    return " ".join(stripped.casefold().split())


def _rough_redact(url: str) -> str:
    """`url` up to its query or fragment, without userinfo, for a URL `urlsplit` rejects."""
    head = re.split(r"[?#]", url, maxsplit=1)[0]
    scheme, sep, rest = head.partition("://")
    if not sep:
        return head
    authority, slash, path = rest.partition("/")
    return f"{scheme}{sep}{authority.rpartition('@')[2]}{slash}{path}"
