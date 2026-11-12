"""Cookie jars: read them from a file or a browser, narrow and combine them, send or save them.

How it works: every entry point gives a `Jar`, an immutable collection of cookies, and every
change to one (`filter`, `merge`) gives a new `Jar`. A cookie is a dict in Playwright's shape,
with the keys `name`, `value`, `domain`, `path`, `expires` (Unix seconds, -1 for a session
cookie), `secure`, `httpOnly` and `sameSite`, so a jar moves between a file, an installed
browser, a live `shared.browser` and an HTTP header without conversion.

- **Files.** `load` reads both formats people export, telling them apart by content: a Netscape
  `cookies.txt` (seven tab-separated fields a line; curl and browser exports mark an HttpOnly
  cookie by prefixing its domain with `#HttpOnly_`), or a JSON list of cookie objects, as
  Playwright and the browser extensions write them. `Jar.save` writes a Netscape file, through
  `shared.atomic` with mode 0600, since a cookie file is a login.
- **Installed browsers.** `from_browser` copies the profile's cookie database (a running
  browser locks it) and reads it with `sqlite3`. The Chromium family (Chrome, Chromium, Edge,
  Brave) encrypts each value with AES-128-CBC under a key derived from a password the OS keeps:
  macOS's Keychain, or the Linux desktop keyring (or Chromium's fixed "peanuts" without one).
  The CBC decryption is private to this module, as Chromium's store is its one user, and needs
  `cryptography`. Since Chromium 130 (database version 24) each value starts with a SHA-256 of
  its host, which also proves the key was right. Windows' Chromium uses app-bound encryption
  that only the browser can undo, so there it is refused, pointing to `browser.open(cdp=…)`.
  Firefox stores its values in plain text in `cookies.sqlite`, and `profiles.ini` names its
  profiles.

Everything but the Chromium family's `from_browser` uses only the stdlib.

    from shared import cookies

    jar = cookies.from_browser("chrome").filter(domain="example.com")
    headers = {"Cookie": jar.header}                   # "sessionid=…; csrftoken=…"
    if "sessionid" in jar:
        jar.merge(cookies.load("refreshed.txt")).save("example-cookies.txt")

`.raw` is the jar's cookies as a new list of dicts, for code that wants the plain data, such as
Playwright's `BrowserContext.add_cookies`.

No error or log line here ever holds a cookie's value: messages name the file, the line, the
browser, or the cookie's name and domain, and a `Jar`'s `repr` shows only names. Every function
may be called from several threads at once, and a `Jar` never changes once made.
"""

from __future__ import annotations

import configparser
import hashlib
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterable, Iterator, Union

import shared
from shared import _atomic
from shared._log import log

__all__ = ["Error", "Jar", "UnsafeError", "from_browser", "load", "profiles"]

Cookie = dict[str, Any]
PathLike = Union[str, Path]

_PLATFORM = sys.platform

# The largest cookie file `load` reads: 16 MiB. A full browser export is a few hundred KB, so
# anything this size is a mistake or an attack, refused before it is parsed.
MAX_SIZE = 16 * 1024 * 1024

# Keychain name ("<name> Safe Storage"), macOS folder, Linux folder, Linux keyring application.
_CHROMIUM = {
    "chrome": ("Chrome", "Google/Chrome", "google-chrome", "chrome"),
    "chromium": ("Chromium", "Chromium", "chromium", "chromium"),
    "edge": ("Microsoft Edge", "Microsoft Edge", "microsoft-edge", "chromium"),
    "brave": ("Brave", "BraveSoftware/Brave-Browser", "BraveSoftware/Brave-Browser", "brave"),
}
_BROWSERS = (*_CHROMIUM, "firefox")
# Chromium's and Firefox's `samesite` column: 0 none, 1 lax, 2 strict; -1 (unspecified) reads as Lax.
_SAME_SITE = {0: "None", 1: "Lax", 2: "Strict"}
# The spellings JSON exports use: Playwright's, and the extensions' (Chrome's API says "no_restriction").
_JSON_SAME_SITE = {"none": "None", "no_restriction": "None", "lax": "Lax", "strict": "Strict"}
_HTTP_ONLY = "#HttpOnly_"
# Seconds from 1601-01-01 (Chromium's epoch, Windows' FILETIME) to 1970-01-01.
_WINDOWS_EPOCH_OFFSET = 11_644_473_600
# The cookie database version from which each value starts with SHA-256(host), 32 bytes.
_HOST_HASH_VERSION = 24
_HOST_HASH_SIZE = 32


