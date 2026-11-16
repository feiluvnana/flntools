"""The flnd command line: flags in, one library call, events out as lines on the terminal.

    flnd download ins:example_user --only reels --when 2026
    flnd follow tik:example_user
    flnd sync
    flnd login ins chrome

Who calls it: `flnd` (the console script) and `python -m flnd`, through `main`. What it calls:
the library alone (`flnd.open`, its `Run` and events), so anything the command line does, a
script can do too; `flnd.sources._manifest` for the sources `--help` and `flnd sources` list;
and the library modules `flnd._prompt` (`flnd login`'s questions) and `flnd._ui` (the level of
flnd's own log lines, and the live view).

How a command runs (`main`):
1. argparse reads the flags. A mistake prints the usage and exits 2; `--help` prints the help
   made from each command's docstring, with the target forms and categories of every source,
   and imports no source, no engine and no Rich (it stays under 40 ms).
2. The command function opens the home (`--home`, else `$FLND_HOME`, else `./.flnd`) and makes
   one library call with the flags.
3. A run's events become output (`_Plain`, `_Inline` or `_Json`): a line per saved or failed
   file, a live view on a terminal, or one JSON object per line. The end of a run prints a
   short summary, and its failures decide the exit code.
4. Whatever ends the command becomes an exit code (`exit_code`): 0 ok, 1 some files or targets
   failed, 2 usage, 3 a login is needed, 4 flnd itself stopped (a full disk, a missing tool, a
   broken source, a bug), 130 Ctrl-C. A `flnd.Error` prints its message and its hint, never a
   traceback; a bug prints one line naming a crash file, or the traceback with `-v`.

Rules a change must keep:
- Import nothing heavy at the top: argparse, os and sys only. Everything else is imported by
  the command that needs it, so `flnd --help` pays for none of it.
- One flag, one spelling: no aliases. The docs' CLI page (docs/cli) names every command and
  flag, and `tests/test_docs.py` checks it against this parser.
- `--json` never asks a question: a command that would ask is a usage error instead.
"""

from __future__ import annotations

import argparse
import os
import sys
from collections.abc import Callable, Sequence
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    import flnd

__all__ = ["exit_code", "main", "parser"]

# The exit codes (docs/cli). A cron job can tell "some files failed" (1) from "flnd itself
# stopped" (4).
OK, FAILED, USAGE, LOGIN, BROKE, STOPPED = 0, 1, 2, 3, 4, 130
# Each error class's exit code when it ends a command, by class name, so that reading it needs
# no import. The first class in an error's MRO that is listed wins; an unlisted `flnd.Error`
# exits 4.
CODES = {
    "UsageError": USAGE,
    "LoginError": LOGIN,
    "SiteError": FAILED,
    "NetworkError": FAILED,
    "DiskError": BROKE,
    "ToolError": BROKE,
    "SourceError": BROKE,
}
# The views of a run: plain lines, or a live view on a terminal (the default there).
VIEWS = ("plain", "inline")


# ---------------------------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------------------------

def main(argv: Sequence[str] | None = None) -> int:
    """Run one flnd command and return its exit code; nothing escapes but the code.

    Args:
        argv: the arguments after the program's name; None reads `sys.argv`.

    Returns:
        The exit code: the command's own, or the one what ended it maps to (`exit_code`).
    """
    try:
        args = parser().parse_args(argv)
    except SystemExit as stop:
        # argparse exits 0 after --help and --version, and 2 on a usage mistake.
        return stop.code if isinstance(stop.code, int) else USAGE
    _settle(args)
    if args.command is None:
        parser().print_help()
        return OK
    try:
        return args.command(args)
    except KeyboardInterrupt:
        # `flnd.Stopped` is one: Ctrl-C during a run, which the run already stopped cleanly.
        print("stopped", file=sys.stderr)
        return STOPPED
    except Exception as error:
        import flnd

        if isinstance(error, flnd.Error):
            _report(error)
            return exit_code(error)
        return _crashed(error, args)


