"""Each source's login, sealed in vault `.flnd/metadata/secrets.db`.

A cookie source logs in by `from_browser` or `from_file`, then `store` verifies the jar
and seals it in the vault as the source's `cookies` value; `load` reads it back.
Sources with custom logins keep secrets beside it (`session`, `api_id`, `api_hash`).

Secrets are encrypted with AES-GCM bound to `<code>:<name>`; key is `secret.key`.
Legacy plain cookie files are migrated into the vault on first access.

    jar = auth.from_browser(Instagram(), browser="chrome")
    message = auth.store(Instagram(), jar, home=Path(".flnd"), origin="Chrome")
    with auth.secrets(Path(".flnd"), "tlg") as secrets:
        secrets["session"] = "..."
"""

from __future__ import annotations

import base64
import json
import os
import time
# Only `token_bytes`: `secrets` is this module's own function, the vault's entry point.
from secrets import token_bytes
from collections.abc import Iterable, Iterator, MutableMapping
from http.cookiejar import Cookie
from pathlib import Path
from typing import TYPE_CHECKING

# A source's own login (tlg) raises it too; `auth.LoginError` is the same class.
from flnd.common import paths
from flnd.source import LoginError
import shared
from shared import cookies, crypto, text, ui

if TYPE_CHECKING:
    from flnd.source import Source

# Browsers `from_browser` reads cookies from, by `shared.cookies` name with display name.
BROWSERS = {
    "chrome": "Chrome", "firefox": "Firefox", "edge": "Edge",
    "brave": "Brave", "chromium": "Chromium",
}
# The folder of the plain cookie files of earlier versions, moved into the vault on first use.
COOKIE_FOLDER = "cookies"
# A plain cookie file was `<code>.txt`, or `<code>.json`, the older form; the first one found wins.
COOKIE_SUFFIXES = (".txt", ".json")
# The vault value that holds a cookie source's jar, as JSON: a list of cookie dicts.
COOKIES = "cookies"
# The vault value that holds how a source logged in (e.g. "Chrome", "cookies.txt", "qr").
ORIGIN = "origin"
# The secret that proves a login for a source with its own (tlg): the session its `login` stores.
# A cookie source's jar (`COOKIES`) proves one too. Any other secret, such as tlg's `api_id` and
# `api_hash`, is only the source's API key.
LOGIN_SECRET = "session"
LOGIN_SECRETS = frozenset({COOKIES, LOGIN_SECRET})
TABLE = "secrets"
KEY_SIZE = 32
# How long a key file shorter than KEY_SIZE may be another run's key still being written, in
# seconds, before it counts as damaged; 32 bytes are written in microseconds, so 2 s is ample.
KEY_WAIT = 2.0
# How often a short key file is read again while waiting, in seconds.
KEY_POLL = 0.01


def from_browser(source: Source, *, browser: str, profile: str | None = None) -> cookies.Jar:
    """The source's cookies from an installed browser, for each of its domains."""
    try:
        return _own(source, cookies.load(browser, profile=profile))
    # Every way reading a browser fails: missing file, platform refusal, bad decrypt, or no crypto.
    except (OSError, RuntimeError, ValueError, ImportError, cookies.Error) as error:
        raise LoginError(
            f"{source.code}: can't read {BROWSERS.get(browser, browser)}'s cookies: {error}; "
            "try a cookie file instead"
        ) from None


def from_file(source: Source, path: Path) -> cookies.Jar:
    """The source's cookies from a Netscape or JSON cookie file; a whole browser export works."""
    try:
        return _own(source, cookies.load(path))
    except (OSError, ValueError, cookies.Error) as error:
        raise LoginError(f"{source.code}: can't read {path}: {error}") from None


