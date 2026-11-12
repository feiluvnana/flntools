"""Cookies in Playwright's shape: read from a browser or a file, write a file, build a header.

A cookie is a dict with the keys `name`, `value`, `domain`, `path`, `expires` (Unix
seconds, -1 for a session cookie), `secure`, `httpOnly` and `sameSite`, and a list of
them is a jar. Everything but `load_from_browser` uses only the stdlib; that one needs
`cryptography` for the Chromium family.
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
from typing import Any, Iterator, Union

import shared
from shared import _atomic

__all__ = ["CookieError", "Error", "load_from_browser", "load_from_file", "profiles", "to_file", "to_header"]

Cookie = dict[str, Any]
PathLike = Union[str, Path]

_PLATFORM = sys.platform

# Keychain name ("<name> Safe Storage"), macOS folder, Linux folder, Linux keyring application.
_CHROMIUM = {
    "chrome": ("Chrome", "Google/Chrome", "google-chrome", "chrome"),
    "chromium": ("Chromium", "Chromium", "chromium", "chromium"),
    "edge": ("Microsoft Edge", "Microsoft Edge", "microsoft-edge", "chromium"),
    "brave": ("Brave", "BraveSoftware/Brave-Browser", "BraveSoftware/Brave-Browser", "brave"),
}
_BROWSERS = (*_CHROMIUM, "firefox")
_SAME_SITE = {0: "None", 1: "Lax", 2: "Strict"}
_JSON_SAME_SITE = {"none": "None", "no_restriction": "None", "lax": "Lax", "strict": "Strict"}
_HTTP_ONLY = "#HttpOnly_"
_WINDOWS_EPOCH_OFFSET = 11_644_473_600
_HOST_HASH_VERSION = 24
_HOST_HASH_SIZE = 32


class Error(shared.Error):
    """The base of every error `shared.cookies` raises."""


# Still a ValueError, as before the error tree: flnd's auth catches it as one.
class CookieError(Error, ValueError):
    """A cookie file is broken, or a browser's cookie value can't be decrypted."""


def load_from_browser(domain: str, *, browser: str = "chrome", profile: str | None = None) -> list[Cookie]:
    """Read the cookies for `domain` and its subdomains from an installed browser's profile.

        jar = cookies.load_from_browser("loix.cc", browser="firefox")

    `browser` is "chrome", "chromium", "edge", "brave" or "firefox". `profile` is the
    profile folder's name: `Default` for the Chromium family, and the default profile in
    `profiles.ini` for Firefox. A running browser may lock its database, so it is copied
    first. A value that can't be decrypted raises CookieError.

    - macOS: the first read shows a Keychain prompt ("Chrome Safe Storage"). Allow it once.
    - Linux: the browser's key comes from the desktop keyring.
    - Windows: only Firefox. For the Chromium family, use `browser.connect(cdp=…)`, then
      `b.cookies.all`: a running browser's cookies need no decryption.
    """
    if browser == "firefox":
        return _load_firefox(domain, profile)
    if browser not in _CHROMIUM:
        raise ValueError(f"unknown browser {browser!r}: use one of {', '.join(_BROWSERS)}")
    if _PLATFORM == "win32":
        # Chrome 127 and later use app-bound encryption, which only Chrome itself can undo.
        raise RuntimeError(
            f"can't read {browser} cookies on Windows: use browser.connect(cdp=…), then b.cookies.all"
        )
    return _load_chromium(domain, browser, profile or "Default")


def profiles(browser: str = "chrome") -> list[str]:
    """The installed browser's profiles, as `load_from_browser`'s `profile=` takes them, the default first.

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
    # The default first, as `load_from_browser` reads it without `profile=`.
    return sorted(found, key=lambda name: (name != "Default", name))


def load_from_file(path: PathLike, *, domain: str | None = None) -> list[Cookie]:
    """Read a Netscape `cookies.txt` or a JSON list of cookies, telling them apart by content.

        jar = cookies.load_from_file("all.txt", domain="instagram.com")

    With `domain`, only that domain and its subdomains are kept. A broken file raises
    CookieError naming it.
    """
    path = Path(path)
    try:
        text = path.read_text(encoding="utf-8")
        jar = _parse_json(text) if text.lstrip()[:1] in ("[", "{") else _parse_netscape(text)
    except ValueError as err:
        raise CookieError(f"{path}: {err}") from None
    if domain is None:
        return jar
    return [cookie for cookie in jar if _belongs(cookie["domain"], domain)]


def to_file(jar: list[Cookie], path: PathLike) -> None:
    """Write the jar as a Netscape `cookies.txt` that only its owner can read.

        cookies.to_file(jar, ".flnd/cookies/ins.txt")

    The file is replaced in one step, so a crash never leaves half a file. Netscape files
    have no `sameSite`, so `load_from_file` reads it back as "Lax".
    """
    path = Path(path)
    text = _netscape_text(jar)
    path.parent.mkdir(parents=True, exist_ok=True)
    # Mode 0600: the file is a login. The atomic write means a reader never sees half a file.
    _atomic.write(path, text.encode("utf-8"), mode=0o600)


def to_header(jar: list[Cookie]) -> str:
    """The value of a `Cookie:` request header for the jar, in the jar's order.

        headers = {"Cookie": cookies.to_header(jar)}    # "sessionid=…; csrftoken=…"
    """
    return "; ".join(f"{cookie['name']}={cookie['value']}" for cookie in jar)


