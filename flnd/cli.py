"""The flnd command line: `flnd download`, `flnd sync`, `flnd target`, and `flnd auth`."""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Annotated, Optional, Sequence

import cyclopts
from rich.table import Table

from flnd import Engine, LoginError, Range, TargetError, __version__
from flnd.sources import SOURCES
from flnd import _cli as cli, _prompt as prompt, _text as text
from flnd._ui import ui

# Data folder relative to current working directory: stores logins, history, settings.
HOME = Path(".flnd")
METHOD_LABELS = {
    "browser": "From your browser",
    "file": "From a cookie file",
    "qr": "With a QR code",
    "phone": "With your phone number and a code",
}


def describe() -> str:
    """Return the one-line summary naming every source in the registry."""
    names = [source.name for source in SOURCES.values()]
    return f"Dead simple media downloader for {', '.join(names[:-1])}, and {names[-1]}."


def epilog() -> str:
    """Return the help footer with target forms, categories, and examples."""
    forms = "\n".join(f"  {form}" for source in SOURCES.values() for form in source.forms)
    categories = "\n".join(
        f"  {code}: {', '.join(source.categories)}" for code, source in SOURCES.items()
    )
    return (
        f"Target format:\n"
        f"  <source>:<name, id or URL>, <source>?<query>, or a bare URL\n"
        f"  Sources: {', '.join(SOURCES)}\n"
        f"{forms}\n\n"
        f"Categories (--from; required for profiles, chats, and queries):\n"
        f"{categories}"
    )


app = cli.App(name="flnd", help=f"{describe()}\n\n{epilog()}")
app.raw.version = __version__


@app.command
def download(
    targets: Annotated[
        list[str],
        cyclopts.Parameter(
            help="Targets (source:value, source?query, or URL); reads stdin if empty."
        ),
    ] = [],  # noqa: B006 - cyclopts reads it; nothing mutates it
    *,
    categories: Annotated[
        Optional[list[str]],
        cyclopts.Parameter(name="--from", help="Categories to download (e.g. posts,reels or all)."),
    ] = None,
    time: Annotated[
        Optional[str],
        cyclopts.Parameter(help="Date range as SINCE,UNTIL (e.g. 2026-01-01,2026-06-30)."),
    ] = None,
    sync: bool = False,
    again: bool = False,
    limit: Optional[int] = None,
    dry: bool = False,
    verbose: Annotated[
        bool, cyclopts.Parameter(name=["-v", "--verbose"], negative="")
    ] = False,
) -> int:
    """Download each target.

    Args:
        targets: Target specs or URLs; read from stdin when omitted.
        categories: Categories to fetch from each source.
        time: Inclusive date range SINCE,UNTIL.
        sync: Mark targets to be updated by `flnd sync`.
        again: Ignore saved cursors and run from start to finish.
        limit: Cap discovered targets or downloaded posts.
        dry: List targets or files without downloading.
        verbose: Show debug logs and every file event.
    """
    resolved_targets = _read_targets(targets)
    with ui.scope(level="debug" if verbose else None):
        try:
            report = Engine(home=HOME).download(
                resolved_targets,
                categories=categories,
                time=Range() if time is None else time,
                limit=limit,
                dry=dry,
                sync=sync,
                again=again,
            )
        except (TargetError, ValueError) as error:
            raise cli.UsageError(str(error)) from error
    if not dry:
        ui.ok("Complete. {files}", files=text.count(report.files, "file"))
    return 0 if report.ok else 1


@app.command("sync")
def sync(
    targets: list[str] = [],  # noqa: B006 - cyclopts reads it; nothing mutates it
    *,
    verbose: Annotated[
        bool, cyclopts.Parameter(name=["-v", "--verbose"], negative="")
    ] = False,
) -> int:
    """Bring synced targets up to date: fetch new posts and sum up.

    Args:
        targets: Only these targets; every synced target when omitted.
        verbose: Show debug logs and every file event.
    """
    with ui.scope(level="debug" if verbose else None):
        report = Engine(home=HOME).sync(targets)
    return 0 if report.ok else 1


target_group = app.group("target", help="Saved targets and their sync state.")


@target_group.command("ls")
def target_ls() -> int:
    """List every known target with sync state, settings, disk size, and category status."""
    items = Engine(home=HOME).target_ls()
    if not items:
        ui.info("no targets")
        return 0
    table = Table(title="Targets")
    for col in ("Target", "Sync", "Stats", "Last", "Categories"):
        table.add_column(col)
    for item in items:
        stats = (
            text.count(item.seen, "seen")
            if item.query
            else f"{text.count(item.files, 'file')}, {text.size(item.size)}"
        )
        cats = ", ".join(f"{cat}: {st}" for cat, st in item.categories.items())
        table.add_row(
            str(item.target),
            "on" if item.sync else "off",
            stats,
            item.at[:10] if item.at else "",
            cats,
        )
    ui.raw.print(table)
    return 0


