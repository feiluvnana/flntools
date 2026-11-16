"""The flnd command line: flags in, one library call, events out as lines on the terminal.

    flnd download ins:example_user --only reels --when 2026
    flnd follow tik:example_user
    flnd sync
    flnd login ins chrome

Who calls it: `flnd` (the console script) and `python -m flnd`, through `main`. What it calls:
the library alone (`flnd.open`, its `Run` and events), so anything the command line does, a
script can do too; `flnd.sources._manifest` for the built-in sources `--help` lists;
and the library modules `flnd._prompt` (`flnd login`'s questions) and `flnd._ui` (the level of
flnd's own log lines, and the live view), and `flnd._screen` (the full-screen view).

How a command runs (`main`):
1. argparse reads the flags. A mistake prints the usage and exits 2; `--help` prints the help
   made from each command's docstring, with the target forms and categories of every source,
   and imports no source, no engine and no Rich (it stays under 40 ms).
2. The command function opens the home (`--home`, else `$FLND_HOME`, else `./.flnd`) and makes
   one library call with the flags.
3. A run's events become output (`_Full`, `_Inline`, `_Plain` or `_Json`): the full-screen
   view on a terminal (the default there), a live view under the prompt, a line per saved or
   failed file, or one JSON object per line. With `--exec CMD`, each `Saved` event also runs
   CMD, without a shell (`_Exec`). The end of a run prints a short summary, and its failures
   decide the exit code.
4. Whatever ends the command becomes an exit code (`exit_code`): 0 ok, 1 some files or targets
   failed, 2 usage, 3 a login is needed, 4 flnd itself stopped (a full disk, a missing tool, a
   broken source, a bug), 130 Ctrl-C. A `flnd.Error` prints its message and its hint, never a
   traceback; a bug prints one line naming a crash file, or the traceback with `-v`.

Rules a change must keep:
- Import nothing heavy at the top: argparse, os and sys only. Everything else is imported by
  the command that needs it, so `flnd --help` pays for none of it.
- One flag, one spelling: no aliases. The docs' CLI page (docs/cli) names every command and
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
# The views of a run: the full screen (the default on a terminal), a live view under the
# prompt, or plain lines (the default anywhere else).
VIEWS = ("full", "inline", "plain")


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
            _report(error, as_json=args.json)
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

    `--ui` left out stays None here: the view a run takes then depends on the home's
    `config.toml`, which `_view` reads once the home is open.

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

    --exec CMD runs CMD once for each saved file, after every step (compress included), with
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
                          compress=args.compress, dry=args.dry)
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
        run = fl.follow(targets, only=args.only, when=args.when, compress=args.compress)
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
        return _watch(fl, fl.sync(args.targets, compress=args.compress), args, runner=runner)


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
        print("no run yet")
        return OK
    ended = f", ended {found.ended[11:19]}" if found.ended else ", never ended"
    print(f"last run: {found.command}, {found.started[:10]} {found.started[11:19]}{ended}: "
          f"{found.saved} saved, {len(found.failures)} failed")
    for failure in found.failures:
        where = f"{failure.target} {failure.post}" if failure.post else failure.target
        runs = f", {failure.attempts} runs" if failure.attempts > 1 else ""
        print(f"  {where} ({_word(failure.error)}{runs}): {failure.error}")
        if failure.error.hint:
            print(f"    {failure.error.hint}")
    if found.failures:
        print(_summary([(failure.error, bool(failure.post)) for failure in found.failures]))
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
    from flnd import _text

    with _open(args) as fl:
        found = fl.check(fix=args.fix)
    if args.json:
        from flnd._events import encode

        sys.stdout.write('{"type":"Checked",' + encode(found).decode()[1:] + "\n")
        sys.stdout.flush()
        return OK if found.ok else FAILED
    for record in found.missing:
        print(f"missing     {_shown(record.path)}")
    for path in found.unknown:
        print(f"unknown     {_shown(path)}")
    for record in found.resized:
        print(f"wrong size  {_shown(record.path)}")
    problems = [f"{len(found.missing)} missing"] if found.missing else []
    if found.unknown:
        problems.append(f"{len(found.unknown)} unknown")
    if found.resized:
        problems.append(f"{len(found.resized)} of the wrong size")
    line = f"checked {_text.count(found.files, 'file')}: "
    if not problems:
        print(line + "history and disk agree")
    elif found.fixed:
        print(line + ", ".join(problems) + "; the history now matches the disk")
    else:
        print(line + ", ".join(problems) + "; flnd check --fix makes the history match",
              file=sys.stderr)
    return OK if found.ok else FAILED


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
        version = f" {row.version}" if row.version else ""
        print(f"{row.code}  {row.name}  ({row.origin}{version})")
        if row.disabled:
            print(f"  disabled:   {row.disabled}")
            continue
        if row.origin != "built-in":
            print(f"  from:       {row.where}")
        more = ", …" if len(row.forms) > 3 else ""
        ways = f" ({', '.join(row.logins)})" if row.logins else ""
        print(f"  targets:    {', '.join(row.forms[:3])}{more}")
        print(f"  categories: {', '.join(row.categories)}")
        print(f"  login:      {row.login}{ways}")
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
    _again(sub)
    _exec(sub)

    sub = command(follow)
    sub.add_argument("targets", nargs="*", metavar="target",
                     help="the targets to follow; read from stdin when none")
    _only(sub)
    _when(sub)
    _compress(sub)
    _again(sub)

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
                       help="how a run shows: the full screen, a live view under the prompt, "
                            "or plain lines (default: full on a terminal, else plain)")
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


def _compress(sub: argparse.ArgumentParser) -> None:
    # `--compress` and `--no-compress`; neither leaves it None, and the library reads
    # `[defaults] compress` from config.toml.
    sub.add_argument("--compress", action=argparse.BooleanOptionalAction, default=None,
                     help="shrink photos and videos as they land when a smaller copy looks the "
                          "same, and remove each archive once it is extracted; --no-compress "
                          "turns off [defaults] compress for this run")


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
    from flnd import _prompt
    from flnd.sources import _manifest

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

def _watch(fl: flnd.Flnd, run: flnd.Run, args: argparse.Namespace, *,
           runner: _Exec | None = None) -> int:
    """Read a run's events into the chosen view, print its end, and return its exit code.

    flnd's own log lines are held to warnings for the run (a wait, a login about to expire),
    or opened to debug lines with `-v`: the events already say what each file did. The full
    screen shows info lines too, in its log panel, where they don't scroll anything away.

    Args:
        fl: the open home, whose `config.toml` may choose the view.
        run: the run, not started yet.
        args: the parsed flags: `--json`, `--ui`, `-v`.
        runner: `--exec`'s command, run for each `Saved` event; None runs nothing.

    Returns:
        0 when nothing failed, 3 when a target needs a login, else 1.

    Raises:
        Error: what stopped the whole run (a full disk), from the run.
        Stopped: Ctrl-C ended it.
    """
    from contextlib import ExitStack

    from flnd._ui import ui

    view: _View
    chosen = _view(fl, args)
    if args.json:
        view = _Json()
    elif chosen == "full":
        view = _Full(run, verbose=args.verbose)
    elif chosen == "inline":
        view = _Inline(verbose=args.verbose)
    else:
        view = _Plain(verbose=args.verbose)
    quiet = "info" if chosen == "full" else "warn"
    with ExitStack() as stack:
        # The view first, so the log's scope (which may write into it) ends before it does.
        stack.enter_context(view)
        stack.enter_context(ui.scope(level="debug" if args.verbose else quiet, dest=view.log))
        for event in run:
            view.show(event)
            # The run waits while this runs, so one command at a time, in the order files end.
            if runner is not None and type(event).__name__ == "Saved":
                runner.run(event)
    report = run.report
    view.end(report)
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

        posix = os.name != "nt"
        try:
            argv = shlex.split(command, posix=posix)
        except ValueError as error:
            raise flnd.UsageError(f"--exec: {error}: {command!r}") from None
        if not posix:
            argv = [arg[1:-1] if len(arg) > 1 and arg[0] == arg[-1] == '"' else arg
                    for arg in argv]
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
            print(f"--exec: {argv[0]}: {error.strerror or error}", file=sys.stderr)
            return 127
        if done.returncode:
            print(f"--exec: {argv[0]} exited {done.returncode} for {_shown(event.path)}",
                  file=sys.stderr)
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


def _view(fl: flnd.Flnd, args: argparse.Namespace) -> str:
    """The view a run takes: `--ui`, else the home's `[defaults] ui`, else full screen on a
    terminal; plain where nobody watches (standard error isn't a terminal).

    The full screen needs a terminal to take over, so off one it is plain whatever was asked;
    `--ui inline` is kept as asked, and draws its last frame when the run ends.

    Args:
        fl: the open home.
        args: the parsed flags; `args.ui` is None when `--ui` was left out.

    Returns:
        "full", "inline" or "plain".
    """
    terminal = sys.stderr.isatty()
    chosen = args.ui or (fl.ui if terminal else None) or ("full" if terminal else "plain")
    return "plain" if chosen == "full" and not terminal else chosen


def _kind(error: BaseException) -> str:
    """The name of the nearest class of `error` that `CODES` lists; "" for none."""
    return next((kind.__name__ for kind in type(error).__mro__ if kind.__name__ in CODES), "")


class _View:
    """How a run shows: `show` each event as it comes, then `end` with the report.

    A view is a context manager, so a live display stops even when the run raises.

    Args:
        verbose: also show what a person rarely needs (each skipped file).
    """

    # Where flnd's own log lines go while the view shows: None leaves them on standard error.
    log: Any = None

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
        if report.retried:
            done.append(f"{report.retried} brought back by a retry")
        if report.unchanged:
            done.append(f"nothing new in {_text.count(report.unchanged, 'listing')}")
        print("done: " + ", ".join(done))
        if report.failed:
            failed = [(event.error, bool(event.post)) for event in report.failed]
            print(_summary(failed, details=True), file=sys.stderr)


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
        A `Stats` snapshot isn't shown: this view keeps to its rows.

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


class _Full(_View):
    """The full-screen view (`flnd._screen.Screen`) on the terminal's alternate screen.

    `q` stops the run gracefully, `p` pauses and resumes it, and Ctrl-C still works in its two
    stages. flnd's log lines go to the screen's log panel. Leaving it restores the terminal,
    then prints what scrollback should keep: a dry run's plan and each failure with its hint;
    `end` then prints the summary under them, as the other views do.

    Args:
        run: the run, whose name is the screen's title and which the keys act on.
        verbose: as `_View` takes it.
    """

    def __init__(self, run: Any, *, verbose: bool = False) -> None:
        super().__init__(verbose=verbose)
        from flnd._screen import Screen

        self.screen = Screen(run.name, run=run)
        self.log = self.screen.log

    def __enter__(self) -> _Full:
        self.screen.__enter__()
        return self

    def __exit__(self, *exc: object) -> None:
        try:
            self.screen.__exit__(*exc)
        finally:
            self.screen.close()

    def show(self, event: Any) -> None:
        """Store the event for the next frame.

        Args:
            event: one event of the run.
        """
        self.screen.show(event)


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


def _summary(failed: Sequence[tuple[BaseException, bool]], *, details: bool = False) -> str:
    """The one line that sums a run's failures up, by kind, saying which a retry may bring back.

        ✗ 3 files failed: 2 network (retry: flnd retry), 1 gone. Details: flnd status

    Args:
        failed: each failure's error, with whether it was a file's (else a whole target's).
        details: whether to end with where the details are (`flnd status`).

    Returns:
        The line.
    """
    from flnd import _text

    files = sum(is_file for _, is_file in failed)
    nouns = [_text.count(count, noun) for count, noun in
             ((files, "file"), (len(failed) - files, "target")) if count]
    kinds: dict[str, int] = {}
    for error, _ in failed:
        word = _word(error)
        kinds[word] = kinds.get(word, 0) + 1
    retried = [f"{count} {word}" for word, count in kinds.items() if word not in FINAL]
    final = [f"{count} {word}" for word, count in kinds.items() if word in FINAL]
    parts = [f"{', '.join(retried)} (retry: flnd retry)"] if retried else []
    line = f"{_mark()} {' and '.join(nouns)} failed: {', '.join(parts + final)}"
    return line + ". Details: flnd status" if details else line


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
