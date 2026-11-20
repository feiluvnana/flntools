"""Accounts, switching, the sealed vault and the login methods.

COOKBOOK.md#logins-and-accounts explains it.
"""

from __future__ import annotations

import asyncio
import contextlib
import dataclasses
import datetime
import itertools
import json
import os
import sqlite3
import threading
import weakref
from collections.abc import Callable, Iterable, Iterator, Mapping, MutableMapping, Sequence
from http.cookiejar import Cookie
from pathlib import Path
from typing import Any

from flnd.engine import jobs, store
from flnd.errors import (
    Blocked,
    DiskError,
    Error,
    Hidden,
    LoginError,
    NotFound,
    Stopped,
    ToolError,
    UsageError,
)
from flnd.plugin import Account, Cookies, Plugin, Target
from flnd.ui import forms
from flnd.ui.log import ui
from flnkit import browser, clock, cookies, db, text, vault

__all__ = ["Switcher", "Vault", "account", "default", "expiring", "fix_domains", "load",
           "logged_in", "login", "logout", "pick", "seal_plaintext", "secrets", "settle",
           "write_back"]

# The key file; None is the user's (`flnkit.vault.keyfile`). The tests set one of their own.
KEYFILE: Path | None = None
# The secret that keeps a cookie login's jar, as JSON, and the one that keeps a plugin's own
# session (tlg's); either proves a login, and any other (an API key) doesn't.
COOKIES = "cookies"
LOGINS = frozenset({COOKIES, "session"})
# The kind of the pseudo-target a plugin's account `Ctx` acts for.
ACCOUNT = "account"
# How long the `browser` method waits for a login in its window, and how often it looks.
WINDOW_WAIT = 600.0
WINDOW_POLL = 1.0
# The browsers whose cookie stores a login reads, by method, with the names people know.
BROWSERS = {"chrome": "Chrome", "firefox": "Firefox", "safari": "Safari"}
TABLE = ("CREATE TABLE IF NOT EXISTS secrets (account INTEGER NOT NULL, name TEXT NOT NULL,"
         " sealed BLOB NOT NULL, PRIMARY KEY (account, name)) WITHOUT ROWID")
# The name a version-1 home's one account per plugin takes.
DEFAULT = "default"


# ---------------------------------------------------------------------------------------------
# The sealed vault
# ---------------------------------------------------------------------------------------------

class Vault(MutableMapping[str, str]):
    """One account's secrets in `secret.db`, each sealed under the user's key (6.7).

    Opening makes neither `secret.db` nor the key file. A read with the key missing or wrong
    raises `LoginError` and keeps the rows; the first write makes a missing key file.

    Args:
        home: the home folder.
        account: the account's id.
        plugin: its plugin's code, for the hint of a key that is gone.
    """

    def __init__(self, home: Path, account: int, *, plugin: str = "") -> None:
        self._home, self._account, self._plugin = home, account, plugin
        self._db = db.Db(home / store.SECRETS, schema=TABLE, secure=True)

    def __getitem__(self, name: str) -> str:
        found = self._db.one("SELECT sealed FROM secrets WHERE account = ? AND name = ?",
                             (self._account, name))
        if found is None:
            raise KeyError(name)
        try:
            return vault.unseal(_key(self._home, create=False), self._account, name, found[0])
        except LoginError:
            raise LoginError(vault.KEY_GONE, hint=f"flnd auth login {self._plugin}"
                             if self._plugin else "") from None

    def __setitem__(self, name: str, value: str) -> None:
        sealed = vault.seal(_key(self._home, create=True), self._account, name, str(value))
        self._db.write("INSERT OR REPLACE INTO secrets VALUES (?, ?, ?)",
                       (self._account, name, sealed))

    def __delitem__(self, name: str) -> None:
        if name not in self:
            raise KeyError(name)
        self._db.write("DELETE FROM secrets WHERE account = ? AND name = ?",
                       (self._account, name))

    def __contains__(self, name: object) -> bool:
        return isinstance(name, str) and self._db.one(
            "SELECT 1 FROM secrets WHERE account = ? AND name = ?",
            (self._account, name)) is not None

    def __iter__(self) -> Iterator[str]:
        found = self._db.all("SELECT name FROM secrets WHERE account = ? ORDER BY name",
                             (self._account,))
        return iter([name for (name,) in found])

    def __len__(self) -> int:
        return sum(1 for _ in self)

    def clear(self) -> None:
        """Remove every secret of the account; a home without `secret.db` stays without."""
        if self._db.path.is_file():
            self._db.write("DELETE FROM secrets WHERE account = ?", (self._account,))

    def __enter__(self) -> Vault:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    def close(self) -> None:
        """Close the database; the same as leaving `with`."""
        self._db.close()