@target_group.command("sync-on")
def target_sync_on(targets: list[str]) -> int:
    """Enable syncing for each known target."""
    if not targets:
        raise cli.UsageError("give at least one target")
    for target in Engine(home=HOME).target_sync(targets, enabled=True):
        ui.ok("{target}: sync on", target=str(target))
    return 0


@target_group.command("sync-off")
def target_sync_off(targets: list[str]) -> int:
    """Disable syncing for each known target."""
    if not targets:
        raise cli.UsageError("give at least one target")
    for target in Engine(home=HOME).target_sync(targets, enabled=False):
        ui.ok("{target}: sync off", target=str(target))
    return 0


@target_group.command("del")
def target_del(targets: list[str]) -> int:
    """Remove saved targets from `targets.db` (downloaded files are kept)."""
    if not targets:
        raise cli.UsageError("give at least one target")
    for target in Engine(home=HOME).target_del(targets):
        ui.ok("{target}: removed", target=str(target))
    return 0


auth_group = app.group("auth", help="Logins and saved credentials.")


@auth_group.command("login")
def login_command() -> int:
    """Log in to a source interactively."""
    if not (sys.stdin.isatty() and ui.raw.is_terminal):
        raise cli.UsageError("flnd auth login asks its questions in a terminal")
    engine = Engine(home=HOME)
    code = _ask_source(engine)
    source = SOURCES[code]
    methods = source.login_methods
    method = (
        methods[0]
        if len(methods) == 1
        else prompt.choose(
            f"Log in to {source.name}",
            {METHOD_LABELS.get(name, name): name for name in methods},
        )
    )
    answers: dict[str, Optional[str]] = {}
    if code in engine.cookie_logins:
        answers = _ask_cookie_login(engine, method)
    try:
        message = engine.login(code, method, **answers)
    except LoginError as error:
        ui.error("{error}", error=error)
        return 1
    if message:
        ui.ok(message)
    return 0


@auth_group.command("logout")
def logout(sources: list[str] = []) -> int:  # noqa: B006 - never mutated
    """Remove saved logins for the given sources, or `all`."""
    engine = Engine(home=HOME)
    if not sources:
        logged_in = [code for code, state in engine.logins.items() if state == "logged in"]
        msg = f"logged in: {', '.join(logged_in)}" if logged_in else "no source is logged in"
        raise cli.UsageError(f"give the sources to log out of, or all; {msg}")
    bad = [code for code in sources if code != "all" and code not in SOURCES]
    if bad:
        raise cli.UsageError(f"unknown source {bad[0]!r}; use all or {', '.join(SOURCES)}")
    everything = "all" in sources
    removed = engine.logout(sources)
    for code in SOURCES if everything else dict.fromkeys(sources):
        if code in removed:
            ui.ok("{code}: logged out", code=code)
        elif not everything:
            ui.info("{code}: not logged in", code=code)
    return 0


@auth_group.command("ls")
def auth_ls() -> int:
    """List each site with its login state, origin, and expiring cookies."""
    table = Table(title="Auth")
    for col in ("Code", "Site", "State", "Origin", "Expiring"):
        table.add_column(col)
    for item in Engine(home=HOME).auth_ls():
        exp = ", ".join(
            f"{name} expired" if rem <= 0 else f"{name} in {text.duration(rem)}"
            for name, rem in item.expiring
        )
        table.add_row(item.code, item.name, item.state, item.origin, exp)
    ui.raw.print(table)
    return 0


def _read_targets(targets: Sequence[str]) -> list[str]:
    if targets:
        return list(targets)
    if not sys.stdin.isatty():
        lines = [
            line.strip()
            for line in sys.stdin
            if line.strip() and not line.strip().startswith("#")
        ]
        if lines:
            return lines
    raise cli.UsageError("give at least one target, or pipe targets on stdin")


def _ask_cookie_login(engine: Engine, method: str) -> dict[str, Optional[str]]:
    if method == "file":
        return {"path": prompt.ask("Path to cookies.txt or .json", validate=_readable).strip()}
    choices = {label: name for name, label in engine.browsers.items()}
    return {"browser": prompt.choose("Read cookies from", choices), "profile": None}


def _ask_source(engine: Engine) -> str:
    logins = engine.logins
    options = {
        f"{source.name} ({code})" + (f" · {logins[code]}" if code in logins else ""): code
        for code, source in SOURCES.items()
    }
    return prompt.choose("Log in to", options)


def _readable(raw: str) -> bool | str:
    path = Path(raw.strip()).expanduser()
    return True if path.is_file() else f"no file at {raw.strip() or '(empty)'}"
