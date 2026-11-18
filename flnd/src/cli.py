"""The flnd command line: flags in, one library call, events out as lines on the terminal.

    flnd download ins:example_user --only reels --when 2026
    flnd follow tik:example_user
    flnd sync
    flnd login ins chrome

Who calls it: `flnd` (the console script) and `python -m flnd`, through `main`. What it calls:
the library alone (`flnd.open`, its `Run` and events), so anything the command line does, a
script can do too; `flnd.sources._manifest` for the built-in sources `--help` lists;
`flnd.ui.out` for every line it prints; `flnd.ui.forms` (`flnd login`'s questions) and
`flnd.ui.log` (the level of flnd's own log lines and progress bars).

How a command runs (`main`):
1. argparse reads the flags. A mistake prints an error and exits 2; `--help` prints the help
   made from each command's docstring, drawn in the theme by `flnd.ui.out.help`: `flnd -h`
   fits in 24 lines, and `flnd download -h` and `flnd follow -h` add a row per source.
2. The command function opens the home (`--home`, else `$FLND_HOME`, else `./.flnd`) and makes
   one library call with the flags.
3. A run's own display is the summary table the library draws on a terminal: one row per
   target and category, with the counts of its files (queued, downloading, saved, failed) and
   the spinner on the rows still going. Events go to the journal and the view: a dry run prints
   its listing, `--json` prints one JSON object per line. With `--exec CMD`, each `Saved`
   event also runs CMD, without a shell (`_Exec`). The end of a run prints a short summary, and
   its failures decide the exit code.
4. Whatever ends the command becomes an exit code (`exit_code`): 0 ok, 1 some files or targets
   failed, 2 usage, 3 a login is needed, 4 flnd itself stopped (a full disk, a missing tool, a
   broken source, a bug), 130 Ctrl-C. A `flnd.Error` prints its message and its hint, never a
   traceback; a bug prints one line naming a crash file, or the traceback with `-v`.

Rules a change must keep:
- Import nothing heavy at the top: argparse, os and sys only. Everything else is imported by
  the command that needs it, so `flnd --help` pays for none of it.
- One flag, one spelling: no aliases. The docs' CLI page (src/docs/cli) names every command and
  flag, and `tests/test_docs.py` checks it against this parser.
- `--json` never asks a question: a command that would ask is a usage error instead. Every
  line it prints on standard output is one JSON object with a `type`, an error included.
- `--exec` never starts a shell: CMD is split once with `shlex`, and a value from a site (a
  file name, a post id) only ever fills a placeholder inside one argument.
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

# The exit codes (src/docs/cli). A cron job can tell "some files failed" (1) from "flnd itself
# stopped" (4).
OK, FAILED, USAGE, LOGIN, BROKE, STOPPED = 0, 1, 2, 3, 4, 130
# Each error class's exit code when it ends a command, by class name: the first of these an
# error is an instance of wins, and anything else exits 4. Read by `isinstance` on flnd's own
# classes, so a plugin's own class that is merely named `LoginError` gets no special code.
CODES = (
    ("Stopped", STOPPED),
    ("UsageError", USAGE),
    ("LoginError", LOGIN),
    ("DiskError", BROKE),
    ("ToolError", BROKE),
    ("SourceError", BROKE),
    ("Error", FAILED),
)
# ---------------------------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------------------------

def main(argv: Sequence[str] | None = None) -> int:
    """Run one flnd command and return its exit code; nothing escapes but the code.

    Args:
        argv: the arguments after the program's name; None reads the arguments from sys.argv.

    Returns:
        The exit code: the command's own exit code, or the exit code mapped from an error.
    """
    from flnd.ui import out

    try:
        return _main(argv)
    finally:
        out.close()


def _main(argv: Sequence[str] | None) -> int:
    from flnd.ui import out

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
        out.line(("stopped", "warn"), err=True)
        return STOPPED
    except Exception as error:
        import flnd

        if isinstance(error, flnd.Error):
            _report(error, as_json=args.json)
            return exit_code(error)
        return _crashed(error, args)


def exit_code(error: BaseException | None) -> int:
    """The exit code an error ends a command with, by its class; 0 for None.

    Args:
        error: the exception that ended the command; None if the command finished successfully.

    Returns:
        0 if the command succeeded, else the code of the first class in `CODES` the error is an
        instance of: 130 for `flnd.Stopped`, 2, 3, 4 or 1 for a `flnd.Error`, and 4 for anything
        else.

    Example:
        >>> import flnd
        >>> exit_code(flnd.LoginError("ins: not logged in")), exit_code(flnd.Blocked("wait"))
        (3, 1)
    """
    if error is None:
        return OK
    import flnd

    for name, code in CODES:
        if isinstance(error, getattr(flnd, name)):
            return code
    return BROKE


def _settle(args: argparse.Namespace) -> None:
    """Give each global flag left out its default.

    The global flags are accepted before and after the command, so both parsers leave them
    out unless given (`argparse.SUPPRESS`), and this sets what is still missing.

    Args:
        args: the parsed flags, changed in place.
    """
    for name, default in (("home", None), ("verbose", False), ("json", False)):
        if not hasattr(args, name):
            setattr(args, name, default)
    if not hasattr(args, "command"):
        args.command = None


def _report(error: Any, *, as_json: bool = False) -> None:
    """Print an error a person can act on: its message, then its hint; never a traceback.

    Args:
        error: the `flnd.Error` that ended the command.
        as_json: also print it on standard output as `{"type": "Error", ...}`, for `--json`.
    """
    from flnd.ui import out

    # `out.error` first: it gives the error flnd's hint when it has none, which the JSON shows.
    out.error(error)
    if as_json:
        from flnd.engine.events import encode

        out.plain('{"type":"Error","error":' + encode(error).decode() + "}")


def _crashed(error: Exception, args: argparse.Namespace) -> int:
    """Report a bug: "flnd hit a bug" and its one line, then the crash file that holds the
    traceback; with `-v`, the traceback itself.

    The crash file is `<home>/log/crash-<time>.txt`. When it can't be written (a read-only
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

    import flnd
    from flnd.engine.home import LOGS
    from flnd.ui import out

    trace = "".join(traceback.format_exception(error))
    first = (str(error).strip().splitlines() or [""])[0]
    line = f"flnd hit a bug: {type(error).__name__}" + (f": {first}" if first else "")
    home = Path(args.home or os.environ.get("FLND_HOME") or ".flnd").expanduser()
    path = home / LOGS / f"crash-{datetime.datetime.now():%Y%m%d-%H%M%S}.txt"
    if not args.verbose:
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(trace, encoding="utf-8")
        except OSError:
            pass
        else:
            out.error(flnd.Error(line, hint=f"details in {path}"))
            return BROKE
    out.plain(trace, err=True)
    out.error(flnd.Error(line))
    return BROKE


