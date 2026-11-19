"""The home's layout, its logs and samples, and the logins in its vault.

`flnd.db` is `src/engine/store.py`'s. This module keeps the rest of the home: a target's folder
under `media/`, the samples and crash files under `log/` and their sweep, and each plugin's
login in `secret.db` (`flnkit.vault`, `flnkit.cookies`). `src/docs/storage` is the guide to it.

The logins: a cookie plugin logs in by `from_browser` or `from_file`; `store` checks the jar
proves a login and `save` stores it in the plugin's slot of `secret.db` (`secrets`), and its
account's row in `flnd.db` (`record_origin`); a run calls `load` for the jar, and `write_back`
saves the cookies the site refreshed. A secret's value never reaches a message, a log line or
`flnd.db`.
"""

from __future__ import annotations

import json
import re
import shutil
import time
from collections.abc import Iterable
from http.cookiejar import Cookie
from pathlib import Path
from typing import TYPE_CHECKING

import flnd.engine.auth
import flnd.engine.store
from flnd.engine.store import DB, MEDIA, SECRETS
from flnd.errors import Error, LoginError, UsageError
from flnd.ui.log import ui
from flnkit import cookies, text

if TYPE_CHECKING:
    from flnd.plugin import Plugin, Target

__all__ = ["sample", "secrets", "sweep", "target_folder"]

# ---------------------------------------------------------------------------------------------
# The home's layout
# ---------------------------------------------------------------------------------------------

# Under the home: media/ for downloads, metadata/ for stores (flnd.db and secret.db), and
# log/ for run logs and crash reports.
LOGS = Path("log")

# Logs and samples older than this are swept at the start of each run that downloads.
LOG_DAYS = 30
# A response whose URL looks like this is a login's, and is never kept as a sample: its body may
# hold the session it just made.
LOGIN_URL = re.compile(r"login|logon|signin|sign_in|oauth|auth|session|token|password|account",
                       re.IGNORECASE)


def target_folder(home: Path, target: Target) -> Path:
    """A target's folder: `<home>/media/<code>/<value>`, with the value made a safe name.

    Args:
        home: the home folder.
        target: the target, resolved.

    Returns:
        The folder; nothing is made on disk.

    Example:
        >>> from flnd.plugin import Target
        >>> target_folder(Path("home"), Target("ins:example_user", kind="user")).as_posix()
        'home/media/ins/example_user'
    """
    return home / MEDIA / target.plugin / text.filename(target.value)


# ---------------------------------------------------------------------------------------------
# Logs and samples
# ---------------------------------------------------------------------------------------------

def sample(home: Path, run: int | None, error: BaseException) -> Path | None:
    """Keep the answer a `Changed` error was raised on, scrubbed, in `logs/<run>/samples/`.

    A parse failure is a site that changed or a source's bug, and the answer is what shows
    which. It is kept only when it is safe to:
    - values under keys that look like secrets (`token`, `csrf`, `session`, `cookie`, `auth`,
      `password`, `key`, …) become "…" (`flnkit.text.scrub`);
    - a login's answer is never kept: one whose URL looks like a login (`LOGIN_URL`), or that
      sets a cookie, may hold the session it made;
    - the file is named by a count, never by anything the site sent.

    Args:
        home: the home folder.
        run: the run's id; None (outside a recorded run) keeps nothing.
        error: what failed; anything but a `Changed` with a response keeps nothing.

    Returns:
        The sample's path; None when nothing was kept.
    """
    from flnd.errors import Changed

    response = getattr(error, "response", None)
    if run is None or not isinstance(error, Changed) or response is None:
        return None
    url = str(getattr(response, "url", "") or "")
    headers = getattr(response, "headers", None) or {}
    if LOGIN_URL.search(text.redact(url)) or "set-cookie" in {str(k).lower() for k in headers}:
        return None
    content = getattr(response, "content", b"")
    body = content.decode("utf-8", "replace") if isinstance(content, bytes) else str(content)
    folder = home / LOGS / str(run) / "samples"
    try:
        folder.mkdir(parents=True, exist_ok=True)
        count = len(list(folder.iterdir()))
        while True:
            count += 1
            path = folder / f"{count}.txt"
            try:
                # An exclusive create, so two samples kept at once never take the same name.
                with path.open("x", encoding="utf-8") as out:
                    out.write(f"{text.redact(url)}\n\n{text.scrub(body)}")
                break
            except FileExistsError:
                continue
    except OSError as failed:
        # A sample is a help for a bug report, never worth failing the run over.
        ui.debug("sample not kept: {error}", error=failed)
        return None
    return path