def store(source: Source, jar: cookies.Jar, *, home: Path, origin: str) -> str:
    """Check that the jar proves a login, seal it in the source's vault, and return summary.

        auth.store(Instagram(), jar, home=Path(".flnd"), origin="Chrome")

    Args:
        source: the source logging in; one of its `needs` cookies must be in the jar.
        jar: the cookies, from `from_browser` or `from_file`.
        home: flnd's folder, `.flnd`.
        origin: where the jar came from, for the messages: a browser's name or the file's.

    Raises:
        LoginError: none of the source's `needs` cookies is in the jar.
        ValueError: the key file is damaged (`key`).
        OSError: the vault can't be written.

    Nothing is written under `.flnd/` but the vault. A plain cookie file an earlier version left
    for the source is deleted once the new login is sealed, so it can never come back.
    """
    if source.needs and not any(name in jar for name in source.needs):
        raise LoginError(
            f"{source.code}: no {' or '.join(source.needs)} for {source.domains[0]} in {origin}; "
            "log in there first"
        )
    save(home, source.code, jar, origin=origin)
    for path in _cookie_files(home, source.code):
        path.unlink(missing_ok=True)
    _remove_if_empty(home / COOKIE_FOLDER)
    return f"{source.code}: logged in from {origin} ({text.fmt.count(len(jar), 'cookie')})"


def save(home: Path, code: str, jar: cookies.Jar, *, origin: str | None = None) -> None:
    """Seal a source's cookie jar in its vault as `cookies`, replacing existing ones.

        auth.save(Path(".flnd"), "ins", jar)

    Args:
        home: flnd's folder, `.flnd`; `secrets.db` and the key file are created when missing.
        code: the source's code; the value is bound to its slot, `<code>:cookies`.
        jar: the cookies; nothing checks that they prove a login (`store` does).
        origin: optional login origin label to record alongside the cookies.

    Raises:
        ValueError: the key file is damaged (`key`).
        OSError: the vault can't be written.
    """
    with secrets(home, code) as vault:
        vault[COOKIES] = json.dumps(fix_domains(jar).raw)
        if origin is not None:
            vault[ORIGIN] = origin


def record_origin(home: Path, code: str, origin: str) -> None:
    """Record how `code` logged in (e.g. "qr", "phone", "Chrome") in its vault.

    Args:
        home: flnd's folder, `.flnd`.
        code: the source's code.
        origin: the login origin label.
    """
    with secrets(home, code) as vault:
        vault[ORIGIN] = origin


def origin(home: Path, code: str) -> str:
    """The recorded login origin for `code`, or "" when none was recorded.

    Args:
        home: flnd's folder, `.flnd`.
        code: the source's code.
    """
    if not (home / paths.SECRETS).is_file():
        return ""
    with secrets(home, code) as vault:
        return vault.get(ORIGIN, "")


def expiring(home: Path, source: Source, *, within: float = 24 * 3600) -> list[tuple[str, float]]:
    """Login cookies of `source.needs` expiring within `within` seconds, as `(name, remaining)`."""
    if not source.needs or not logged_in(home, source.code):
        return []
    now = time.time()
    found: list[tuple[str, float]] = []
    for cookie in load(home, source.code):
        expires = cookie.get("expires", -1)
        if cookie.get("name") in source.needs and isinstance(expires, (int, float)) and expires > 0:
            remaining = expires - now
            if remaining <= within:
                found.append((str(cookie["name"]), remaining))
    return found


def write_back(home: Path, code: str, current: Iterable[Cookie]) -> list[str]:
    """Save refreshed site cookies into the vault's jar for names already present."""
    fresh = list(current)
    if not fresh or not (home / paths.SECRETS).is_file():
        return []
    try:
        saved = load(home, code).raw
        changed = [
            name for cookie in saved if (name := _refresh_cookie(cookie, fresh)) is not None
        ]
        if changed:
            save(home, code, cookies.Jar(saved))
            ui.debug("{code}: refreshed {names}", code=code, names=", ".join(changed))
        return changed
    except (OSError, ValueError, shared.store.Error) as error:
        ui.warn(
            "{code}: could not save refreshed cookies ({kind})",
            code=code, kind=type(error).__name__,
        )
        return []


def _refresh_cookie(cookie: dict, fresh: list[Cookie]) -> str | None:
    """Update `cookie` in place from `fresh` if changed, returning its name or None."""
    domain, path = str(cookie.get("domain", "")).lstrip("."), cookie.get("path") or "/"
    same = [
        found for found in fresh if found.name == cookie["name"]
        and (found.domain.lstrip(".") == domain
             or found.domain.lstrip(".").endswith("." + domain))
    ]
    exact = [
        found for found in same
        if found.domain.lstrip(".") == domain and found.path == path
    ]
    matched = exact or same
    if not matched or (new := matched[-1]).value in (None, "", cookie["value"]):
        return None
    cookie["value"] = new.value
    if new.expires is not None and isinstance(cookie.get("expires"), (int, float)):
        cookie["expires"] = new.expires
    return str(cookie["name"])


