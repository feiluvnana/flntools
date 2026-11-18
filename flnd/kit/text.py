"""Text helpers: safe file names, URLs and bodies without secrets, readable numbers, templates.

A library module. It imports only the stdlib, holds no state, and every function is pure, so any
program and any thread may call it. Inside flnd, the file layer names files with `filename` and
`fill`, every log line and error message passes URLs through `redact`, a bug report's sample body
goes through `scrub`, and the progress display and the summaries use `size`, `duration` and
`count`.

What it offers:
- `filename(text)`: a file name every OS accepts (Windows' reserved names and characters,
  control and format characters, a byte cap that never splits a UTF-8 character);
- `redact(url)`: the URL without user info, query and fragment, where tokens and signatures
  live; it never raises, so it is safe inside an error handler;
- `scrub(body)`: the values under secret-looking names replaced by "…", in JSON or any text;
- `size`, `duration` and `count`: a number the way a person reads it (`1.5 KB`, `3m 5s`,
  `3 files`);
- `fill(template, values)` and `fields(template)`: a name template such as `"{date}_{post}"`,
  filled per `/` segment, with the separators beside an empty field dropped.

How `fill` works:
1. The template is split at each `/` outside braces, so a format spec may hold a slash.
2. Each segment is formatted with `str.format`'s own parser; an empty or missing field takes
   the separators (`SEPARATORS`) next to it along.
3. A segment that comes out empty is dropped, and each other one passes through `filename`.

Rules a change must keep:
- Nothing here may print or return a secret it was given: `redact` and `scrub` err on the side
  of removing too much.
- `filename` names files already saved on disk, so its output for a given input must not change,
  or a re-run would save the same file a second time under a new name.
"""

from __future__ import annotations

import re
import string
import unicodedata
from collections.abc import Mapping
from typing import Any
from urllib.parse import urlsplit, urlunsplit

__all__ = ["count", "duration", "fields", "filename", "fill", "redact", "scrub", "size"]

BYTE_UNITS = ("B", "KB", "MB", "GB", "TB")
# What the name of a secret looks like, for `scrub`: a token, a CSRF value, a session, a cookie,
# an auth or API key, a password, a signature.
SECRET_NAMES = re.compile(r"token|csrf|session|cookie|auth|passw|secret|key|sig", re.IGNORECASE)
UNSAFE_CHARACTERS = re.compile(r'[\\/*?:"<>|\x00-\x1f\x7f-\x9f]')
SEPARATOR_RUNS = re.compile(r"[\s_]+")
# Windows refuses these names with any extension, in any case.
RESERVED_NAMES = {"CON", "PRN", "AUX", "NUL", "CONIN$", "CONOUT$"} | {
    f"{kind}{suffix}"
    for kind in ("COM", "LPT")
    for suffix in (*range(1, 10), "\u00b9", "\u00b2", "\u00b3")
}


def filename(name: str, *, fallback: str = "untitled", max_bytes: int = 200) -> str:
    """Turn any text into a file name that Windows, macOS and Linux all accept.

    In order: format characters (zero-width joiners, direction marks) go; each character Windows
    refuses (`\\ / * ? : " < > |` and control characters) becomes `_`; runs of spaces and
    underscores become one `_`; dots, underscores and spaces are trimmed from both ends; the name
    is cut to `max_bytes` of UTF-8 without splitting a character. A Windows device name (`CON`,
    `com1.txt`) gets a `_` after its base, since Windows refuses it with any extension.

    Args:
        name: the text, such as a caption or a title; any length, any characters.
        fallback: what to return when nothing is left, such as for a caption of only dots.
        max_bytes: the longest name, in UTF-8 bytes; 200 leaves room under the common 255-byte
            limit for an extension and a ` (2)` suffix.

    Returns:
        The safe name, or `fallback`.

    Example:
        >>> filename('a/b: "c"?'), filename("CON.txt"), filename("..."), filename("  x  ")
        ('a_b_c', 'CON_.txt', 'untitled', 'x')
    """
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
    """A URL without its user info, query and fragment, safe to log or show.

    Tokens, signatures and session ids live in those parts, so only the scheme, host, port and
    path are kept. A URL that `urlsplit` refuses (a bad port, a broken IPv6 host) is cut by hand
    instead, so this never raises, even inside an error handler.

    Args:
        url: any URL or text; a relative path keeps only its path.

    Returns:
        The URL with only its scheme, host, port and path.

    Example:
        >>> redact("https://user:pw@example.com:8080/a/b?token=x#frag")
        'https://example.com:8080/a/b'
    """
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
    """A byte count the way a person reads it, with one decimal, in units of 1024.

    The unit is the largest one whose value, rounded, stays under 1024, so 1023.96 bytes reads
    `1.0 KB`, not `1024.0 B`. Past terabytes the number just grows.

    Args:
        n: the bytes; a negative number keeps its sign.

    Returns:
        Text such as `512.0 B`, `1.5 KB` or `3.0 GB`.

    Example:
        >>> size(512), size(1536), size(3 * 1024 ** 3)
        ('512.0 B', '1.5 KB', '3.0 GB')
    """
    amount = float(n)
    for unit in BYTE_UNITS[:-1]:
        if abs(round(amount, 1)) < 1024:
            return f"{amount:.1f} {unit}"
        amount /= 1024
    return f"{amount:.1f} {BYTE_UNITS[-1]}"