def secrets(home: Path, account: int, *, plugin: str = "") -> Vault:
    """The sealed secrets of one account, for a `with` block.

    A version-1 `secret.db`, whose values are plaintext, is sealed first (`seal_plaintext`).
    """
    seal_plaintext(home)
    return Vault(home, account, plugin=plugin)


def seal_plaintext(home: Path) -> None:
    """Seal a version-1 `secret.db` in place, once: each plugin's plaintext values go under
    its default account, made when `flnd.db` has none, in one transaction (6.7).

    The freed pages are zeroed (`secure_delete`), then the file is vacuumed and its WAL
    emptied. A file with free pages left (a run that stopped before its vacuum) is vacuumed the
    next time it is opened, so no plaintext stays on the disk.

    Raises:
        DiskError: `secret.db` can't be read or written.
    """
    path = home / store.SECRETS
    if not path.is_file():
        return
    sql = db.connect(path)
    try:
        if not _plain(sql):
            if not sql.execute("PRAGMA freelist_count").fetchone()[0]:
                return
        else:
            sql.execute("PRAGMA secure_delete = ON")
            sql.execute("BEGIN IMMEDIATE")
            try:
                # Asked again under the lock: another process may have sealed it meanwhile.
                if _plain(sql):
                    _seal(home, sql)
                sql.execute("COMMIT")
            except BaseException:
                if sql.in_transaction:
                    sql.execute("ROLLBACK")
                raise
        sql.execute("VACUUM")
        sql.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    except sqlite3.Error as error:
        raise DiskError(f"{path}: {error}") from error
    finally:
        sql.close()
    if os.name != "nt":
        with contextlib.suppress(OSError):
            path.chmod(0o600)


def _plain(sql: sqlite3.Connection) -> bool:
    """Whether `secret.db`'s table is version 1's, of plaintext values."""
    return "slot" in {row[1] for row in sql.execute("PRAGMA table_info(secrets)")}


def _seal(home: Path, sql: sqlite3.Connection) -> None:
    """Rebuild version 1's `secrets` table sealed, in the caller's transaction."""
    rows = sql.execute("SELECT slot, name, value FROM secrets").fetchall()
    with store.open(home) as home_db:
        ids = {row.plugin: row.id for row in reversed(home_db.accounts()) if row.default}
        for slot in sorted({slot for slot, _, _ in rows} - set(ids)):
            ids[slot] = home_db.login(slot, DEFAULT, origin="").id
    key = _key(home, create=True) if rows else b""
    sql.execute("ALTER TABLE secrets RENAME TO v1_secrets")
    sql.execute(TABLE)
    sql.executemany("INSERT INTO secrets VALUES (?, ?, ?)", [
        (ids[slot], name, vault.seal(key, ids[slot], name, _text(value)))
        for slot, name, value in rows])
    sql.execute("DROP TABLE v1_secrets")


def _key(home: Path, *, create: bool) -> bytes:
    return vault.key(KEYFILE or vault.keyfile(), home=home, create=create)


def _text(value: object) -> str:
    return bytes(value).decode("utf-8", "replace") if isinstance(
        value, (bytes, memoryview)) else str(value)


# ---------------------------------------------------------------------------------------------
# Accounts
# ---------------------------------------------------------------------------------------------

def account(row: store.AccountRow) -> Account:
    """An `accounts` row as the public `Account`."""
    status = row.status if row.status in ("held", "expired") else "ok"
    return Account(id=row.id, plugin=row.plugin, name=row.name, origin=row.origin, at=row.at,
                   is_default=row.default, status=status, held_until=row.held_until,
                   expires=row.expires)


def pick(accounts: Sequence[Account], *, preferred: str | None = None) -> Account | None:
    """The account a target runs as, of a plugin's `accounts` in `auth list` order:
    `preferred` (the one that worked last time) when it is `ok`, else the first `ok` one,
    else the first not expired.

    Returns:
        The account; None when the plugin has none to use.
    """
    usable = [found for found in accounts if found.status != "expired"]
    usable.sort(key=lambda found: (found.status != "ok", found.name != preferred))
    return usable[0] if usable else None


# ---------------------------------------------------------------------------------------------
# Cookies
# ---------------------------------------------------------------------------------------------