def exit_code(error: BaseException | None) -> int:
    """The exit code an error ends a command with, by its class; 0 for None.

    Args:
        error: what ended the command; None when nothing did.

    Returns:
        0 for None, 130 for Ctrl-C (`flnd.Stopped`), the code `CODES` gives a `flnd.Error`'s
        class or its nearest listed base, and 4 for anything else (a bug).

    Example:
        >>> import flnd
        >>> exit_code(flnd.LoginError("ins: not logged in")), exit_code(flnd.Blocked("wait"))
        (3, 1)
    """
    if error is None:
        return OK
    if isinstance(error, KeyboardInterrupt):
        return STOPPED
    for kind in type(error).__mro__:
        if kind.__module__ == "flnd.errors" and kind.__name__ in CODES:
            return CODES[kind.__name__]
    return BROKE


def _settle(args: argparse.Namespace) -> None:
    """Give each global flag left out its default; `--json` implies the plain view.

    The global flags are accepted before and after the command, so both parsers leave them
    out unless given (`argparse.SUPPRESS`), and this sets what is still missing.

    Args:
        args: the parsed flags, changed in place.
    """
    for name, default in (("home", None), ("ui", None), ("verbose", False), ("json", False)):
        if not hasattr(args, name):
            setattr(args, name, default)
    if not hasattr(args, "command"):
        args.command = None
    if args.json:
        args.ui = "plain"
    elif args.ui is None:
        # Inline only where a person watches: the view draws on standard error.
        args.ui = "inline" if sys.stderr.isatty() else "plain"


def _report(error: Any) -> None:
    """Print an error a person can act on: its message, then its hint; never a traceback."""
    print(f"error: {error}", file=sys.stderr)
    if error.hint:
        print(f"  {error.hint}", file=sys.stderr)


def _crashed(error: Exception, args: argparse.Namespace) -> int:
    """Report a bug: one line naming a crash file that holds the traceback, or with `-v` the
    traceback itself.

    The crash file is `<home>/logs/crash-<time>.txt`. When it can't be written (a read-only
    disk), the traceback prints instead, so it is never lost.

    Args:
        error: the exception that escaped the command.
        args: the parsed flags, for `--home` and `-v`.

    Returns:
        4, the exit code of a bug.
    """
    import datetime
    import traceback
    from pathlib import Path

    trace = "".join(traceback.format_exception(error))
    line = f"unexpected {type(error).__name__}: {error}"
    home = Path(args.home or os.environ.get("FLND_HOME") or ".flnd").expanduser()
    path = home / "logs" / f"crash-{datetime.datetime.now():%Y%m%d-%H%M%S}.txt"
    if not args.verbose:
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(trace, encoding="utf-8")
        except OSError:
            pass
        else:
            print(f"error: {line}; the details are in {path}", file=sys.stderr)
            return BROKE
    sys.stderr.write(trace)
    print(f"error: {line}", file=sys.stderr)
    return BROKE


# ---------------------------------------------------------------------------------------------
# The commands: each docstring is the command's help
# ---------------------------------------------------------------------------------------------

def download(args: argparse.Namespace) -> int:
    """Download what the targets have now, once.

    Nothing is followed: a later `flnd sync` doesn't take these targets (`flnd follow` does).
    With no targets, they are read from standard input, one per line; blank lines and lines
    starting with # are skipped.

    Args:
        args: the parsed flags.

    Returns:
        The run's exit code.
    """
    _no_exec(args)
    targets = _targets(args.targets)
    with _open(args) as fl:
        run = fl.download(targets, only=args.only, when=args.when, limit=args.limit,
                          compress=args.compress, dry=args.dry)
        return _watch(run, args)


def follow(args: argparse.Namespace) -> int:
    """Follow targets, so `flnd sync` keeps them up to date, and download what they have now.

    --only is kept with each target for later syncs; following again replaces it. --when
    bounds this first download only.

    Args:
        args: the parsed flags.

    Returns:
        The run's exit code.
    """
    targets = _targets(args.targets)
    with _open(args) as fl:
        run = fl.follow(targets, only=args.only, when=args.when, compress=args.compress)
        return _watch(run, args)


def sync(args: argparse.Namespace) -> int:
    """Download what is new for every followed target, or for the ones named.

    Each target keeps the --only it was followed with. A category stops after a few saved
    posts in a row, so a sync with nothing new costs a few requests per target.

    Args:
        args: the parsed flags.

    Returns:
        The run's exit code.
    """
    _no_exec(args)
    with _open(args) as fl:
        return _watch(fl.sync(args.targets, compress=args.compress), args)