def duration(seconds: float) -> str:
    """A time span the way a person reads it: milliseconds, seconds, or the two largest units.

    Under a second it reads `250ms`; under a minute `12.4s`; past that, whole seconds in at most
    two non-zero units of `h`, `m` and `s` (`1h 2m`, `3m 5s`), precise enough for a summary.

    Args:
        seconds: the span; it should not be negative.

    Returns:
        The text.

    Example:
        >>> duration(0.25), duration(12.44), duration(185), duration(3720)
        ('250ms', '12.4s', '3m 5s', '1h 2m')
    """
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


def _rough_redact(url: str) -> str:
    """`url` up to its query or fragment, without user info, for a URL `urlsplit` rejects.

    Args:
        url: the URL `redact` couldn't parse.

    Returns:
        The text before the first `?` or `#`, with anything up to an `@` in the host removed.
    """
    head = re.split(r"[?#]", url, maxsplit=1)[0]
    scheme, sep, rest = head.partition("://")
    if not sep:
        return head
    authority, slash, path = rest.partition("/")
    return f"{scheme}{sep}{authority.rpartition('@')[2]}{slash}{path}"


def scrub(text: str, *, secrets: re.Pattern[str] = SECRET_NAMES) -> str:
    """`text` with every value under a name that looks like a secret replaced by "…".

    For a body kept to show what an answer looked like (a bug report's sample) without what it
    must not show. Over-scrubbing is the safe side: `key` also matches `monkey`, and its value
    goes too.
    - JSON is parsed, and each value under a matching key becomes "…", a whole object or list
      included; it comes back indented.
    - Any other text (HTML, a form, a script) has its `name: value`, `name=value` and
      `"name": "value"` pairs scrubbed, and the `value` of an HTML input whose `name` matches.

    Args:
        text: the body, as text.
        secrets: what a secret's name matches, anywhere in it, in any case.

    Returns:
        The scrubbed text.

    Example:
        >>> scrub('{"user": "example_user", "csrf_token": "s3cr3t"}')
        '{\\n "user": "example_user",\\n "csrf_token": "…"\\n}'
        >>> scrub("<input name=csrf value=abc> next=page2&auth_key=xyz")
        '<input name=csrf value=…> next=page2&auth_key=…'
    """
    import json

    try:
        data = json.loads(text)
    except ValueError:
        data = None
    if isinstance(data, (dict, list)):
        return json.dumps(_scrubbed(data, secrets), indent=1, ensure_ascii=False)
    # 1. An input's value, wherever its matching name stands before it.
    text = re.sub(r"""(<input[^>]*?\bname=["']?([^"'\s>]*)["']?[^>]*?\bvalue=["']?)([^"'\s>]*)""",
                  lambda m: m[1] + ("…" if secrets.search(m[2]) else m[3]), text,
                  flags=re.IGNORECASE)
    # 2. Any `name = value` or `"name": "value"` pair: the value runs to a quote, a space or a
    # separator, so a query string's next pair stays.
    return re.sub(r"""(["']?)([\w.-]+)\1(\s*[:=]\s*)(["']?)([^"'\s&,;<>{}]*)""",
                  lambda m: m[0] if not secrets.search(m[2]) or not m[5]
                  else f"{m[1]}{m[2]}{m[1]}{m[3]}{m[4]}…", text)


def _scrubbed(data: Any, secrets: re.Pattern[str]) -> Any:
    """A JSON value with the values under secret-looking keys replaced by "…", at any depth.

    Args:
        data: the parsed JSON: an object, a list, or a plain value.
        secrets: what a secret's key matches.

    Returns:
        A scrubbed copy; `data` itself is left as it is.
    """
    if isinstance(data, dict):
        return {key: "…" if secrets.search(str(key)) else _scrubbed(value, secrets)
                for key, value in data.items()}
    if isinstance(data, list):
        return [_scrubbed(value, secrets) for value in data]
    return data