# ---------------------------------------------------------------------------------------------
# The commands: each docstring is the command's help
# ---------------------------------------------------------------------------------------------

def download(args: argparse.Namespace) -> int:
    """Download what the targets have now, once.

    Nothing is followed: a later `flnd sync` doesn't take these targets (`flnd follow` does).
    With no targets, they are read from standard input, one per line; blank lines and lines
    starting with # are skipped.

    --exec CMD runs CMD once for each saved file, after every post-download step, with
    no shell: {path}, {target} and {post} inside an argument become the file's values, which
    are also in $FLND_PATH, $FLND_TARGET and $FLND_POST.

    Args:
        args: the parsed flags.

    Returns:
        The run's exit code.
    """
    # Read before the run is made, so a mistake in CMD is a usage error, not one per file.
    runner = _runner(args)
    targets = _targets(args.targets)
    with _open(args) as fl:
        run = fl.download(targets, only=args.only, when=args.when, limit=args.limit,
                          dry=args.dry)
        return _watch(fl, run, args, runner=runner)


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
        run = fl.follow(targets, only=args.only, when=args.when)
        return _watch(fl, run, args)


def sync(args: argparse.Namespace) -> int:
    """Download what is new for every followed target, or for the ones named.

    Each target keeps the --only it was followed with. A category stops after a few saved
    posts in a row, so a sync with nothing new costs a few requests per target.

    Args:
        args: the parsed flags.

    Returns:
        The run's exit code.
    """
    runner = _runner(args)
    with _open(args) as fl:
        return _watch(fl, fl.sync(args.targets), args, runner=runner)


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
    from flnkit import text

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
        from flnd.ui import out

        out.line(("no targets yet", "dim"))
        return OK
    table = []
    for row in rows:
        held = (text.count(row.seen, "target") + " found" if row.query
                else f"{text.count(row.files, 'file')}, {text.size(row.size)}")
        states = ", ".join(f"{name}: {state}" for name, state in row.categories.items())
        table.append((str(row.target), "followed" if row.follow else "-", held,
                      row.at[:10] or "never", states))
    _table(("target", "follow", "files", "last", "categories"), table)
    return OK


