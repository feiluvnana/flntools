"""The engine's two cookie logins, `browser` and `file`, where each source's cookies live, and logout.

    jar = login.from_browser(Instagram(), browser="chrome")
    message = login.store(Instagram(), jar, home=Path(".flnd"), origin="Chrome")
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from flnd import vault
# A source's own login (tlg) raises it too, so it lives in base; `login.LoginError` is the same class.
from flnd.sources.base import LoginError
from shared import cookies

if TYPE_CHECKING:
    from flnd.sources.base import Source

COOKIE_METHODS = ("browser", "file")
BROWSERS = {"chrome": "Chrome", "firefox": "Firefox", "edge": "Edge", "brave": "Brave", "chromium": "Chromium"}
# Cookie files from before `flnd login`, still read when `.flnd/cookies/<code>.txt` is missing.
LEGACY_DIRS = (Path("E:/flntools/.flnd/cookies"), Path("E:/.flnd/cookies"))


def from_browser(source: Source, *, browser: str, profile: str | None = None) -> list[dict]:
    """The source's cookies from an installed browser, for each of its domains.

        jar = login.from_browser(Instagram(), browser="firefox")

    Raises `LoginError` when the browser can't be read, suggesting `--method file`.
    """
    try:
        return [cookie for domain in source.domains
                for cookie in cookies.load_from_browser(domain, browser=browser, profile=profile)]
    except (OSError, RuntimeError, ValueError, ImportError) as error:
        raise LoginError(f"{source.code}: can't read {BROWSERS.get(browser, browser)}'s cookies: {error}; "
                         "try --method file") from None


def from_file(source: Source, path: Path) -> list[dict]:
    """The source's cookies from a Netscape or JSON cookie file; a whole browser export works.

        jar = login.from_file(Twitter(), Path("~/Downloads/cookies.txt").expanduser())
    """
    try:
        return [cookie for domain in source.domains for cookie in cookies.load_from_file(path, domain=domain)]
    except (OSError, ValueError) as error:
        raise LoginError(f"{source.code}: can't read {path}: {error}") from None


def store(source: Source, jar: list[dict], *, home: Path, origin: str) -> str:
    """Check that the jar proves a login, save it as the source's cookie file, and say so.

        login.store(Instagram(), jar, home=Path(".flnd"), origin="Chrome")   # "ins: logged in from Chrome (14 cookies)"

    Raises `LoginError` when none of the source's `needs` cookies is there.
    """
    names = {cookie["name"] for cookie in jar}
    if source.needs and not names.intersection(source.needs):
        raise LoginError(f"{source.code}: no {' or '.join(source.needs)} for {source.domains[0]} in {origin}; "
                         "log in there first")
    path = cookie_file(home, source.code)
    cookies.to_file(fix_domains(jar), path)
    # One cookie file per source, so an old JSON file can't shadow the new one.
    path.with_suffix(".json").unlink(missing_ok=True)
    return f"{source.code}: logged in from {origin} ({len(jar)} cookies)"


def load(home: Path, code: str) -> list[dict]:
    """The source's saved cookies, ready for a browser or a client, or `[]` when it never logged in."""
    folders = (home / "cookies", *LEGACY_DIRS)
    for path in (folder / f"{code}{suffix}" for folder in folders for suffix in (".txt", ".json")):
        if path.is_file():
            return fix_domains(cookies.load_from_file(path))
    return []


def logout(home: Path, code: str) -> bool:
    """Remove the source's cookie file and secrets, and return whether there was anything to remove."""
    removed = False
    for path in (cookie_file(home, code), cookie_file(home, code).with_suffix(".json")):
        if path.is_file():
            path.unlink()
            removed = True
    if (home / vault.DB_FILE).is_file():
        with vault.open(home, code) as secrets:
            removed = removed or len(secrets) > 0
            secrets.clear()
    return removed


def cookie_file(home: Path, code: str) -> Path:
    """Where a source's cookies are saved: `.flnd/cookies/<code>.txt`."""
    return home / "cookies" / f"{code}.txt"


def fix_domains(jar: list[dict]) -> list[dict]:
    """flnd's legacy fix-up: every domain gets a leading dot, and every path defaults to `/`."""
    # Exported files often hold host-only domains; the dot lets every subdomain see the cookie, as flnd always did.
    return [{**cookie, "domain": "." + cookie.get("domain", "").lstrip("."), "path": cookie.get("path") or "/"}
            for cookie in jar]
