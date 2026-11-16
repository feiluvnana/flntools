"""Cookie jars, a library: read cookies from a file, a browser or text, narrow and save them.

What it offers any caller:
- `Jar`, an immutable collection of cookies in Playwright's dictionary shape (`name`, `value`,
  `domain`, `path`, `expires`, `secure`, `httpOnly`, `sameSite`). It filters by domain and name,
  reads a value by name, builds a `Cookie:` header and saves itself as a Netscape `cookies.txt`.
- `load`, which reads a Netscape or JSON cookie file, raw cookie text, or the cookie database of
  an installed browser (`BROWSERS`: Chrome, Chromium, Edge, Brave, Firefox and, on macOS,
  Safari), decrypting Chromium's encrypted values with the operating system's key store.
- `parse_binarycookies`, a stdlib reader of Safari's `Cookies.binarycookies` format.
- `refuse_profile`, the guard that stops a test suite from reading a real browser profile.

Who calls it: in flnd, the login code (`flnd/site.py`, `flnd/_home.py`) loads a jar at
`flnd login` and saves it in the vault; the HTTP client (`flnd/_net.py`) and the browser
(`flnd/_browser.py`) send it. It calls only the stdlib, `cryptography` (lazily, for a Chromium
value), and the `_files` (atomic writes) and `_ui` (a debug line) libraries. Its errors are
`flnd.errors.LoginError` and `UsageError`, so a caller can show them as they are.

How `load` reads a browser:
1. find the profile folder for the platform (`_chromium_root`, `_firefox_profile`, or Safari's
   cookie file, `SAFARI_FILES`);
2. copy the cookie database into memory with SQLite's backup API, read-only, so a running
   browser's lock or write-ahead log never blocks or corrupts the read;
3. read every row; for Chromium, fetch the AES key from the Keychain (macOS), the keyring
   (Linux) or DPAPI (Windows), but only if some value is encrypted;
4. decrypt each value, strip Chromium's host hash, and skip a value that can't be decrypted,
   failing only when none can.

Rules a change must keep:
- A cookie's value is a login. It is never printed, logged, put in an error message or a
  `repr`; messages name the file, the line number, the browser or the cookie's name only.
- A `Jar` never changes after it is made: every method returns a new jar or a copy.
- A saved file is written atomically with mode 0600, so a crash never leaves half a login.
"""

from __future__ import annotations

import base64
import configparser
import ctypes
import hashlib
import json
import os
import sqlite3
import struct
import subprocess
import sys
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from flnd import _files
from flnd._ui import ui
from flnd.errors import LoginError, UsageError

__all__ = ["BROWSERS", "Jar", "load", "parse_binarycookies"]

Cookie = dict[str, Any]
PathLike = str | Path

_PLATFORM = sys.platform

# The largest cookie file `load` reads: 16 MiB. A full browser export is a few hundred KB, so
# anything this size is a mistake or an attack, refused before it is parsed.
MAX_SIZE = 16 * 1024 * 1024

# Keychain name, macOS folder, Linux folder, Linux keyring app, Windows User Data folder.
_CHROMIUM = {
    "chrome": ("Chrome", "Google/Chrome", "google-chrome", "chrome", "Google/Chrome/User Data"),
    "chromium": ("Chromium", "Chromium", "chromium", "chromium", "Chromium/User Data"),
    "edge": ("Microsoft Edge", "Microsoft Edge", "microsoft-edge", "chromium",
             "Microsoft/Edge/User Data"),
    "brave": ("Brave", "BraveSoftware/Brave-Browser", "BraveSoftware/Brave-Browser", "brave",
              "BraveSoftware/Brave-Browser/User Data"),
}
# Every browser `load` reads cookies from, by the name `flnd login <source> <browser>` takes.
# Safari is read on macOS only.
BROWSERS = (*_CHROMIUM, "firefox", "safari")
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
# Seconds from 1601-01-01 (Chromium's epoch, Windows' FILETIME) to 1970-01-01.
_WINDOWS_EPOCH_OFFSET = 11_644_473_600
# The cookie database version from which each value starts with SHA-256(host), 32 bytes.
_HOST_HASH_VERSION = 24
_HOST_HASH_SIZE = 32
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
    real_home = _real_home().resolve()
    if resolved.is_relative_to(real_home):
        raise RuntimeError(f"{NO_PROFILES} is set: a test may not read or copy the real browser "
                           f"profile {folder}; fake the home folder or the browser instead")