# ---------------------------------------------------------------------------------------------
# Name templates
# ---------------------------------------------------------------------------------------------

# What a template drops beside a field that came out empty: `{date}_{post}` without a date is
# `<post>`, not `_<post>`.
SEPARATORS = "_- .:,"
# The formatter of every template: `parse`, `get_field` and `format_field` are str.format's own.
_FORMATTER = string.Formatter()


def fill(template: str, values: Mapping[str, Any], *, safe: bool = True) -> str:
    """Format a template from `values`, dropping separators around missing or empty fields.

    Each `/`-separated segment is filled on its own; a segment that comes out empty is left
    out, so `{category}/{album}` without an album is just the category.

    Args:
        template: a `str.format` template; `{a.b}` and `{a[0]}` reach into a value.
        values: the fields' values; a missing one, None and "" all read as empty.
        safe: pass each segment through `filename`, so a value holding `/` or `:` can't make
            a folder or an invalid name. False keeps the text as it is (history keys).

    Returns:
        The filled template, its empty segments dropped.

    Example:
        >>> fill("{category}/{album}", {"category": "posts"})
        'posts'
        >>> fill("{date}_{post}{index}", {"post": "C1abc", "date": "", "index": ""})
        'C1abc'
        >>> fill("{author}/{post} {title}", {"author": "a/b", "post": 7})
        'a_b/7'
    """
    segments = []
    for segment in _split_segments(template):
        piece = _fill_segment(segment, values)
        if piece:
            segments.append(filename(piece) if safe else piece)
    return "/".join(segments)


def fields(template: str) -> set[str]:
    """The top-level field names a template uses: `{a.b}` and `{a[0]}` count as `a`.

    Args:
        template: a `str.format` template.

    Returns:
        The names, without their attribute or index parts.

    Raises:
        ValueError: the template's braces don't match.

    Example:
        >>> sorted(fields("{date}_{post}/{album.title}"))
        ['album', 'date', 'post']
    """
    return {
        name.split(".")[0].split("[")[0]
        for _, name, _, _ in _FORMATTER.parse(template) if name
    }


def _split_segments(template: str) -> list[str]:
    """`template` split at `/` outside `{…}`, so a field's format spec may hold a slash.

    Args:
        template: a path template, such as `{user}/{date:%Y/%m}/{name}`.

    Returns:
        The segments, in order; a template without a top-level `/` gives one segment.
    """
    parts, depth, start = [], 0, 0
    for index, character in enumerate(template):
        depth += {"{": 1, "}": -1}.get(character, 0)
        if character == "/" and depth == 0:
            parts.append(template[start:index])
            start = index + 1
    return [*parts, template[start:]]


def _fill_segment(segment: str, values: Mapping[str, Any]) -> str:
    """One segment of a template, filled in, with the separators of empty fields dropped.

    Args:
        segment: one piece of a template, as `_split_segments` returns it.
        values: the fields the template may name, by name.

    Returns:
        The filled text; it may be empty when every field is empty.
    """
    # Each piece is [text, whether literal]; only literals lose separators.
    pieces: list[list[Any]] = []
    # Set by an empty field that starts the segment: the separators after it go too.
    drop_next = False
    for literal, name, spec, conversion in _FORMATTER.parse(segment):
        if literal:
            if drop_next:
                literal, drop_next = literal.lstrip(SEPARATORS), False
            pieces.append([literal, True])
        if name is None:
            continue
        value = _value(name, values)
        text = ""
        if value is not None and value != "":
            text = _FORMATTER.format_field(_FORMATTER.convert_field(value, conversion), spec or "")
        if text:
            pieces.append([text, False])
            drop_next = False
        elif pieces and pieces[-1][1]:
            pieces[-1][0] = pieces[-1][0].rstrip(SEPARATORS)
        elif not any(piece for piece, _ in pieces):
            drop_next = True
    return "".join(piece for piece, _ in pieces)


def _value(name: str, values: Mapping[str, Any]) -> Any:
    """A field's value, `a.b` and `a[0]` included; None when it's missing.

    Args:
        name: the field's name as written in the template, with any `.attr` or `[index]`.
        values: the fields to look the name up in.

    Returns:
        The value, or None when the field, attribute or index does not exist.
    """
    try:
        return _FORMATTER.get_field(name, (), values)[0]
    # A missing field is empty, whichever way it's missing.
    except (KeyError, AttributeError, IndexError, TypeError):
        return None
