"""Cookie jars: read cookies from a file or raw text.

SPEC.md#logins-and-accounts explains it."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any

from flnkit import browser
from flnkit.errors import LoginError

__all__ = ["Jar", "load"]

Cookie = dict[str, Any]
PathLike = str | Path

_PLATFORM = sys.platform

# The largest cookie file `load` reads: 16 MiB. A full browser export is a few hundred KB, so
# anything this size is a mistake or an attack, refused before it is parsed.
MAX_SIZE = 16 * 1024 * 1024
# The spellings JSON exports use: Playwright and extensions ("no_restriction" in Chrome).
_JSON_SAME_SITE = {"none": "None", "no_restriction": "None", "lax": "Lax", "strict": "Strict"}
_HTTP_ONLY = "#HttpOnly_"


def home() -> Path:
    """The account's home folder, from the operating system, whatever `Path.home` says.

    Returns:
        The password database's home on POSIX, or `%USERPROFILE%` on Windows.
    """
    if _PLATFORM == "win32":
        return Path(os.environ.get("USERPROFILE") or os.path.expanduser("~"))
    try:
        import pwd

        return Path(pwd.getpwuid(os.getuid()).pw_dir)
    except (ImportError, AttributeError):
        return Path(os.environ.get("USERPROFILE") or os.path.expanduser("~"))


# Errors: cookies are read to log in, so a cookie file that is broken, too big or unreadable
# raises `LoginError`; its message names the file, never a cookie's value.


Jar = browser.Jar


def load(source: PathLike | str) -> Jar:
    """Load cookies from a `cookies.txt` file, or from raw cookie text.

    `source` is read as raw text when it holds a line break or a tab, or starts with `[` or `{`
    (JSON); else as a file path. A file may be a Netscape `cookies.txt` (curl, browser
    extensions) or a JSON list in Playwright's or an extension's shape, and is refused past
    `MAX_SIZE` before it is parsed.

    Args:
        source: cookie text, or the path of a cookie file.

    Returns:
        A new jar with every cookie read, in the source's order.

    Raises:
        LoginError: the file is too big, not UTF-8 or not a cookie file.
        FileNotFoundError: the file is missing.
        OSError: the file can't be read.
    """
    if isinstance(source, str):
        if "\n" in source or "\r" in source or "\t" in source or source.lstrip()[:1] in ("[", "{"):
            return _load_text(source, "<text>")
    path = Path(source)
    if path.stat().st_size > MAX_SIZE:
        raise LoginError(f"{path}: a cookie file over {MAX_SIZE // (1024 * 1024)} MB is refused")
    with open(path, "rb") as file:
        data = file.read(MAX_SIZE + 1)
    if len(data) > MAX_SIZE:
        raise LoginError(f"{path}: a cookie file over {MAX_SIZE // (1024 * 1024)} MB is refused")
    try:
        text = data.decode("utf-8-sig")
    except UnicodeDecodeError as err:
        # Not str(err): it quotes the undecodable byte, which may be part of a value.
        raise LoginError(f"{path}: not UTF-8 text (at byte {err.start})") from None
    return _load_text(text, str(path))


def _load_text(text: str, label: str) -> Jar:
    """Parse raw Netscape or JSON cookie text into a `Jar`."""
    try:
        parsed = _parse_json(text) if text.lstrip()[:1] in ("[", "{") else _parse_netscape(text)
    except ValueError as err:
        raise LoginError(f"{label}: {err}") from None
    return Jar(parsed, error=LoginError)


def _cookie(name: str, value: str, domain: str, path: str, expires: int,
            secure: Any, http_only: Any, same_site: str) -> Cookie:
    return {
        "name": name, "value": value, "domain": domain, "path": path, "expires": expires,
        "secure": bool(secure), "httpOnly": bool(http_only), "sameSite": same_site,
    }


def _parse_netscape(text: str) -> list[Cookie]:
    """The cookies of a Netscape file; a ValueError names the line, never its value."""
    jar = []
    for number, line in enumerate(text.splitlines(), start=1):
        # curl and browser exports mark HttpOnly cookies by prefixing the domain like a comment.
        http_only = line.startswith(_HTTP_ONLY)
        if http_only:
            line = line[len(_HTTP_ONLY):]
        if not line.strip() or line.startswith("#"):
            continue
        # At most 7 fields: a value may itself hold a tab.
        fields = line.split("\t", 6)
        if len(fields) != 7:
            raise ValueError(f"line {number} has {len(fields)} tab-separated fields, not 7")
        domain, _, path, secure, expires, name, value = fields
        try:
            expiry = int(expires.strip() or "0")
        except ValueError:
            raise ValueError(f"line {number}: expiry {expires!r} is not a whole number") from None
        is_sec = secure.upper() == "TRUE"
        jar.append(_cookie(name, value, domain, path, expiry or -1, is_sec, http_only, "Lax"))
    return jar


def _parse_json(text: str) -> list[Cookie]:
    # json's own message gives only a position, never the text around it.
    entries = json.loads(text)
    if not isinstance(entries, list):
        raise ValueError("JSON cookies must be a list")
    return [_json_cookie(entry, index) for index, entry in enumerate(entries)]


def _json_cookie(entry: Any, index: int) -> Cookie:
    """One cookie of a JSON export, in Playwright's or a browser extension's shape."""
    if not isinstance(entry, dict) or not all(key in entry for key in ("name", "value", "domain")):
        raise ValueError(f"cookie {index} needs a name, a value and a domain")
    # Playwright names it "expires"; browser extensions name it "expirationDate".
    expires = entry.get("expires", entry.get("expirationDate"))
    try:
        expiry = -1 if expires is None or entry.get("session") else int(float(expires))
    except (TypeError, ValueError, OverflowError):
        raise ValueError(f"cookie {index}: expiry {expires!r} is not a number") from None
    same_site = _JSON_SAME_SITE.get(str(entry.get("sameSite", "")).lower(), "Lax")
    exp = expiry if expiry > 0 else -1
    return _cookie(
        str(entry["name"]), str(entry["value"]), str(entry["domain"]),
        str(entry.get("path") or "/"), exp, entry.get("secure"), entry.get("httpOnly"), same_site,
    )