def status(args: argparse.Namespace) -> int:
    """Show the last run: what it saved, and each failure it left, with why and what to do.

    A failure a later run saved is left out. A failed file is retried by itself at the start of
    its target's next 3 downloads or syncs; `flnd retry` tries every one at once. With --json,
    one JSON object: the run's command and times, what it saved, and its failures.

    Args:
        args: the parsed flags.

    Returns:
        0.
    """
    from flnd.engine.events import encode
    from flnd.ui import out

    with _open(args) as fl:
        found = fl.status
    if args.json:
        body = encode(found).decode() if found is not None else "{}"
        out.plain('{"type":"Status"' + ("," + body[1:] if body != "{}" else "}"))
        return OK
    if found is None:
        out.line(("no run yet", "dim"))
        return OK
    ended = f", ended {found.ended[11:19]}" if found.ended else ", never ended"
    out.line(("last run: ", "bold focus"), (found.command, "bold"),
             (f", {found.started[:10]} {found.started[11:19]}{ended}: ", "dim"),
             (f"{found.saved} saved", "bold good"), (", ", "dim"),
             (f"{len(found.failures)} failed", "bold bad" if found.failures else "dim"))
    for failure in found.failures:
        where = f"{failure.target} {failure.post}" if failure.post else str(failure.target)
        runs = f", {failure.attempts} runs" if failure.attempts > 1 else ""
        out.line("  ", (where, "bold focus"), (f" ({_word(failure.error)}{runs}): ", "dim"),
                 (str(failure.error), "bad"))
        if failure.error.hint:
            out.line("    ", (str(failure.error.hint), "dim"))
    if found.failures:
        _summary([(failure.error, bool(failure.post)) for failure in found.failures])
    return OK


def retry(args: argparse.Namespace) -> int:
    """Download again every file that failed, however many runs failed it.

    Each failed post is listed again for fresh links, and only its failed files are downloaded.
    A target whose listing failed is downloaded again. A gone file (the site answered 404 or
    410) isn't retried: `flnd download --again` asks for those.

    Args:
        args: the parsed flags.

    Returns:
        The run's exit code: 0 when nothing failed again.
    """
    with _open(args) as fl:
        return _watch(fl, fl.retry(), args)


def check(args: argparse.Namespace) -> int:
    """Compare the history with the disk: files missing, unknown, or of the wrong size.

    --fix makes the history match the disk, and never deletes, moves or downloads a file: a
    missing file's row goes, a file of the wrong size gets its size from the disk, and an
    unknown file under media/ is adopted into the history.

    Args:
        args: the parsed flags.

    Returns:
        0 when history and disk agree (or --fix made them), else 1.
    """
    from flnd.ui import out
    from flnkit import text

    with _open(args) as fl:
        found = fl.check(fix=args.fix)
    if args.json:
        from flnd.engine.events import encode

        out.plain('{"type":"Checked",' + encode(found).decode()[1:])
        return OK if found.ok else FAILED
    for record in found.missing:
        out.line(("missing     ", "bold bad"), (_shown(record.path), "bad"))
    for path in found.unknown:
        out.line(("unknown     ", "bold warn"), (_shown(path), "warn"))
    for record in found.resized:
        out.line(("wrong size  ", "bold warn"), (_shown(record.path), "warn"))
    problems = [f"{len(found.missing)} missing"] if found.missing else []
    if found.unknown:
        problems.append(f"{len(found.unknown)} unknown")
    if found.resized:
        problems.append(f"{len(found.resized)} of the wrong size")
    line = f"checked {text.count(found.files, 'file')}: "
    if not problems:
        out.line((line, "dim"), ("history and disk agree", "bold good"))
    elif found.fixed:
        out.line((line, "dim"), (", ".join(problems), "bold bad"),
                 ("; the history now matches the disk", "dim"))
    else:
        out.line((line, "dim"), (", ".join(problems), "bold bad"),
                 ("; flnd check --fix makes the history match", "dim"), err=True)
    return OK if found.ok else FAILED


def login(args: argparse.Namespace) -> int:
    """Log in to a source, or with no source list every source's login.

    HOW is a browser to read the site's cookies from (chrome, firefox, edge, brave, chromium,
    or safari on macOS), the path of a cookies.txt or JSON cookie export, or the source's own
    way (tlg: browser, qr, phone). Left out in a terminal, it is asked with numbered choices.
    The login is saved encrypted in the home.

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
    """List every source: built in, from a package or from the plugin folder, and its login.

    Each shows where it came from and its version, the targets it reads, its categories and
    its login. A plugin that is disabled is listed with the reason, naming its file and line.
    Plugins load from $FLND_SOURCES, else ~/.config/flnd/sources (APPDATA's flnd/sources on
    Windows), and from installed packages; never from a home.

    Args:
        args: the parsed flags.

    Returns:
        0.
    """
    from flnd.engine.events import encode
    from flnd.ui import out, theme

    with _open(args) as fl:
        found = fl.sources
    for row in found:
        if args.json:
            out.plain('{"type":"Source",' + encode(row).decode()[1:])
            continue
        version = f" {row.version}" if row.version else ""
        out.line((row.code, "bold good"), "  ", (row.name, "bold"), "  ",
                 (f"({row.origin}{version})", "dim"))
        if row.disabled:
            out.line(("  disabled:   ", "bold bad"), (str(row.disabled), "bad"))
            continue
        if row.origin != "built-in":
            out.line(("  from:       ", "bold focus"), (str(row.where), "dim"))
        more = f", {theme.glyph('more', theme.unicode(sys.stdout))}" if len(row.forms) > 3 else ""
        ways = f" ({', '.join(row.logins)})" if row.logins else ""
        out.line(("  targets:    ", "bold focus"), f"{', '.join(row.forms[:3])}{more}")
        out.line(("  categories: ", "bold focus"), ", ".join(row.categories))
        out.line(("  login:      ", "bold focus"),
                 (row.login, "bold good" if row.login == "logged in" else "dim"), (ways, "dim"))
    return OK


def extension(args: argparse.Namespace) -> int:
    """Install flnd's Chrome extension, or uninstall it.

    install copies the extension to ~/.config/flnd/extension/unpacked, writes its host for every
    Chromium browser found, prints the three steps that load it in Chrome, and waits up to 10
    minutes for it to connect. The popup acts on this home. uninstall removes all of it.

    Args:
        args: the parsed flags.

    Returns:
        0.
    """
    from pathlib import Path

    from flnd.engine import extension
    from flnd.ui import out

    if args.action == "uninstall":
        for path in extension.uninstall():
            out.line(("removed ", "dim"), str(path))
        return OK
    home = Path(args.home).expanduser() if args.home else None
    with _open(args) as fl:
        domains = [domain for row in fl.sources if row.origin != "built-in"
                   for domain in row.domains]
    extension.install(home=home, domains=domains)
    return OK


def host(args: argparse.Namespace) -> int:
    """Serve flnd's Chrome extension on standard input and output; Chrome starts it."""
    from flnd.engine import extension

    return extension.host(args.origin)


