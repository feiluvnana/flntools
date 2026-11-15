"""Cookie jars: read them from a file, browser, or text, narrow, combine, send or save them.

A `Jar` is an immutable collection of cookies in Playwright's dictionary shape.
`load` reads Netscape or JSON cookie files, installed browsers, or raw cookie text;
`Jar.save` writes Netscape files and `Jar.filter` narrows them by domain.
Values are never printed, logged, or included in error messages.
"""

from __future__ import annotations

import base64
import configparser
import ctypes
import hashlib
import json
import os
import sqlite3
import subprocess
import sys
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterable, Iterator, Union

import shared
from shared import _fs as fs
from shared._ui import ui

__all__ = ["Error", "Jar", "MissingError", "UnsafeError", "load"]

Cookie = dict[str, Any]
PathLike = Union[str, Path]

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
_BROWSERS = (*_CHROMIUM, "firefox")
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


class Error(shared.Error):
    """A cookie file is broken, unreadable, or cannot be decrypted."""


class MissingError(Error, shared.MissingError):
    """Reading these cookies needs what only the browser has: Chromium's keys on Windows."""


class UnsafeError(Error, shared.UnsafeError):
    """A cookie file over 16 MB, refused before it is read."""


class Jar:
    """An immutable collection of cookies: filter and merge it, read it by name, send or save it.

        jar = cookies.load("cookies.txt").filter(domains="example.com")
        jar["sessionid"], "sessionid" in jar, len(jar)
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

    def filter(
        self, *, domains: str | Iterable[str] | None = None, names: Iterable[str] | None = None
    ) -> Jar:
        """A jar of only the cookies for `domains` and subdomains, and of only those `names`."""
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

    def merge(self, other: Jar | Iterable[Cookie]) -> Jar:
        """Return a new jar with `other`'s cookies added, overriding matching ones."""
        newer = other if isinstance(other, Jar) else Jar(other)
        by_key = {_identity(cookie): cookie for cookie in newer._cookies}
        merged = [by_key.pop(_identity(cookie), cookie) for cookie in self._cookies]
        return Jar(merged + [cookie for cookie in newer._cookies if _identity(cookie) in by_key])

    @property
    def header(self) -> str:
        """The value of a `Cookie:` request header for the whole jar, in the jar's order."""
        return "; ".join(f"{cookie['name']}={cookie['value']}" for cookie in self._cookies)

    @property
    def raw(self) -> list[Cookie]:
        """The cookies as a new list of new dicts, which the caller may change freely."""
        return [dict(cookie) for cookie in self._cookies]

    def save(self, dest: PathLike) -> Path:
        """Write the jar as a Netscape `cookies.txt` with owner-only access, returning path.

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
        with fs.write(path, replace=True) as part:
            part.write_bytes(text.encode("utf-8"))
            if os.name != "nt":
                os.chmod(part, 0o600)
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
    """Load cookies from a `cookies.txt` file, a browser name, or raw cookie text."""
    if isinstance(source, str):
        lower = source.lower()
        if lower in _BROWSERS:
            if lower == "firefox":
                return Jar(_load_firefox(profile))
            return Jar(_load_chromium(lower, profile or "Default"))
        if profile is not None:
            raise Error(f"unknown browser {source!r}: use one of {', '.join(_BROWSERS)}")
        if "\n" in source or "\r" in source or "\t" in source or source.lstrip()[:1] in ("[", "{"):
            return _load_text(source, "<text>")
    path = Path(source)
    if path.stat().st_size > MAX_SIZE:
        raise UnsafeError(f"{path}: a cookie file over {MAX_SIZE // (1024 * 1024)} MB is refused")
    with open(path, "rb") as file:
        data = file.read(MAX_SIZE + 1)
    if len(data) > MAX_SIZE:
        raise UnsafeError(f"{path}: a cookie file over {MAX_SIZE // (1024 * 1024)} MB is refused")
    try:
        text = data.decode("utf-8-sig")
    except UnicodeDecodeError as err:
        # Not str(err): it quotes the undecodable byte, which may be part of a value.
        raise Error(f"{path}: not UTF-8 text (at byte {err.start})") from None
    return _load_text(text, str(path))


def _load_text(text: str, label: str) -> Jar:
    """Parse raw Netscape or JSON cookie text into a `Jar`."""
    try:
        parsed = _parse_json(text) if text.lstrip()[:1] in ("[", "{") else _parse_netscape(text)
    except ValueError as err:
        raise Error(f"{label}: {err}") from None
    return Jar(parsed)


def _identity(cookie: Cookie) -> tuple[str, str, str]:
    """What makes two cookies the same one for `merge`: name, domain without case or dot, path."""
    domain = str(cookie.get("domain", "")).lstrip(".").lower()
    return (str(cookie["name"]), domain, str(cookie.get("path") or "/"))


def _load_firefox(profile: str | None) -> list[Cookie]:
    with _open_copy(_firefox_profile(profile) / "cookies.sqlite") as connection:
        try:
            rows = connection.execute(
                "SELECT host, name, value, path, expiry, isSecure, isHttpOnly, sameSite "
                "FROM moz_cookies"
            ).fetchall()
        except sqlite3.Error as error:
            raise Error(f"cannot read Firefox cookies: {error}") from error
    jar = []
    for host, name, value, path, expiry, secure, http_only, same_site in rows:
        # Firefox stores expiry in ms in newer versions; seconds stay below 10**11 until 5138.
        expires = expiry // 1000 if expiry > 10**11 else expiry
        same = _SAME_SITE.get(same_site, "Lax")
        jar.append(_cookie(name, value, host, path, expires or -1, secure, http_only, same))
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
    # Each OS keeps Firefox's profiles.ini in its usual per-user application folder.
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
        raise Error(f"cannot open cookie database {database}: {error}") from error
    dest = sqlite3.connect(":memory:")
    try:
        try:
            source.backup(dest)
        except sqlite3.Error as error:
            raise Error(f"cannot read cookie database {database}: {error}") from error
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
    """Every cookie of a Chromium-family profile, each encrypted value decrypted."""
    folder = _chromium_root(browser) / profile
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
            raise Error(f"cannot read {browser} cookies: {error}") from error
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
            # rather than failing. Only when every encrypted value fails is key wrong.
            try:
                what = f"{browser} cookie {name} for {host}"
                value = _decrypt_chromium(bytes(encrypted), keys, host, has_host_hash, what)
            except Error as error:
                failures.append(error)
                continue
        # Chromium counts microseconds from 1601; 0 marks a session cookie.
        expires = expires_utc // 1_000_000 - _WINDOWS_EPOCH_OFFSET if expires_utc else -1
        same = _SAME_SITE.get(same_site, "Lax")
        jar.append(_cookie(name, value, host, path, expires, secure, http_only, same))
    if failures and len(failures) == encrypted_count:
        raise Error(
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
    _, mac_folder, linux_folder, _, win_folder = _CHROMIUM[browser]
    if _PLATFORM == "darwin":
        return Path.home() / "Library" / "Application Support" / mac_folder
    if _PLATFORM == "win32":
        local = os.environ.get("LOCALAPPDATA") or (Path.home() / "AppData" / "Local")
        return Path(local) / win_folder
    return Path.home() / ".config" / linux_folder


def _chromium_keys(browser: str) -> dict[bytes, bytes]:
    """The AES keys by the 3-byte prefix a value starts with: `v10`, and `v11`."""
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
    _fields_ = [("cbData", ctypes.c_uint32), ("pbData", ctypes.POINTER(ctypes.c_ubyte))]


def _read_windows_key(browser: str) -> bytes:
    """Read and DPAPI-decrypt Chromium's AES-256-GCM master key from `Local State`."""
    state_path = _chromium_root(browser) / "Local State"
    if not state_path.is_file():
        raise MissingError(f"no Local State file for {browser} at {state_path}")
    try:
        state = json.loads(state_path.read_text(encoding="utf-8-sig"))
        raw = base64.b64decode(state["os_crypt"]["encrypted_key"])
    except (OSError, ValueError, KeyError, TypeError) as error:
        raise Error(f"cannot read {browser} Local State key: {error}") from error
    if not raw.startswith(b"DPAPI"):
        raise Error(f"unexpected {browser} Local State key format")
    return _dpapi_unprotect(raw[5:])