def unfollow(args: argparse.Namespace) -> int:
    """Stop following targets: `flnd sync` leaves them out. Their files and history stay.

    Args:
        args: the parsed flags.

    Returns:
        0.
    """
    with _open(args) as fl:
        for target in fl.unfollow(args.targets):
            _line(args, {"type": "Unfollowed", "target": str(target)}, f"{target}: unfollowed")
    return OK


def ls(args: argparse.Namespace) -> int:
    """List the targets this home knows: followed or not, files, size, last download.

    Args:
        args: the parsed flags.

    Returns:
        0.
    """
    from flnd import _text

    with _open(args) as fl:
        rows = fl.targets()
    if args.targets:
        named = set(args.targets)
        rows = [row for row in rows if str(row.target) in named]
    if args.json:
        for row in rows:
            _json(row)
        return OK
    if not rows:
        print("no targets yet")
        return OK
    table = []
    for row in rows:
        held = (_text.count(row.seen, "target") + " found" if row.query
                else f"{_text.count(row.files, 'file')}, {_text.size(row.size)}")
        states = ", ".join(f"{name}: {state}" for name, state in row.categories.items())
        table.append((str(row.target), "followed" if row.follow else "-", held,
                      row.at[:10] or "never", states))
    _table(("target", "follow", "files", "last", "categories"), table)
    return OK


def status(args: argparse.Namespace) -> int:
    """Show the last run: what failed and why. Not built yet.

    Args:
        args: the parsed flags.

    Returns:
        Never returns today.

    Raises:
        UsageError: always, until the home keeps a failure report.
    """
    import flnd

    raise flnd.UsageError("status isn't built yet",
                          hint="the end of each run lists what failed")


def retry(args: argparse.Namespace) -> int:
    """Download again the files the last run failed on. Not built yet.

    Args:
        args: the parsed flags.

    Returns:
        The run's exit code, once it is built.
    """
    with _open(args) as fl:
        return _watch(fl.retry(), args)


def check(args: argparse.Namespace) -> int:
    """Compare the history with the disk: files missing, unknown, or of the wrong size.

    Not built yet.

    Args:
        args: the parsed flags.

    Returns:
        0 once it is built.
    """
    with _open(args) as fl:
        fl.check(fix=args.fix)
    return OK


def login(args: argparse.Namespace) -> int:
    """Log in to a source, or with no source list every source's login.

    HOW is a browser to read the site's cookies from (chrome, firefox, edge, brave, chromium),
    the path of a cookies.txt or JSON cookie export, or the source's own way (tlg: browser, qr,
    phone). Left out in a terminal, it is asked with numbered choices. The login is saved
    encrypted in the home.

    Args:
        args: the parsed flags.

    Returns:
        0.

    Raises:
        UsageError: `how` is left out where nobody can be asked (no terminal, or --json).
    """
    if args.source is None:
        return _logins(args)
    how = args.how or _ask_how(args)
    with _open(args) as fl:
        done = fl.login(args.source, how)
    origin = f" ({done.origin})" if done.origin else ""
    _line(args, done, f"{done.source}: {done.state}{origin}")
    return OK


def logout(args: argparse.Namespace) -> int:
    """Remove a source's saved login.

    Args:
        args: the parsed flags.

    Returns:
        0.
    """
    with _open(args) as fl:
        removed = fl.logout(args.source)
    message = "logged out" if removed else "was not logged in"
    _line(args, {"type": "Logout", "source": args.source, "removed": removed},
          f"{args.source}: {message}")
    return OK


def sources(args: argparse.Namespace) -> int:
    """List the sources: their names, the targets they read, and their categories.

    Args:
        args: the parsed flags.

    Returns:
        0.
    """
    from flnd.sources import _manifest

    for code, entry in _manifest.SOURCES.items():
        if args.json:
            _json({"type": "Source", "code": code, "name": entry["name"],
                   "domains": entry["domains"], "forms": entry["forms"],
                   "categories": entry["categories"], "logins": entry["logins"]})
            continue
        print(f"{code}  {entry['name']}")
        print(f"  targets:    {', '.join(entry['forms'][:3])}" +
              (", …" if len(entry["forms"]) > 3 else ""))  # type: ignore[arg-type]
        print(f"  categories: {', '.join(entry['categories'])}")  # type: ignore[arg-type]
        print(f"  logins:     {', '.join(entry['logins'])}")  # type: ignore[arg-type]
    return OK