# Still a ValueError, as `CookieError` was before the error tree: flnd's auth catches a broken
# cookie file or a value that won't decrypt as a ValueError.
class Error(shared.Error, ValueError):
    """A cookie file is broken, a cookie can't be written or read, or a browser's value can't be decrypted."""


class UnsafeError(Error, shared.UnsafeError):
    """A cookie file over 16 MB, refused before it is read."""


class Jar:
    """An immutable collection of cookies: filter and merge it, read it by name, send or save it.

        jar = cookies.load("cookies.txt").filter(domain="example.com")
        jar["sessionid"], "sessionid" in jar, jar.names, len(jar)
        for cookie in jar:
            print(cookie["name"], cookie["domain"])

    Args:
        cookies: the cookies, as dicts in Playwright's shape; each needs a `name` and a `value`,
            and is copied, so changing the dict afterwards doesn't change the jar. Empty by default.

    Raises:
        Error: a cookie isn't a dict, or has no `name` or `value`.

    Iterating gives a copy of each cookie, in the jar's order. A jar may hold two cookies of the
    same name for different domains or paths; `[name]` and `header` follow the jar's order then.
    """

    __slots__ = ("_cookies",)

    def __init__(self, cookies: Iterable[Cookie] = ()):
        kept = []
        for index, cookie in enumerate(cookies):
            if not isinstance(cookie, dict) or "name" not in cookie or "value" not in cookie:
                raise Error(f"cookie {index} needs a name and a value")
            kept.append(dict(cookie))
        self._cookies: tuple[Cookie, ...] = tuple(kept)

    def filter(self, *, domain: str | None = None, names: Iterable[str] | None = None) -> Jar:
        """A jar of only the cookies for `domain` and its subdomains, and of only those `names`.

            jar.filter(domain="example.com")                 # example.com, .example.com, www.example.com
            jar.filter(names=["sessionid", "csrftoken"])

        Args:
            domain: keep a cookie whose domain is this one or a subdomain of it, ignoring case and
                a leading dot; None keeps every domain.
            names: keep a cookie with one of these names; a single `str` is one name. None keeps
                every name.
        """
        wanted = None if names is None else ({names} if isinstance(names, str) else set(names))
        return Jar(cookie for cookie in self._cookies
                   if (domain is None or _belongs(str(cookie.get("domain", "")), domain))
                   and (wanted is None or cookie["name"] in wanted))

    def merge(self, other: Jar | Iterable[Cookie]) -> Jar:
        """This jar with `other`'s cookies added, where `other`'s cookie wins over the same one here.

            saved.merge(refreshed)          # refreshed: what the site set during a run

        "Newer" is `other`: the argument is the jar read later, such as the cookies a site rotated
        during a run. Expiry isn't used, since a site often rotates a token with the same or a
        shorter expiry, and a session cookie has none. Two cookies are the same one when their
        name, their domain (ignoring case and a leading dot) and their path (`/` when missing)
        match, as a browser keys them.

        Args:
            other: the newer cookies, as a `Jar` or any iterable of cookie dicts.

        Returns:
            A new jar: this jar's cookies in their order, each replaced in place by `other`'s same
            cookie, then `other`'s other cookies in its order.
        """
        newer = other if isinstance(other, Jar) else Jar(other)
        by_key = {_identity(cookie): cookie for cookie in newer._cookies}
        merged = [by_key.pop(_identity(cookie), cookie) for cookie in self._cookies]
        return Jar(merged + [cookie for cookie in newer._cookies if _identity(cookie) in by_key])

    @property
    def names(self) -> list[str]:
        """Every cookie's name, once each, in the jar's order."""
        return list(dict.fromkeys(str(cookie["name"]) for cookie in self._cookies))

    @property
    def header(self) -> str:
        """The value of a `Cookie:` request header for the whole jar, in the jar's order; "" when empty.

            headers = {"Cookie": jar.filter(domain="example.com").header}    # "sessionid=…; csrftoken=…"
        """
        return "; ".join(f"{cookie['name']}={cookie['value']}" for cookie in self._cookies)

    @property
    def raw(self) -> list[Cookie]:
        """The cookies as a new list of new dicts, which the caller may change freely."""
        return [dict(cookie) for cookie in self._cookies]

    def save(self, dest: PathLike) -> Path:
        """Write the jar as a Netscape `cookies.txt` that only its owner can read, and return its path.

            jar.save(".cookies/example.txt")

        Args:
            dest: the file to write; its folders are created when missing, and an existing file
                is replaced.

        Raises:
            Error: a cookie holds a tab (outside its value) or a line break, which a Netscape file
                can't hold; nothing is written then.
            OSError: the file can't be written.

        The file is replaced in one step with mode 0600, so a crash never leaves half a file.
        Netscape files have no `sameSite`, so `load` reads every cookie back as "Lax".
        """
        path = Path(dest)
        text = _netscape_text(self._cookies)
        path.parent.mkdir(parents=True, exist_ok=True)
        # Mode 0600: the file is a login. The atomic write means a reader never sees half a file.
        _atomic.write(path, text.encode("utf-8"), mode=0o600)
        return path

    def __getitem__(self, name: str) -> str:
        """The value of the first cookie named `name`; KeyError, naming only `name`, when there is none."""
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
        return f"Jar({', '.join(self.names)})"