def _dpapi_unprotect(data: bytes) -> bytes:
    """Decrypt `data` using Windows DPAPI (`CryptUnprotectData`)."""
    windll = getattr(ctypes, "windll", None)
    if windll is None:
        raise MissingError("Windows DPAPI is only available on Windows")
    buf = (ctypes.c_ubyte * len(data)).from_buffer_copy(data)
    blob_in = _DataBlob(len(data), buf)
    blob_out = _DataBlob()
    ok = windll.crypt32.CryptUnprotectData(
        ctypes.byref(blob_in), None, None, None, None, 0, ctypes.byref(blob_out)
    )
    if not ok:
        raise Error("Windows DPAPI CryptUnprotectData failed")
    try:
        return ctypes.string_at(blob_out.pbData, blob_out.cbData)
    finally:
        windll.kernel32.LocalFree(blob_out.pbData)


def _read_keychain_password(service: str) -> str:
    result = subprocess.run(
        ["security", "find-generic-password", "-w", "-s", service],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        err = result.stderr.strip() or "denied"
        raise Error(f"can't read the Keychain item {service!r}: {err}")
    return result.stdout.strip()


def _read_keyring_password(application: str) -> str | None:
    # No `secret-tool` or no entry means no keyring: Chromium uses "peanuts".
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
    """One Chromium value, decrypted; errors name `what`, never the value."""
    key = keys.get(encrypted[:3])
    if key is None:
        raise Error(f"can't decrypt {what}: unknown format {encrypted[:3]!r}")
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
        raise Error(f"can't decrypt {what}: wrong key or damaged value") from None


def _decrypt_aes_gcm(key: bytes, ciphertext: bytes) -> bytes:
    """AES-256-GCM with 12-byte nonce followed by ciphertext and 16-byte tag."""
    try:
        from cryptography.exceptions import InvalidTag
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    except ImportError:
        raise ImportError(
            "shared.cookies needs cryptography: pip install 'flntools[browser]'"
        ) from None
    if len(ciphertext) < 12 + 16:
        raise ValueError("ciphertext too short")
    try:
        return AESGCM(key).decrypt(ciphertext[:12], ciphertext[12:], None)
    except InvalidTag:
        raise ValueError("authentication tag mismatch") from None


def _decrypt_aes_cbc(key: bytes, ciphertext: bytes) -> bytes:
    """AES-CBC with Chromium's fixed IV of 16 spaces, then PKCS#7 unpadding."""
    try:
        from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
        from cryptography.hazmat.primitives.padding import PKCS7
    except ImportError:
        raise ImportError(
            "shared.cookies needs cryptography: pip install 'flntools[browser]'"
        ) from None
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
    """The jar as a Netscape file; an Error names the cookie if one cannot be written."""
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
            raise Error(
                f"cookie {name!r} for {domain} has a tab or newline Netscape cannot hold"
            )
        lines.append("\t".join(fields))
    return "\n".join(lines) + "\n"