# ---------------------------------------------------------------------------------------------
# The parser
# ---------------------------------------------------------------------------------------------

def parser() -> argparse.ArgumentParser:
    """The whole command line: the global flags, then one sub-parser per command.

    Each command's help is its function's docstring: the first line in the list of commands,
    the paragraphs before `Args:` on its own `--help`.

    Returns:
        The parser; `parse_args` sets `command` to the command's function.
    """
    import flnd

    top = argparse.ArgumentParser(
        prog="flnd", description=_about(), epilog=_epilog(),
        formatter_class=argparse.RawDescriptionHelpFormatter, parents=[_globals()],
    )
    top.add_argument("-V", "--version", action="version", version=f"flnd {flnd.__version__}")
    verbs = top.add_subparsers(title="commands", metavar="<command>")

    def command(function: Callable[[argparse.Namespace], int]) -> argparse.ArgumentParser:
        summary, _, body = _help(function).partition("\n")
        sub = verbs.add_parser(function.__name__, help=summary, description=_help(function),
                               formatter_class=argparse.RawDescriptionHelpFormatter,
                               parents=[_globals()])
        sub.set_defaults(command=function)
        return sub

    sub = command(download)
    sub.add_argument("targets", nargs="*", metavar="target",
                     help="ins:example_user, a link, or mht?words; read from stdin when none")
    _only(sub)
    _when(sub)
    sub.add_argument("--limit", type=int, metavar="N",
                     help="at most N posts per target, or N targets per search")
    _compress(sub)
    sub.add_argument("--dry", action="store_true",
                     help="list what would be downloaded, and download and write nothing")
    _exec(sub)

    sub = command(follow)
    sub.add_argument("targets", nargs="*", metavar="target",
                     help="the targets to follow; read from stdin when none")
    _only(sub)
    _when(sub)
    _compress(sub)

    sub = command(sync)
    sub.add_argument("targets", nargs="*", metavar="target",
                     help="only these followed targets; every one when none")
    _compress(sub)
    _exec(sub)

    sub = command(unfollow)
    sub.add_argument("targets", nargs="+", metavar="target", help="the targets to stop following")

    sub = command(ls)
    sub.add_argument("targets", nargs="*", metavar="target", help="only these targets")

    command(status)
    command(retry)

    sub = command(check)
    sub.add_argument("--fix", action="store_true", help="also repair what can be repaired")

    sub = command(login)
    sub.add_argument("source", nargs="?", help="the source's code (ins); none lists the logins")
    sub.add_argument("how", nargs="?",
                     help="a browser, a cookie file's path, or the source's own way (qr, phone)")

    sub = command(logout)
    sub.add_argument("source", help="the source's code (ins)")

    command(sources)
    return top


def _globals() -> argparse.ArgumentParser:
    """The flags every command takes, before or after its name.

    Their defaults are `SUPPRESS`, so a flag given before the command isn't overwritten by the
    command's own parser; `_settle` fills in the real defaults after parsing.

    Returns:
        A parser to use as a parent; it adds no `--help` of its own.
    """
    shared = argparse.ArgumentParser(add_help=False)
    group = shared.add_argument_group("global flags")
    group.add_argument("--home", metavar="DIR", default=argparse.SUPPRESS,
                       help="where downloads, history and logins live "
                            "(default: $FLND_HOME, else ./.flnd)")
    group.add_argument("--ui", choices=VIEWS, default=argparse.SUPPRESS,
                       help="how a run shows: plain lines, or a live view "
                            "(default: inline on a terminal)")
    group.add_argument("-v", "--verbose", action="store_true", default=argparse.SUPPRESS,
                       help="show flnd's debug lines, and a bug's traceback")
    group.add_argument("--json", action="store_true", default=argparse.SUPPRESS,
                       help="print events and rows as JSON lines; implies --ui plain "
                            "and never asks")
    return shared


def _only(sub: argparse.ArgumentParser) -> None:
    sub.add_argument("--only", action="append", metavar="CATS",
                     help="only these categories (reels,posts); every category when left out")


def _when(sub: argparse.ArgumentParser) -> None:
    sub.add_argument("--when", metavar="WHEN",
                     help="posts from 2026, 2026-06, 2026-06-15, 2026-01..2026-03, or 30d")