def load(path: PathLike) -> Jar:
    """Read a Netscape `cookies.txt` or a JSON list of cookies, telling them apart by content.

        jar = cookies.load("all.txt").filter(domain="example.com")

    Args:
        path: the file. Text starting with `[` or `{` is JSON; anything else is Netscape.

    Raises:
        UnsafeError: the file is over 16 MB.
        Error: the file isn't UTF-8 text, or isn't a cookie file; the message names the file
            and, for Netscape, the line.
        OSError: the file is missing or unreadable.
    """
    path = Path(path)
    # The size is checked before reading and again by reading one byte past the limit, so a
    # file that grows in between is still refused without reading it all.
    if path.stat().st_size > MAX_SIZE:
        raise UnsafeError(f"{path}: a cookie file over {MAX_SIZE // (1024 * 1024)} MB is refused")
    with open(path, "rb") as file:
        data = file.read(MAX_SIZE + 1)
    if len(data) > MAX_SIZE:
        raise UnsafeError(f"{path}: a cookie file over {MAX_SIZE // (1024 * 1024)} MB is refused")
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as err:
        # Not str(err): it quotes the undecodable byte, which may be part of a value.
        raise Error(f"{path}: not UTF-8 text (at byte {err.start})") from None
    try:
        cookies = _parse_json(text) if text.lstrip()[:1] in ("[", "{") else _parse_netscape(text)
    except ValueError as err:
        raise Error(f"{path}: {err}") from None
    return Jar(cookies)


def from_browser(browser: str = "chrome", *, profile: str | None = None) -> Jar:
    """Read every cookie of an installed browser's profile.

        jar = cookies.from_browser("firefox").filter(domain="example.com")

    Args:
        browser: "chrome", "chromium", "edge", "brave" or "firefox".
        profile: the profile, as `profiles` lists it: a folder name such as "Profile 1" for the
            Chromium family, or a `Path` of `profiles.ini` for Firefox. None reads `Default` for
            the Chromium family, and the profile Firefox opens first.

    Raises:
        ValueError: `browser` isn't one of those.
        FileNotFoundError: there is no such profile, or it has no cookie database.
        RuntimeError: a Chromium-family browser on Windows (see below).
        Error: the OS refused the key (a denied Keychain prompt), or not one encrypted value can
            be decrypted (a wrong key); the message names a cookie, never its value.
        ImportError: a Chromium-family browser without `cryptography`.

    The database is copied first, since a running browser may lock it. Every row is read, so a
    profile's cookies come back whole and the caller filters them.

    A Chromium-family cookie whose value can't be decrypted (a newer scheme such as Chrome's
    app-bound "v20", or a damaged row) is skipped, and the count of skipped cookies is logged at
    debug level, without names or values. When every encrypted value fails, the key itself is
    wrong, and that raises `Error`.

    - macOS: the first read of a Chromium-family browser shows a Keychain prompt ("Chrome Safe
      Storage"). Allow it once.
    - Linux: the browser's key comes from the desktop keyring through `secret-tool`.
    - Windows: only Firefox. For the Chromium family, use `browser.open(cdp=…)`, then
      `b.cookies`: a running browser's cookies need no decryption.
    """
    if browser == "firefox":
        return Jar(_load_firefox(profile))
    if browser not in _CHROMIUM:
        raise ValueError(f"unknown browser {browser!r}: use one of {', '.join(_BROWSERS)}")
    if _PLATFORM == "win32":
        # Chrome 127 and later use app-bound encryption, which only Chrome itself can undo.
        raise RuntimeError(
            f"can't read {browser} cookies on Windows: use browser.open(cdp=…), then b.cookies"
        )
    return Jar(_load_chromium(browser, profile or "Default"))