def _real_home() -> Path:
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


class Jar:
    """An immutable collection of cookies: filter it, read it by name, send or save it.

        jar = cookies.load("cookies.txt").filter(domains="example.com")
        jar["sessionid"], "sessionid" in jar, len(jar)
        for cookie in jar:
            print(cookie["name"], cookie["domain"])

    Args:
        cookies: the cookies, as dicts in Playwright's shape; each needs a `name` and a `value`,
            and is copied, so changing the dict afterwards doesn't change the jar. Empty by default.

    Raises:
        LoginError: a cookie isn't a dict, or has no `name` or `value`.

    Iterating gives a copy of each cookie, in the jar's order. A jar may hold two cookies of the
    same name for different domains or paths; `[name]` and `header` follow the jar's order then.
    """

    __slots__ = ("_cookies",)

    def __init__(self, cookies: Iterable[Cookie] = ()):
        kept = []
        for index, cookie in enumerate(cookies):
            if not isinstance(cookie, dict) or "name" not in cookie or "value" not in cookie:
                raise LoginError(f"cookie {index} needs a name and a value")
            kept.append(dict(cookie))
        self._cookies: tuple[Cookie, ...] = tuple(kept)

    def filter(
        self, *, domains: str | Iterable[str] | None = None, names: Iterable[str] | None = None
    ) -> Jar:
        """A jar of only the cookies for `domains` and subdomains, and of only those `names`.

        A cookie belongs to a domain when its own domain, without a leading dot and case, is
        that domain or ends with `.` plus it: `.www.example.com` belongs to `example.com`, but
        `notexample.com` doesn't. The order of the jar is kept.

        Args:
            domains: one domain or several; `None` keeps every domain.
            names: the cookie names to keep (a single string is one name); `None` keeps every
                name.

        Returns:
            A new jar; this one is unchanged.
        """
        if domains is None:
            wanted_domains: tuple[str, ...] | None = None
        elif isinstance(domains, str):
            wanted_domains = (domains,)
        else:
            wanted_domains = tuple(domains)
        if names is None:
            wanted: set[str] | None = None
        elif isinstance(names, str):
            wanted = {names}
        else:
            wanted = set(names)
        return Jar(
            cookie for cookie in self._cookies
            if (wanted_domains is None
                or any(_belongs(str(cookie.get("domain", "")), dom) for dom in wanted_domains))
            and (wanted is None or cookie["name"] in wanted)
        )

    @property
    def header(self) -> str:
        """The value of a `Cookie:` request header for the whole jar, in the jar's order."""
        return "; ".join(f"{cookie['name']}={cookie['value']}" for cookie in self._cookies)

    @property
    def list(self) -> list[Cookie]:
        """The cookies as a new list of new dicts, which the caller may change freely."""
        return [dict(cookie) for cookie in self._cookies]

    def save(self, dest: PathLike) -> Path:
        """Write the jar as a Netscape `cookies.txt` with owner-only access, returning path.

            jar.save(".cookies/example.txt")

        Args:
            dest: the file to write; its folders are created when missing, and an existing file
                is replaced.

        Returns:
            `dest`, as a `Path`.

        Raises:
            LoginError: a cookie holds a tab (outside its value) or a line break, which a
                Netscape file can't hold; nothing is written then.
            OSError: the file can't be written.

        The file is replaced in one step with mode 0600, so a crash never leaves half a file.
        Netscape files have no `sameSite`, so `load` reads every cookie back as "Lax".
        """
        path = Path(dest)
        text = _netscape_text(self._cookies)
        path.parent.mkdir(parents=True, exist_ok=True)
        # Mode 0600: the file is a login. The atomic write means a reader never sees half a file.
        with _files.part(path) as held:
            if os.name != "nt":
                os.chmod(held.fd, 0o600)
            held.write(text.encode("utf-8"))
        return path

    def __getitem__(self, name: str) -> str:
        """The value of the first cookie named `name`; KeyError when there is none."""
        for cookie in self._cookies:
            if cookie["name"] == name:
                return str(cookie["value"])
        raise KeyError(name)

    def __contains__(self, name: object) -> bool:
        return any(cookie["name"] == name for cookie in self._cookies)

    def __iter__(self) -> Iterator[Cookie]:
        return iter([dict(cookie) for cookie in self._cookies])

    def __len__(self) -> int:
        return len(self._cookies)

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, Jar):
            return NotImplemented
        return self._cookies == other._cookies

    def __repr__(self) -> str:
        # Names only: a repr ends up in tracebacks and logs, and a value is a login.
        names = list(dict.fromkeys(str(cookie["name"]) for cookie in self._cookies))
        return f"Jar({', '.join(names)})"


