"""Cookie jars: read cookies from a file, a browser or text, and save them.

SPEC.md#logins-and-accounts explains it."""

from __future__ import annotations

import configparser
import json
import logging
import os
import sqlite3
import struct
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from flnkit import browser
from flnkit.errors import LoginError, UsageError

_REMOVED = frozenset({"chrome", "chromium", "edge", "brave"})

_log = logging.getLogger(__name__)

__all__ = ["BROWSERS", "Jar", "load", "parse_binarycookies"]

Cookie = dict[str, Any]
PathLike = str | Path

_PLATFORM = sys.platform

# The largest cookie file `load` reads: 16 MiB. A full browser export is a few hundred KB, so
# anything this size is a mistake or an attack, refused before it is parsed.
MAX_SIZE = 16 * 1024 * 1024

# Every browser `load` reads cookies from, by the name `flnd auth login <source> <browser>` takes.
# Safari is read on macOS only.
BROWSERS = ("firefox", "safari")
# Safari's cookie file, under the home folder: in its sandbox container since Safari 14, and
# where older versions kept it. The first that exists is read.
SAFARI_FILES = (
    Path("Library/Containers/com.apple.Safari/Data/Library/Cookies/Cookies.binarycookies"),
    Path("Library/Cookies/Cookies.binarycookies"),
)
# What a binarycookies file and each of its pages start with.
SAFARI_MAGIC = b"cook"
SAFARI_PAGE = b"\x00\x00\x01\x00"
# Safari's times count seconds from 2001-01-01 (Apple's reference date), not from 1970.
APPLE_EPOCH_OFFSET = 978_307_200
# A Safari cookie's flags: bit 0 secure, bit 2 HTTP-only.
SAFARI_SECURE, SAFARI_HTTP_ONLY = 0x1, 0x4
# What to do when macOS's privacy protection keeps Safari's file from the terminal.
FULL_DISK_ACCESS = ("give your terminal Full Disk Access (System Settings > Privacy & Security "
                    "> Full Disk Access), or export a cookie file from Safari instead")
# Chromium and Firefox `samesite`: 0 none, 1 lax, 2 strict; -1 (unspecified) is Lax.
_SAME_SITE = {0: "None", 1: "Lax", 2: "Strict"}
# The spellings JSON exports use: Playwright and extensions ("no_restriction" in Chrome).
_JSON_SAME_SITE = {"none": "None", "no_restriction": "None", "lax": "Lax", "strict": "Strict"}
_HTTP_ONLY = "#HttpOnly_"
# Set by flnd's test suite (`make test`, and `tests/fakes.py`): while set, a browser profile in
# the real home folder is refused, so no test can read a person's real cookies or logins.
NO_PROFILES = "FLND_TEST_NO_PROFILES"


def refuse_profile(folder: Path) -> None:
    """Refuse `folder`, a browser profile about to be read or copied, if the test suite is running.

    When the `FLND_TEST_NO_PROFILES` environment variable is set, a profile inside the real home
    folder raises at once; one inside a temporary folder (a test's fake home) is allowed. The
    real home is the account's own, not `Path.home()`, which a test may point elsewhere. Without
    the variable this does nothing.

    Args:
        folder: the profile folder (or the folder holding the profiles).

    Raises:
        RuntimeError: the variable is set and `folder` is in the real home.
    """
    if not os.environ.get(NO_PROFILES):
        return
    import tempfile

    resolved = Path(folder).resolve()
    try:
        candidates = (os.environ.get("TEMP"), os.environ.get("TMP"), tempfile.gettempdir())
        if any(c and resolved.is_relative_to(Path(c).resolve()) for c in candidates):
            return
    except (ValueError, OSError):
        pass
    real_home = home().resolve()
    if resolved.is_relative_to(real_home):
        raise RuntimeError(f"{NO_PROFILES} is set: a test may not read or copy the real browser "
                           f"profile {folder}; fake the home folder or the browser instead")


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


# Errors: cookies are read to log in, so a cookie file that is broken, too big or unreadable, and
# a browser whose cookies can't be decrypted, all raise `LoginError`; its message names the file
# or the browser, never a cookie's value. An unknown browser name is a `UsageError`.


Jar = browser.Jar