def profiles(browser: str = "chrome") -> list[str]:
    """The installed browser's profiles, as `from_browser`'s `profile=` takes them, the default first.

        cookies.profiles("chrome")      # ["Default", "Profile 1"]

    Args:
        browser: "chrome", "chromium", "edge", "brave" or "firefox".

    Returns:
        For the Chromium family, each profile folder with a cookie database, `Default` first;
        for Firefox, each `Path` in `profiles.ini`, the one Firefox opens first. `[]` when the
        browser isn't installed or has no profile.

    Raises:
        ValueError: `browser` isn't one of those.
    """
    if browser == "firefox":
        try:
            return _firefox_paths(_firefox_root())
        except FileNotFoundError:
            return []
    if browser not in _CHROMIUM:
        raise ValueError(f"unknown browser {browser!r}: use one of {', '.join(_BROWSERS)}")
    root = _chromium_root(browser)
    if not root.is_dir():
        return []
    found = [folder.name for folder in root.iterdir()
             if (folder / "Network" / "Cookies").is_file() or (folder / "Cookies").is_file()]
    # The default first, as `from_browser` reads it without `profile=`.
    return sorted(found, key=lambda name: (name != "Default", name))


def _identity(cookie: Cookie) -> tuple[str, str, str]:
    """What makes two cookies the same one for `merge`: name, domain without case or dot, path."""
    return (str(cookie["name"]), str(cookie.get("domain", "")).lstrip(".").lower(), str(cookie.get("path") or "/"))


def _load_firefox(profile: str | None) -> list[Cookie]:
    with _open_copy(_firefox_profile(profile) / "cookies.sqlite") as connection:
        rows = connection.execute(
            "SELECT host, name, value, path, expiry, isSecure, isHttpOnly, sameSite FROM moz_cookies"
        ).fetchall()
    jar = []
    for host, name, value, path, expiry, secure, http_only, same_site in rows:
        # Newer Firefox versions store the expiry in milliseconds; seconds stay below 10**11 until 5138.
        expires = expiry // 1000 if expiry > 10**11 else expiry
        jar.append(_cookie(name, value, host, path, expires or -1, secure, http_only, _SAME_SITE.get(same_site, "Lax")))
    return jar