def load(source: PathLike | str, *, profile: str | None = None) -> Jar:
    """Load cookies from a `cookies.txt` file, a browser name, or raw cookie text.

    `source` is read as, in order: a browser name from `BROWSERS` (any case); raw text, when it
    holds a line break or a tab, or starts with `[` or `{` (JSON); else a file path. A file may
    be a Netscape `cookies.txt` (curl, browser extensions) or a JSON list in Playwright's or an
    extension's shape, and is refused past `MAX_SIZE` before it is parsed. Reading a Chromium
    browser may show the operating system's prompt for the Keychain or keyring.

    Args:
        source: a browser name, cookie text, or the path of a cookie file.
        profile: the browser profile's folder name (Chromium's `Default`, `Profile 1`, or a
            Firefox profile folder); `None` means Chromium's `Default`, or the profile Firefox
            opens first. Only for a browser name.

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
        if lower in BROWSERS:
            if lower == "firefox":
                return Jar(_load_firefox(profile))
            if lower == "safari":
                return Jar(_load_safari())
            return Jar(_load_chromium(lower, profile or "Default"))
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
    """Parse raw Netscape or JSON cookie text into a `Jar`.

    Text that starts with `[` or `{` (after spaces) is JSON; anything else is Netscape.

    Args:
        text: the whole cookie file's text.
        label: what the text came from (a path, or `<text>`), the start of an error message.

    Returns:
        The jar of the parsed cookies.

    Raises:
        LoginError: the text isn't a valid cookie file; the message names the line or the
            cookie's index, never a value.
    """
    try:
        parsed = _parse_json(text) if text.lstrip()[:1] in ("[", "{") else _parse_netscape(text)
    except ValueError as err:
        raise LoginError(f"{label}: {err}") from None
    return Jar(parsed)


def _load_firefox(profile: str | None) -> list[Cookie]:
    """Read cookies from a Firefox `cookies.sqlite` database.

    Firefox stores values in plain text, so no key is needed.

    Args:
        profile: the profile folder's name, or `None` for the one Firefox opens first.

    Returns:
        The cookies, in the database's row order.

    Raises:
        LoginError: the database can't be opened or read.
        FileNotFoundError: there is no Firefox profile, or it has no cookie database.
    """
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
    """Read Safari's cookies from its `Cookies.binarycookies` file, on macOS.

    1. Find the file: Safari's container first, then the older place (`SAFARI_FILES`).
    2. Read it whole; macOS refuses a program without Full Disk Access, which is a
       `LoginError` whose hint says how to allow it.
    3. Parse it (`parse_binarycookies`). Safari keeps values in plain text: no key is needed.

    Returns:
        The cookies, in the file's order.

    Raises:
        UsageError: not on macOS.
        LoginError: the file can't be read, or isn't a binarycookies file.
        FileNotFoundError: Safari has no cookie file.
    """
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
    """The cookies of one page of a binarycookies file (see `parse_binarycookies`).

    Args:
        page: the page's bytes.
        number: its number from 1, for messages.

    Yields:
        Each cookie.

    Raises:
        ValueError: the page or one of its cookies runs past its end.
    """
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
    """The zero-ended text at `offset` in one cookie's bytes.

    Args:
        cookie: the cookie's bytes, from its size field on.
        offset: where the text starts, from the cookie's start.

    Returns:
        The text; a byte that isn't UTF-8 becomes U+FFFD.

    Raises:
        IndexError: the offset is past the cookie, or the text has no end.
    """
    end = cookie.find(b"\0", offset)
    if offset >= len(cookie) or end < 0:
        raise IndexError(offset)
    return cookie[offset:end].decode("utf-8", "replace")


def _firefox_profile(profile: str | None) -> Path:
    """The folder of a Firefox profile: the one named, or the one Firefox opens first.

    A named profile is looked for at the top of Firefox's folder (Linux and Windows keep them
    there) and then under `Profiles/` (macOS).

    Args:
        profile: the profile folder's name, or `None` for the default one.

    Returns:
        The profile's folder; for a named profile it may not exist.

    Raises:
        FileNotFoundError: there is no `profiles.ini`, or it lists no profile.
        RuntimeError: the test suite forbids reading the real profile (`refuse_profile`).
    """
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
    """The folder that holds Firefox's `profiles.ini`, in the platform's per-user app folder.

    Returns:
        `~/Library/Application Support/Firefox` on macOS, `%APPDATA%/Mozilla/Firefox` on
        Windows, `~/.mozilla/firefox` elsewhere. It may not exist.
    """
    if _PLATFORM == "darwin":
        return Path.home() / "Library" / "Application Support" / "Firefox"
    if _PLATFORM == "win32":
        appdata = os.environ.get("APPDATA") or (Path.home() / "AppData" / "Roaming")
        return Path(appdata) / "Mozilla" / "Firefox"
    return Path.home() / ".mozilla" / "firefox"


@contextmanager
def _open_copy(database: Path) -> Iterator[sqlite3.Connection]:
    """A connection to an in-memory backup of `database`, closed afterwards.

    A running browser holds its cookie database open, and may be writing to it. Opening the
    file read-only (`mode=ro`) and copying it with SQLite's backup API reads a consistent
    snapshot, write-ahead log included, without taking the browser's lock or changing the file.

    Args:
        database: the browser's SQLite cookie file.

    Yields:
        A connection to the in-memory copy; it is closed when the `with` block ends.

    Raises:
        FileNotFoundError: `database` isn't a file.
        LoginError: SQLite can't open or copy it (locked, damaged, not a database).
    """
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


def _belongs(cookie_domain: str, domain: str) -> bool:
    host = cookie_domain.lstrip(".").lower()
    wanted = domain.lstrip(".").lower()
    return host == wanted or host.endswith("." + wanted)


def _cookie(name: str, value: str, domain: str, path: str, expires: int,
            secure: Any, http_only: Any, same_site: str) -> Cookie:
    return {
        "name": name, "value": value, "domain": domain, "path": path, "expires": expires,
        "secure": bool(secure), "httpOnly": bool(http_only), "sameSite": same_site,
    }


def _load_chromium(browser: str, profile: str) -> list[Cookie]:
    """Every cookie of a Chromium-family profile, each encrypted value decrypted.

    The steps:
    1. find the cookie database (`Network/Cookies`, or `Cookies` in an old profile);
    2. copy it into memory and read its rows and its schema version;
    3. read the key, only when some value is encrypted, since reading it may prompt;
    4. decrypt each value; one that fails is skipped, so a single cookie in a scheme this
       reader can't undo doesn't lose every other site's login.

    Args:
        browser: a Chromium-family name from `_CHROMIUM`.
        profile: the profile's folder name, such as `Default` or `Profile 1`.

    Returns:
        The cookies that could be read, in the database's row order.

    Raises:
        LoginError: the database can't be read, the key can't be read, or not one encrypted
            value could be decrypted (a wrong key, most likely).
        FileNotFoundError: the profile has no cookie database.
        RuntimeError: the test suite forbids reading the real profile (`refuse_profile`).
    """
    folder = _chromium_root(browser) / profile
    refuse_profile(folder)
    # Chromium 96 moved the database into Network/; an older profile still has it at the top.
    database = folder / "Network" / "Cookies"
    if not database.is_file() and (folder / "Cookies").is_file():
        database = folder / "Cookies"
    with _open_copy(database) as connection:
        try:
            version_row = connection.execute(
                "SELECT value FROM meta WHERE key = 'version'"
            ).fetchone()
            rows = connection.execute(
                "SELECT host_key, name, value, encrypted_value, path, expires_utc, is_secure, "
                "is_httponly, samesite FROM cookies"
            ).fetchall()
        except sqlite3.Error as error:
            raise LoginError(f"cannot read {browser} cookies: {error}") from error
    has_host_hash = int(version_row[0]) >= _HOST_HASH_VERSION if version_row else False
    # The key is read only when a value needs it, so a profile of plain values shows no prompt.
    keys = _chromium_keys(browser) if any(row[3] for row in rows) else {}
    jar = []
    encrypted_count = 0
    failures: list[LoginError] = []
    for host, name, value, encrypted, path, expires_utc, secure, http_only, same_site in rows:
        if encrypted:
            encrypted_count += 1
            # A whole profile holds every site's cookies, and some use a scheme this reader can't
            # undo (Chrome's app-bound "v20" values, a damaged row), so one such cookie is skipped
            # rather than failing. Only when every encrypted value fails is key wrong.
            try:
                what = f"{browser} cookie {name} for {host}"
                value = _decrypt_chromium(bytes(encrypted), keys, host, has_host_hash, what)
            except LoginError as error:
                failures.append(error)
                continue
        # Chromium counts microseconds from 1601; 0 marks a session cookie.
        expires = expires_utc // 1_000_000 - _WINDOWS_EPOCH_OFFSET if expires_utc else -1
        same = _SAME_SITE.get(same_site, "Lax")
        jar.append(_cookie(name, value, host, path, expires, secure, http_only, same))
    if failures and len(failures) == encrypted_count:
        raise LoginError(
            f"can't decrypt any of {encrypted_count} {browser} cookies: wrong key or unknown "
            f"format; first: {failures[0]}"
        )
    if failures:
        # The count only: a cookie's name or host says which sites a person uses.
        ui.debug(
            "skipped {count} {browser} cookies that can't be decrypted",
            count=len(failures),
            browser=browser,
        )
    return jar


def _chromium_root(browser: str) -> Path:
    """The `User Data` folder of a Chromium-family browser, which holds its profiles.

    Args:
        browser: a Chromium-family name from `_CHROMIUM`.

    Returns:
        The folder in the platform's per-user application folder; it may not exist.
    """
    _, mac_folder, linux_folder, _, win_folder = _CHROMIUM[browser]
    if _PLATFORM == "darwin":
        return Path.home() / "Library" / "Application Support" / mac_folder
    if _PLATFORM == "win32":
        local = os.environ.get("LOCALAPPDATA") or (Path.home() / "AppData" / "Local")
        return Path(local) / win_folder
    return Path.home() / ".config" / linux_folder


def _chromium_keys(browser: str) -> dict[bytes, bytes]:
    """The AES keys by the 3-byte prefix a value starts with: `v10`, and `v11`.

    Each platform keeps Chromium's secret differently: a Keychain item on macOS, a keyring
    entry (or the fixed password "peanuts") on Linux, and a DPAPI-protected key in `Local State`
    on Windows. macOS may show a Keychain prompt here.

    Args:
        browser: a Chromium-family name from `_CHROMIUM`.

    Returns:
        `{prefix: key}`: 16-byte AES-128 keys on macOS and Linux, a 32-byte AES-256 key on
        Windows.
    """
    keychain_name, _, _, application, _ = _CHROMIUM[browser]
    if _PLATFORM == "darwin":
        # macOS: PBKDF2 over the Keychain's password, 1003 rounds, as Chromium's os_crypt_mac does.
        return {b"v10": _derive_key(_read_keychain_password(f"{keychain_name} Safe Storage"), 1003)}
    if _PLATFORM == "win32":
        key = _read_windows_key(browser)
        return {b"v10": key, b"v11": key}
    # Linux writes v10 with the fixed password, and v11 with the keyring's when there is one.
    keyring_password = _read_keyring_password(application)
    v11_pw = keyring_password or "peanuts"
    return {b"v10": _derive_key("peanuts", 1), b"v11": _derive_key(v11_pw, 1)}


class _DataBlob(ctypes.Structure):
    """Windows' `DATA_BLOB`: a byte count and a pointer, what `CryptUnprotectData` takes."""

    _fields_ = [("cbData", ctypes.c_uint32), ("pbData", ctypes.POINTER(ctypes.c_ubyte))]