def fix_domains(jar: cookies.Jar) -> cookies.Jar:
    """Every domain with a leading dot, so its subdomains see the cookie, and every path `/`
    when it has none; a `__Host-` cookie stays on its host alone, as browsers require."""
    fixed = []
    for cookie in jar:
        bare = str(cookie.get("domain", "")).lstrip(".")
        host = str(cookie.get("name", "")).startswith("__Host-") or not bare
        fixed.append({**cookie, "domain": bare if host else f".{bare}",
                      "path": cookie.get("path") or "/"})
    return cookies.Jar(fixed, error=LoginError)


def load(home: Path, found: Account | None) -> cookies.Jar:
    """An account's sealed cookies; an empty jar for no account, or one with none.

    Raises:
        LoginError: the vault's key is gone.
    """
    if found is None or not (home / store.SECRETS).is_file():
        return cookies.Jar()
    with secrets(home, found.id, plugin=found.plugin) as saved:
        kept = saved.get(COOKIES)
    return fix_domains(cookies.Jar(json.loads(kept), error=LoginError)) if kept else cookies.Jar()


def logged_in(home: Path, found: Account) -> bool:
    """Whether the account has cookies or a session in the vault."""
    if not (home / store.SECRETS).is_file():
        return False
    with secrets(home, found.id) as saved:
        return any(name in saved for name in LOGINS)


def expiring(plugin: Plugin, jar: Iterable[Mapping[str, Any]], *,
             within: float) -> list[tuple[str, float]]:
    """The login's cookies (its `names`) that expire within `within` seconds, each with its
    seconds left (0 or less: expired); a session cookie never does."""
    names = getattr(plugin.login, "names", ())
    now = clock.wall()
    return [(str(cookie["name"]), float(cookie["expires"]) - now) for cookie in jar
            if cookie.get("name") in names and isinstance(cookie.get("expires"), (int, float))
            and 0 < cookie["expires"] <= now + within]


def write_back(home: Path, found: Account, current: Iterable[Cookie]) -> list[str]:
    """Save the cookies the site refreshed during a run, for the names the jar holds already.

    A failure is a warning: the files are saved, and the next run uses the cookies it had.

    Returns:
        The names that changed.
    """
    fresh = list(current)
    if not fresh or not (home / store.SECRETS).is_file():
        return []
    try:
        saved = load(home, found).list
        changed = [name for cookie in saved if (name := _refresh(cookie, fresh)) is not None]
        if changed:
            with secrets(home, found.id, plugin=found.plugin) as vaulted:
                vaulted[COOKIES] = json.dumps(saved)
            ui.debug("{code}: refreshed {names}", code=found.plugin, names=", ".join(changed))
        return changed
    except (OSError, ValueError, Error) as error:
        ui.warn("{code}: could not save refreshed cookies ({kind})", code=found.plugin,
                kind=type(error).__name__)
        return []


def _refresh(cookie: dict[str, Any], fresh: list[Cookie]) -> str | None:
    """Take a saved cookie's new value from the run's, when the site changed it: the same name
    on the same domain and path wins, else on a subdomain. Returns its name when it changed."""
    domain, path = str(cookie.get("domain", "")).lstrip("."), cookie.get("path") or "/"
    same = [found for found in fresh if found.name == cookie["name"] and (
        found.domain.lstrip(".") == domain or found.domain.lstrip(".").endswith("." + domain))]
    exact = [found for found in same if found.domain.lstrip(".") == domain and found.path == path]
    matched = exact or same
    if not matched or (new := matched[-1]).value in (None, "", cookie["value"]):
        return None
    cookie["value"] = new.value
    if new.expires is not None and isinstance(cookie.get("expires"), (int, float)):
        cookie["expires"] = new.expires
    return str(cookie["name"])


# ---------------------------------------------------------------------------------------------
# Logging in and out
# ---------------------------------------------------------------------------------------------

