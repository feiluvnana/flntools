"""Each source's login: its cookie file in `.flnd/cookies/`, and its secrets sealed in `.flnd/secrets.db`.

A cookie source logs in by `from_browser` or `from_file`, then `store` checks that the jar
proves a login and writes `.flnd/cookies/<code>.txt`; `load` reads it back for the engine. A
source with its own login (tlg) keeps what it needs in `secrets`, a mapping sealed per value.
`logged_in` and `logout` see both. Asking the questions is `flnd login`'s job, not this module's.

Secrets are encrypted with AES-GCM, each bound to its source and name, so a value copied into
another slot no longer decrypts; the key is `.flnd/secret.key`. This protects against someone
who copies only `secrets.db`. It doesn't protect against someone who can read the whole `.flnd/`
folder, which is the same exposure as the cookie files beside it.

    jar = auth.from_browser(Instagram(), browser="chrome")
    message = auth.store(Instagram(), jar, home=Path(".flnd"), origin="Chrome")
    with auth.secrets(Path(".flnd"), "tlg") as secrets:
        secrets["session"] = "1BVts…"
"""

from __future__ import annotations

import base64
import os
import time
# Only `token_bytes`: `secrets` is this module's own function, the vault's entry point.
from secrets import token_bytes
from collections.abc import Iterator, MutableMapping
from pathlib import Path
from typing import TYPE_CHECKING

# A source's own login (tlg) raises it too, so it lives in base; `auth.LoginError` is the same class.
from flnd.sources.base import LoginError
from shared import cookies, crypto, kv

if TYPE_CHECKING:
    from flnd.sources.base import Source

# The browsers `from_browser` reads cookies from, by `shared.cookies` name, with the name a person knows.
BROWSERS = {"chrome": "Chrome", "firefox": "Firefox", "edge": "Edge", "brave": "Brave", "chromium": "Chromium"}
# A cookie file is `<code>.txt`; `<code>.json` is the older form, still read and removed.
COOKIE_SUFFIXES = (".txt", ".json")
DB_FILE = "secrets.db"
KEY_FILE = "secret.key"
TABLE = "secrets"
KEY_SIZE = 32
# How long a key file shorter than KEY_SIZE may be another run's key still being written, in
# seconds, before it counts as damaged; 32 bytes are written in microseconds, so 2 s is ample.
KEY_WAIT = 2.0
# How often a short key file is read again while waiting, in seconds.
KEY_POLL = 0.01


def from_browser(source: Source, *, browser: str, profile: str | None = None) -> cookies.Jar:
    """The source's cookies from an installed browser, for each of its domains.

        jar = auth.from_browser(Instagram(), browser="firefox")

    Args:
        source: the source whose `domains` are read.
        browser: a key of `BROWSERS`, such as "chrome".
        profile: the browser's profile folder, such as "Profile 1"; None reads the default one.

    Raises:
        LoginError: the browser's cookies can't be read (no such browser or profile, a locked
            Keychain, Windows' app-bound encryption); the message suggests a cookie file.
    """
    try:
        return _own(source, cookies.from_browser(browser, profile=profile))
    # Every way reading a browser fails: a missing file (OSError), the platform refusing
    # (RuntimeError), a value that won't decrypt (cookies.Error is a ValueError), or no `cryptography`.
    except (OSError, RuntimeError, ValueError, ImportError) as error:
        raise LoginError(f"{source.code}: can't read {BROWSERS.get(browser, browser)}'s cookies: {error}; "
                         "try a cookie file instead") from None


def from_file(source: Source, path: Path) -> cookies.Jar:
    """The source's cookies from a Netscape or JSON cookie file; a whole browser export works.

        jar = auth.from_file(Twitter(), Path("~/Downloads/cookies.txt").expanduser())

    Raises:
        LoginError: the file is missing, unreadable, over 16 MB, or not a cookie file.
    """
    try:
        return _own(source, cookies.load(path))
    # cookies.Error and its UnsafeError are ValueErrors; a missing or unreadable file is an OSError.
    except (OSError, ValueError) as error:
        raise LoginError(f"{source.code}: can't read {path}: {error}") from None