# ---------------------------------------------------------------------------------------------
# The parser
# ---------------------------------------------------------------------------------------------

class _Parser(argparse.ArgumentParser):
    """argparse, with its help and its errors printed by `flnd.ui.out`.

    `tail` holds the rows the help ends with: the sources, for a command that takes targets.
    """

    tail: Sequence[tuple[str, Sequence[Any]]] = ()

    def print_help(self, file: Any = None) -> None:
        """Print the help in the theme; `file` is ignored, as the help goes to standard output."""
        from flnd.ui import out

        out.help(self, tail=self.tail)

    def error(self, message: str) -> Any:
        """A mistake in the flags: print it as any usage error is, with the help to read, and
        exit 2."""
        import flnd
        from flnd.ui import out

        out.error(flnd.UsageError(f"{self.prog}: {message}",
                                  hint=f"{self.prog} -h lists what it takes"))
        self.exit(USAGE)


class _Version(argparse.Action):
    """`-V`: print flnd's version on standard output, and exit 0."""

    def __init__(self, option_strings: Sequence[str], version: str, dest: str = "version",
                 help: str = "print flnd's version") -> None:
        super().__init__(option_strings, dest, nargs=0, default=argparse.SUPPRESS, help=help)
        self.version = version

    def __call__(self, parser: argparse.ArgumentParser, *args: Any) -> None:
        from flnd.ui import out

        out.plain(self.version)
        parser.exit()


def parser() -> argparse.ArgumentParser:
    """The whole command line: the global flags, then one sub-parser per command.

    Each command's help is its function's docstring: its first clause in the list of commands,
    the paragraphs before `Args:` on its own `--help`.

    Returns:
        The parser; `parse_args` sets `command` to the command's function.
    """
    import flnd

    top = _Parser(prog="flnd", description="download whole accounts from the web",
                  parents=[_globals()])
    top.add_argument("-V", "--version", action=_Version, version=f"flnd {flnd.__version__}")
    top.tail = [("sources", [" ".join(_sources())]),
                ("", [("their targets: flnd download -h", "dim")])]
    verbs = top.add_subparsers(title="commands", metavar="<command>")

    def command(function: Callable[[argparse.Namespace], int]) -> _Parser:
        sub = verbs.add_parser(function.__name__, help=_short(_help(function)),
                               description=_help(function), parents=[_globals()])
        sub.set_defaults(command=function)
        return sub

    sub = command(download)
    sub.add_argument("targets", nargs="*", metavar="target",
                     help="ins:example_user, a link, or mht?words; read from stdin when none")
    _only(sub)
    _when(sub)
    sub.add_argument("--limit", type=int, metavar="N",
                     help="at most N posts per target, or N targets per search")
    sub.add_argument("--dry", action="store_true",
                     help="list what would be downloaded, and download and write nothing")
    _again(sub)
    _exec(sub)
    sub.tail = _forms()

    sub = command(follow)
    sub.add_argument("targets", nargs="*", metavar="target",
                     help="the targets to follow; read from stdin when none")
    _only(sub)
    _when(sub)
    _again(sub)
    sub.tail = _forms()

    sub = command(sync)
    sub.add_argument("targets", nargs="*", metavar="target",
                     help="only these followed targets; every one when none")
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

    sub = command(extension)
    sub.add_argument("action", choices=("install", "uninstall"),
                     help="install connects Chrome; uninstall removes the extension")

    # Hidden from the help: Chrome starts it, with the extension's origin (and, on Windows,
    # --parent-window).
    sub = verbs.add_parser("host", add_help=False)
    sub.set_defaults(command=host)
    sub.add_argument("origin", nargs="?")
    sub.add_argument("--parent-window", help=argparse.SUPPRESS)
    return top