def sweep(home: Path, *, days: int = LOG_DAYS) -> int:
    """Remove the logs older than `days`: each run's folder (its samples) and crash files.

    Called at the start of each run that downloads, so a home that runs every day keeps a
    month of them. An entry that can't be removed is left for the next sweep.

    Args:
        home: the home folder.
        days: how old, by its modified time, an entry may get.

    Returns:
        How many entries were removed.

    Example:
        >>> import tempfile
        >>> sweep(Path(tempfile.mkdtemp()))
        0
    """
    logs = home / LOGS
    if not logs.is_dir():
        return 0
    oldest = time.time() - days * 86_400
    removed = 0
    for entry in logs.iterdir():
        try:
            if entry.lstat().st_mtime >= oldest:
                continue
            if entry.is_dir() and not entry.is_symlink():
                shutil.rmtree(entry)
            else:
                entry.unlink()
            removed += 1
        except OSError as error:
            ui.debug("{name}: not swept: {error}", name=entry.name, error=error)
    return removed


# ---------------------------------------------------------------------------------------------
# Logins, in the vault
# ---------------------------------------------------------------------------------------------

# Browsers `from_browser` reads cookies from, by `flnkit.cookies` name with display name.
BROWSERS = {
    "chrome": "Chrome", "firefox": "Firefox", "edge": "Edge",
    "brave": "Brave", "chromium": "Chromium", "safari": "Safari",
}
# The folder of the plain cookie files of much older versions, moved into the vault on first use.
COOKIE_FOLDER = "cookies"
# A plain cookie file was `<code>.txt`, or `<code>.json`, the older form; the first one found wins.
COOKIE_SUFFIXES = (".txt", ".json")
# The secret that holds a cookie source's jar, as JSON: a list of cookie dicts.
COOKIES = "cookies"
# The secret an old home kept a login's origin in; `flnd.db`'s `accounts` table holds it now.
ORIGIN = "origin"
# The name of a plugin's one account: a home keeps one account per plugin.
ACCOUNT = "default"
# The secret that proves a login for a source with its own (tlg): the session its `login` stores.
# A cookie source's jar (`COOKIES`) proves one too. Any other secret, such as tlg's `api_id` and
# `api_hash`, is only the source's API key.
LOGIN_SECRET = "session"
LOGIN_SECRETS = frozenset({COOKIES, LOGIN_SECRET})


def from_browser(source: Plugin, *, browser: str, profile: str | None = None) -> cookies.Jar:
    """The source's cookies from an installed browser, for each of its domains.

    Args:
        source: the source; only cookies of its domains and their subdomains are kept.
        browser: a key of `BROWSERS`.
        profile: the browser's profile folder; None for its default.

    Returns:
        The cookies.

    Raises:
        LoginError: the browser's cookies can't be read; the message suggests a cookie file.
    """
    try:
        return _own(source, cookies.load(browser, profile=profile))
    # Safari off macOS: a usage error as it is.
    except UsageError:
        raise
    # Every way reading a browser fails: missing file, platform refusal, bad decrypt, no crypto.
    # An error with its own advice (Safari's Full Disk Access) keeps it.
    except (OSError, RuntimeError, ValueError, ImportError, Error) as error:
        hint = getattr(error, "hint", "") or "try a cookie file instead"
        raise LoginError(
            f"{source.code}: can't read {BROWSERS.get(browser, browser)}'s cookies: {error}",
            hint=hint) from None


def from_file(source: Plugin, path: Path) -> cookies.Jar:
    """The source's cookies from a Netscape or JSON cookie file; a whole browser export works.

    Args:
        source: the source; only cookies of its domains and their subdomains are kept.
        path: the file; it is read once and never copied.

    Returns:
        The cookies.

    Raises:
        LoginError: the file can't be read, or isn't a cookie file.
    """
    try:
        return _own(source, cookies.load(path))
    except (OSError, ValueError, Error) as error:
        raise LoginError(f"{source.code}: can't read {path}: {error}") from None


def store(source: Plugin, jar: cookies.Jar, *, home: Path, origin: str) -> str:
    """Check that the jar proves a login, seal it in the source's vault, and return a summary.

    A plain cookie file a much older version left for the source is deleted once the new login
    is sealed, so it can never come back.

    Args:
        source: the source logging in; one of its `needs` cookies must be in the jar.
        jar: the cookies, from `from_browser` or `from_file`.
        home: the home folder.
        origin: where the jar came from, for the messages: a browser's name or the file's.

    Returns:
        One line saying where the login came from, and how many cookies it has.

    Raises:
        LoginError: none of the cookies the source's login names is in the jar.
        DiskError: the key file is damaged, or the vault can't be written.
    """
    names = _login_names(source)
    if names and not any(name in jar for name in names):
        raise LoginError(
            f"{source.code}: no {' or '.join(names)} for {source.domains[0]} in {origin}; "
            "log in there first"
        )
    save(home, source.code, jar, origin=origin)
    for path in _cookie_files(home, source.code):
        path.unlink(missing_ok=True)
    _remove_if_empty(home / COOKIE_FOLDER)
    return f"{source.code}: logged in from {origin} ({text.count(len(jar), 'cookie')})"


