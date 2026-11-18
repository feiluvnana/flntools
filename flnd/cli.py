"""The flnd command line: flags in, one library call, events out as lines on the terminal.

    flnd download ins:example_user --only reels --when 2026
    flnd follow tik:example_user
    flnd sync
    flnd login ins chrome

Who calls it: `flnd` (the console script) and `python -m flnd`, through `main`. What it calls:
the library alone (`flnd.open`, its `Run` and events), so anything the command line does, a
script can do too; `flnd.sources._manifest` for the built-in sources `--help` lists;
and the library modules `flnd.ui.forms` (`flnd login`'s questions) and `flnd.ui.log` (the level of
flnd's own log lines and progress bars).

How a command runs (`main`):
1. argparse reads the flags. A mistake prints the usage and exits 2; `--help` prints the help
   made from each command's docstring, with the target forms and categories of every source,
   rendered with Charm Huh / Cyclopts panels.
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
- One flag, one spelling: no aliases. The docs' CLI page (flnd/docs/cli) names every command and
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

# The exit codes (flnd/docs/cli). A cron job can tell "some files failed" (1) from "flnd itself
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
        from rich.text import Text

        _console(sys.stderr).print(Text("stopped", style="yellow"))
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
        0 if the command succeeded, 130 for Ctrl-C, the exit code from `CODES` for `flnd.Error` 
        or its base classes, and 4 for other exceptions.

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


def _console(file: Any = None) -> Any:
    """Return a Rich Console targeting `file` (default sys.stdout) with CLI theme.

    Args:
        file: text stream to write to; defaults to sys.stdout.

    Returns:
        A configured Rich Console instance.
    """
    from rich.console import Console
    from rich.theme import Theme

    from flnd.ui.log import PALETTE

    class _Console(Console):
        def print(self, *args: Any, **kwargs: Any) -> None:
            super().print(*args, **kwargs)
            if hasattr(self.file, "flush"):
                self.file.flush()

    return _Console(
        file=file or sys.stdout,
        theme=Theme(PALETTE),
        highlight=False,
        soft_wrap=True,
    )


def _report(error: Any, *, as_json: bool = False) -> None:
    """Print an error a person can act on: its message, then its hint; never a traceback.

    Args:
        error: the `flnd.Error` that ended the command.
        as_json: also print it on standard output as `{"type": "Error", ...}`, for `--json`.
    """
    if as_json:
        from flnd._events import encode

        sys.stdout.write('{"type":"Error","error":' + encode(error).decode() + "}\n")
        sys.stdout.flush()
    from rich.text import Text

    c = _console(sys.stderr)
    c.print(Text.assemble(("error: ", "bold #ED567A"), (str(error), "#ED567A")))
    if error.hint:
        c.print(Text.assemble(("  ", ""), (str(error.hint), "color(243)")))


def _crashed(error: Exception, args: argparse.Namespace) -> int:
    """Report a bug: one line naming a crash file that holds the traceback, or with `-v` the
    traceback itself.

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

    from rich.text import Text

    from flnd import _home

    trace = "".join(traceback.format_exception(error))
    line = f"unexpected {type(error).__name__}: {error}"
    home = Path(args.home or os.environ.get("FLND_HOME") or ".flnd").expanduser()
    path = home / _home.LOGS / f"crash-{datetime.datetime.now():%Y%m%d-%H%M%S}.txt"
    c = _console(sys.stderr)
    if not args.verbose:
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(trace, encoding="utf-8")
        except OSError:
            pass
        else:
            c.print(Text.assemble(
                ("error: ", "bold #ED567A"),
                (f"{line}; the details are in {path}", "#ED567A"),
            ))
            return BROKE
    sys.stderr.write(trace)
    c.print(Text.assemble(("error: ", "bold #ED567A"), (line, "#ED567A")))
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
    from flnd.kit import text

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
        from rich.text import Text

        _console().print(Text("no targets yet", style="color(243)"))
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
    from flnd._events import encode

    with _open(args) as fl:
        found = fl.status
    if args.json:
        body = encode(found).decode() if found is not None else "{}"
        sys.stdout.write('{"type":"Status"' + ("," + body[1:] if body != "{}" else "}") + "\n")
        return OK
    if found is None:
        from rich.text import Text

        _console().print(Text("no run yet", style="color(243)"))
        return OK
    from rich.text import Text

    ended = f", ended {found.ended[11:19]}" if found.ended else ", never ended"
    hdr = Text()
    hdr.append("last run: ", style="bold #7D84D6")
    hdr.append(found.command, style="bold")
    hdr.append(f", {found.started[:10]} {found.started[11:19]}{ended}: ", style="color(245)")
    hdr.append(f"{found.saved} saved", style="bold #02BF87")
    hdr.append(", ", style="color(243)")
    fail_style = "bold #ED567A" if found.failures else "color(243)"
    hdr.append(f"{len(found.failures)} failed", style=fail_style)
    _console().print(hdr)
    for failure in found.failures:
        where = f"{failure.target} {failure.post}" if failure.post else str(failure.target)
        runs = f", {failure.attempts} runs" if failure.attempts > 1 else ""
        fail_line = Text()
        fail_line.append("  ")
        fail_line.append(where, style="bold #7D84D6")
        fail_line.append(f" ({_word(failure.error)}{runs})", style="color(243)")
        fail_line.append(": ", style="color(243)")
        fail_line.append(str(failure.error), style="#ED567A")
        _console().print(fail_line)
        if failure.error.hint:
            _console().print(Text.assemble(("    ", ""), (str(failure.error.hint), "color(243)")))
    if found.failures:
        _console().print(_summary([(failure.error, bool(failure.post))
                                   for failure in found.failures]))
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
    from flnd.kit import text

    with _open(args) as fl:
        found = fl.check(fix=args.fix)
    if args.json:
        from flnd._events import encode

        sys.stdout.write('{"type":"Checked",' + encode(found).decode()[1:] + "\n")
        sys.stdout.flush()
        return OK if found.ok else FAILED
    from rich.text import Text

    c = _console()
    for record in found.missing:
        c.print(Text.assemble(
            ("missing     ", "bold #ED567A"),
            (_shown(record.path), "#ED567A"),
        ))
    for path in found.unknown:
        c.print(Text.assemble(
            ("unknown     ", "bold yellow"),
            (_shown(path), "yellow"),
        ))
    for record in found.resized:
        c.print(Text.assemble(
            ("wrong size  ", "bold yellow"),
            (_shown(record.path), "yellow"),
        ))
    problems = [f"{len(found.missing)} missing"] if found.missing else []
    if found.unknown:
        problems.append(f"{len(found.unknown)} unknown")
    if found.resized:
        problems.append(f"{len(found.resized)} of the wrong size")
    line = f"checked {text.count(found.files, 'file')}: "
    if not problems:
        c.print(Text.assemble((line, "color(245)"), ("history and disk agree", "bold #02BF87")))
    elif found.fixed:
        c.print(Text.assemble(
            (line, "color(245)"),
            (", ".join(problems), "bold #ED567A"),
            ("; the history now matches the disk", "color(243)"),
        ))
    else:
        _console(sys.stderr).print(Text.assemble(
            (line, "color(245)"),
            (", ".join(problems), "bold #ED567A"),
            ("; flnd check --fix makes the history match", "color(243)"),
        ))
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
    from flnd._events import encode

    with _open(args) as fl:
        found = fl.sources
    for row in found:
        if args.json:
            sys.stdout.write('{"type":"Source",' + encode(row).decode()[1:] + "\n")
            continue
        from rich.text import Text

        c = _console()
        version = f" {row.version}" if row.version else ""
        hdr = Text()
        hdr.append(row.code, style="bold #02BF87")
        hdr.append("  ")
        hdr.append(row.name, style="bold")
        hdr.append("  ")
        hdr.append(f"({row.origin}{version})", style="color(243)")
        c.print(hdr)
        if row.disabled:
            c.print(Text.assemble(
                ("  disabled:   ", "bold #ED567A"),
                (str(row.disabled), "#ED567A"),
            ))
            continue
        if row.origin != "built-in":
            c.print(Text.assemble(
                ("  from:       ", "bold #7D84D6"),
                (str(row.where), "color(243)"),
            ))
        more = ", …" if len(row.forms) > 3 else ""
        ways = f" ({', '.join(row.logins)})" if row.logins else ""
        c.print(Text.assemble(
            ("  targets:    ", "bold #7D84D6"),
            (f"{', '.join(row.forms[:3])}{more}", "color(252)"),
        ))
        c.print(Text.assemble(
            ("  categories: ", "bold #7D84D6"),
            (", ".join(row.categories), "color(252)"),
        ))
        login_style = "bold #02BF87" if row.login == "logged in" else "color(243)"
        c.print(Text.assemble(
            ("  login:      ", "bold #7D84D6"),
            (row.login, login_style),
            (ways, "color(243)"),
        ))
    return OK