def _globals() -> argparse.ArgumentParser:
    """The flags every command takes, before or after its name.

    Their defaults are `SUPPRESS`, so a flag given before the command isn't overwritten by the
    command's own parser; `_settle` fills in the real defaults after parsing.

    Returns:
        A parser to use as a parent; it adds no `--help` of its own.
    """
    shared = _Parser(add_help=False)
    group = shared.add_argument_group("global flags")
    group.add_argument("--home", metavar="DIR", default=argparse.SUPPRESS,
                       help="where downloads, history and logins live "
                            "(default: $FLND_HOME, else ./.flnd)")
    group.add_argument("-v", "--verbose", action="store_true", default=argparse.SUPPRESS,
                       help="show flnd's debug lines, and a bug's traceback")
    group.add_argument("--json", action="store_true", default=argparse.SUPPRESS,
                       help="print events and rows as JSON lines and never asks")
    return shared


def _only(sub: argparse.ArgumentParser) -> None:
    sub.add_argument("--only", action="append", metavar="CATS",
                     help="only these categories (reels,posts); every category when left out")


def _when(sub: argparse.ArgumentParser) -> None:
    # Left out, it is None, and the library reads `[defaults] when` from config.toml.
    sub.add_argument("--when", metavar="WHEN",
                     help="posts from 2026, 2026-06, 2026-06-15, 2026-01..2026-03, or 30d; "
                          "\"\" for every day")


def _again(sub: argparse.ArgumentParser) -> None:
    """Add `--again` to a command that downloads.

    Args:
        sub: the command's parser.
    """
    sub.add_argument("--again", action="store_true",
                     help="list every post again, and ask again for files the site said are "
                          "gone (404 or 410)")


def _exec(sub: argparse.ArgumentParser) -> None:
    sub.add_argument("--exec", metavar="CMD",
                     help="run CMD for each saved file, without a shell; {path}, {target} and "
                          "{post} become the file's")


def _help(function: Callable[..., Any]) -> str:
    """A command's help: its docstring up to `Args:`, dedented."""
    import inspect

    text = inspect.cleandoc(function.__doc__ or "")
    return text.split("\n\nArgs:")[0].strip()


def _short(text: str) -> str:
    """A command's row in `flnd -h`: its help up to the first comma, colon or full stop."""
    import re

    return re.split(r"[,:.](?:\s|$)", text, maxsplit=1)[0]


def _sources() -> list[str]:
    """The code of every built-in source, read from the manifest, so no source is imported."""
    from flnd.sources import _manifest

    return list(_manifest.SOURCES)


def _forms() -> list[tuple[str, list[Any]]]:
    """A row per built-in source for a command's help: the targets it reads, its categories.

    Returns:
        The rows: the source's code, then its forms and categories as styled parts.
    """
    from flnd.sources import _manifest

    rows: list[tuple[str, list[Any]]] = [("sources", [("the targets each reads, then its "
                                                      "categories", "dim")])]
    for code, entry in _manifest.SOURCES.items():
        forms: Any = entry["forms"]
        domains: Any = entry["domains"]
        categories: Any = entry["categories"]
        own = [form for form in forms if not form.startswith("http")]
        rows.append((code, [", ".join([*own, f"a link on {', '.join(domains)}"]),
                            ("\n--only ", "dim"), " ".join(categories)]))
    return rows


# ---------------------------------------------------------------------------------------------
# Helpers of the commands
# ---------------------------------------------------------------------------------------------

def _open(args: argparse.Namespace) -> flnd.Flnd:
    """The library over `--home`; the caller uses it in a `with`."""
    import flnd

    return flnd.open(args.home, again=True if getattr(args, "again", False) else None)


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
    from flnd.sources import _manifest
    from flnd.ui import forms

    entry: Any = _manifest.SOURCES.get(args.source)
    if entry is None:
        # Not built in: a plugin's ways are read from its class, which this imports.
        with _open(args) as fl:
            rows = [row for row in fl.sources if not row.disabled]
        found = next((row for row in rows if row.code == args.source), None)
        if found is None:
            codes = ", ".join(row.code for row in rows)
            raise flnd.UsageError(f"unknown source {args.source!r}. Sources: {codes}")
        entry = {"name": found.name, "logins": found.logins}
    ways: tuple[str, ...] = tuple(entry["logins"])
    hint = f"name it: flnd login {args.source} <how>, with how one of {_ways(ways)}"
    if args.json or not forms.interactive():
        raise flnd.UsageError(f"{args.source}: how to log in?", hint=hint)
    if "file" not in ways:
        return forms.choose(f"Log in to {entry['name']} with", list(ways))
    from flnkit.cookies import BROWSERS

    # Safari is read on macOS only, so it is offered only there.
    choices = {f"cookies from {browser}": browser for browser in BROWSERS
               if browser != "safari" or sys.platform == "darwin"}
    choices["a cookie file (cookies.txt or JSON)"] = "file"
    picked = forms.choose(f"Log in to {entry['name']} with", choices)
    if picked == "file":
        return forms.ask("Path of the cookie file").strip()
    return picked