def save(home: Path, code: str, jar: cookies.Jar, *, origin: str | None = None) -> None:
    """Seal a source's cookie jar in its vault as `cookies`, replacing the one there.

    Args:
        home: the home folder; `secrets.db` and the key file are made when missing.
        code: the source's code; the value is bound to its row.
        jar: the cookies; nothing checks that they prove a login (`store` does).
        origin: how it logged in, recorded in `flnd.db`; None leaves the record as it is.

    Raises:
        DiskError: the key file is damaged, or the vault can't be written.
    """
    with secrets(home, code, create=True) as vault:
        vault[COOKIES] = json.dumps(fix_domains(jar).list)
    if origin is not None:
        record_origin(home, code, origin)


def record_origin(home: Path, code: str, origin: str) -> None:
    """Record how a plugin logged in ("qr", "phone", "Chrome", a cookie file's name) as its
    default account in `flnd.db`, as schema 2 names an origin (a file's is `file`)."""
    with flnd.engine.store.open(home) as db:
        db.login(code, ACCOUNT, origin=flnd.engine.store.ORIGINS.get(origin.lower(), "file"))


def origin(home: Path, code: str) -> str:
    """How a source logged in, or "" when nothing was recorded.

    A home migrated from before `flnd.db` kept it as a secret; that is read when `flnd.db` has
    no row.

    Args:
        home: the home folder.
        code: the source's code.

    Returns:
        The origin, as `store` or a source's own login recorded it.
    """
    with flnd.engine.store.open(home) as db:
        found = next((row.origin for row in db.accounts(code) if row.name == ACCOUNT), None)
    if found is not None:
        return found
    if not (home / SECRETS).is_file():
        return ""
    with secrets(home, code) as vault:
        return vault.get(ORIGIN, "")


def expiring(home: Path, source: Plugin, *, within: float = 24 * 3600) -> list[tuple[str, float]]:
    """Login cookies (the names its `login` gives) that expire soon, to warn before the wall.

    Args:
        home: the home folder.
        source: the source; only the cookies its login names count.
        within: how soon, in seconds; a day by default.

    Returns:
        Each such cookie's name and its seconds left (negative once expired).
    """
    names = _login_names(source)
    if not names or not logged_in(home, source.code):
        return []
    now = time.time()
    found: list[tuple[str, float]] = []
    for cookie in load(home, source.code):
        expires = cookie.get("expires", -1)
        if cookie.get("name") in names and isinstance(expires, (int, float)) and expires > 0:
            remaining = expires - now
            if remaining <= within:
                found.append((str(cookie["name"]), remaining))
    return found


def write_back(home: Path, code: str, current: Iterable[Cookie]) -> list[str]:
    """Save the cookies a site refreshed into the vault's jar, for names already there.

    A failure is a warning, never an error: the run's files are saved, and the next run logs in
    with the cookies it had.

    Args:
        home: the home folder.
        code: the source's code.
        current: the cookies the run's client holds now.

    Returns:
        The names of the cookies that changed.
    """
    fresh = list(current)
    if not fresh or not _has_vault(home):
        return []
    try:
        saved = load(home, code).list
        changed = [
            name for cookie in saved if (name := _refresh_cookie(cookie, fresh)) is not None
        ]
        if changed:
            save(home, code, cookies.Jar(saved, error=LoginError))
            ui.debug("{code}: refreshed {names}", code=code, names=", ".join(changed))
        return changed
    except (OSError, ValueError, Error) as error:
        ui.warn(
            "{code}: could not save refreshed cookies ({kind})",
            code=code, kind=type(error).__name__,
        )
        return []


def _refresh_cookie(cookie: dict, fresh: list[Cookie]) -> str | None:
    """Take a saved cookie's new value from the run's cookies, when the site changed it.

    The same name on the same domain and path wins; else the same name on a subdomain.

    Args:
        cookie: a saved cookie, changed in place.
        fresh: the cookies the run's client holds now.

    Returns:
        The cookie's name when it changed; None when it didn't.
    """
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

    A plain cookie file of a much older version is moved into the vault first.

    Args:
        home: the home folder.
        code: the source's code.

    Returns:
        The jar, with `fix_domains` applied.

    Raises:
        DiskError: the key file is damaged.
        LoginError: the sealed jar doesn't open: it was copied from another row, or sealed
            with another key.
    """
    _move(home, code)
    if not _has_vault(home):
        return cookies.Jar()
    with secrets(home, code) as vault:
        text = vault.get(COOKIES)
    return fix_domains(cookies.Jar(json.loads(text), error=LoginError)) if text else cookies.Jar()


def logged_in(home: Path, code: str) -> bool:
    """Whether the source has a saved login in the vault: cookies or a session."""
    _move(home, code)
    return bool(LOGIN_SECRETS & _secret_names(home, code))


def api_key_saved(home: Path, code: str) -> bool:
    """Whether the source has secrets but no login: its API key is stored, the login isn't."""
    return not logged_in(home, code) and bool(_secret_names(home, code))