# ---------------------------------------------------------------------------------------------
# Rich Help Formatter
# ---------------------------------------------------------------------------------------------

class RichArgumentParser(argparse.ArgumentParser):
    """ArgumentParser that renders help using Charm / Cyclopts theme in Rich panels."""

    def print_help(self, file: Any = None) -> None:
        """Render help in Rich panels using the CLI_PALETTE theme.

        Args:
            file: where to write the help output; None writes to sys.stdout.
        """
        dest = file if file is not None else sys.stdout
        is_term = getattr(dest, "isatty", lambda: False)()
        if not is_term and dest is not sys.stdout:
            super().print_help(file=dest)
            return

        from rich import box
        from rich.console import Console
        from rich.panel import Panel
        from rich.table import Table
        from rich.text import Text

        console = Console(file=dest, highlight=False, soft_wrap=True)
        border_color = "#7D84D6"
        name_color = "#02BF87"
        usage_style = "#7D84D6 bold"

        subparsers_action = None
        positionals = []
        options = []
        global_flags = []

        for action in self._actions:
            if isinstance(action, argparse._SubParsersAction):
                subparsers_action = action
            elif any(opt in ("--home", "-v", "--verbose", "--json")
                     for opt in action.option_strings):
                global_flags.append(action)
            elif action.option_strings:
                if action.dest not in ("help", "version"):
                    options.append(action)
            elif action.dest not in ("help", "version"):
                positionals.append(action)

        is_root = subparsers_action is not None
        if is_root:
            console.print(Text.assemble(
                ("Usage: ", usage_style), (self.prog + " COMMAND", "bold"),
            ))
        else:
            prog_name = self.prog
            pos_names = " ".join(f"[{a.metavar or a.dest}]" for a in positionals)
            usage_str = f"{prog_name} [OPTIONS] {pos_names}".strip()
            console.print(Text.assemble(("Usage: ", usage_style), (usage_str, "bold")))

        if self.description:
            console.print(f"\n{self.description}\n")

        if subparsers_action is not None:
            table = Table.grid(padding=(0, 4))
            table.add_column(style=f"bold {name_color}")
            table.add_column()
            for name, choice in subparsers_action.choices.items():
                desc = (choice.description or "").split("\n\n")[0].strip()
                summary = desc.split("\n")[0].strip()
                table.add_row(name, summary)
            table.add_row("--help (-h)", "Display this message and exit.")
            table.add_row("--version (-V)", "Display application version.")
            console.print(Panel(table, title="Commands", title_align="left",
                                border_style=border_color, box=box.SQUARE))

        if not is_root and (positionals or options):
            table = Table.grid(padding=(0, 4))
            table.add_column(style=f"bold {name_color}")
            table.add_column()
            for action in positionals:
                metavar = action.metavar or action.dest.upper()
                table.add_row(metavar, action.help or "")
            for action in options:
                opts = ", ".join(action.option_strings)
                if action.metavar:
                    opts += f" {action.metavar}"
                table.add_row(opts, action.help or "")
            table.add_row("--help (-h)", "Display this message and exit.")
            console.print(Panel(table, title="Parameters", title_align="left",
                                border_style=border_color, box=box.SQUARE))

        if global_flags:
            table = Table.grid(padding=(0, 4))
            table.add_column(style=f"bold {name_color}")
            table.add_column()
            for action in global_flags:
                opts = ", ".join(action.option_strings)
                if action.metavar:
                    opts += f" {action.metavar}"
                table.add_row(opts, action.help or "")
            console.print(Panel(table, title="Global Flags", title_align="left",
                                border_style=border_color, box=box.SQUARE))

        if self.epilog:
            console.print(f"\n{self.epilog}")


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

    top = RichArgumentParser(
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
    sub.add_argument("--dry", action="store_true",
                     help="list what would be downloaded, and download and write nothing")
    _again(sub)
    _exec(sub)

    sub = command(follow)
    sub.add_argument("targets", nargs="*", metavar="target",
                     help="the targets to follow; read from stdin when none")
    _only(sub)
    _when(sub)
    _again(sub)

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
    return top


def _globals() -> argparse.ArgumentParser:
    """The flags every command takes, before or after its name.

    Their defaults are `SUPPRESS`, so a flag given before the command isn't overwritten by the
    command's own parser; `_settle` fills in the real defaults after parsing.

    Returns:
        A parser to use as a parent; it adds no `--help` of its own.
    """
    shared = RichArgumentParser(add_help=False)
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
    from flnd.kit.cookies import BROWSERS

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
    from flnd.kit import text

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
    if args.json:
        _json(value)
        return
    if ": " in text:
        head, colon, tail = text.partition(": ")
        from rich.text import Text

        line = Text()
        line.append(head, style="bold #7D84D6")
        line.append(colon, style="color(243)")
        if tail.startswith("logged in"):
            login_part, _, origin_part = tail.partition(" (")
            line.append(login_part, style="bold #02BF87")
            if origin_part:
                line.append(" (" + origin_part, style="color(243)")
        elif tail in ("logged out", "unfollowed"):
            line.append(tail, style="bold #02BF87")
        else:
            line.append(tail, style="color(252)")
        _console().print(line)
    else:
        _console().print(text)


def _json(value: Any, *, stream: Any = None) -> None:
    """Print one value (an event, a row, or a plain dict) as one line of JSON.

    Args:
        value: an event, record, or dict to encode.
        stream: destination stream; defaults to sys.stdout.
    """
    from flnd._events import encode

    out = stream if stream is not None else sys.stdout
    out.write(encode(value).decode() + "\n")
    if hasattr(out, "flush"):
        out.flush()


def _table(header: Sequence[str], rows: Sequence[Sequence[str]]) -> None:
    """Print rows as a styled Rich table on a terminal, or plain columns elsewhere.

    Args:
        header: column titles.
        rows: table rows.
    """
    if not sys.stdout.isatty():
        widths = [max(len(str(cell)) for cell in column) for column in zip(header, *rows)]
        for row in (header, *rows):
            print("  ".join(str(cell).ljust(width) for cell, width in zip(row, widths)).rstrip())
        return

    from rich import box
    from rich.table import Table
    from rich.text import Text

    table = Table(box=box.ROUNDED, border_style="#7D84D6", header_style="bold #02BF87")
    for title in header:
        table.add_column(title)
    for row in rows:
        styled_row = []
        for i, cell in enumerate(row):
            text_val = str(cell)
            if i == 0:
                styled_row.append(Text(text_val, style="bold #7D84D6"))
            elif text_val in ("followed", "logged in", "on"):
                styled_row.append(Text(text_val, style="bold #02BF87"))
            elif text_val in ("-", "never", "off"):
                styled_row.append(Text(text_val, style="color(243)"))
            elif "expired" in text_val:
                styled_row.append(Text(text_val, style="bold #ED567A"))
            elif " in " in text_val:
                styled_row.append(Text(text_val, style="yellow"))
            else:
                styled_row.append(Text(text_val, style="color(252)"))
        table.add_row(*styled_row)
    _console().print(table)


# ---------------------------------------------------------------------------------------------
# Watching a run
# ---------------------------------------------------------------------------------------------

def _watch(fl: flnd.Flnd, run: flnd.Run, args: argparse.Namespace, *,
           runner: _Exec | None = None) -> int:
    """Read a run's events into the chosen view, print its end, and return its exit code.

    flnd's own log lines are held to warnings for the run, or opened to debug lines with `-v`.
    The run's events go to `<home>/log/<stamp>_<command>.jsonl` (see flnd/docs/storage), and
    the library draws the run's summary table from the rows it keeps (`ui.group(drawn=…)`).

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
    from contextlib import ExitStack
    from pathlib import Path

    from flnd import _home
    from flnd.ui.log import ui

    journal_path: Path | None = None
    try:
        home = getattr(fl, "home", None)
        if isinstance(home, (str, os.PathLike, Path)):
            log_dir = Path(home) / _home.LOGS
            log_dir.mkdir(parents=True, exist_ok=True)
            cmd = getattr(args, "command", None)
            name = cmd.__name__ if cmd and hasattr(cmd, "__name__") else "run"
            stamp = f"{datetime.datetime.now():%Y%m%d-%H%M%S}"
            journal_path = log_dir / f"{stamp}_{name}.jsonl"
    except (OSError, TypeError):
        pass

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
        cmd = getattr(args, "command", None)
        name = cmd.__name__ if cmd and hasattr(cmd, "__name__") else "run"
        if journal is not None:
            import flnd

            journal.write({"type": "Start", "command": name, "version": flnd.__version__})
        for event in run:
            view.show(event)
            if runner is not None and type(event).__name__ == "Saved":
                runner.run(event)
        report = run.report
        view.end(report)
        if journal is not None:
            journal.write(report)
    if any(_kind(failed.error) == "LoginError" for failed in report.failed):
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

        values = self._values(event)
        env = {**os.environ, "FLND_PATH": values["path"], "FLND_TARGET": values["target"],
               "FLND_POST": values["post"]}
        argv = self.argv_for(event)
        try:
            done = subprocess.run(argv, env=env, shell=False, stdin=subprocess.DEVNULL,
                                  stdout=sys.stderr if self.quiet else None, check=False)
        except OSError as error:
            from rich.text import Text

            _console(sys.stderr).print(Text.assemble(
                ("--exec: ", "bold #ED567A"),
                (f"{argv[0]}: {error.strerror or error}", "#ED567A"),
            ))
            return 127
        if done.returncode:
            from rich.text import Text

            _console(sys.stderr).print(Text.assemble(
                ("--exec: ", "bold #ED567A"),
                (f"{argv[0]} exited {done.returncode} for {_shown(event.path)}", "#ED567A"),
            ))
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


def _kind(error: BaseException) -> str:
    """The name of the nearest class of `error` that `CODES` lists; "" for none."""
    return next((kind.__name__ for kind in type(error).__mro__ if kind.__name__ in CODES), "")


class _View:
    """How a run shows: `show` each event as it comes, then `end` with the report.

    A view is a context manager, so a live display stops even when the run raises.

    Args:
        log_stream: stream where flnd's own log lines go; None leaves them on standard error.
        out_stream: stream where terminal output goes; None leaves it on standard output.
    """

    # Where flnd's own log lines go while the view shows: None leaves them on standard error.
    log: Any = None

    def __init__(
        self,
        *,
        log_stream: Any = None,
        out_stream: Any = None,
    ) -> None:
        self.log = log_stream
        self.out = out_stream
        self._con = _console(out_stream) if out_stream is not None else _console()
        self._err_con = (
            _console(log_stream) if log_stream is not None else _console(sys.stderr)
        )

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
        from rich.text import Text

        from flnd.kit import text

        if report.planned:
            self._con.print(Text.assemble(
                ("planned ", "bold #7D84D6"),
                (text.count(report.planned, "file"), "bold #7D84D6"),
                ("; nothing was downloaded", "color(243)"),
            ))
            return
        line = Text()
        line.append("done: ", style="bold #02BF87")
        line.append(f"{text.count(report.saved, 'file')} saved", style="bold #02BF87")
        if report.bytes:
            line.append(f" ({text.size(report.bytes)})", style="color(245)")
        if report.skipped:
            line.append(", ", style="color(243)")
            line.append(f"{report.skipped} already saved", style="color(243)")
        if report.retried:
            line.append(", ", style="color(243)")
            line.append(f"{report.retried} brought back by a retry", style="bold #02BF87")
        if report.unchanged:
            line.append(", ", style="color(243)")
            line.append(f"nothing new in {text.count(report.unchanged, 'listing')}",
                        style="color(243)")
        self._con.print(line)
        if report.failed:
            failed = [(event.error, bool(event.post)) for event in report.failed]
            self._err_con.print(_summary(failed, details=True))
            for hint in _hints(failed):
                self._err_con.print(Text.assemble(("  ", ""), (hint, "color(243)")))


class _JournalView(_View):
    """Terminal output from run events; the machine record goes to the `.jsonl` journal.

    The run's own display is the summary table the library draws (one row per target and
    category, `flnd.job.Board`), so a view prints no line per file or failure: the table
    counts them, and `end` sums them up.
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
        name = type(event).__name__
        if name == "Progress":
            return
        if self.journal is not None:
            self.journal.write(event)
        if name == "Planned":
            from rich.text import Text

            if event.path is None:
                self._con.print(Text.assemble(
                    ("found ", "color(243)"),
                    (str(event.target), "bold #7D84D6"),
                ))
            else:
                path_str = _shown(event.path)
                folder, filename = os.path.split(path_str)
                folder_part = (folder + os.sep) if folder else ""
                state_style = "bold #7D84D6" if event.state == "new" else "color(243)"
                self._con.print(Text.assemble(
                    (f"{event.state} ", state_style),
                    (folder_part, "color(243)"),
                    (filename, "color(252)"),
                ))