def login(home: Path, plugin: Plugin, method: str, *, path: Path | str | None = None,
          profile: str | None = None, interactive: bool = False,
          name: str | None = None) -> Account:
    """Log an account of `plugin` in by `method`, check it, and seal it in the vault.

    A cookie login reads its jar by the method; any other runs the plugin's `Login.start`,
    which alone may write `ctx.secrets`. Then `Login.check` must pass, and the account is
    named by `name`, else asked for in a terminal (offering `Login.whoami`, else the next
    `account<n>`; a taken name asks before it replaces that account's login), else `Login.whoami`,
    else the plugin's default account's name, else `account<n>`: logging in again replaces that
    account's login.

    Args:
        home: the home folder.
        plugin: the plugin, made from its class.
        method: one of its login's `methods`.
        path: the cookie file, for `file`.
        profile: the browser's profile, for a browser's cookie store.
        interactive: whether the login may ask in the terminal: the plugin's own steps, and the
            account's name.
        name: the account's name, asked for and looked up nowhere else.

    Raises:
        UsageError: a plugin with no login, a method it doesn't take, or a missing `path`.
        LoginError: the cookies found don't prove a login, or the check failed.
    """
    own = plugin.login
    if own is None:
        raise UsageError(f"{plugin.code} needs no login")
    if method not in own.methods:
        raise UsageError(f"{plugin.code} logs in by {', '.join(own.methods)}, not {method!r}")
    jar, kept = cookies.Jar(), {}
    with store.open(home) as db:
        first = next((row for row in db.accounts(plugin.code) if row.default), None)
    if isinstance(own, Cookies):
        jar = _read_jar(plugin, own, method, path=path, profile=profile)
    elif first is not None and (home / store.SECRETS).is_file():
        # A stored API key isn't asked for again.
        with contextlib.suppress(LoginError), secrets(home, first.id) as saved:
            kept = dict(saved)
    found = _signed_in(home, plugin, method, jar, kept, interactive=interactive)
    name = (name or "").strip()
    with store.open(home) as db:
        if not name and interactive and forms.interactive():
            name = _asked_name(db, plugin.code, found)
        name = name or found or (first.name if first else _next_name(db, plugin.code))
        row = db.login(plugin.code, name, origin=method)
        with secrets(home, row.id, plugin=plugin.code) as saved:
            saved.clear()
            saved.update(kept)
            if isinstance(own, Cookies):
                saved[COOKIES] = json.dumps(jar.list)
        ends = [float(cookie["expires"]) for cookie in jar
                if cookie.get("name") in getattr(own, "names", ())
                and isinstance(cookie.get("expires"), (int, float)) and cookie["expires"] > 0]
        if ends:
            at = datetime.datetime.fromtimestamp(min(ends), datetime.UTC)
            db.mark(row.id, status="ok", expires=at.isoformat(timespec="seconds"))
        return account(next(found for found in db.accounts(plugin.code) if found.id == row.id))


def _signed_in(home: Path, plugin: Plugin, method: str, jar: cookies.Jar,
               kept: dict[str, str], *, interactive: bool) -> str | None:
    """Run the plugin's login steps on a `Ctx` of its account: `start` (not for cookies), which
    writes into `kept`, then `check`, then `whoami`, whose name it returns."""
    from flnd.engine import crawl, ctx, run

    async def steps(context: ctx.Ctx) -> str | None:
        assert plugin.login is not None
        if not isinstance(plugin.login, Cookies):
            await ctx.start(plugin.login, context, method)
        if not await plugin.login.check(context):
            raise LoginError(f"{plugin.code}: the login didn't work",
                             hint=f"flnd auth login {plugin.code}")
        found = await plugin.login.whoami(context)
        return found.strip() if isinstance(found, str) and found.strip() else None

    with run.stopping() as stop:
        settings = crawl.RunShared(home=home, stop=stop, interactive=interactive)
        with ctx.plugin_run(settings, plugin, jar=jar, secrets=kept) as opened:
            opened.load_state()
            try:
                return opened.loop.run(steps(ctx.Ctx(opened, Target(f"{plugin.code}:",
                                                                    kind=ACCOUNT), login=True)))
            finally:
                opened.save_state()


