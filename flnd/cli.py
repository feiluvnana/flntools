"""The flnd command line: `flnd download`, `flnd sync`, and `flnd auth`.

Usage:
  flnd download ins:example_user https://x.com/example_handle --from reels --time 2026-01-01,
  cat list.txt | flnd download
  flnd download ins:example_user --shrink --sync
  flnd sync
  flnd sync ls
  flnd sync toggle ins:example_user
  flnd auth login
  flnd auth logout ins tik
  flnd auth ls

It parses arguments, invokes `flnd.Engine`, and formats reports and errors.
"""

from __future__ import annotations

from pathlib import Path
from typing import Annotated, Optional

from flnd import Engine, LoginError, Range, __version__
from flnd.sources import SOURCES
from shared import cli, cookies, fmt, log, prompt, theme

# Data folder relative to current working directory: stores logins, history, settings.
HOME = Path(".flnd")
METHOD_LABELS = {
    "browser": "From your browser", "file": "From a cookie file", "qr": "With a QR code",
    "phone": "With your phone number and a code",
}


def describe() -> str:
    """The one-line summary that opens the help, naming every source in the registry."""
    names = [source.name for source in SOURCES.values()]
    return f"Dead simple media downloader for {', '.join(names[:-1])}, and {names[-1]}."


def epilog() -> str:
    """The help footer: target forms, categories, and examples from sources."""
    # Each form a source's routes read, placeholders in angle brackets.
    forms = "\n".join(f"  {form}" for source in SOURCES.values() for form in source.forms)
    categories = "\n".join(
        f"  {code}: {', '.join(source.categories)}" for code, source in SOURCES.items()
    )
    queries = "\n".join(
        f"  {code}?<query>: {', '.join(route.params) if route.params else 'words'}"
        for code, source in SOURCES.items()
        for route in source.routes if route.is_query
    )
    query_section = f"\n\nQuery parameters (<source>?<query>):\n{queries}" if queries else ""
    return f"""\
Target format:
  <source>:<name, id or URL>, <source>?<query>, or a bare URL
  Sources: {", ".join(SOURCES)}
{forms}

Categories (--from; without it, every one; not for a single post's link):
{categories}{query_section}

Examples:
  flnd download ins:example_user
  flnd download tik:example_creator twt:example_handle --from all
  flnd download fbk:example.page --from profile,stories
  flnd download https://x.com/example_handle/status/1790000000000000000
  flnd download ins:example_user --shrink --sync
  flnd download twt:example_handle --time 2026-01-01,2026-06-30
  cat list.txt | flnd download
  flnd sync
  flnd sync ls
  flnd sync toggle ins:example_user
  flnd auth login
  flnd auth logout all
  flnd auth ls
"""


app = cli.App("flnd", version=__version__, description=describe(), epilog=epilog())