def store(source: Source, jar: cookies.Jar, *, home: Path, origin: str) -> str:
    """Check that the jar proves a login, save it as the source's cookie file, and say so.

        auth.store(Instagram(), jar, home=Path(".flnd"), origin="Chrome")   # "ins: logged in from Chrome (14 cookies)"

    Args:
        source: the source logging in; one of its `needs` cookies must be in the jar.
        jar: the cookies, from `from_browser` or `from_file`.
        home: flnd's folder, `.flnd`.
        origin: where the jar came from, for the messages: a browser's name or the file's.

    Raises:
        LoginError: none of the source's `needs` cookies is in the jar.
    """
    if source.needs and not any(name in jar for name in source.needs):
        raise LoginError(f"{source.code}: no {' or '.join(source.needs)} for {source.domains[0]} in {origin}; "
                         "log in there first")
    path = cookie_file(home, source.code)
    fix_domains(jar).save(path)
    # One cookie file per source, so an old JSON file can't shadow the new one.
    path.with_suffix(".json").unlink(missing_ok=True)
    return f"{source.code}: logged in from {origin} ({len(jar)} cookies)"


def load(home: Path, code: str) -> cookies.Jar:
    """The source's saved cookies, ready for a browser or a client, or an empty jar when it never logged in.

        jar = auth.load(Path(".flnd"), "ins")

    Args:
        home: flnd's folder, `.flnd`.
        code: the source's code, such as "ins".

    Returns:
        The cookies of `.flnd/cookies/<code>.txt`, or else of `<code>.json`, with `fix_domains` applied.
    """
    for path in _cookie_files(home, code):
        if path.is_file():
            return fix_domains(cookies.load(path))
    return cookies.Jar()


def logged_in(home: Path, code: str) -> bool:
    """Whether the source has a saved login: a cookie file, or any secret.

        auth.logged_in(Path(".flnd"), "tlg")    # True after `flnd login` for Telegram

    It creates neither `secrets.db` nor the key file.
    """
    if any(path.is_file() for path in _cookie_files(home, code)):
        return True
    if not (home / DB_FILE).is_file():
        return False
    with secrets(home, code) as saved:
        return len(saved) > 0


def logout(home: Path, code: str) -> bool:
    """Remove the source's cookie file and secrets, and return whether there was anything to remove.

        auth.logout(Path(".flnd"), "ins")       # False when ins never logged in
    """
    removed = logged_in(home, code)
    for path in _cookie_files(home, code):
        path.unlink(missing_ok=True)
    # Opening the database would create it, so a home that has none is left without one.
    if (home / DB_FILE).is_file():
        with secrets(home, code) as saved:
            saved.clear()
    return removed


def cookie_file(home: Path, code: str) -> Path:
    """Where a source's cookies are saved: `.flnd/cookies/<code>.txt`."""
    return home / "cookies" / f"{code}{COOKIE_SUFFIXES[0]}"


def _cookie_files(home: Path, code: str) -> list[Path]:
    return [home / "cookies" / f"{code}{suffix}" for suffix in COOKIE_SUFFIXES]


def fix_domains(jar: cookies.Jar) -> cookies.Jar:
    """flnd's legacy fix-up: every domain gets a leading dot, and every path defaults to `/`."""
    # Exported files often hold host-only domains; the dot lets every subdomain see the cookie, as flnd always did.
    return cookies.Jar({**cookie, "domain": "." + cookie.get("domain", "").lstrip("."), "path": cookie.get("path") or "/"}
                       for cookie in jar)


def _own(source: Source, jar: cookies.Jar) -> cookies.Jar:
    """The cookies of `jar` for the source's domains and their subdomains, domain by domain."""
    return cookies.Jar(cookie for domain in source.domains for cookie in jar.filter(domain=domain))


def secrets(home: Path, code: str) -> Vault:
    """The secrets of one source, as a mapping of `str` to `str`, for a `with` block.

        with auth.secrets(Path(".flnd"), "tlg") as saved:
            saved["session"] = "1BVts…"

    Args:
        home: flnd's folder, `.flnd`; `secrets.db` is created in it when missing.
        code: the source's code; another source's secrets aren't visible.
    """
    return Vault(home, code)