def load(source: PathLike | str, *, profile: str | None = None) -> Jar:
    """Load cookies from a `cookies.txt` file, a browser name, or raw cookie text.

    `source` is read as, in order: a browser name from `BROWSERS` (any case); raw text, when it
    holds a line break or a tab, or starts with `[` or `{` (JSON); else a file path. A file may
    be a Netscape `cookies.txt` (curl, browser extensions) or a JSON list in Playwright's or an
    extension's shape, and is refused past `MAX_SIZE` before it is parsed.

    Args:
        source: a browser name, cookie text, or the path of a cookie file.
        profile: the Firefox profile folder name; `None` means the profile Firefox opens first.
            Only for a browser name.

    Returns:
        A new jar with every cookie read, in the source's order.

    Raises:
        UsageError: `profile` is given but `source` isn't a known browser, or Safari is asked
            for off macOS.
        LoginError: the file is too big, not UTF-8 or not a cookie file; the browser's database
            can't be read, or none of its encrypted values can be decrypted; macOS's privacy
            protection keeps Safari's file from this program (the hint says how to allow it).
        FileNotFoundError: the file, the browser's profile or its cookie database is missing.
        OSError: the file can't be read.
    """
    if isinstance(source, str):
        lower = source.lower()
        if lower in _REMOVED:
            raise UsageError(
                f"{lower}'s cookies are read through the downloader's Chrome extension",
                hint="use the chrome login, not cookies.load",
            )
        if lower in BROWSERS:
            if lower == "firefox":
                return Jar(_load_firefox(profile), error=LoginError)
            return Jar(_load_safari(), error=LoginError)
        if profile is not None:
            raise UsageError(f"unknown browser {source!r}: use one of {', '.join(BROWSERS)}")
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


def _load_firefox(profile: str | None) -> list[Cookie]:
    """Read cookies from a Firefox `cookies.sqlite` database."""
    folder = _firefox_profile(profile)
    refuse_profile(folder)
    with _open_copy(folder / "cookies.sqlite") as connection:
        try:
            rows = connection.execute(
                "SELECT host, name, value, path, expiry, isSecure, isHttpOnly, sameSite "
                "FROM moz_cookies"
            ).fetchall()
        except sqlite3.Error as error:
            raise LoginError(f"cannot read Firefox cookies: {error}") from error
    jar = []
    for host, name, value, path, expiry, secure, http_only, same_site in rows:
        # Firefox stores expiry in ms in newer versions; seconds stay below 10**11 until 5138.
        expires = expiry // 1000 if expiry > 10**11 else expiry
        same = _SAME_SITE.get(same_site, "Lax")
        jar.append(_cookie(name, value, host, path, expires or -1, secure, http_only, same))
    return jar


def _load_safari() -> list[Cookie]:
    """Read Safari's cookies from its `Cookies.binarycookies` file, on macOS."""
    if _PLATFORM != "darwin":
        raise UsageError("Safari's cookies are read on macOS only",
                         hint="use another browser, or a cookie file")
    for relative in SAFARI_FILES:
        path = Path.home() / relative
        refuse_profile(path.parent)
        try:
            data = path.read_bytes()
        except FileNotFoundError:
            continue
        except PermissionError:
            # macOS's privacy protection (TCC) answers EPERM for Safari's container to any
            # program the person hasn't given Full Disk Access, even with the file's mode open.
            raise LoginError(f"macOS doesn't let this program read {path}",
                             hint=FULL_DISK_ACCESS) from None
        if len(data) > MAX_SIZE:
            raise LoginError(f"{path}: a cookie file over {MAX_SIZE // (1024 * 1024)} MB is "
                             "refused")
        try:
            return parse_binarycookies(data)
        except ValueError as error:
            raise LoginError(f"{path}: {error}") from None
    raise FileNotFoundError(f"Safari has no cookie file in {Path.home() / 'Library'}")


def parse_binarycookies(data: bytes) -> list[Cookie]:
    """Parse Safari's binarycookies format, with the stdlib alone.

    The layout, as Safari writes it (big-endian where marked, little-endian elsewhere):
    1. `cook`, then the number of pages (4 bytes, big-endian) and each page's size (4 bytes
       each, big-endian); the pages follow, back to back.
    2. A page: `00 00 01 00`, the number of cookies (4 bytes), each cookie's offset from the
       page's start (4 bytes each), then 4 zero bytes; the cookies follow.
    3. A cookie: its size, a version, its flags (`SAFARI_SECURE`, `SAFARI_HTTP_ONLY`), a word
       that tells whether it names a port, then the offsets from the cookie's start of its
       domain, name, path and value (4 bytes each), a comment's offset and 4 more bytes, then
       its expiry and creation times as doubles from 2001-01-01; the four strings follow,
       each ending in a zero byte.
    4. After the pages: a checksum and a property list, which aren't needed.

    Args:
        data: the whole file.

    Returns:
        The cookies, in the file's order; a session cookie (no expiry) has `expires` -1.

    Raises:
        ValueError: the data isn't a binarycookies file, or a page or cookie runs past its end;
            the message names the page and cookie, never a value.
    """
    if data[:4] != SAFARI_MAGIC or len(data) < 8:
        raise ValueError("not a Safari binarycookies file")
    (pages,) = struct.unpack_from(">I", data, 4)
    sizes_end = 8 + 4 * pages
    if len(data) < sizes_end:
        raise ValueError("the page table runs past the end of the file")
    sizes = struct.unpack_from(f">{pages}I", data, 8)
    jar: list[Cookie] = []
    at = sizes_end
    for number, size in enumerate(sizes, 1):
        page = data[at:at + size]
        at += size
        if len(page) != size or page[:4] != SAFARI_PAGE:
            raise ValueError(f"page {number} is damaged")
        jar.extend(_safari_page(page, number))
    return jar