class _Json(_View):
    """Each event as one JSON object per line, then the report as `{"type": "Report", ...}`."""

    def show(self, event: Any) -> None:
        _json(event, stream=self.out)

    def end(self, report: Any) -> None:
        """Write the finished run's report as a JSON object on its own line.

        Args:
            report: the run's report to encode.
        """
        from flnd._events import encode

        body = encode(report).decode()
        stream = self.out or sys.stdout
        stream.write('{"type":"Report",' + body[1:] + "\n")
        if hasattr(stream, "flush"):
            stream.flush()


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


def _hints(failed: Sequence[tuple[BaseException, bool]]) -> list[str]:
    """The hints of a run's failures a person must act on now, each once, in order.

    A login a target needs is one: `flnd retry` can't fetch it, so the hint's command is the
    way on, and the run ends with exit code 3. The rest wait in `flnd status`, which shows
    every failure with its hint.

    Args:
        failed: each failure's error, with whether it was a file's (else a whole target's).

    Returns:
        The hint texts, deduplicated.
    """
    seen: list[str] = []
    for error, _ in failed:
        if _kind(error) != "LoginError":
            continue
        hint = str(getattr(error, "hint", "") or "")
        if hint and hint not in seen:
            seen.append(hint)
    return seen


def _summary(failed: Sequence[tuple[BaseException, bool]], *, details: bool = False) -> Any:
    """The one line that sums a run's failures up, by kind, saying which a retry may bring back.

        ✗ 3 files failed: 2 network (retry: flnd retry), 1 gone. Details: flnd status

    Args:
        failed: each failure's error, with whether it was a file's (else a whole target's).
        details: whether to end with where the details are (`flnd status`).

    Returns:
        Rich Text instance with styled failure counts and hints.
    """
    from rich.text import Text

    from flnd.kit import text

    files = sum(is_file for _, is_file in failed)
    nouns = [text.count(count, noun) for count, noun in
             ((files, "file"), (len(failed) - files, "target")) if count]
    kinds: dict[str, int] = {}
    for error, _ in failed:
        word = _word(error)
        kinds[word] = kinds.get(word, 0) + 1
    retried = [f"{count} {word}" for word, count in kinds.items() if word not in FINAL]
    final = [f"{count} {word}" for word, count in kinds.items() if word in FINAL]
    parts = [f"{', '.join(retried)} (retry: flnd retry)"] if retried else []

    line = Text()
    line.append(f"{_mark()} ", style="bold #ED567A")
    line.append(f"{' and '.join(nouns)} failed", style="bold #ED567A")
    line.append(": ", style="color(243)")
    if parts:
        line.append(", ".join(retried), style="#ED567A")
        line.append(" (retry: ", style="color(243)")
        line.append("flnd retry", style="bold #7D84D6")
        line.append(")", style="color(243)")
        if final:
            line.append(", ", style="color(243)")
    if final:
        line.append(", ".join(final), style="#ED567A")
    if details:
        line.append(". Details: ", style="color(243)")
        line.append("flnd status", style="bold #7D84D6")
    return line


def _mark() -> str:
    """The mark a failure summary starts with.

    Returns:
        "✗"; "x" when standard error's encoding has no "✗" (a pipe on Windows, in cp1252).
    """
    try:
        "✗".encode(getattr(sys.stderr, "encoding", None) or "utf-8")
    except (LookupError, UnicodeEncodeError):
        return "x"
    return "✗"


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