@app.command
def download(
    targets: Annotated[list[str], cli.arg(nargs="+")],
    *,
    categories: Annotated[Optional[list[str]], cli.opt("--from", metavar="CATEGORIES")] = None,
    time: Annotated[Optional[str], cli.opt(metavar="SINCE,UNTIL")] = None,
    # `--shrink` alone is on; `--shrink off` turns a saved one off; not given keeps the saved one.
    shrink: Annotated[Optional[str], cli.opt(choices=("on", "off"), const="on")] = None,
    sync: Annotated[bool, cli.opt("--sync")] = False,
    limit: Annotated[Optional[int], cli.opt("--limit", metavar="N")] = None,
    dry: Annotated[bool, cli.opt("--dry")] = False,
    verbose: Annotated[bool, cli.opt("-v", "--verbose")] = False,
) -> int:
    """Download each target.

    --shrink is saved for each target and used by every later download and sync of it; turning
    it on shrinks the files already saved too, and --shrink off turns it off.

    Args:
        targets: source:value pairs, source?query targets, or URLs; read one per line from stdin
            when none are given.
        categories: what to take from each source, such as posts,reels; a comma list or repeated.
            Without it, every category. Not for a single post's link, which is downloaded whole.
        time: only items dated from one day to another, both included, as 2026-01-01,2026-06-30;
            leave a side empty, or give '*' (quoted), for no limit: 2026-01-01, or ,2026-06-30.
        shrink: compress each photo, video and audio file to a balanced quality in its own format,
            replacing it when smaller, metadata (GPS too) stripped; off stops it.
        sync: keep these targets up to date with `flnd sync`.
        limit: cap how many results a query target downloads, or how many posts a normal target
            downloads.
        dry: list the targets or files it would download, and download none.
        verbose: also show every file as it runs and ends, and each skip, link and shrink,
            under the status lines.
    """
    # The files' rows and the debug lines, such as each skipped file, are shown only when asked for.
    if verbose:
        log.level = "debug"
    try:
        # ValueError is a usage error: engine raises it for bad option/target with message.
        report = Engine(home=HOME).download(
            targets, categories=categories, time=Range() if time is None else time,
            shrink=None if shrink is None else shrink == "on",
            limit=limit, dry=dry, sync=sync,
        )
    except ValueError as error:
        raise cli.UsageError(str(error)) from None
    if not dry:
        log.ok("Complete. {files}", files=fmt.count(report.files, "file"))
    # 1 when a target failed, so a script can tell. A rate-limit wait, a skipped file, or one file
    # of a target that failed while the target went on, isn't a failed target.
    return 0 if report.ok else 1


def sync(
    targets: Annotated[list[str], cli.arg(nargs="*", stdin=False)],
    *,
    verbose: Annotated[bool, cli.opt("-v", "--verbose")] = False,
) -> int:
    """Bring synced targets up to date: repair broken or missing files, fetch new posts, and sum up.

    Each target runs with its saved --shrink and each source's default
    categories; sync changes no setting. To change one, run flnd download with it.

    Args:
        targets: only these targets, as in flnd download; every synced one without them.
        verbose: also show every file as it runs and ends, and each skip, link and shrink,
            under the status lines.
    """
    # -v is how the run is shown, not how a target is synced, so it changes no setting.
    if verbose:
        log.level = "debug"
    try:
        # Engine logs each target's line and totals; ValueError is usage error, rest propagates.
        report = Engine(home=HOME).sync(targets)
    except ValueError as error:
        raise cli.UsageError(str(error)) from None
    return 0 if report.ok else 1


sync_group = app.group("sync", default=sync)


@sync_group.command("ls")
def sync_ls() -> int:
    """List every known target with sync state, settings, disk size and category status."""
    items = Engine(home=HOME).sync_ls()
    if not items:
        log.info("no targets")
        return 0
    for item in items:
        state = "sync on" if item["sync"] else "sync off"
        shrink = ", --shrink" if item["shrink"] else ""
        stats = (fmt.count(item["seen"], "seen") if item["query"]
                 else f"{fmt.count(item['files'], 'file')}, {fmt.size(item['size'])}")
        last = f", {item['at'][:10]}" if item["at"] else ""
        cats = ", ".join(f"{cat}: {st}" for cat, st in item["categories"].items())
        cats_part = f" ({cats})" if cats else ""
        log.info(
            "{target}: {state}{shrink}, {stats}{last}{cats}",
            target=str(item["target"]), state=state, shrink=shrink, stats=stats, last=last,
            cats=cats_part,
        )
    return 0


@sync_group.command("toggle")
def sync_toggle(targets: Annotated[list[str], cli.arg(nargs="+", stdin=False)]) -> int:
    """Flip each target between synced and not, keeping all saved settings and history.

    Args:
        targets: the targets to toggle, as in flnd download.
    """
    try:
        toggled = Engine(home=HOME).sync_toggle(targets)
    except ValueError as error:
        raise cli.UsageError(str(error)) from None
    for target, state in toggled:
        log.ok("{target}: sync {state}", target=str(target), state="on" if state else "off")
    return 0


auth_group = app.group("auth", description="Logins and saved credentials.")