def _safari_page(page: bytes, number: int) -> Iterator[Cookie]:
    """The cookies of one page of a binarycookies file (see `parse_binarycookies`)."""
    index = 0
    try:
        (count,) = struct.unpack_from("<I", page, 4)
        offsets = struct.unpack_from(f"<{count}I", page, 8)
        for start in offsets:
            # Counted by hand, so the error below can name the cookie that broke.
            index += 1
            size, _, flags, _, domain, name, path, value = struct.unpack_from("<8I", page, start)
            expiry, _ = struct.unpack_from("<2d", page, start + 40)
            cookie = page[start:start + size]
            if len(cookie) != size:
                raise struct.error("short cookie")
            expires = int(expiry) + APPLE_EPOCH_OFFSET if expiry > 0 else -1
            yield _cookie(_safari_text(cookie, name), _safari_text(cookie, value),
                          _safari_text(cookie, domain), _safari_text(cookie, path) or "/",
                          expires, flags & SAFARI_SECURE, flags & SAFARI_HTTP_ONLY, "Lax")
    except (struct.error, IndexError):
        place = f"cookie {index} of page {number}" if index else f"page {number}"
        raise ValueError(f"{place} is damaged") from None


def _safari_text(cookie: bytes, offset: int) -> str:
    """The zero-ended text at `offset` in one cookie's bytes."""
    end = cookie.find(b"\0", offset)
    if offset >= len(cookie) or end < 0:
        raise IndexError(offset)
    return cookie[offset:end].decode("utf-8", "replace")


def _firefox_profile(profile: str | None) -> Path:
    """The folder of a Firefox profile: the one named, or the one Firefox opens first."""
    root = _firefox_root()
    refuse_profile(root)
    if profile is not None:
        named = root / profile
        return named if named.is_dir() else root / "Profiles" / profile
    paths = _firefox_paths(root)
    if not paths:
        raise FileNotFoundError(f"no Firefox profile in {root / 'profiles.ini'}")
    # An absolute Path (IsRelative=0) replaces the root when joined.
    return root / paths[0]


def _firefox_paths(root: Path) -> list[str]:
    """Every profile's `Path` in `root/profiles.ini`, the one Firefox opens first, without repeats.

    The order is: the profiles an `[Install…]` section names as default (what Firefox really
    opens since version 67), then a `[Profile…]` marked `Default=1` (older Firefox), then every
    other profile in file order.

    Args:
        root: Firefox's folder, which holds `profiles.ini`.

    Returns:
        Each profile's `Path` value, relative to `root` or absolute.

    Raises:
        FileNotFoundError: there is no `profiles.ini` in `root`.
    """
    ini = configparser.ConfigParser(interpolation=None)
    if not ini.read(root / "profiles.ini", encoding="utf-8"):
        raise FileNotFoundError(f"no Firefox profiles.ini in {root}")
    # The [Install…] section names the profile Firefox really opens; Default=1 is older.
    installs = [
        ini[name]["Default"] for name in ini.sections()
        if name.startswith("Install") and "Default" in ini[name]
    ]
    profiles = [
        ini[name] for name in ini.sections()
        if name.startswith("Profile") and "Path" in ini[name]
    ]
    marked = [section["Path"] for section in profiles if section.get("Default") == "1"]
    return list(dict.fromkeys(installs + marked + [section["Path"] for section in profiles]))


def _firefox_root() -> Path:
    """The folder that holds Firefox's `profiles.ini`, in the platform's per-user app folder."""
    if _PLATFORM == "darwin":
        return Path.home() / "Library" / "Application Support" / "Firefox"
    if _PLATFORM == "win32":
        appdata = os.environ.get("APPDATA") or (Path.home() / "AppData" / "Roaming")
        return Path(appdata) / "Mozilla" / "Firefox"
    return Path.home() / ".mozilla" / "firefox"


@contextmanager
def _open_copy(database: Path) -> Iterator[sqlite3.Connection]:
    """A connection to an in-memory backup of `database`, closed afterwards."""
    if not database.is_file():
        raise FileNotFoundError(f"no cookie database at {database}")
    uri = f"{database.resolve().as_uri()}?mode=ro"
    try:
        source = sqlite3.connect(uri, uri=True)
    except sqlite3.Error as error:
        raise LoginError(f"cannot open cookie database {database}: {error}") from error
    dest = sqlite3.connect(":memory:")
    try:
        try:
            source.backup(dest)
        except sqlite3.Error as error:
            raise LoginError(f"cannot read cookie database {database}: {error}") from error
        finally:
            source.close()
        yield dest
    finally:
        dest.close()


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
