"""Logins: one cookie file per plugin in the logins folder, and the login steps.

SPEC.md#logins explains it.
"""

from __future__ import annotations

import datetime
import json
import os
from collections.abc import Callable, Iterable, Mapping
from http.cookiejar import Cookie
from pathlib import Path
from typing import Any

from flnd.engine import config
from flnd.errors import Error, LoginError, UsageError
from flnd.plugin import Cookies, LoggedIn, Plugin, Target
from flnd.ui.log import ui
from flnkit import clock, cookies, files

__all__ = ["LOGIN", "SOON_DAYS", "codes", "entry", "expiring", "fix_domains", "folder", "load",
           "login", "logout", "secrets", "write_back"]

# The logins folder; None is the user's, beside config.toml. The tests set one of their own.
LOGINS: Path | None = None
# The kind of the pseudo-target a plugin's own `Ctx` acts for.
LOGIN = "login"
# A login that expires within this many days is listed as expiring.
SOON_DAYS = 3


# ---------------------------------------------------------------------------------------------
# The logins folder
# ---------------------------------------------------------------------------------------------

def folder() -> Path:
    """The logins folder, `logins/` in the user's flnd folder (`config.folder`).

    Raises:
        RuntimeError: the real one, while `FLND_TEST_NO_PROFILES` is set.
    """
    if LOGINS is not None:
        return LOGINS
    found = config.folder() / "logins"
    config.guard(found)
    return found


def _made() -> Path:
    """The logins folder, made with mode 0700 when it is missing."""
    found = folder()
    found.parent.mkdir(parents=True, exist_ok=True)
    found.mkdir(mode=0o700, exist_ok=True)
    if os.name != "nt":
        found.chmod(0o700)
    return found


def _cookie_file(code: str) -> Path:
    return folder() / f"{code}.txt"


def _secret_file(code: str) -> Path:
    return folder() / f"{code}.json"


def _write(where: Path, text: str) -> None:
    """Write `where` whole with mode 0600, through a temp file and an atomic replace."""
    _made()
    with files.part(where) as held:
        if os.name != "nt":
            os.chmod(held.fd, 0o600)
        held.write(text.encode("utf-8"))


# ---------------------------------------------------------------------------------------------
# Reading a login
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


def load(code: str) -> cookies.Jar:
    """The plugin's saved cookies; an empty jar when it has none.

    Raises:
        LoginError: the file can't be read.
    """
    where = _cookie_file(code)
    if not where.is_file():
        return cookies.Jar()
    try:
        return fix_domains(cookies.load(where))
    except OSError as error:
        raise LoginError(f"{code}: can't read {where.name}: {error.strerror}",
                         hint=f"flnd auth login {code} <file>") from None