def load(home: Path, code: str) -> cookies.Jar:
    """The source's saved cookies, or an empty jar when it never logged in.

        jar = auth.load(Path(".flnd"), "ins")

    Args:
        home: flnd's folder, `.flnd`.
        code: the source's code, such as "ins".

    Returns:
        The jar sealed in the vault, with `fix_domains` applied. A plain cookie file of an earlier
        version is moved into the vault first.

    Raises:
        ValueError: the key file is damaged, or the sealed jar doesn't decrypt (`crypto.Error`):
            it was copied from another slot, or sealed with another key.
    """
    _move(home, code)
    if not (home / paths.SECRETS).is_file():
        return cookies.Jar()
    with secrets(home, code) as vault:
        text = vault.get(COOKIES)
    return fix_domains(cookies.Jar(json.loads(text))) if text else cookies.Jar()


def logged_in(home: Path, code: str) -> bool:
    """Whether the source has a saved login in the vault: cookies or session."""
    _move(home, code)
    return bool(LOGIN_SECRETS & _secret_names(home, code))


def api_key_saved(home: Path, code: str) -> bool:
    """Whether the source has secrets but no login: its API key is stored, `flnd login` isn't done.

        auth.api_key_saved(Path(".flnd"), "tlg")    # True with only `api_id` and `api_hash`
    """
    return not logged_in(home, code) and bool(_secret_names(home, code))


def logout(home: Path, code: str) -> bool:
    """Remove every secret of the source, returning whether anything was removed.

        auth.logout(Path(".flnd"), "ins")       # False when ins never logged in
    """
    removed = logged_in(home, code) or api_key_saved(home, code)
    # Opening the database would create it, so a home that has none is left without one.
    if (home / paths.SECRETS).is_file():
        with secrets(home, code) as saved:
            saved.clear()
    for path in _cookie_files(home, code):
        if path.is_file():
            path.unlink()
            removed = True
    _remove_if_empty(home / COOKIE_FOLDER)
    return removed


def _secret_names(home: Path, code: str) -> set[str]:
    """The names of the source's stored secrets, without creating `secrets.db` or the key file."""
    if not (home / paths.SECRETS).is_file():
        return set()
    with secrets(home, code) as saved:
        return set(saved)


def _cookie_files(home: Path, code: str) -> list[Path]:
    """Plain cookie files an earlier version may have left for a source."""
    return [home / COOKIE_FOLDER / f"{code}{suffix}" for suffix in COOKIE_SUFFIXES]


def _move(home: Path, code: str) -> None:
    """Move a source's plain cookie file into its vault on first access.

    The first file found (`.txt`, then `.json`) is sealed as `cookies`, unless the vault holds a
    jar already, which is the newer login. Each file is deleted only after that, so a vault write
    that fails (a damaged key, a full disk) keeps the file, warned of, for the next run to try
    again. The folder is removed once empty. Sources run on threads of their own, each calling
    this for its own code, so two threads never touch one file.
    """
    folder = home / COOKIE_FOLDER
    if not folder.is_dir():
        return
    for path in _cookie_files(home, code):
        if not path.is_file():
            continue
        try:
            jar = cookies.load(path)
            with secrets(home, code) as vault:
                if COOKIES not in vault:
                    vault[COOKIES] = json.dumps(fix_domains(jar).raw)
        # A file that can't be read or isn't a cookie file (cookies.Error is a ValueError), a
        # damaged key (ValueError), or a vault that can't be opened or written (OSError, kv.Error,
        # sqlite3.Error): the plain file is the only copy of the login, so it stays. Each of these
        # names the file or the key, never a cookie's value.
        except (OSError, ValueError, cookies.Error, shared.store.Error) as error:
            ui.warn(
                "{code}: can't move {name} into the vault: {error}; kept it",
                code=code, name=path.name, error=error,
            )
            continue
        path.unlink(missing_ok=True)
    _remove_if_empty(folder)