def _compress(sub: argparse.ArgumentParser) -> None:
    sub.add_argument("--compress", action="store_true",
                     help="shrink photos and videos as they land when a smaller copy looks the "
                          "same, and remove each archive once it is extracted")


def _exec(sub: argparse.ArgumentParser) -> None:
    sub.add_argument("--exec", metavar="CMD",
                     help="run CMD for each saved file, without a shell (not built yet)")


def _help(function: Callable[..., Any]) -> str:
    """A command's help: its docstring up to `Args:`, dedented."""
    import inspect

    text = inspect.cleandoc(function.__doc__ or "")
    return text.split("\n\nArgs:")[0].strip()


def _about() -> str:
    """The first line of `flnd --help`, naming every built-in source."""
    from flnd.sources import _manifest

    names = [str(entry["name"]) for entry in _manifest.SOURCES.values()]
    return f"Download media from {', '.join(names[:-1])} and {names[-1]}."


def _epilog() -> str:
    """The end of `flnd --help`: every target form and category of every source.

    It is read from the manifest, so no source is imported.

    Returns:
        The text, one form per line.
    """
    from flnd.sources import _manifest

    lines = ["targets: <source>:<name, id or link>, <source>?<words> for a search, or a link",
             ""]
    for code, entry in _manifest.SOURCES.items():
        lines.append(f"  {code} ({entry['name']})")
        lines.extend(f"    {form}" for form in entry["forms"])  # type: ignore[attr-defined]
        lines.append(f"    --only: {', '.join(entry['categories'])}")  # type: ignore[arg-type]
    lines += ["", "examples:",
              "  flnd download ins:example_user --only reels --when 2026",
              "  flnd follow https://www.tiktok.com/@example_user",
              "  flnd sync",
              "  flnd login ins chrome",
              "", "Run `flnd <command> --help` for a command's flags."]
    return "\n".join(lines)


# ---------------------------------------------------------------------------------------------
# Helpers of the commands
# ---------------------------------------------------------------------------------------------

def _open(args: argparse.Namespace) -> flnd.Flnd:
    """The library over `--home`; the caller uses it in a `with`."""
    import flnd

    return flnd.open(args.home)


def _targets(given: Sequence[str]) -> list[str]:
    """The targets on the command line, else the lines of a piped standard input.

    Args:
        given: the targets typed after the command.

    Returns:
        The targets, in order.

    Raises:
        UsageError: there are none, and standard input is a terminal or holds none.
    """
    if given:
        return list(given)
    if not sys.stdin.isatty():
        lines = [line.strip() for line in sys.stdin]
        found = [line for line in lines if line and not line.startswith("#")]
        if found:
            return found
    import flnd

    raise flnd.UsageError("give at least one target, or pipe targets on stdin")


def _no_exec(args: argparse.Namespace) -> None:
    """Refuse `--exec`, which has no implementation yet."""
    if args.exec is not None:
        import flnd

        raise flnd.UsageError("--exec isn't built yet", hint="leave --exec out")


def _ask_how(args: argparse.Namespace) -> str:
    """Ask how to log in to `args.source`, with numbered choices.

    A source that logs in by cookies offers each browser and a cookie file (whose path is then
    asked); a source with its own login (tlg) offers its own ways.

    Args:
        args: the parsed flags, with `source` set.

    Returns:
        What `Flnd.login` takes as `how`.

    Raises:
        UsageError: an unknown source, `--json`, or no terminal to ask in.
    """
    import flnd
    from flnd import _prompt
    from flnd.sources import _manifest

    entry = _manifest.SOURCES.get(args.source)
    if entry is None:
        codes = ", ".join(_manifest.SOURCES)
        raise flnd.UsageError(f"unknown source {args.source!r}. Sources: {codes}")
    ways: tuple[str, ...] = entry["logins"]  # type: ignore[assignment]
    hint = f"name it: flnd login {args.source} <how>, with how one of {_ways(ways)}"
    if args.json or not _prompt.interactive():
        raise flnd.UsageError(f"{args.source}: how to log in?", hint=hint)
    if "file" not in ways:
        return _prompt.choose(f"Log in to {entry['name']} with", list(ways))
    from flnd._cookies import BROWSERS

    choices = {f"cookies from {browser}": browser for browser in BROWSERS}
    choices["a cookie file (cookies.txt or JSON)"] = "file"
    picked = _prompt.choose(f"Log in to {entry['name']} with", choices)
    if picked == "file":
        return _prompt.ask("Path of the cookie file").strip()
    return picked