def _read_jar(plugin: Plugin, own: Cookies, method: str, *, path: Path | str | None,
             profile: str | None) -> cookies.Jar:
    """The plugin's cookies by a cookie method, on its domains only."""
    if method == "browser":
        from flnd.engine import run

        origin = "the browser window"
        with run.stopping() as stop:
            jar = asyncio.run(_window(plugin, own, stop.soon))
    elif method == "file":
        if path is None:
            raise UsageError(f"{plugin.code}: a file login needs the cookie file's path")
        where = Path(path).expanduser()
        try:
            jar = cookies.load(where)
        except (OSError, ValueError, Error) as error:
            raise LoginError(f"{plugin.code}: can't read {where}: {error}") from None
        origin = where.name
    elif method == "chrome":
        origin = BROWSERS.get(method, method)
        from flnd.engine import chrome

        try:
            jar = asyncio.run(chrome.extension_cookies(plugin.domains))
        except UsageError:
            raise
        except (OSError, RuntimeError, ValueError, ImportError, Error) as error:
            raise LoginError(f"{plugin.code}: can't read {origin}'s cookies: {error}",
                             hint=getattr(error, "hint", "") or "flnd extension install"
                             ) from None
    else:
        origin = BROWSERS.get(method, method)
        try:
            jar = cookies.load(method, profile=profile)
        except UsageError:
            raise
        except (OSError, RuntimeError, ValueError, ImportError, Error) as error:
            raise LoginError(f"{plugin.code}: can't read {origin}'s cookies: {error}",
                             hint=getattr(error, "hint", "") or "try a cookie file instead"
                             ) from None
    jar = fix_domains(jar.filter(domains=plugin.domains))
    if not any(name in jar for name in own.names):
        raise LoginError(f"{plugin.code}: no {' or '.join(own.names)} for {plugin.domains[0]} "
                         f"in {origin}; log in there first")
    return jar


async def _window(plugin: Plugin, own: Cookies, stop: threading.Event) -> cookies.Jar:
    """Open flnd's Chromium, visible, at the login page; then read its cookies every
    `WINDOW_POLL` seconds until one of `own.names` is set on the plugin's domains, and close it.

    Raises:
        LoginError: the window was closed first, or `WINDOW_WAIT` passed.
        ToolError: Playwright isn't installed.
        Stopped: Ctrl-C.
    """
    try:
        window = await browser.launch(headless=False)
    except ImportError as error:
        raise ToolError(str(error), hint="pip install playwright") from None
    try:
        await (await window.tab()).goto(own.url)
        ends = clock.now() + WINDOW_WAIT
        while True:
            jar = (await window.cookies()).filter(domains=plugin.domains)
            if any(name in jar for name in own.names):
                return jar
            if not window.raw.pages:
                raise LoginError(f"{plugin.code}: the window was closed before the login")
            if clock.now() >= ends:
                raise LoginError(f"{plugin.code}: no login in the window in "
                                 f"{text.duration(WINDOW_WAIT)}")
            if not await clock.pause(WINDOW_POLL, stop=stop):
                raise Stopped(f"{plugin.code}: the login was stopped")
    finally:
        await window.close()


def _asked_name(db: store.Store, code: str, found: str | None) -> str:
    """Ask for the account's name in a terminal, offering the site's own (`found`) or the next
    free `account<n>`. A name that is taken asks before it replaces that account's login."""
    taken = {row.name for row in db.accounts(code)}
    offer = found or _next_name(db, code)
    while True:
        name = forms.ask("Account name", default=offer).strip() or offer
        if name not in taken or forms.confirm(f"Replace the login of {name}?"):
            return name
        offer = _next_name(db, code)


def _next_name(db: store.Store, plugin: str) -> str:
    taken = {row.name for row in db.accounts(plugin)}
    return next(f"account{n}" for n in itertools.count(1) if f"account{n}" not in taken)


def logout(home: Path, plugin: str, name: str | None = None) -> Account:
    """Log one account of `plugin` out: its secrets, then its row; the default when no `name`.

    Returns:
        The account that was removed.

    Raises:
        UsageError: the plugin has no such account.
    """
    found = _named(home, plugin, name)
    with store.open(home) as db:
        if (home / store.SECRETS).is_file():
            with secrets(home, found.id) as saved:
                saved.clear()
        db.logout(found.id)
    return found


def default(home: Path, plugin: str, name: str) -> Account:
    """Make the account `name` the one `plugin`'s runs use when none is named.

    Raises:
        UsageError: the plugin has no such account.
    """
    found = _named(home, plugin, name)
    with store.open(home) as db:
        db.make_default(found.id)
    return dataclasses.replace(found, is_default=True)


def _named(home: Path, plugin: str, name: str | None) -> Account:
    """The account `name` of `plugin`, or its default when none is named."""
    rows = []
    if (home / store.DB).is_file():
        with store.open(home) as db:
            rows = [account(row) for row in db.accounts(plugin)]
    for found in rows:
        if found.name == name or (name is None and found.is_default):
            return found
    taken = ", ".join(found.name for found in rows) or "none"
    raise UsageError(f"{plugin}: no account {name!r}" if name else f"{plugin}: no account",
                     hint=f"accounts of {plugin}: {taken}")


# ---------------------------------------------------------------------------------------------
# Switching
# ---------------------------------------------------------------------------------------------