@auth_group.command("login")
def login_command() -> int:
    """Log in to a source: from your browser, a cookie file, or Telegram's own ways.

    Every answer is asked, in a terminal: the source, then the method when there are several,
    then what the method needs.
    """
    # Logging in is a one-time step done by a person, so there is no flag form for scripts. With no
    # terminal there is nobody to answer the questions, so the command refuses before asking any.
    if not theme.screen.interactive:
        raise cli.UsageError("flnd auth login asks its questions in a terminal")
    engine = Engine(home=HOME)
    code = _ask_source(engine)
    source = SOURCES[code]
    methods = source.login_methods
    method = methods[0] if len(methods) == 1 else prompt.choose(
        f"Log in to {source.name}", {METHOD_LABELS.get(name, name): name for name in methods})
    answers: dict[str, Optional[str]] = {}
    # A source with its own login (tlg) asks its own questions, through the engine.
    if code in engine.cookie_logins:
        if method == "file":
            answers["path"] = prompt.ask("Path to cookies.txt or .json", validate=_readable).strip()
        else:
            choices = {label: name for name, label in engine.browsers.items()}
            browser = prompt.choose("Read cookies from", choices)
            answers.update(browser=browser, profile=_ask_profile(browser))
    try:
        message = engine.login(code, method, **answers)
    # A failed login is expected, logged as error; any other exception is a bug.
    except LoginError as error:
        log.error("{error}", error=error)
        return 1
    if message:
        log.ok(message)
    return 0


@auth_group.command("logout")
def logout(
    sources: Annotated[list[str], cli.arg(nargs="*", choices=(*SOURCES, "all"), stdin=False)],
) -> int:
    """Remove saved logins: each source's cookies and secrets, an API key included.

    Args:
        sources: the sources to log out of, such as ins tik, or all for every one.
    """
    engine = Engine(home=HOME)
    if not sources:
        logged_in = [code for code, state in engine.logins.items() if state == "logged in"]
        msg = f"logged in: {', '.join(logged_in)}" if logged_in else "no source is logged in"
        raise cli.UsageError(f"give the sources to log out of, or all; {msg}")
    everything = "all" in sources
    removed = engine.logout(sources)
    for code in SOURCES if everything else dict.fromkeys(sources):
        if code in removed:
            log.ok("{code}: logged out", code=code)
        # `all` names every source, so only the ones asked for by name say they had no login.
        elif not everything:
            log.info("{code}: not logged in", code=code)
    return 0


@auth_group.command("ls")
def auth_ls() -> int:
    """List each site with its login state, origin, and any expiring login cookie."""
    for item in Engine(home=HOME).auth_ls():
        origin = f" ({item['origin']})" if item["origin"] else ""
        exp_parts = [f"{name} expired" if rem <= 0 else f"{name} expires in {fmt.duration(rem)}"
                     for name, rem in item["expiring"]]
        exp = f"; {', '.join(exp_parts)}" if exp_parts else ""
        log.info(
            "{code} ({name}): {state}{origin}{exp}",
            code=item["code"], name=item["name"], state=item["state"], origin=origin, exp=exp,
        )
    return 0


def _ask_source(engine: Engine) -> str:
    """Ask which source to log in to, and return its code."""
    # List differentiates sources logged in or holding only an API key.
    logins = engine.logins
    options = {f"{source.name} ({code})" + (f" · {logins[code]}" if code in logins else ""): code
               for code, source in SOURCES.items()}
    return prompt.choose("Log in to", options)


def _ask_profile(browser: str) -> Optional[str]:
    """Ask which of the browser's profiles to read, with the default one preselected.

    Only asked when there are several. With none found, None lets the engine's login say why.
    """
    profiles = cookies.profiles(browser)
    if len(profiles) <= 1:
        return profiles[0] if profiles else None
    return prompt.choose("Profile", profiles, default=profiles[0])


def _readable(raw: str) -> Optional[str]:
    """Validate cookie file path: None if exists, else error message to prompt again."""
    path = Path(raw.strip()).expanduser()
    return None if path.is_file() else f"no file at {raw.strip() or '(empty)'}"