def _ways(ways: Sequence[str]) -> str:
    """A source's login ways as `flnd login` takes them, for a hint."""
    if "file" in ways:
        return "chrome, firefox, edge, brave, chromium, safari (macOS), or a cookie file's path"
    return ", ".join(ways)


def _logins(args: argparse.Namespace) -> int:
    """`flnd login` with no source: a table of every source's login.

    Args:
        args: the parsed flags.

    Returns:
        0.
    """
    from flnkit import text

    with _open(args) as fl:
        found = fl.logins
    if args.json:
        for row in found:
            _json(row)
        return OK
    table = []
    for row in found:
        soon = ", ".join(f"{name} expired" if left <= 0 else f"{name} in {text.duration(left)}"
                         for name, left in row.expiring)
        table.append((row.source, row.name, row.state, row.origin, soon))
    _table(("source", "site", "login", "from", "expiring"), table)
    return OK


def _line(args: argparse.Namespace, value: Any, text: str) -> None:
    """Print one result: as JSON with `--json`, else as styled `text`.

    Args:
        args: the parsed flags.
        value: the result object for JSON encoding.
        text: plain text representation to show.
    """
    from flnd.ui import out

    if args.json:
        _json(value)
        return
    head, colon, tail = text.partition(": ")
    if not colon:
        out.line(text)
        return
    parts: list[Any] = [(head, "bold focus"), (colon, "dim")]
    if tail.startswith("logged in"):
        login_part, paren, origin_part = tail.partition(" (")
        parts += [(login_part, "bold good"), (paren + origin_part, "dim")]
    elif tail in ("logged out", "unfollowed"):
        parts.append((tail, "bold good"))
    else:
        parts.append(tail)
    out.line(*parts)


def _json(value: Any) -> None:
    """Print one value (an event, a row, or a plain dict) as one line of JSON on standard output.

    Args:
        value: an event, record, or dict to encode.
    """
    from flnd.engine.events import encode
    from flnd.ui import out

    out.plain(encode(value).decode())


def _table(header: Sequence[str], rows: Sequence[Sequence[str]]) -> None:
    """Print rows as a table: the first column in focus, and each state in its colour.

    Args:
        header: column titles.
        rows: table rows.
    """
    from flnd.ui import out

    out.table([[_cell(index, str(cell)) for index, cell in enumerate(row)] for row in rows],
              header)


def _cell(index: int, text: str) -> tuple[str, str]:
    """A table cell with its style: the first column in focus, then by what the text says."""
    if index == 0:
        return text, "bold focus"
    if text in ("followed", "logged in", "on"):
        return text, "bold good"
    if text in ("-", "never", "off"):
        return text, "dim"
    if "expired" in text:
        return text, "bold bad"
    if " in " in text:
        return text, "warn"
    return text, "text"


# ---------------------------------------------------------------------------------------------
# Watching a run
# ---------------------------------------------------------------------------------------------

def _watch(fl: flnd.Flnd, run: flnd.Run, args: argparse.Namespace, *,
           runner: _Exec | None = None) -> int:
    """Read a run's events into the chosen view, print its end, and return its exit code.

    flnd's own log lines are held to warnings for the run, or opened to debug lines with `-v`.
    The run's events go to `<home>/log/<stamp>_<command>.jsonl` (see src/docs/storage). On a
    terminal the screen draws the run from its snapshot (`run.state`, `flnd.ui.screen`); the
    end summary follows.

    Args:
        fl: the open home.
        run: the run, not started yet.
        args: the parsed flags: `--json`, `-v`.
        runner: `--exec`'s command, run for each `Saved` event; None runs nothing.

    Returns:
        0 when nothing failed, 3 when a target needs a login, else 1.

    Raises:
        Error: what stopped the whole run (a full disk), from the run.
        Stopped: Ctrl-C ended it.
    """
    import datetime
    from contextlib import ExitStack, nullcontext
    from pathlib import Path

    from flnd.engine.home import LOGS
    from flnd.ui.log import ui
    from flnd.ui.screen import Screen

    journal_path: Path | None = None
    try:
        home = getattr(fl, "home", None)
        if isinstance(home, (str, os.PathLike, Path)):
            log_dir = Path(home) / LOGS
            log_dir.mkdir(parents=True, exist_ok=True)
            cmd = getattr(args, "command", None)
            name = cmd.__name__ if cmd and hasattr(cmd, "__name__") else "run"
            stamp = f"{datetime.datetime.now():%Y%m%d-%H%M%S}"
            journal_path = log_dir / f"{stamp}_{name}.jsonl"
    except (OSError, TypeError):
        pass

    cmd = getattr(args, "command", None)
    name = cmd.__name__ if cmd and hasattr(cmd, "__name__") else "run"
    view: _View = (
        _Json()
        if args.json
        else _JournalView(journal_path=journal_path)
    )
    with ExitStack() as stack:
        journal = (
            stack.enter_context(ui.file(journal_path)) if journal_path is not None else None
        )
        view.journal = journal
        stack.enter_context(view)
        stack.enter_context(ui.scope(level="debug" if args.verbose else "warn"))
        if journal is not None:
            import flnd

            journal.write({"type": "Start", "command": name, "version": flnd.__version__})
        # The screen ends before the summary: its last frame stays above it.
        with nullcontext() if args.json else Screen(lambda: run.state, command=name):
            for event in run:
                view.show(event)
                if runner is not None and type(event).__name__ == "Saved":
                    runner.run(event)
            report = run.report
            # The warnings still folded print above the screen, never after the summary.
            ui.drain()
        view.end(report, run.state)
        if journal is not None:
            journal.write(report)
    if any(_login(failed.error) for failed in report.failed):
        return LOGIN
    return FAILED if report.failed else OK