def _firefox_profile(profile: str | None) -> Path:
    root = _firefox_root()
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

    Raises `FileNotFoundError` when there is no `profiles.ini`.
    """
    ini = configparser.ConfigParser(interpolation=None)
    if not ini.read(root / "profiles.ini", encoding="utf-8"):
        raise FileNotFoundError(f"no Firefox profiles.ini in {root}")
    # The [Install…] section names the profile Firefox really opens; Default=1 is older.
    installs = [ini[name]["Default"] for name in ini.sections() if name.startswith("Install") and "Default" in ini[name]]
    profiles = [ini[name] for name in ini.sections() if name.startswith("Profile") and "Path" in ini[name]]
    marked = [section["Path"] for section in profiles if section.get("Default") == "1"]
    return list(dict.fromkeys(installs + marked + [section["Path"] for section in profiles]))


def _firefox_root() -> Path:
    # Each OS keeps Firefox's profiles.ini in its usual per-user application folder.
    if _PLATFORM == "darwin":
        return Path.home() / "Library" / "Application Support" / "Firefox"
    if _PLATFORM == "win32":
        return Path(os.environ.get("APPDATA") or Path.home() / "AppData" / "Roaming") / "Mozilla" / "Firefox"
    return Path.home() / ".mozilla" / "firefox"


@contextmanager
def _open_copy(database: Path) -> Iterator[sqlite3.Connection]:
    """A connection to a temporary copy of `database` and its write-ahead log, deleted afterwards."""
    if not database.is_file():
        raise FileNotFoundError(f"no cookie database at {database}")
    with tempfile.TemporaryDirectory() as folder:
        copy = Path(folder) / database.name
        shutil.copyfile(database, copy)
        # Firefox keeps recent writes in the write-ahead log until it checkpoints.
        wal = database.with_name(database.name + "-wal")
        if wal.is_file():
            shutil.copyfile(wal, copy.with_name(copy.name + "-wal"))
        connection = sqlite3.connect(copy)
        try:
            yield connection
        finally:
            connection.close()


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
    """Every cookie of a Chromium-family profile, each encrypted value decrypted."""
    folder = _chromium_root(browser) / profile
    # Chromium 96 moved the database into Network/; an older profile still has it at the top.
    database = folder / "Network" / "Cookies"
    if not database.is_file() and (folder / "Cookies").is_file():
        database = folder / "Cookies"
    with _open_copy(database) as connection:
        version_row = connection.execute("SELECT value FROM meta WHERE key = 'version'").fetchone()
        rows = connection.execute(
            "SELECT host_key, name, value, encrypted_value, path, expires_utc, is_secure, is_httponly, samesite"
            " FROM cookies"
        ).fetchall()
    has_host_hash = int(version_row[0]) >= _HOST_HASH_VERSION if version_row else False
    # The key is read only when a value needs it, so a profile of plain values shows no prompt.
    keys = _chromium_keys(browser) if any(row[3] for row in rows) else {}
    jar = []
    encrypted_count = 0
    failures: list[Error] = []
    for host, name, value, encrypted, path, expires_utc, secure, http_only, same_site in rows:
        if encrypted:
            encrypted_count += 1
            # A whole profile holds every site's cookies, and some use a scheme this reader can't
            # undo (Chrome's app-bound "v20" values, a damaged row), so one such cookie is skipped
            # rather than failing every login. Only when every encrypted value fails is the key wrong.
            try:
                value = _decrypt_chromium(bytes(encrypted), keys, host, has_host_hash, f"{browser} cookie {name} for {host}")
            except Error as error:
                failures.append(error)
                continue
        # Chromium counts microseconds from 1601; 0 marks a session cookie.
        expires = expires_utc // 1_000_000 - _WINDOWS_EPOCH_OFFSET if expires_utc else -1
        jar.append(_cookie(name, value, host, path, expires, secure, http_only, _SAME_SITE.get(same_site, "Lax")))
    if failures and len(failures) == encrypted_count:
        raise Error(f"can't decrypt any of {encrypted_count} {browser} cookies, so the key is wrong "
                    f"or the format unknown; the first: {failures[0]}")
    if failures:
        # The count only: a cookie's name or host says which sites a person uses.
        log.debug("skipped {count} {browser} cookies that can't be decrypted", count=len(failures), browser=browser)
    return jar


def _chromium_root(browser: str) -> Path:
    _, mac_folder, linux_folder, _ = _CHROMIUM[browser]
    if _PLATFORM == "darwin":
        return Path.home() / "Library" / "Application Support" / mac_folder
    return Path.home() / ".config" / linux_folder


def _chromium_keys(browser: str) -> dict[bytes, bytes]:
    """The AES keys by the 3-byte prefix a value starts with: `v10`, and on Linux `v11`."""
    keychain_name, _, _, application = _CHROMIUM[browser]
    if _PLATFORM == "darwin":
        # macOS: PBKDF2 over the Keychain's password, 1003 rounds, as Chromium's os_crypt_mac does.
        return {b"v10": _derive_key(_read_keychain_password(f"{keychain_name} Safe Storage"), 1003)}
    # Linux writes v10 with the fixed password, and v11 with the keyring's when there is one.
    keyring_password = _read_keyring_password(application)
    return {b"v10": _derive_key("peanuts", 1), b"v11": _derive_key(keyring_password or "peanuts", 1)}


def _read_keychain_password(service: str) -> str:
    result = subprocess.run(
        ["security", "find-generic-password", "-w", "-s", service], capture_output=True, text=True, check=False
    )
    if result.returncode != 0:
        raise Error(f"can't read the Keychain item {service!r}: {result.stderr.strip() or 'denied'}")
    return result.stdout.strip()


def _read_keyring_password(application: str) -> str | None:
    # No `secret-tool` (FileNotFoundError) or no entry both mean no keyring: Chromium then uses "peanuts".
    try:
        result = subprocess.run(
            ["secret-tool", "lookup", "application", application], capture_output=True, text=True, check=False
        )
    except FileNotFoundError:
        return None
    if result.returncode != 0:
        return None
    return result.stdout.strip() or None


def _derive_key(password: str, iterations: int) -> bytes:
    # Chromium's fixed salt and 16-byte (AES-128) key.
    return hashlib.pbkdf2_hmac("sha1", password.encode("utf-8"), b"saltysalt", iterations, 16)


def _decrypt_chromium(encrypted: bytes, keys: dict[bytes, bytes], host: str, has_host_hash: bool, what: str) -> str:
    """One Chromium value, decrypted; `what` names the cookie in an error, which never shows the value."""
    key = keys.get(encrypted[:3])
    if key is None:
        raise Error(f"can't decrypt {what}: unknown format {encrypted[:3]!r}")
    # A ValueError here is a bad padding, a bad host hash or bad UTF-8: all a wrong key or a damaged value.
    try:
        plain = _decrypt_aes_cbc(key, encrypted[3:])
        if has_host_hash:
            # A matching host hash also proves the key was right.
            if plain[:_HOST_HASH_SIZE] != hashlib.sha256(host.encode("utf-8")).digest():
                raise ValueError("host hash differs")
            plain = plain[_HOST_HASH_SIZE:]
        return plain.decode("utf-8")
    except ValueError:
        raise Error(f"can't decrypt {what}: wrong key or damaged value") from None


def _decrypt_aes_cbc(key: bytes, ciphertext: bytes) -> bytes:
    """AES-CBC with Chromium's fixed IV of 16 spaces, then PKCS#7 unpadding; private, for Chromium only."""
    try:
        from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
        from cryptography.hazmat.primitives.padding import PKCS7
    except ImportError:
        raise ImportError("shared.cookies needs cryptography: pip install 'flntools[browser]'") from None
    decryptor = Cipher(algorithms.AES(key), modes.CBC(b" " * 16)).decryptor()
    unpadder = PKCS7(128).unpadder()
    padded = decryptor.update(ciphertext) + decryptor.finalize()
    return unpadder.update(padded) + unpadder.finalize()


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
            expiry = int(expires)
        except ValueError:
            raise ValueError(f"line {number}: expiry {expires!r} is not a whole number") from None
        jar.append(_cookie(name, value, domain, path, expiry or -1, secure.upper() == "TRUE", http_only, "Lax"))
    return jar