def _ways(ways: Sequence[str]) -> str:
    """A source's login ways as `flnd login` takes them, for a hint."""
    if "file" in ways:
        return "chrome, firefox, edge, brave, chromium, or a cookie file's path"
    return ", ".join(ways)


def _logins(args: argparse.Namespace) -> int:
    """`flnd login` with no source: a table of every source's login.

    Args:
        args: the parsed flags.

    Returns:
        0.
    """
    from flnd import _text

    with _open(args) as fl:
        found = fl.logins
    if args.json:
        for row in found:
            _json(row)
        return OK
    table = []
    for row in found:
        soon = ", ".join(f"{name} expired" if left <= 0 else f"{name} in {_text.duration(left)}"
                         for name, left in row.expiring)
        table.append((row.source, row.name, row.state, row.origin, soon))
    _table(("source", "site", "login", "from", "expiring"), table)
    return OK


def _line(args: argparse.Namespace, value: Any, text: str) -> None:
    """Print one result: as JSON with `--json`, else as `text`."""
    if args.json:
        _json(value)
    else:
        print(text)


def _json(value: Any) -> None:
    """Print one value (an event, a row, or a plain dict) as one line of JSON."""
    from flnd._events import encode

    sys.stdout.write(encode(value).decode() + "\n")
    sys.stdout.flush()


def _table(header: Sequence[str], rows: Sequence[Sequence[str]]) -> None:
    """Print rows as plain columns, padded to the widest cell of each."""
    widths = [max(len(str(cell)) for cell in column) for column in zip(header, *rows)]
    for row in (header, *rows):
        print("  ".join(str(cell).ljust(width) for cell, width in zip(row, widths)).rstrip())


# ---------------------------------------------------------------------------------------------
# Watching a run
# ---------------------------------------------------------------------------------------------

def _watch(run: flnd.Run, args: argparse.Namespace) -> int:
    """Read a run's events into the chosen view, print its end, and return its exit code.

    flnd's own log lines are held to warnings for the run (a wait, a login about to expire),
    or opened to debug lines with `-v`: the events already say what each file did.

    Args:
        run: the run, not started yet.
        args: the parsed flags: `--json`, `--ui`, `-v`.

    Returns:
        0 when nothing failed, 3 when a target needs a login, else 1.

    Raises:
        Error: what stopped the whole run (a full disk), from the run.
        Stopped: Ctrl-C ended it.
    """
    from flnd._ui import ui

    view: _View
    if args.json:
        view = _Json()
    elif args.ui == "inline":
        view = _Inline(verbose=args.verbose)
    else:
        view = _Plain(verbose=args.verbose)
    with ui.scope(level="debug" if args.verbose else "warn"), view:
        for event in run:
            view.show(event)
    report = run.report
    view.end(report)
    if any(_kind(failed.error) == "LoginError" for failed in report.failed):
        return LOGIN
    return FAILED if report.failed else OK


def _kind(error: BaseException) -> str:
    """The name of the nearest class of `error` that `CODES` lists; "" for none."""
    return next((kind.__name__ for kind in type(error).__mro__ if kind.__name__ in CODES), "")


class _View:
    """How a run shows: `show` each event as it comes, then `end` with the report.

    A view is a context manager, so a live display stops even when the run raises.

    Args:
        verbose: also show what a person rarely needs (each skipped file).
    """

    def __init__(self, *, verbose: bool = False) -> None:
        self.verbose = verbose

    def __enter__(self) -> _View:
        return self

    def __exit__(self, *exc: object) -> None:
        return None

    def show(self, event: Any) -> None:
        """Show one event."""

    def end(self, report: Any) -> None:
        """Show the run's end: what it saved, and what failed.

        Args:
            report: the run's `flnd.Report`.
        """
        from flnd import _text

        if report.planned:
            print(f"planned {_text.count(report.planned, 'file')}; nothing was downloaded")
            return
        done = [f"{_text.count(report.saved, 'file')} saved"]
        if report.bytes:
            done[0] += f" ({_text.size(report.bytes)})"
        if report.skipped:
            done.append(f"{report.skipped} already saved")
        print("done: " + ", ".join(done))
        if report.failed:
            kinds: dict[str, int] = {}
            for failed in report.failed:
                name = type(failed.error).__name__
                kinds[name] = kinds.get(name, 0) + 1
            listed = ", ".join(f"{count} {name}" for name, count in kinds.items())
            print(f"{len(report.failed)} failed: {listed}", file=sys.stderr)