def _remove_if_empty(folder: Path) -> None:
    """Remove the old cookie folder once nothing is left in it."""
    try:
        folder.rmdir()
    # Missing, or still holding another source's file (or anything else): left as it is.
    except OSError:
        pass


def fix_domains(jar: cookies.Jar) -> cookies.Jar:
    """flnd's legacy fix-up: every domain gets a leading dot, and every path defaults to `/`."""
    # Exported files often hold host-only domains; dot lets every subdomain see the cookie.
    return cookies.Jar(
        {
            **cookie,
            "domain": "." + cookie.get("domain", "").lstrip("."),
            "path": cookie.get("path") or "/",
        }
        for cookie in jar
    )


def _own(source: Source, jar: cookies.Jar) -> cookies.Jar:
    """The cookies of `jar` for the source's domains and their subdomains, domain by domain."""
    return jar.filter(domains=source.domains)


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
    """One source's secrets. Key file is created on first read or write of a value.

    Each value is stored as base64 of its AES-GCM sealing under the slot `<code>:<name>`, with
    `<code>\\0<name>` as the associated data: a value moved to another slot fails to decrypt.
    """

    def __init__(self, home: Path, code: str):
        self._home = home
        self._code = code
        self._file = shared.store.open(home / paths.SECRETS)
        self._db = self._file[TABLE]
        self._key: bytes | None = None

    def __getitem__(self, name: str) -> str:
        sealed = base64.b64decode(self._db[self._slot(name)])
        decrypted = crypto.decrypt(sealed, self._secret_key(), context=self._context(name))
        return decrypted.decode("utf-8")

    def __setitem__(self, name: str, value: str) -> None:
        sealed = crypto.encrypt(value, self._secret_key(), context=self._context(name))
        self._db[self._slot(name)] = base64.b64encode(sealed).decode("ascii")

    def __delitem__(self, name: str) -> None:
        del self._db[self._slot(name)]

    def __iter__(self) -> Iterator[str]:
        prefix = self._slot("")
        return iter([key[len(prefix):] for key, _ in self._db.rows.prefix(prefix).get()])

    def __len__(self) -> int:
        return self._db.rows.prefix(self._slot("")).count

    def __enter__(self) -> Vault:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    def close(self) -> None:
        """Close the database; the same as leaving `with`."""
        self._file.close()

    def _slot(self, name: str) -> str:
        return f"{self._code}:{name}"

    def _context(self, name: str) -> bytes:
        return f"{self._code}\0{name}".encode("utf-8")

    def _secret_key(self) -> bytes:
        if self._key is None:
            self._key = key(self._home)
        return self._key


def key(home: Path) -> bytes:
    """The 32-byte key in `home/metadata/secret.key`, created with mode 0600 when missing."""
    path = home / paths.KEY
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
            # Lost the race: winner's file exists now, maybe still short, so read it again.
            if time.monotonic() >= deadline:
                raise ValueError(
                    f"{path} is damaged: exists but cannot be read (e.g. broken link); "
                    "delete it, then log in again"
                )
            time.sleep(KEY_POLL)
            continue
        if len(secret) == KEY_SIZE:
            return secret
        # A run killed between creating the file and writing it leaves it empty for good.
        if len(secret) > KEY_SIZE or time.monotonic() >= deadline:
            raise ValueError(
                f"{path} is damaged: it should hold {KEY_SIZE} bytes, not {len(secret)}; "
                "delete it, then log in again"
            )
        time.sleep(KEY_POLL)


def _create_key(path: Path) -> bytes | None:
    """Create key file with new key, returning key or None when another run won."""
    path.parent.mkdir(parents=True, exist_ok=True)
    # O_EXCL makes "first creator wins" without hard link.
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0)
    try:
        handle = os.open(path, flags, 0o600)
    except FileExistsError:
        return None
    # Safe because key is 32 bytes, written in one write, and readers wait up to KEY_WAIT.
    # The stdlib's CSPRNG makes the key; `shared.crypto` leaves keys to it.
    secret = token_bytes(KEY_SIZE)
    try:
        try:
            file = os.fdopen(handle, "wb")
        except BaseException:
            os.close(handle)
            raise
        with file:
            file.write(secret)
    except BaseException:
        path.unlink(missing_ok=True)
        raise
    return secret