class _Exec:
    """`--exec CMD`: a command run once per saved file, never through a shell.

    CMD is split into arguments once, here, with `shlex` (POSIX rules; on Windows, quotes are
    kept by the split and then dropped, and backslashes stay as they are, for paths). For each
    file, `{path}`, `{target}` and `{post}` inside each argument become its values in one pass,
    so a value that itself holds `{post}` or `;` or `$(...)` stays literal text inside its one
    argument. The same values are in the environment as `FLND_PATH`, `FLND_TARGET` and
    `FLND_POST`.

    Args:
        command: CMD as given.
        quiet: send the command's output to standard error, so `--json` keeps standard
            output to JSON lines.

    Raises:
        UsageError: CMD is empty, its quotes don't close, or its program isn't found.
    """

    # The placeholders, matched in one pass so a substituted value is never read again.
    FIELDS = ("path", "target", "post")

    def __init__(self, command: str, *, quiet: bool = False) -> None:
        import re
        import shlex
        import shutil

        import flnd

        lexer = shlex.shlex(command, posix=True)
        lexer.whitespace_split = True
        if os.name == "nt":
            lexer.escape = ""
        try:
            argv = list(lexer)
        except ValueError as error:
            raise flnd.UsageError(f"--exec: {error}: {command!r}") from None
        if not argv:
            raise flnd.UsageError("--exec needs a command", hint="--exec 'echo {path}'")
        if shutil.which(argv[0]) is None:
            raise flnd.UsageError(f"--exec: {argv[0]}: no such program",
                                  hint="give a program on PATH, or its full path")
        self.argv = argv
        self.quiet = quiet
        self._pattern = re.compile(r"\{(" + "|".join(self.FIELDS) + r")\}")

    def argv_for(self, event: Any) -> list[str]:
        """The command's arguments for one saved file.

        Args:
            event: the `flnd.Saved` event.

        Returns:
            CMD's arguments with the placeholders filled in.
        """
        values = self._values(event)
        return [self._pattern.sub(lambda found: values[found[1]], arg) for arg in self.argv]

    def run(self, event: Any) -> int:
        """Run the command for one saved file and wait for it.

        A command that can't start or fails is reported on standard error, and the run goes
        on: the file is saved either way.

        Args:
            event: the `flnd.Saved` event.

        Returns:
            The command's exit code; 127 when it couldn't start.
        """
        import subprocess

        from flnd.ui import out

        values = self._values(event)
        env = {**os.environ, "FLND_PATH": values["path"], "FLND_TARGET": values["target"],
               "FLND_POST": values["post"]}
        argv = self.argv_for(event)
        try:
            done = subprocess.run(argv, env=env, shell=False, stdin=subprocess.DEVNULL,
                                  stdout=sys.stderr if self.quiet else None, check=False)
        except OSError as error:
            out.warn(f"--exec: {argv[0]}: {error.strerror or error}")
            return 127
        if done.returncode:
            out.warn(f"--exec: {argv[0]} exited {done.returncode} for {_shown(event.path)}")
        return done.returncode

    @staticmethod
    def _values(event: Any) -> dict[str, str]:
        return {"path": str(event.path), "target": str(event.target), "post": event.post}


def _runner(args: argparse.Namespace) -> _Exec | None:
    """`--exec`'s command, read from the flags; None without `--exec`.

    Args:
        args: the parsed flags.

    Returns:
        The command, ready to run per file.

    Raises:
        UsageError: CMD is empty, its quotes don't close, or its program isn't found.
    """
    command = getattr(args, "exec", None)
    return _Exec(command, quiet=args.json) if command is not None else None