def _load_firefox(domain: str, profile: str | None) -> list[Cookie]:
    with _open_copy(_firefox_profile(profile) / "cookies.sqlite") as connection:
        rows = connection.execute(
            "SELECT host, name, value, path, expiry, isSecure, isHttpOnly, sameSite"
            " FROM moz_cookies WHERE belongs(host, ?)",
            (domain,),
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
    if _PLATFORM == "darwin":
        return Path.home() / "Library" / "Application Support" / "Firefox"
    if _PLATFORM == "win32":
        return Path(os.environ.get("APPDATA") or Path.home() / "AppData" / "Roaming") / "Mozilla" / "Firefox"
    return Path.home() / ".mozilla" / "firefox"


@contextmanager
def _open_copy(database: Path) -> Iterator[sqlite3.Connection]:
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
            connection.create_function("belongs", 2, _belongs, deterministic=True)
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


def _load_chromium(domain: str, browser: str, profile: str) -> list[Cookie]:
    folder = _chromium_root(browser) / profile
    database = folder / "Network" / "Cookies"
    if not database.is_file() and (folder / "Cookies").is_file():
        database = folder / "Cookies"
    with _open_copy(database) as connection:
        version_row = connection.execute("SELECT value FROM meta WHERE key = 'version'").fetchone()
        rows = connection.execute(
            "SELECT host_key, name, value, encrypted_value, path, expires_utc, is_secure, is_httponly, samesite"
            " FROM cookies WHERE belongs(host_key, ?)",
            (domain,),
        ).fetchall()
    has_host_hash = int(version_row[0]) >= _HOST_HASH_VERSION if version_row else False
    keys = _chromium_keys(browser) if any(row[3] for row in rows) else {}
    jar = []
    for host, name, value, encrypted, path, expires_utc, secure, http_only, same_site in rows:
        if encrypted:
            value = _decrypt_chromium(bytes(encrypted), keys, host, has_host_hash, f"{browser} cookie {name} for {host}")
        # Chromium counts microseconds from 1601; 0 marks a session cookie.
        expires = expires_utc // 1_000_000 - _WINDOWS_EPOCH_OFFSET if expires_utc else -1
        jar.append(_cookie(name, value, host, path, expires, secure, http_only, _SAME_SITE.get(same_site, "Lax")))
    return jar


def _chromium_root(browser: str) -> Path:
    _, mac_folder, linux_folder, _ = _CHROMIUM[browser]
    if _PLATFORM == "darwin":
        return Path.home() / "Library" / "Application Support" / mac_folder
    return Path.home() / ".config" / linux_folder


def _chromium_keys(browser: str) -> dict[bytes, bytes]:
    keychain_name, _, _, application = _CHROMIUM[browser]
    if _PLATFORM == "darwin":
        return {b"v10": _derive_key(_read_keychain_password(f"{keychain_name} Safe Storage"), 1003)}
    # Linux writes v10 with the fixed password, and v11 with the keyring's when there is one.
    keyring_password = _read_keyring_password(application)
    return {b"v10": _derive_key("peanuts", 1), b"v11": _derive_key(keyring_password or "peanuts", 1)}


def _read_keychain_password(service: str) -> str:
    result = subprocess.run(
        ["security", "find-generic-password", "-w", "-s", service], capture_output=True, text=True, check=False
    )
    if result.returncode != 0:
        raise CookieError(f"can't read the Keychain item {service!r}: {result.stderr.strip() or 'denied'}")
    return result.stdout.strip()


def _read_keyring_password(application: str) -> str | None:
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
    return hashlib.pbkdf2_hmac("sha1", password.encode("utf-8"), b"saltysalt", iterations, 16)


def _decrypt_chromium(encrypted: bytes, keys: dict[bytes, bytes], host: str, has_host_hash: bool, what: str) -> str:
    key = keys.get(encrypted[:3])
    if key is None:
        raise CookieError(f"can't decrypt {what}: unknown format {encrypted[:3]!r}")
    try:
        plain = _decrypt_aes_cbc(key, encrypted[3:])
        if has_host_hash:
            # A matching host hash also proves the key was right.
            if plain[:_HOST_HASH_SIZE] != hashlib.sha256(host.encode("utf-8")).digest():
                raise ValueError("host hash differs")
            plain = plain[_HOST_HASH_SIZE:]
        return plain.decode("utf-8")
    except ValueError:
        raise CookieError(f"can't decrypt {what}: wrong key or damaged value") from None


def _decrypt_aes_cbc(key: bytes, ciphertext: bytes) -> bytes:
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
    jar = []
    for number, line in enumerate(text.splitlines(), start=1):
        # curl and browser exports mark HttpOnly cookies by prefixing the domain like a comment.
        http_only = line.startswith(_HTTP_ONLY)
        if http_only:
            line = line[len(_HTTP_ONLY):]
        if not line.strip() or line.startswith("#"):
            continue
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


def _netscape_text(jar: list[Cookie]) -> str:
    lines = ["# Netscape HTTP Cookie File"]
    for cookie in jar:
        domain = str(cookie["domain"])
        fields = [
            (_HTTP_ONLY if cookie.get("httpOnly") else "") + domain,
            "TRUE" if domain.startswith(".") else "FALSE",
            str(cookie.get("path") or "/"),
            "TRUE" if cookie.get("secure") else "FALSE",
            str(max(int(cookie.get("expires", -1)), 0)),
            str(cookie["name"]),
            str(cookie["value"]),
        ]
        # A tab before the value or any line break would make the file read back differently.
        if any("\t" in field for field in fields[:-1]) or any(mark in "".join(fields) for mark in "\r\n"):
            raise ValueError(f"cookie {cookie['name']!r} has a tab or line break that Netscape files can't hold")
        lines.append("\t".join(fields))
    return "\n".join(lines) + "\n"