class _Plain(_View):
    """One line per saved, failed or planned file on standard output; no Rich."""

    def show(self, event: Any) -> None:
        """Print the event's line, if it has one: a `Progress` has none.

        Args:
            event: one event of the run.
        """
        name = type(event).__name__
        if name == "Saved":
            print(f"saved {_shown(event.path)}")
        elif name == "Failed":
            _failed(event, print)
        elif name == "Planned":
            if event.path is None:
                print(f"found {event.target}")
            else:
                print(f"{event.state} {_shown(event.path)}")
        elif name == "Skipped" and self.verbose:
            print(f"skipped {_shown(event.path) if event.path else event.target}")


class _Json(_View):
    """Each event as one JSON object per line, then the report as `{"type": "Report", ...}`."""

    def show(self, event: Any) -> None:
        _json(event)

    def end(self, report: Any) -> None:
        from flnd._events import encode

        body = encode(report).decode()
        sys.stdout.write('{"type":"Report",' + body[1:] + "\n")
        sys.stdout.flush()


class _Inline(_View):
    """A live view on standard error: one row per target and category, redrawn 10 times a
    second, with failures printed above it as they happen.

    `show` only stores the latest counts (a dict write); Rich redraws them on its own thread,
    so a burst of events costs no more than one frame. The rows are left on the screen at the
    end, so the scrollback keeps them.

    Args:
        verbose: as `_View` takes it.
    """

    # Frames a second; each frame redraws every row from the latest counts.
    FPS = 10

    def __init__(self, *, verbose: bool = False) -> None:
        super().__init__(verbose=verbose)
        self._rows: dict[tuple[str, str], Any] = {}
        self._live: Any = None

    def __enter__(self) -> _Inline:
        from rich.live import Live

        from flnd._ui import ui

        self._live = Live(self, console=ui.raw, refresh_per_second=self.FPS, transient=False)
        self._live.start()
        return self

    def __exit__(self, *exc: object) -> None:
        if self._live is not None:
            self._live.stop()

    def show(self, event: Any) -> None:
        """Keep a `Progress`'s counts for the next frame; print a failure or a plan above.

        Args:
            event: one event of the run.
        """
        name = type(event).__name__
        if name == "Progress":
            self._rows[(str(event.target), event.category)] = event
        elif name == "Failed":
            _failed(event, self._print)
        elif name == "Planned":
            self._print(f"{event.state} {_shown(event.path)}" if event.path
                        else f"found {event.target}")

    def _print(self, text: str) -> None:
        """Print a line above the live rows, as it is: a name from a site is never markup."""
        self._live.console.print(text, markup=False, highlight=False)

    def __rich__(self) -> Any:
        from rich.table import Table

        from flnd import _text

        table = Table.grid(padding=(0, 2))
        for (target, category), step in list(self._rows.items()):
            if not step.listed:
                state = "listing"
            elif step.found > step.saved + step.skipped + step.failed:
                state = "finishing"
            else:
                state = "done"
            counts = f"{step.saved}/{step.found} saved"
            if step.skipped:
                counts += f", {step.skipped} skipped"
            if step.failed:
                counts += f", {step.failed} failed"
            table.add_row(target, category or "-", counts, _text.size(step.bytes), state)
        return table


def _failed(event: Any, write: Callable[..., None]) -> None:
    """Print a failed file or target: its message, then its hint.

    Args:
        event: the `flnd.Failed` event.
        write: what prints one line (`print`, or the live view's console).
    """
    where = f"{event.target} {event.post}" if event.post else str(event.target)
    write(f"failed {where}: {event.error}")
    hint = getattr(event.error, "hint", "")
    if hint:
        write(f"  {hint}")


def _shown(path: Any) -> str:
    """A path as short as it can be shown: relative to the current folder.

    Args:
        path: the path of a saved or planned file.

    Returns:
        The relative path; the path as it is when it has no relative form.
    """
    try:
        return os.path.relpath(path)
    except ValueError:
        # On Windows, a path on another drive has no relative form.
        return str(path)