def _login(error: BaseException) -> bool:
    """Whether `error` is a `flnd.LoginError`: a login the target needs, exit code 3."""
    import flnd

    return isinstance(error, flnd.LoginError)


class _View:
    """How a run shows: `show` each event as it comes, then `end` with the report.

    A view is a context manager, so a live display stops even when the run raises.
    """

    def __enter__(self) -> _View:
        return self

    def __exit__(self, *exc: object) -> None:
        return None

    def show(self, event: Any) -> None:
        """Show one event."""

    def end(self, report: Any, state: Any) -> None:
        """Show the run's end summary (`flnd.ui.screen.summary`); a dry run's planned files.

        Args:
            report: the run's `flnd.Report`.
            state: its last snapshot (`run.state`).
        """
        from flnd.ui import out, screen
        from flnkit import text

        if report.planned:
            out.line(("planned ", "bold focus"), (text.count(report.planned, "file"), "bold focus"),
                     ("; nothing was downloaded", "dim"))
            return
        screen.summary(report, state)


class _JournalView(_View):
    """Terminal output from run events; the machine record goes to the `.jsonl` journal.

    The run's own display is the screen (`flnd.ui.screen`), drawn from the run's snapshot, so
    a view prints no line per file or failure: the screen counts them, and `end` sums them up.
    """

    journal: Any = None

    def __init__(self, *, journal_path: Any = None) -> None:
        """Args:
            journal_path: where the JSON Lines run log is written; None skips the file.
        """
        super().__init__()
        self.journal_path = journal_path

    def show(self, event: Any) -> None:
        """Record one event, and print a dry run's planned files.

        Args:
            event: the event to record and potentially display.
        """
        from flnd.ui import out

        name = type(event).__name__
        if self.journal is not None:
            self.journal.write(event)
        if name != "Planned":
            return
        if event.path is None:
            out.line(("found ", "dim"), (str(event.target), "bold focus"))
            return
        folder, filename = os.path.split(_shown(event.path))
        out.line((f"{event.state} ", "bold focus" if event.state == "new" else "dim"),
                 ((folder + os.sep) if folder else "", "dim"), filename)


class _Json(_View):
    """Each event as one JSON object per line, then the report as `{"type": "Report", ...}`."""

    def show(self, event: Any) -> None:
        _json(event)

    def end(self, report: Any, state: Any) -> None:
        """Write the finished run's report as a JSON object on its own line.

        Args:
            report: the run's report to encode.
            state: its last snapshot, which the JSON lines leave out.
        """
        from flnd.engine.events import encode
        from flnd.ui import out

        out.plain('{"type":"Report",' + encode(report).decode()[1:])


# Each error class's word in a failure summary, by class name; the first class in an error's MRO
# that is listed wins.
WORDS = {
    "NotFound": "gone", "Blocked": "blocked", "Changed": "changed", "LoginError": "login",
    "SiteError": "site", "NetworkError": "network", "DiskError": "disk", "ToolError": "tool",
    "SourceError": "bug", "UsageError": "usage",
}
# The words of failures a retry won't bring back: a gone file is never asked for again.
FINAL = frozenset({"gone"})


def _word(error: BaseException) -> str:
    """The word a failure summary counts an error under: "network", "gone", …; else "error"."""
    return next((WORDS[kind.__name__] for kind in type(error).__mro__ if kind.__name__ in WORDS),
                "error")


def _summary(failed: Sequence[tuple[BaseException, bool]], *, details: bool = False,
             err: bool = False) -> None:
    """Print the one line that sums a run's failures up, by kind, saying which a retry may bring
    back.

        ✗ 3 files failed: 2 network (retry: flnd retry), 1 gone. Details: flnd status

    Args:
        failed: each failure's error, with whether it was a file's (else a whole target's).
        details: whether to end with where the details are (`flnd status`).
        err: print it on standard error instead of standard output.
    """
    from flnd.ui import out
    from flnkit import text

    files = sum(is_file for _, is_file in failed)
    nouns = [text.count(count, noun) for count, noun in
             ((files, "file"), (len(failed) - files, "target")) if count]
    kinds: dict[str, int] = {}
    for error, _ in failed:
        word = _word(error)
        kinds[word] = kinds.get(word, 0) + 1
    retried = [f"{count} {word}" for word, count in kinds.items() if word not in FINAL]
    final = [f"{count} {word}" for word, count in kinds.items() if word in FINAL]

    parts: list[Any] = [(f"{' and '.join(nouns)} failed", "bold bad"), (": ", "dim")]
    if retried:
        parts += [(", ".join(retried), "bad"), (" (retry: ", "dim"), ("flnd retry", "bold focus"),
                  (")", "dim")]
        if final:
            parts.append((", ", "dim"))
    if final:
        parts.append((", ".join(final), "bad"))
    if details:
        parts += [(". Details: ", "dim"), ("flnd status", "bold focus")]
    out.line(*parts, mark="error", err=err)


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