class Switcher:
    """The run's `jobs.Scheduler.switch`: a job refused on one account moves to the plugin's
    next one, for a plugin whose `switch` is on (6.2, 6.7).

    The next account is the first in `auth list` order that the job hasn't run as, isn't
    expired, and isn't held; with every one held, the one whose hold ends first. A job never
    goes back to an account it left, so two walled accounts can't pass it back and forth: once
    it has run as every one, the scheduler's own policy holds or fails it. What the run learns
    of its accounts (a block holds one, a login error expires it) is written to `flnd.db` as
    the run ends (`settle`).

    Args:
        scheduler: the run's scheduler, whose holds it reads and sets.
        enabled: whether a plugin's `switch` is on, by its code.
        accounts: a plugin's accounts in `auth list` order, by its code.
        shown: called with each move's line, for the `now` line; None shows nothing.
    """

    def __init__(self, scheduler: jobs.Scheduler, enabled: Callable[[str], bool],
                 accounts: Callable[[str], Sequence[Account]], *,
                 shown: Callable[[str], None] | None = None) -> None:
        self.scheduler, self._enabled, self._accounts = scheduler, enabled, accounts
        self._shown = shown
        self._tried: weakref.WeakKeyDictionary[jobs.Job[Any], set[str]] = \
            weakref.WeakKeyDictionary()
        # Each held account's end (Unix seconds), and the expired ones, by (plugin, name).
        self.held: dict[tuple[str, str], float] = {}
        self.expired: set[tuple[str, str]] = set()

    def __call__(self, job: jobs.Job[Any], error: BaseException) -> jobs.Job[Any] | None:
        if job.account is None or not self._enabled(job.plugin):
            return None
        left = job.account
        tried = self._tried.setdefault(job, set())
        tried.add(left)
        if isinstance(error, LoginError):
            self.expired.add((job.plugin, left))
        elif isinstance(error, Blocked):
            wait = max(error.wait or 0.0, jobs.ROUND_WAITS[0])
            self.held[(job.plugin, left)] = clock.wall() + wait
            with contextlib.suppress(Blocked):
                self.scheduler.hold(job.plugin, left, seconds=wait, reason=str(error))
        free = [found for found in self._accounts(job.plugin) if found.name not in tried
                and found.status != "expired" and (job.plugin, found.name) not in self.expired]
        if not free:
            return None
        job.account = min(free, key=lambda found: self._wait(job, found)).name
        reason = _reason(error)
        ui.note({"type": "switch", "plugin": job.plugin, "from": left, "to": job.account,
                 "reason": reason})
        if self._shown is not None:
            self._shown(f"{job.plugin}: {left} → {job.account} ({reason})")
        return job

    def _wait(self, job: jobs.Job[Any], found: Account) -> float:
        """How long `found` is held for `job`: by this run's scheduler, or by an earlier run."""
        now = clock.now()
        waits = [hold.until - now for hold in self.scheduler.holds
                 if hold.plugin == job.plugin and hold.account == found.name
                 and hold.bucket in (None, job.bucket)]
        if found.status == "held" and found.held_until:
            ends = datetime.datetime.fromisoformat(found.held_until)
            waits.append(ends.timestamp() - clock.wall())
        return max([0.0, *waits])


def _reason(error: BaseException) -> str:
    """Why a job moved, as the journal names it."""
    for kind, reason in ((LoginError, "expired"), (Blocked, "blocked"), (Hidden, "hidden"),
                         (NotFound, "not_found")):
        if isinstance(error, kind):
            return reason
    return type(error).__name__.lower()


def settle(db: store.Store, switcher: Switcher, expired: Iterable[tuple[str, str | None]],
           ) -> None:
    """Write what a run learnt of its accounts: each held one's end, and each expired one,
    from the switcher and from the scheduler's own login errors (`expired`); off the loop."""
    ended = {*switcher.expired, *((plugin, name) for plugin, name in expired if name)}
    marks = {**{key: ("held", until) for key, until in switcher.held.items()},
             **dict.fromkeys(ended, ("expired", None))}
    for plugin in {plugin for plugin, _ in marks}:
        for row in db.accounts(plugin):
            status, until = marks.get((plugin, row.name), (None, None))
            if status is not None:
                held = (datetime.datetime.fromtimestamp(until, datetime.UTC)
                        .isoformat(timespec="seconds") if until is not None else None)
                db.mark(row.id, status=status, held_until=held)