def secrets(code: str) -> dict[str, str]:
    """What the plugin keeps of its own login (`logins/<code>.json`): `ctx.secrets` reads it.
    Empty when it has none.

    Raises:
        LoginError: the file can't be read.
    """
    where = _secret_file(code)
    if not where.is_file():
        return {}
    try:
        kept = json.loads(where.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        raise LoginError(f"{code}: can't read {where.name}",
                         hint=f"flnd auth login {code}") from None
    if not isinstance(kept, dict):
        raise LoginError(f"{code}: {where.name} isn't a login file", hint=f"flnd auth login {code}")
    return {str(name): str(value) for name, value in kept.items()}


def _proves(plugin: Plugin | type[Plugin]) -> Callable[[str], bool]:
    """The plugin's test for a login cookie's name; never true for a plugin without a login."""
    return getattr(plugin.login, "proves", lambda name: False)


def _ends(proves: Callable[[str], bool], jar: Iterable[Mapping[str, Any]]) -> list[float]:
    """When the login cookies in `jar` (those the login `proves`) end, in Unix seconds; a
    session cookie has no end."""
    return [float(cookie["expires"]) for cookie in jar
            if proves(str(cookie.get("name")))
            and isinstance(cookie.get("expires"), (int, float)) and cookie["expires"] > 0]


def codes() -> list[str]:
    """The codes of the plugins that have a login file, in order."""
    where = folder()
    if not where.is_dir():
        return []
    return sorted({found.stem for found in where.iterdir() if found.suffix in (".txt", ".json")})


def entry(code: str, plugin: Plugin | type[Plugin]) -> LoggedIn | None:
    """The plugin's login, as `flnd auth list` shows it; None when it has none."""
    where = next((found for found in (_cookie_file(code), _secret_file(code))
                  if found.is_file()), None)
    if where is None:
        return None
    ends: list[float] = []
    if where.suffix == ".txt":
        try:
            ends = _ends(_proves(plugin), cookies.load(where))
        except (OSError, Error):
            return LoggedIn(code, "unreadable", str(where))
    status, expires = "ok", None
    if ends:
        end = min(ends)
        left = end - clock.wall()
        expires = datetime.datetime.fromtimestamp(end, datetime.UTC).isoformat(timespec="seconds")
        if left <= 0:
            status = "expired"
        elif left < SOON_DAYS * 86400:
            status = f"expires in {int(left // 86400) + 1} days"
    return LoggedIn(code, status, str(where), expires)


def expiring(plugin: Plugin, jar: Iterable[Mapping[str, Any]], *,
             within: float) -> list[tuple[str, float]]:
    """The login's cookies (those it `proves`) that expire within `within` seconds, each with its
    seconds left (0 or less: expired); a session cookie never does."""
    proves = _proves(plugin)
    now = clock.wall()
    return [(str(cookie["name"]), float(cookie["expires"]) - now) for cookie in jar
            if proves(str(cookie.get("name"))) and isinstance(cookie.get("expires"), (int, float))
            and 0 < cookie["expires"] <= now + within]


# ---------------------------------------------------------------------------------------------
# Writing it back
# ---------------------------------------------------------------------------------------------

def write_back(code: str, current: Iterable[Cookie]) -> list[str]:
    """Save the cookies the site refreshed during a run, for the names the file holds already.

    A failure is a warning: the file is as it was, and the next run uses the cookies it had.

    Returns:
        The names that changed.
    """
    fresh = list(current)
    if not fresh:
        return []
    try:
        saved = load(code).list
        changed = [name for cookie in saved if (name := _refresh(cookie, fresh)) is not None]
        if changed:
            cookies.Jar(saved, error=LoginError).save(_cookie_file(code))
            ui.debug("{code}: refreshed {names}", code=code, names=", ".join(changed))
        return changed
    except (OSError, ValueError, Error) as error:
        ui.warn("{code}: could not save refreshed cookies ({kind})", code=code,
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
          interactive: bool = False) -> LoggedIn:
    """Log in to `plugin` by `method`, check it, and keep it in the logins folder.

    A cookie login reads its jar from the file, and writes `logins/<code>.txt`; any other runs
    the plugin's `Login.start`, which alone may write `ctx.secrets`, and writes
    `logins/<code>.json`. Then `Login.check` must pass. An earlier login is replaced.

    Args:
        home: the home folder, for the plugin's settings and state.
        plugin: the plugin, made from its class.
        method: one of its login's `methods`.
        path: the cookie file, for `file`.
        interactive: whether the plugin's own steps may ask in the terminal.

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
    if isinstance(own, Cookies):
        jar = _read_jar(plugin, own, path=path)
    else:
        # A stored API key isn't asked for again.
        try:
            kept = secrets(plugin.code)
        except LoginError:
            kept = {}
    _signed_in(home, plugin, method, jar, kept, interactive=interactive)
    if isinstance(own, Cookies):
        _made()
        jar.save(_cookie_file(plugin.code))
    else:
        _write(_secret_file(plugin.code), json.dumps(kept))
    found = entry(plugin.code, plugin)
    assert found is not None
    return found


def _signed_in(home: Path, plugin: Plugin, method: str, jar: cookies.Jar,
               kept: dict[str, str], *, interactive: bool) -> None:
    """Run the plugin's login steps on a `Ctx` of its own: `start` (not for cookies), which
    writes into `kept`, then `check`."""
    from flnd.engine import crawl, ctx, run

    async def steps(context: ctx.Ctx) -> None:
        assert plugin.login is not None
        if not isinstance(plugin.login, Cookies):
            await ctx.start(plugin.login, context, method)
        if not await plugin.login.check(context):
            raise LoginError(f"{plugin.code}: the login didn't work",
                             hint=f"flnd auth login {plugin.code}")

    with run.stopping() as stop:
        settings = crawl.RunShared(home=home, stop=stop, interactive=interactive)
        with ctx.plugin_run(settings, plugin, jar=jar, secrets=kept) as opened:
            opened.load_state()
            try:
                opened.loop.run(steps(ctx.Ctx(opened, Target(f"{plugin.code}:", kind=LOGIN),
                                              login=True)))
            finally:
                opened.save_state()


def _read_jar(plugin: Plugin, own: Cookies, *, path: Path | str | None) -> cookies.Jar:
    """The plugin's cookies from its cookie file, on its domains only; one login cookie of
    its must be there, and not every one of them expired."""
    code = plugin.code
    if path is None:
        raise UsageError(f"{code}: a file login needs the cookie file's path")
    where = Path(path).expanduser()
    try:
        jar = cookies.load(where)
    except (OSError, ValueError, Error) as error:
        raise LoginError(f"{code}: can't read {where}: {error}") from None
    jar = fix_domains(jar.filter(domains=plugin.domains))
    present = [cookie for cookie in jar if own.proves(str(cookie.get("name")))]
    if not present:
        raise LoginError(f"{code}: no login cookie ({own.label}) in {where.name}",
                         hint="export the cookies from a browser where you are logged in")
    now = clock.wall()
    if all(isinstance(cookie.get("expires"), (int, float)) and 0 < cookie["expires"] <= now
           for cookie in present):
        raise LoginError(f"{code}: the login in {where.name} has expired",
                         hint="export a fresh cookie file")
    return jar


def logout(code: str, plugin: Plugin | type[Plugin]) -> LoggedIn:
    """Delete the plugin's login files.

    Returns:
        The login as it stood.

    Raises:
        UsageError: the plugin has no login.
    """
    found = entry(code, plugin)
    if found is None:
        raise UsageError(f"{code}: not logged in")
    for where in (_cookie_file(code), _secret_file(code)):
        where.unlink(missing_ok=True)
    return found