def _parse_json(text: str) -> list[Cookie]:
    # json's own message gives only a position, never the text around it.
    entries = json.loads(text)
    if not isinstance(entries, list):
        raise ValueError("JSON cookies must be a list")
    return [_json_cookie(entry, index) for index, entry in enumerate(entries)]


def _json_cookie(entry: Any, index: int) -> Cookie:
    if not isinstance(entry, dict) or not all(key in entry for key in ("name", "value", "domain")):
        raise ValueError(f"cookie {index} needs a name, a value and a domain")
    # Playwright names it "expires"; browser extensions name it "expirationDate".
    expires = entry.get("expires", entry.get("expirationDate"))
    try:
        expiry = -1 if expires is None or entry.get("session") else int(float(expires))
    except (TypeError, ValueError, OverflowError):
        raise ValueError(f"cookie {index}: expiry {expires!r} is not a number") from None
    same_site = _JSON_SAME_SITE.get(str(entry.get("sameSite", "")).lower(), "Lax")
    return _cookie(str(entry["name"]), str(entry["value"]), str(entry["domain"]), str(entry.get("path") or "/"),
                   expiry if expiry > 0 else -1, entry.get("secure"), entry.get("httpOnly"), same_site)


def _netscape_text(jar: Iterable[Cookie]) -> str:
    """The jar as a Netscape file; an `Error` naming the cookie (not its value) when one can't be written."""
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
            str(max(int(cookie.get("expires", -1)), 0)),
            str(cookie["name"]),
            str(cookie["value"]),
        ]
        # A tab before the value or any line break would make the file read back differently.
        if any("\t" in field for field in fields[:-1]) or any(mark in "".join(fields) for mark in "\r\n"):
            raise Error(f"cookie {cookie['name']!r} for {domain} has a tab or line break that Netscape files can't hold")
        lines.append("\t".join(fields))
    return "\n".join(lines) + "\n"