def logout(home: Path, code: str) -> bool:
    """Remove every secret of the source, and its login record.

    Args:
        home: the home folder; a home with no vault is left without one.
        code: the source's code.

    Returns:
        Whether there was a login, an API key or a plain cookie file to remove.
    """
    removed = logged_in(home, code) or api_key_saved(home, code)
    # Opening the vault would create it, so a home that has none is left without one.
    if _has_vault(home):
        with secrets(home, code) as saved:
            saved.clear()
    if (home / DB).is_file():
        with flnd.engine.store.open(home) as db:
            for row in db.accounts(code):
                db.logout(row.id)
    for path in _cookie_files(home, code):
        if path.is_file():
            path.unlink()
            removed = True
    _remove_if_empty(home / COOKIE_FOLDER)
    return removed


def _has_vault(home: Path) -> bool:
    """Whether the home has a vault in `metadata/secret.db`."""
    return (home / SECRETS).is_file()


def _secret_names(home: Path, code: str) -> set[str]:
    """The names of the source's secrets, without creating `secrets.db` or the key file."""
    if not _has_vault(home):
        return set()
    with secrets(home, code) as saved:
        return set(saved)


def _cookie_files(home: Path, code: str) -> list[Path]:
    """Plain cookie files a much older version may have left for a source."""
    return [home / COOKIE_FOLDER / f"{code}{suffix}" for suffix in COOKIE_SUFFIXES]


def _move(home: Path, code: str) -> None:
    """Move a source's plain cookie file into its vault on first access.

    Args:
        home: the home folder.
        code: the source's code.

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
            with secrets(home, code, create=True) as vault:
                if COOKIES not in vault:
                    vault[COOKIES] = json.dumps(fix_domains(jar).list)
        # A file that can't be read or isn't a cookie file (a LoginError), a damaged key (a
        # DiskError), or a vault that can't be opened or written (OSError, DiskError): the plain
        # file is the only copy of the login, so it stays. Each of these names the file or the
        # key, never a cookie's value.
        except (OSError, ValueError, Error) as error:
            ui.warn(
                "{code}: can't move {name} into the vault: {error}; kept it",
                code=code, name=path.name, error=error,
            )
            continue
        path.unlink(missing_ok=True)
    _remove_if_empty(folder)


def _remove_if_empty(folder: Path) -> None:
    """Remove the old cookie folder once nothing is left in it.

    Args:
        folder: the folder; missing, or not empty, it is left as it is.
    """
    try:
        folder.rmdir()
    # Missing, or still holding another source's file (or anything else): left as it is.
    except OSError:
        pass


def fix_domains(jar: cookies.Jar) -> cookies.Jar:
    """Give every domain a leading dot, and every path `/` when it has none.

    Args:
        jar: the cookies, as read or saved.

    Returns:
        A new jar; the one given is unchanged.
    """
    # Exported files often hold host-only domains; the dot lets every subdomain see the cookie.
    return cookies.Jar(
        ({
            **cookie,
            "domain": "." + cookie.get("domain", "").lstrip("."),
            "path": cookie.get("path") or "/",
        }
        for cookie in jar),
        error=LoginError,
    )


def _own(source: Plugin, jar: cookies.Jar) -> cookies.Jar:
    """The cookies of `jar` for the source's domains and their subdomains, domain by domain."""
    return jar.filter(domains=source.domains)




def secrets(home: Path, code: str, *, create: bool = False) -> flnd.engine.auth.Vault:
    """The sealed secrets of a plugin's default account, for a `with` block.

    Args:
        home: the home folder; `metadata/secret.db` is made in it on the first write.
        code: the plugin's code; another plugin's secrets aren't visible.
        create: make the default account when there is none, to write to it; else a plugin
            without one reads no secrets.
    """
    flnd.engine.auth.seal_plaintext(home)
    account = 0
    if create or (home / DB).is_file():
        with flnd.engine.store.open(home) as db:
            found = next((row for row in db.accounts(code) if row.default), None)
            if found is None and create:
                found = db.login(code, ACCOUNT, origin="")
        account = found.id if found is not None else 0
    return flnd.engine.auth.secrets(home, account, plugin=code)


def _login_names(source: Plugin) -> tuple[str, ...]:
    """The cookies that prove a source's login (`cookies(...)`'s names); none for other logins."""
    return tuple(getattr(source.login, "names", ())) if source.login is not None else ()