class Vault(MutableMapping[str, str]):
    """One source's secrets; see `secrets`. The key file is created on the first read or write of a value.

    Each value is stored as base64 of its AES-GCM sealing under the slot `<code>:<name>`, with
    `<code>\\0<name>` as the associated data: a value moved to another slot fails to decrypt.
    """

    def __init__(self, home: Path, code: str):
        self._home = home
        self._code = code
        self._db = kv.open(home / DB_FILE, table=TABLE)
        self._key: bytes | None = None

    def __getitem__(self, name: str) -> str:
        sealed = base64.b64decode(self._db[self._slot(name)])
        return crypto.decrypt(sealed, self._secret_key(), context=self._context(name)).decode("utf-8")

    def __setitem__(self, name: str, value: str) -> None:
        sealed = crypto.encrypt(value, self._secret_key(), context=self._context(name))
        self._db[self._slot(name)] = base64.b64encode(sealed).decode("ascii")

    def __delitem__(self, name: str) -> None:
        del self._db[self._slot(name)]

    def __iter__(self) -> Iterator[str]:
        prefix = self._slot("")
        return iter([key[len(prefix):] for key in self._db.keys(prefix=prefix)])

    def __len__(self) -> int:
        return len(self._db.keys(prefix=self._slot("")))

    def __enter__(self) -> Vault:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    def close(self) -> None:
        """Close the database; the same as leaving `with`."""
        self._db.close()

    def _slot(self, name: str) -> str:
        return f"{self._code}:{name}"

    def _context(self, name: str) -> bytes:
        return f"{self._code}\0{name}".encode("utf-8")

    def _secret_key(self) -> bytes:
        if self._key is None:
            self._key = key(self._home)
        return self._key


def key(home: Path) -> bytes:
    """The key in `home/secret.key`, created with mode 0600 when missing.

        secret = auth.key(Path(".flnd"))

    Args:
        home: flnd's folder, `.flnd`; it is created when missing.

    Returns:
        The 32-byte key.

    Raises:
        ValueError: the key file holds anything but 32 bytes, or exists but can't be read (a
            dangling symlink), and still does after `KEY_WAIT`.
        OSError: the folder or the key file can't be created or read.

    When two runs create the key at once, the first one to create the file wins and the other
    reads its key: both end with the same one. No temporary file is written.
    """
    path = home / KEY_FILE
    # A short key may be another run's that is still being written, so it is read again until
    # `KEY_WAIT` has passed; only then is it damaged (a run that died while writing it).
    deadline = time.monotonic() + KEY_WAIT
    while True:
        try:
            secret = path.read_bytes()
        except FileNotFoundError:
            created = _create_key(path)
            if created is not None:
                return created
            # Lost the race: the winner's file exists now, maybe still short, so read it again. A
            # path that exists but can't be read, such as a dangling symlink (O_EXCL refuses it,
            # read_bytes finds nothing), loses every time, so it too gives up at the deadline
            # instead of spinning forever.
            if time.monotonic() >= deadline:
                raise ValueError(f"{path} is damaged: it exists but can't be read, such as a broken link; "
                                 "delete it, then log in again")
            time.sleep(KEY_POLL)
            continue
        if len(secret) == KEY_SIZE:
            return secret
        # A run killed between creating the file and writing it leaves it empty for good, so the
        # message says how to get out: without the key, the secrets it sealed can't be read anyway.
        if len(secret) > KEY_SIZE or time.monotonic() >= deadline:
            raise ValueError(f"{path} is damaged: it should hold {KEY_SIZE} bytes, not {len(secret)}; "
                             "delete it, then log in again")
        time.sleep(KEY_POLL)


def _create_key(path: Path) -> bytes | None:
    """Create the key file with a new key, and return the key, or None when another run created it first."""
    path.parent.mkdir(parents=True, exist_ok=True)
    # O_EXCL makes "first creator wins" without a hard link: `os.link`, used before 2026-11, is
    # refused with a plain OSError on FAT, exFAT and some network drives, so logging in to tlg
    # failed there. O_BINARY (Windows only, 0 elsewhere) keeps the C runtime from turning a
    # 0x0A byte of the key into 0x0D 0x0A. Mode 0600: the key is as private as the cookie files.
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0)
    try:
        handle = os.open(path, flags, 0o600)
    except FileExistsError:
        return None
    # The winner writes into the published file, so a reader can see it empty or short for a
    # moment. That is safe because the key is only 32 bytes, written in one write of a fresh
    # file, and every reader (key() above) treats a short file as "not written yet" and reads
    # again until KEY_WAIT has passed, never using a short key. A key that fails to be written
    # is removed, so no run is left with a damaged file.
    # The stdlib's CSPRNG makes the key; `shared.crypto` leaves keys to it (redesign C2).
    secret = token_bytes(KEY_SIZE)
    try:
        with os.fdopen(handle, "wb") as file:
            file.write(secret)
    except BaseException:
        path.unlink(missing_ok=True)
        raise
    return secret