def _read_windows_key(browser: str) -> bytes:
    """Read and DPAPI-decrypt Chromium's AES-256-GCM master key from `Local State`.

    `Local State` is JSON; `os_crypt.encrypted_key` is base64 of `DPAPI` plus the key that
    Windows protected for the signed-in account.

    Args:
        browser: a Chromium-family name from `_CHROMIUM`.

    Returns:
        The 32-byte key.

    Raises:
        LoginError: `Local State` is missing, unreadable, or holds a key in another format, or
            DPAPI refuses to decrypt it.
    """
    state_path = _chromium_root(browser) / "Local State"
    if not state_path.is_file():
        raise LoginError(f"no Local State file for {browser} at {state_path}")
    try:
        state = json.loads(state_path.read_text(encoding="utf-8-sig"))
        raw = base64.b64decode(state["os_crypt"]["encrypted_key"])
    except (OSError, ValueError, KeyError, TypeError) as error:
        raise LoginError(f"cannot read {browser} Local State key: {error}") from error
    if not raw.startswith(b"DPAPI"):
        raise LoginError(f"unexpected {browser} Local State key format")
    return _dpapi_unprotect(raw[5:])


def _dpapi_unprotect(data: bytes) -> bytes:
    """Decrypt `data` using Windows DPAPI (`CryptUnprotectData`).

    DPAPI decrypts only what the same Windows account protected, so this works for the
    signed-in user's own browser. The output buffer is Windows-allocated and freed with
    `LocalFree` even if copying it fails.

    Args:
        data: the protected bytes, without Chromium's `DPAPI` prefix.

    Returns:
        The plain bytes.

    Raises:
        LoginError: not on Windows, or Windows refuses to decrypt `data`.
    """
    windll = getattr(ctypes, "windll", None)
    if windll is None:
        raise LoginError("Windows DPAPI is only available on Windows")
    buf = (ctypes.c_ubyte * len(data)).from_buffer_copy(data)
    blob_in = _DataBlob(len(data), buf)
    blob_out = _DataBlob()
    ok = windll.crypt32.CryptUnprotectData(
        ctypes.byref(blob_in), None, None, None, None, 0, ctypes.byref(blob_out)
    )
    if not ok:
        raise LoginError("Windows DPAPI CryptUnprotectData failed")
    try:
        return ctypes.string_at(blob_out.pbData, blob_out.cbData)
    finally:
        windll.kernel32.LocalFree(blob_out.pbData)


