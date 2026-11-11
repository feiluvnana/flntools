"""The flnd command line: `flnd download`, `flnd login` and `flnd logout`.

Usage:
  flnd download ins:example_user https://x.com/example_handle --from reels,media --since 2026-01-01
  cat list.txt | flnd download
  flnd login ins --method browser --browser firefox
  flnd logout --all

Only `login` asks questions; `download` never does.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path
from typing import Annotated, Optional

from flnd import __version__, login as logins
from flnd.engine import Engine
from flnd.session import Options, unknown_categories
from flnd.sources import SOURCES
from flnd.sources.base import Source
from flnd.target import TargetError
from shared import cli, log, prompt, theme

HOME = Path(".flnd")
METHOD_LABELS = {"browser": "From your browser", "file": "From a cookie file"}


def describe() -> str:
    names = [source.name for source in SOURCES.values()]
    return f"Dead simple media downloader for {', '.join(names[:-1])}, and {names[-1]}."


def epilog() -> str:
    examples = "\n".join(f"  {example}" for source in SOURCES.values() for example in source.examples)
    categories = "\n".join(f"  {code}: {', '.join(_marked(source))}" for code, source in SOURCES.items())
    return f"""\
Target format:
  <source>:<name, id or URL>, or a bare URL
  Sources: {", ".join(SOURCES)}
{examples}

Categories (--from; * is downloaded without --from):
{categories}

Examples:
  flnd download ins:example_user
  flnd download tik:example_creator twt:example_handle --from all
  flnd download fbk:example.page --from profile,stories
  flnd download https://x.com/example_handle/status/1790000000000000000
  cat list.txt | flnd download
  flnd login ins
"""


def _marked(source: type[Source]) -> list[str]:
    return [name + ("*" if name in source.default_categories else "") for name in source.categories]


app = cli.App("flnd", version=__version__, description=describe(), epilog=epilog())


@app.command
def download(
    targets: Annotated[list[str], cli.arg(nargs="+")],
    *,
    categories: Annotated[Optional[list[str]], cli.opt("--from", metavar="CATEGORIES")] = None,
    since: Annotated[Optional[date], cli.opt(type=date.fromisoformat, metavar="DATE")] = None,
    until: Annotated[Optional[date], cli.opt(type=date.fromisoformat, metavar="DATE")] = None,
    unpack: bool = False,
    zip: bool = False,
    locale: Optional[str] = None,
    timezone: Optional[str] = None,
) -> int:
    """Download each target.

    Args:
        targets: source:value pairs or URLs; read one per line from stdin when none are given.
        categories: what to take from each source, such as posts,reels or all; a comma list or repeated.
            Without it, each source's own content; other people's (tagged, reposts) only when named.
        since: only items dated on or after this day (YYYY-MM-DD).
        until: only items dated on or before this day (YYYY-MM-DD).
        unpack: unpack each saved archive into a folder named after it.
        zip: zip each target's folder when it ends.
        locale: the browser's language, such as fr-FR; match where your connection comes from.
        timezone: the browser's timezone, such as Europe/Paris; match where your connection comes from.
    """
    if since and until and since > until:
        raise cli.UsageError("--since is after --until")
    engine = Engine(home=HOME, locale=locale, timezone=timezone)
    try:
        resolved = engine.resolve(targets)
    except TargetError as error:
        raise cli.UsageError(str(error)) from None
    names = None
    if categories is not None:
        names = tuple(dict.fromkeys(part.strip() for value in categories for part in value.split(",") if part.strip()))
        problem = unknown_categories(names, list(dict.fromkeys(SOURCES[target.source] for target in resolved)))
        if problem:
            raise cli.UsageError(problem)
    log.info("Storage: {path}", path=(HOME / "downloads").resolve())
    total = engine.run(resolved, Options(categories=names, since=since, until=until, unpack=unpack, zip=zip))
    log.ok("Complete. {n} files", n=total)
    return 0


@app.command("login")
def login_command(
    source: Annotated[str, cli.arg(choices=SOURCES)],
    *,
    method: Optional[str] = None,
    path: Optional[Path] = None,
    browser: Annotated[Optional[str], cli.opt(choices=logins.BROWSERS)] = None,
    profile: Optional[str] = None,
) -> int:
    """Log in to a source, from your browser or a cookie file.

    Each method asks only for what it needs and wasn't given. Without a terminal, give them all.

    Args:
        source: the source to log in to.
        method: how to log in: browser or file for cookie sources; asked when there are several.
        path: the cookie file (Netscape cookies.txt or JSON) for --method file; a whole browser export works.
        browser: the installed browser to read cookies from, for --method browser.
        profile: the browser profile's folder name, such as "Profile 1"; the default profile otherwise.
    """
    site = SOURCES[source]()
    method = method or _choose_method(site)
    if method not in site.login_methods:
        raise cli.UsageError(f"{source} has no login method {method!r}: use {', '.join(site.login_methods)}")
    try:
        if type(site).login is not Source.login:
            Engine(home=HOME).login(site, method)
            return 0
        if method == "file":
            path = (path or _ask_path()).expanduser()
            message = logins.store(site, logins.from_file(site, path), home=HOME, origin=path.name)
        else:
            browser = browser or _ask_browser()
            jar = logins.from_browser(site, browser=browser, profile=profile)
            message = logins.store(site, jar, home=HOME, origin=logins.BROWSERS[browser])
    except logins.LoginError as error:
        log.error("{error}", error=error)
        return 1
    log.ok(message)
    return 0


@app.command
def logout(source: Annotated[Optional[str], cli.arg(choices=SOURCES)] = None, *, all: bool = False) -> int:
    """Remove a source's saved login: its cookie file and secrets.

    Args:
        source: the source to log out of.
        all: log out of every source.
    """
    if (source is None) == (not all):
        raise cli.UsageError("give a source or --all, not both" if all else "give a source, or --all")
    for code in SOURCES if all else [source or ""]:
        if logins.logout(HOME, code):
            log.ok("{code}: logged out", code=code)
        elif not all:
            log.info("{code}: was not logged in", code=code)
    return 0


def _choose_method(source: Source) -> str:
    methods = source.login_methods
    if len(methods) == 1:
        return methods[0]
    if not theme.screen.interactive:
        raise cli.UsageError(f"give --method: {', '.join(methods)}")
    return prompt.choice(f"Log in to {source.name}", {METHOD_LABELS.get(name, name): name for name in methods})


def _ask_path() -> Path:
    if not theme.screen.interactive:
        raise cli.UsageError("give --path with the cookie file")
    return Path(prompt.input("Path to cookies.txt or .json", validate=_readable)).expanduser()


def _readable(raw: str) -> Optional[str]:
    path = Path(raw.strip()).expanduser()
    return None if path.is_file() else f"no file at {raw.strip() or '(empty)'}"


def _ask_browser() -> str:
    if not theme.screen.interactive:
        raise cli.UsageError(f"give --browser: {', '.join(logins.BROWSERS)}")
    return prompt.choice("Read cookies from", {label: name for name, label in logins.BROWSERS.items()})