def _read_keychain_password(service: str) -> str:
    """The password of a macOS Keychain item, read with the `security` tool.

    macOS may ask the person to allow the read; a refusal is an error, not an empty password.

    Args:
        service: the item's service name, such as `Chrome Safe Storage`.

    Returns:
        The password, without its trailing newline.

    Raises:
        LoginError: `security` failed: no such item, or the person denied access.
    """
    result = subprocess.run(
        ["security", "find-generic-password", "-w", "-s", service],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        err = result.stderr.strip() or "denied"
        raise LoginError(f"can't read the Keychain item {service!r}: {err}")
    return result.stdout.strip()


def _read_keyring_password(application: str) -> str | None:
    """Chromium's password from the Linux Secret Service keyring, read with `secret-tool`.

    No `secret-tool`, or no entry, means the browser runs without a keyring, and Chromium then
    encrypts with the fixed password "peanuts"; the caller falls back to it.

    Args:
        application: the keyring's `application` attribute, such as `chrome`.

    Returns:
        The password, or `None` when there is none.
    """
    try:
        result = subprocess.run(
            ["secret-tool", "lookup", "application", application],
            capture_output=True,
            text=True,
            check=False,
        )
    except FileNotFoundError:
        return None
    if result.returncode != 0:
        return None
    return result.stdout.strip() or None


def _derive_key(password: str, iterations: int) -> bytes:
    # Chromium's fixed salt and 16-byte (AES-128) key.
    return hashlib.pbkdf2_hmac("sha1", password.encode("utf-8"), b"saltysalt", iterations, 16)


def _decrypt_chromium(
    encrypted: bytes, keys: dict[bytes, bytes], host: str, has_host_hash: bool, what: str
) -> str:
    """One Chromium value, decrypted; errors name `what`, never the value.

    A value is a 3-byte version prefix (`v10`, `v11`) and the ciphertext: AES-256-GCM on
    Windows, AES-128-CBC elsewhere. From database version 24 the plain text starts with
    SHA-256 of the cookie's host, which is checked and removed.

    Args:
        encrypted: the `encrypted_value` column.
        keys: the keys by prefix, from `_chromium_keys`.
        host: the cookie's `host_key`, for the host hash.
        has_host_hash: whether the database's version adds the host hash.
        what: how errors name the cookie, such as `chrome cookie sid for .example.com`.

    Returns:
        The value as text.

    Raises:
        LoginError: an unknown prefix, a wrong key, a damaged value, or a host hash that
            differs.
    """
    key = keys.get(encrypted[:3])
    if key is None:
        raise LoginError(f"can't decrypt {what}: unknown format {encrypted[:3]!r}")
    # ValueError: bad padding, bad host hash, or bad UTF-8 (wrong key or damaged value).
    try:
        plain = (
            _decrypt_aes_gcm(key, encrypted[3:]) if _PLATFORM == "win32"
            else _decrypt_aes_cbc(key, encrypted[3:])
        )
        if has_host_hash:
            # A matching host hash also proves the key was right.
            if plain[:_HOST_HASH_SIZE] != hashlib.sha256(host.encode("utf-8")).digest():
                raise ValueError("host hash differs")
            plain = plain[_HOST_HASH_SIZE:]
        return plain.decode("utf-8")
    except ValueError:
        raise LoginError(f"can't decrypt {what}: wrong key or damaged value") from None


def _decrypt_aes_gcm(key: bytes, ciphertext: bytes) -> bytes:
    """AES-256-GCM with 12-byte nonce followed by ciphertext and 16-byte tag.

    Args:
        key: the 32-byte key.
        ciphertext: the nonce, the ciphertext and the tag, in that order.

    Returns:
        The plain bytes.

    Raises:
        ValueError: the input is too short or the tag doesn't match (wrong key or damage).
        ImportError: the `cryptography` package isn't installed.
    """
    try:
        from cryptography.exceptions import InvalidTag
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    except ImportError:
        raise ImportError(
            "flnd._cookies needs cryptography: pip install cryptography"
        ) from None
    if len(ciphertext) < 12 + 16:
        raise ValueError("ciphertext too short")
    try:
        return AESGCM(key).decrypt(ciphertext[:12], ciphertext[12:], None)
    except InvalidTag:
        raise ValueError("authentication tag mismatch") from None


def _decrypt_aes_cbc(key: bytes, ciphertext: bytes) -> bytes:
    """AES-CBC with Chromium's fixed IV of 16 spaces, then PKCS#7 unpadding.

    Args:
        key: the 16-byte key from `_derive_key`.
        ciphertext: the value without its version prefix.

    Returns:
        The plain bytes.

    Raises:
        ValueError: the padding is wrong, which almost always means a wrong key.
        ImportError: the `cryptography` package isn't installed.
    """
    try:
        from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
        from cryptography.hazmat.primitives.padding import PKCS7
    except ImportError:
        raise ImportError(
            "flnd._cookies needs cryptography: pip install cryptography"
        ) from None
    decryptor = Cipher(algorithms.AES(key), modes.CBC(b" " * 16)).decryptor()
    unpadder = PKCS7(128).unpadder()
    padded = decryptor.update(ciphertext) + decryptor.finalize()
    return unpadder.update(padded) + unpadder.finalize()


def _parse_netscape(text: str) -> list[Cookie]:
    """The cookies of a Netscape file; a ValueError names the line, never its value.

    Each line is seven tab-separated fields: domain, include-subdomains flag, path, secure,
    expiry (Unix seconds, 0 for a session cookie), name, value. Blank lines and `#` comments
    are skipped, except the `#HttpOnly_` prefix, which marks an HttpOnly cookie. Netscape files
    have no `sameSite`, so every cookie reads as "Lax".

    Args:
        text: the file's text.

    Returns:
        The cookies, in file order.

    Raises:
        ValueError: a line hasn't seven fields, or its expiry isn't a whole number.
    """
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
    """One cookie of a JSON export, in Playwright's or a browser extension's shape.

    Args:
        entry: one item of the JSON list.
        index: its place in the list, which an error names instead of the value.

    Returns:
        The cookie in this module's shape; a session cookie, or one without an expiry, gets -1.

    Raises:
        ValueError: the entry isn't an object with a name, a value and a domain, or its expiry
            isn't a number.
    """
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


def _netscape_text(jar: Iterable[Cookie]) -> str:
    """The jar as a Netscape file; a LoginError names the cookie if one cannot be written.

    Args:
        jar: the cookies to write, in order.

    Returns:
        The file's text: a header line, then one line per cookie, ending in a newline.

    Raises:
        LoginError: a field holds a tab (the value may) or any field a line break, which would
            make the file read back as different cookies.
    """
    lines = ["# Netscape HTTP Cookie File"]
    for cookie in jar:
        domain = str(cookie.get("domain", ""))
        fields = [
            (_HTTP_ONLY if cookie.get("httpOnly") else "") + domain,
            # The "include subdomains" flag: a leading dot means the cookie is a domain cookie.
            "TRUE" if domain.startswith(".") else "FALSE",
            str(cookie.get("path") or "/"),
            "TRUE" if cookie.get("secure") else "FALSE",
            # Netscape files write a session cookie's expiry as 0.
            str(max(int(-1 if (exp := cookie.get("expires")) is None else exp), 0)),
            str(cookie["name"]),
            str(cookie["value"]),
        ]
        # A tab before the value or any line break would make the file read back differently.
        if any("\t" in field for field in fields[:-1]) or any(m in "".join(fields) for m in "\r\n"):
            name = cookie["name"]
            raise LoginError(
                f"cookie {name!r} for {domain} has a tab or newline Netscape cannot hold"
            )
        lines.append("\t".join(fields))
    return "\n".join(lines) + "\n"
