"""The flnd command line: flags in, one library call, events out as lines.

SPEC.md#commands-and-flags explains it."""

from __future__ import annotations

import argparse
import os
import sys
from collections.abc import Callable, Sequence
from typing import TYPE_CHECKING, Any

from flnd import __version__, errors
from flnd.plugins import TABLE

if TYPE_CHECKING:
    from flnd import _library
    from flnd.engine.run import Run

__all__ = ["exit_code", "main", "parser"]

# The exit codes (SPEC.md#when-it-goes-wrong). A cron job can tell "some files failed" (1)
# from "flnd itself stopped" (4).
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
    ("PluginError", BROKE),
    ("Error", FAILED),
)
# The most failed targets `status` lists before "and N more".
SHOWN = 10


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
        (getattr(args, "group", None) or parser()).print_help()
        return OK
    try:
        return args.command(args)
    except KeyboardInterrupt:
        # `errors.Stopped` is one: Ctrl-C during a run, which the run already stopped cleanly.
        out.line(("stopped", "warn"), err=True)
        return STOPPED
    except Exception as error:

        if isinstance(error, errors.Error):
            out.error(error)
            return exit_code(error)
        return _crashed(error, args)


def exit_code(error: BaseException | None) -> int:
    """The exit code an error ends a command with, by its class; 0 for None.

    Args:
        error: the exception that ended the command; None if the command finished successfully.

    Returns:
        0 if the command succeeded, else the code of the first class in `CODES` the error is an
        instance of: 130 for `errors.Stopped`, 2, 3, 4 or 1 for an `errors.Error`, and 4 for
        anything else.

    Example:
        >>> exit_code(errors.LoginError("ins: not logged in")), exit_code(errors.Blocked("wait"))
        (3, 1)
    """
    if error is None:
        return OK

    for name, code in CODES:
        if isinstance(error, getattr(errors, name)):
            return code
    return BROKE


def _settle(args: argparse.Namespace) -> None:
    """Give each global flag left out its default."""
    for name, default in (("home", None), ("verbose", False)):
        if not hasattr(args, name):
            setattr(args, name, default)
    if not hasattr(args, "command"):
        args.command = None


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
    import traceback

    from flnd.ui import log, out

    trace = "".join(traceback.format_exception(error))
    first = (str(error).strip().splitlines() or [""])[0]
    line = f"flnd hit a bug: {type(error).__name__}" + (f": {first}" if first else "")
    if not args.verbose:
        path = log.crash(args.home or os.environ.get("FLND_HOME") or ".flnd", trace)
        if path is not None:
            out.error(errors.Error(line, hint=f"details in {path}"))
            return BROKE
    out.plain(trace, err=True)
    out.error(errors.Error(line))
    return BROKE


# ---------------------------------------------------------------------------------------------
# The commands: each docstring is the command's help
# ---------------------------------------------------------------------------------------------

def _lower_priority() -> None:
    """Lower process priority for heavy runs on Windows, macOS and Linux."""
    if sys.platform == "win32":
        try:
            import ctypes

            kernel32 = ctypes.windll.kernel32
            handle = kernel32.GetCurrentProcess()
            if kernel32.GetPriorityClass(handle) in (0x20, 0x80, 0x100, 0x8000):
                kernel32.SetPriorityClass(handle, 0x4000)
        except Exception:
            pass
        return
    if hasattr(os, "nice"):
        try:
            cur = os.nice(0)
            if cur < 5:
                os.nice(5 - cur)
        except OSError:
            pass


def get(args: argparse.Namespace) -> int:
    """Get what the targets have, and follow them, so `flnd get` alone keeps them up to date.

    A target that isn't a single post is added to the followed ones; the categories it names
    (ins:example_user|reels) are kept with it. With no target, from the arguments or standard
    input (one per line; blank lines and lines starting with # are skipped), every followed
    target is synced.

    Args:
        args: the parsed flags.

    Returns:
        The run's exit code.
    """
    _lower_priority()
    targets = _targets(args.targets)
    with _open(args) as fl:
        if not targets and not any(row.follow for row in fl.targets):
            if args.folder is not None:
                fl.get(when=args.when, limit=args.limit, dry=args.dry,
                       folder=args.folder).wait()
            from flnd.ui import out

            out.line(("nothing to sync", "dim"))
            return OK
        run = fl.get(*targets, when=args.when, limit=args.limit, dry=args.dry,
                     folder=args.folder)
        return _watch(fl, run, args)


def sync(args: argparse.Namespace) -> int:
    """Sync followed targets matching any glob pattern, or every followed target.

    Each pattern is a case-insensitive glob (*, ?, [seq]) matched against followed targets
    (ins:example_user, twt?words). With no pattern, every followed target is synced.
    """
    _lower_priority()
    with _open(args) as fl:
        run = fl.sync(*args.patterns, when=args.when, limit=args.limit, dry=args.dry)
        if run.name == "sync":
            from flnd.ui import out

            out.line(("nothing to sync", "dim"))
            return OK
        return _watch(fl, run, args, "sync")


def target_list(args: argparse.Namespace) -> int:
    """List the targets this home knows: followed or not, status, files, size, last run.

    Args:
        args: the parsed flags.

    Returns:
        0.
    """
    from flnd.ui import out

    with _open(args) as fl:
        rows = list(fl.targets)
    _table(("target", "followed", "categories", "status", "files", "size", "last run"),
           [(str(row.target), "followed" if row.follow else "-", " ".join(row.categories),
             _status(row), f"{row.seen:,} found" if row.query else f"{row.files:,} files",
             out.size(row.size) if row.size else "-", _ago(row.at))
            for row in rows])
    return OK


def _status(row: Any) -> str:
    """A target's download state: "synced", "partial", "new", or "3 failed" and "3 retry"."""
    state = row.status or ("new" if not row.at and not row.files else "synced")
    return f"{row.failed:,} {state}" if state in ("failed", "retry") and row.failed else state


def target_follow(args: argparse.Namespace) -> int:
    """Follow targets, so `flnd get` and `flnd sync` take them; nothing is downloaded.

    Args:
        args: the parsed flags.

    Returns:
        0.
    """
    with _open(args) as fl:
        done = fl.targets.follow(*args.targets)
    for row in done:
        _line(f"{row.target}: followed")
    return OK


def target_unfollow(args: argparse.Namespace) -> int:
    """Stop following known targets; their files and history stay.

    Args:
        args: the parsed flags.

    Returns:
        0.
    """
    with _open(args) as fl:
        done = fl.targets.unfollow(*args.targets)
    for row in done:
        _line(f"{row.target}: unfollowed")
    return OK


def target_retry(args: argparse.Namespace) -> int:
    """Make known targets' failed files retryable: their next get tries them again.

    A file the site said is gone (404 or 410) is never retried.

    Args:
        args: the parsed flags.

    Returns:
        0.
    """
    with _open(args) as fl:
        done = fl.targets.retry(*args.targets)
    for row in done:
        _line(f"{row.target}: failed files will be retried")
    return OK


def target_clean(args: argparse.Namespace) -> int:
    """Forget targets: their rows, files rows, failures, cursors, heads and gaps.

    --media also deletes their recorded files, links, extracted folders and the
    folders left empty. Files under media/ that nothing recorded are never deleted.

    Args:
        args: the parsed flags.

    Returns:
        0.
    """
    with _open(args) as fl:
        if not _sure(fl, args):
            _line("kept: nothing was forgotten")
            return OK
        forgotten = fl.targets.clean(*args.targets, media=args.media)
    for row in forgotten:
        _line(f"{row.target}: forgotten" + (" with its files" if args.media else ""))
    return OK


def _sure(fl: _library.Flnd, args: argparse.Namespace) -> bool:
    """Ask "Forget 2 targets and delete 1.2 GB?" of a terminal; a pipe isn't asked."""
    from flnd.ui import forms, out

    named = {str(each) for each in args.targets}
    size = sum(row.size for row in fl.targets if str(row.target) in named)
    title = f"Forget {out.count(len(named), 'target')}"
    if args.media:
        title += f" and delete {out.size(size)}" if size else " and delete their files"
    return forms.confirm(title + "?")


def auth_list(args: argparse.Namespace) -> int:
    """List the logins: each plugin that has one, whether it is usable, and its file.

    Args:
        args: the parsed flags.

    Returns:
        0.
    """
    from flnd.ui import out

    with _open(args) as fl:
        rows = list(fl.auth)
    if not rows:
        out.line(("no logins", "dim"))
    else:
        _table(("plugin", "status", "file"), [(row.plugin, row.status, row.file) for row in rows])
    return OK


def auth_login(args: argparse.Namespace) -> int:
    """Log in to a plugin, and keep the login in the logins folder.

    HOW is the cookie file (a cookies.txt or JSON export) for a cookie plugin, or the
    plugin's own way (tlg: qr, phone). A cookie file left out in a terminal is asked.

    Args:
        args: the parsed flags.

    Returns:
        0.

    Raises:
        UsageError: the cookie file is left out where nobody can be asked (no terminal).
    """
    method, path = _how(args)
    with _open(args) as fl:
        done = fl.auth.login(args.plugin, method=method, path=path)
    _line(f"{done.plugin}: logged in ({done.status})")
    return OK


def auth_logout(args: argparse.Namespace) -> int:
    """Log out of a plugin: delete its saved login.

    Args:
        args: the parsed flags.

    Returns:
        0.
    """
    from flnd.ui import forms

    with _open(args) as fl:
        if not forms.confirm(f"Log out of {args.plugin}?"):
            _line("kept: nobody was logged out")
            return OK
        gone = fl.auth.logout(args.plugin)
    _line(f"{gone.plugin}: logged out")
    return OK


def status(args: argparse.Namespace) -> int:
    """Show the last run, its failures by target, the logins that need care, and the disk.

    A failure a later run saved is left out. A failed file is retried by itself at the start of
    its target's next 3 gets; `flnd target retry` asks for it again after that.

    --fix repairs the history rows whose file is missing or the wrong size, without asking; on a
    terminal, `flnd status` alone offers to.

    Args:
        args: the parsed flags.

    Returns:
        0 when all is well, else 1.
    """
    from flnd.ui import forms, out

    with _open(args) as fl:
        found = fl.status
        logins = [row for row in fl.auth if row.status != "ok"]
        checked = fl.check(fix=args.fix)
    well = (found is None or not found.failures) and not logins and checked.ok
    if found is None:
        out.line(("no run yet", "dim"))
    else:
        _last(found)
    for row in logins:
        out.line(("  login ", "dim"), (row.plugin, "bold focus"), ": ", (row.status, "warn"))
    _disk(checked)
    rows = len(checked.missing) + len(checked.resized)
    if rows and not checked.fixed and forms.confirm(f"Repair {out.count(rows, 'row')}?",
                                                    default=False):
        with _open(args) as fl:
            checked = fl.check(fix=True)
        _disk(checked)
        well = (found is None or not found.failures) and not logins and checked.ok
    return OK if well else FAILED


def config(args: argparse.Namespace) -> int:
    """Show every setting of config.toml: its value, its default and where it comes from.

    Edit the settings by hand, in the home's file or in the user's file.

    Args:
        args: the parsed flags.

    Returns:
        0.
    """
    from flnd.ui import out

    with _open(args) as fl:
        _table(("key", "value", "default", "from"),
               [(key, _show(fl.config[key]), _show(fl.config.default(key)), fl.config.where(key))
                for key in fl.config])
        home, user = fl.config.files
    out.line(("edit ", "dim"), (str(home), "bold focus"), (" for this home, or ", "dim"),
             (str(user), "bold focus"), (" for every home", "dim"))
    return OK


def _show(value: Any) -> str:
    """A setting's value as the table shows it."""
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value) if value not in ("", None) else "-"


def _last(found: Any) -> None:
    """Print the last run and its failures, grouped by target."""
    from flnd.ui import out

    ended = f", ended {found.ended[11:19]}" if found.ended else ", never ended"
    out.line(("last run: ", "bold focus"), (found.command, "bold"),
             (f", {found.started[:10]} {found.started[11:19]}{ended}: ", "dim"),
             (f"{found.saved} saved", "bold good"), (", ", "dim"),
             (f"{len(found.failures)} failed", "bold bad" if found.failures else "dim"))
    groups: dict[str, list[Any]] = {}
    for failure in found.failures:
        groups.setdefault(str(failure.target), []).append(failure)
    for name, failures in list(groups.items())[:SHOWN]:
        first = failures[0]
        runs = f", {first.attempts} runs" if first.attempts > 1 else ""
        out.line("  ", (name, "bold focus"),
                 (f"  {out.count(len(failures), 'failure')} ({_word(first.error)}{runs}): ",
                  "dim"), (str(first.error), "bad"))
        if first.error.hint:
            out.line("    ", (str(first.error.hint), "dim"))
    if len(groups) > SHOWN:
        out.line(("  and ", "dim"), out.count(len(groups) - SHOWN, "more target"))


def _disk(found: Any) -> None:
    """Print how the disk compares to the history."""
    from flnd.ui import out

    problems = [f"{len(found.missing)} missing"] if found.missing else []
    if found.unknown:
        problems.append(f"{len(found.unknown)} unknown")
    if found.resized:
        problems.append(f"{len(found.resized)} of the wrong size")
    line = f"disk: {out.count(found.files, 'file')}: "
    if not problems:
        out.line((line, "dim"), ("history and disk agree", "bold good"))
    elif found.fixed:
        out.line((line, "dim"), ("repaired " + ", ".join(problems), "bold good"),
                 ("; the history now matches the disk", "dim"))
    else:
        out.line((line, "dim"), (", ".join(problems), "bold bad"))


# ---------------------------------------------------------------------------------------------
# The parser
# ---------------------------------------------------------------------------------------------

class _Parser(argparse.ArgumentParser):
    """argparse, with its help and its errors printed by `flnd.ui.out`."""

    # Rows to end the help with, or a function making them when the help is printed.
    tail: Sequence[tuple[str, Sequence[Any]]] | Callable[[], Sequence[tuple[str, Sequence[Any]]]]
    tail = ()

    def print_help(self, file: Any = None) -> None:
        """Print the help in the theme; `file` is ignored, as the help goes to standard output."""
        from flnd.ui import out

        out.help(self, tail=self.tail() if callable(self.tail) else self.tail)

    def error(self, message: str) -> Any:
        """A mistake in the flags: print it as any usage error is, with the help to read, and
        exit 2."""
        from flnd.ui import out

        out.error(errors.UsageError(f"{self.prog}: {message}",
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
    """The whole command line: the global flags, then one sub-parser per command or group.

    Each command's help is its function's docstring: its first clause in the list of commands,
    the paragraphs before `Args:` on its own `--help`.

    Returns:
        The parser; `parse_args` sets `command` to the command's function.
    """

    top = _Parser(prog="flnd", description="download whole accounts from the web",
                  parents=[_globals()])
    top.add_argument("-V", "--version", action=_Version, version=f"flnd {__version__}")
    top.tail = [("plugins", [" ".join(entry.code for entry in TABLE)]),
                ("", [("a plugin's targets: flnd get -h", "dim")])]
    verbs = top.add_subparsers(title="commands", metavar="<command>")

    def command(owner: Any, name: str, function: Callable[[argparse.Namespace], int]
                ) -> _Parser:
        sub = owner.add_parser(name, help=_short(_help(function)),
                               description=_help(function), parents=[_globals()])
        sub.set_defaults(command=function)
        return sub

    def group(name: str, help: str) -> Any:
        sub = verbs.add_parser(name, help=help, description=help, parents=[_globals()])
        sub.set_defaults(command=None, group=sub)
        return sub.add_subparsers(title="commands", metavar="<command>")

    sub = command(verbs, "get", get)
    sub.add_argument("targets", nargs="*", metavar="target",
                     help="ins:example_user, \"ins:example_user|reels,posts\", a link, or "
                          "mht?words; read from stdin too; none syncs every followed target")
    _when(sub)
    sub.add_argument("--limit", type=int, metavar="N",
                     help="at most N posts per target, or N targets per search")
    sub.add_argument("--dry", action="store_true",
                     help="list what would be saved, and save and write nothing")
    sub.add_argument("--folder", metavar="TEMPLATE",
                     help="save under media/<plugin>/<TEMPLATE>/ instead of each target's folder; "
                          "TEMPLATE is one, or a list like posts:{target}/feed,*:{target}/other; "
                          "a search needs one")
    _again(sub)
    sub.tail = _forms

    sub = command(verbs, "sync", sync)
    sub.add_argument("patterns", nargs="*", metavar="pattern",
                     help="glob patterns matching followed targets (ins:*, *example*); "
                          "none syncs every followed target")
    _when(sub)
    sub.add_argument("--limit", type=int, metavar="N",
                     help="at most N posts per target, or N targets per search")
    sub.add_argument("--dry", action="store_true",
                     help="list what would be saved, and save and write nothing")
    _again(sub)

    targets = group("target", "List the targets flnd knows, follow, unfollow, retry or forget some")
    command(targets, "list", target_list)
    for name, function in (("follow", target_follow), ("unfollow", target_unfollow),
                           ("retry", target_retry)):
        sub = command(targets, name, function)
        sub.add_argument("targets", nargs="+", metavar="target",
                         help="the targets, as `flnd target list` shows them")
    sub = command(targets, "clean", target_clean)
    sub.add_argument("targets", nargs="+", metavar="target", help="the targets to forget")
    sub.add_argument("--media", action="store_true",
                     help="also delete their files, links and extracted folders")

    logins = group("auth", "List the logins, log in to a plugin or out of one")
    command(logins, "list", auth_list)
    sub = command(logins, "login", auth_login)
    sub.add_argument("plugin", help="the plugin's code (ins)")
    sub.add_argument("how", nargs="?", metavar="file",
                     help="the cookie file's path; for tlg, its way (qr, phone)")
    sub = command(logins, "logout", auth_logout)
    sub.add_argument("plugin", help="the plugin's code (ins)")

    sub = command(verbs, "status", status)
    sub.add_argument("--fix", action="store_true",
                     help="repair the history rows of missing or resized files, without asking")
    command(verbs, "config", config)
    return top


def _globals() -> argparse.ArgumentParser:
    """The flags every command takes, before or after its name."""
    shared = _Parser(add_help=False)
    group = shared.add_argument_group("global flags")
    group.add_argument("--home", metavar="DIR", default=argparse.SUPPRESS,
                       help="where downloads, history and logins live "
                            "(default: $FLND_HOME, else ./.flnd)")
    group.add_argument("-v", "--verbose", action="store_true", default=argparse.SUPPRESS,
                       help="show flnd's debug lines, and a bug's traceback")
    return shared


def _when(sub: argparse.ArgumentParser) -> None:
    # Left out, it is None, and the library reads `[defaults] when` from config.toml.
    sub.add_argument("--when", metavar="WHEN",
                     help="posts from 2026, 2026-06, 2026-06-15, 2026-01..2026-03, or 30d; "
                          "\"\" for every day")


def _again(sub: argparse.ArgumentParser) -> None:
    """Add `--again` to a command that gets."""
    sub.add_argument("--again", action="store_true",
                     help="list every post again, and ask again for files the site said are "
                          "gone (404 or 410)")


def _help(function: Callable[..., Any]) -> str:
    """A command's help: its docstring up to `Args:`, dedented."""
    import inspect

    text = inspect.cleandoc(function.__doc__ or "")
    return text.split("\n\nArgs:")[0].strip()


def _short(text: str) -> str:
    """A command's row in `flnd -h`: its help up to the first comma, colon or full stop."""
    import re

    return re.split(r"[,:.](?:\s|$)", text, maxsplit=1)[0]


def _classes() -> list[Any]:
    """The class of every built-in plugin, imported: only a help that lists them needs this."""
    import importlib

    return [getattr(importlib.import_module(entry.module), entry.cls) for entry in TABLE]


def _methods(code: str) -> tuple[str, ...]:
    """The login methods of a built-in plugin; none for any other code."""
    import importlib

    entry = next((entry for entry in TABLE if entry.code == code), None)
    if entry is None:
        return ()
    login = getattr(importlib.import_module(entry.module), entry.cls).login
    return tuple(login.methods) if login is not None else ()


def _forms() -> list[tuple[str, list[Any]]]:
    """A row per built-in plugin for a command's help: the targets it reads, its categories."""
    rows: list[tuple[str, list[Any]]] = [("plugins", [("the targets each reads, then its "
                                                      "categories", "dim")])]
    for cls in _classes():
        own = [form for form in cls.forms if not form.startswith("http")]
        on = [f"a link on {', '.join(cls.domains)}"] if cls.domains else []
        cats: list[Any] = [("\n|", "dim"), ",".join(cls.categories)] if cls.categories else []
        rows.append((cls.code, [", ".join([*own, *on]), *cats]))
    return rows


# ---------------------------------------------------------------------------------------------
# Helpers of the commands
# ---------------------------------------------------------------------------------------------

def _open(args: argparse.Namespace) -> _library.Flnd:
    """The library over `--home`; the caller uses it in a `with`."""
    from flnd import _library

    return _library.open(args.home, again=True if getattr(args, "again", False) else None)


def _targets(given: Sequence[str]) -> list[str]:
    """The targets on the command line, then the lines of a piped standard input."""
    found = list(given)
    if not sys.stdin.isatty():
        lines = [line.strip() for line in sys.stdin]
        found += [line for line in lines if line and not line.startswith("#")]
    return found


def _how(args: argparse.Namespace) -> tuple[str, str | None]:
    """The method and the cookie file of a login: a cookie plugin's HOW is its file (asked in a
    terminal when left out), any other's is its method, the first it has by default."""
    from flnd.ui import forms

    code = args.plugin
    ways = _methods(code)
    if not ways:
        raise errors.UsageError(f"unknown plugin {code!r}", hint="flnd --help lists the plugins")
    if ways != ("file",):
        return args.how or ways[0], None
    if args.how:
        return "file", args.how
    if not forms.interactive():
        raise errors.UsageError(f"{code}: which cookie file?",
                                hint=f"flnd auth login {code} <file>")
    return "file", (forms.text("Cookie file") or "").strip()


def _ago(at: str) -> str:
    """`2h ago`: the largest whole unit since an ISO 8601 time; "never" for none."""
    import datetime

    if not at:
        return "never"
    seconds = (datetime.datetime.now(datetime.UTC) - datetime.datetime.fromisoformat(at)
               ).total_seconds()
    for unit, length in (("d", 86400), ("h", 3600), ("m", 60)):
        if seconds >= length:
            return f"{int(seconds // length)}{unit} ago"
    return "just now"


def _line(text: str) -> None:
    """Print one result as styled `text`: "head: tail" with the head in focus."""
    from flnd.ui import out

    head, colon, tail = text.partition(": ")
    if not colon:
        out.line(text)
        return
    out.line((head, "bold focus"), (colon, "dim"), tail)


def _table(header: Sequence[str], rows: Sequence[Sequence[str]]) -> None:
    """Print rows as a table: the first column in focus, and each state in its colour."""
    from flnd.ui import out

    out.table([[_cell(index, str(cell)) for index, cell in enumerate(row)] for row in rows],
              header)


def _cell(index: int, text: str) -> tuple[str, str]:
    """A table cell with its style: the first column in focus, then by what the text says."""
    if index == 0:
        return text, "bold focus"
    if text in ("followed", "ok", "on", "default", "true"):
        return text, "bold good"
    if text in ("-", "never", "off", "false"):
        return text, "dim"
    if "expired" in text or text == "disabled":
        return text, "bold bad"
    if " in " in text or text.startswith("held"):
        return text, "warn"
    return text, "text"


# ---------------------------------------------------------------------------------------------
# Watching a run
# ---------------------------------------------------------------------------------------------

def _watch(fl: _library.Flnd, run: Run, args: argparse.Namespace, name: str = "get") -> int:
    """Read a run's events into the chosen view, print its end, and return its exit code."""
    from contextlib import ExitStack

    from flnd.ui.log import stamped, ui
    from flnd.ui.screen import Screen

    journal_path = stamped(fl.home, name, ".jsonl")
    view = _JournalView()
    with ExitStack() as stack:
        try:
            journal = stack.enter_context(ui.file(journal_path))
        except OSError:
            journal = None
        view.journal = journal
        stack.enter_context(view)
        stack.enter_context(ui.scope(level="debug" if args.verbose else "warn"))
        if journal is not None:
            journal.write({"type": "Start", "command": name, "version": __version__})
        # The screen ends before the summary: its last frame stays above it.
        with Screen(lambda: run.state, command=name):
            for event in run:
                view.show(event)
            report = run.report
            # The warnings still folded print above the screen, never after the summary.
            ui.drain()
        view.end(report, run.state)
        if journal is not None:
            journal.write(report)
    if any(_login(failed.error) for failed in report.failed):
        return LOGIN
    return FAILED if report.failed else OK


def _login(error: BaseException) -> bool:
    """Whether `error` is a `errors.LoginError`: a login the target needs, exit code 3."""

    return isinstance(error, errors.LoginError)


class _View:
    """How a run shows: `show` each event as it comes, then `end` with the report."""

    journal: Any = None

    def __enter__(self) -> _View:
        return self

    def __exit__(self, *exc: object) -> None:
        return None

    def show(self, event: Any) -> None:
        """Show one event."""

    def end(self, report: Any, state: Any) -> None:
        """Show the run's end summary (`flnd.ui.screen.summary`); a dry run's planned files.

        Args:
            report: the run's `Report`.
            state: its last snapshot (`run.state`).
        """
        from flnd.ui import out, screen

        if report.planned:
            out.line(("planned ", "bold focus"), (out.count(report.planned, "file"),
                                                 "bold focus"), ("; nothing was saved", "dim"))
            return
        screen.summary(report, state)


class _JournalView(_View):
    """Terminal output from run events; the machine record goes to the `.jsonl` journal."""

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


# Each error class's word in a failure line, by class name; the first class in an error's MRO
# that is listed wins.
WORDS = {
    "NotFound": "gone", "Blocked": "blocked", "Changed": "changed", "LoginError": "login",
    "SiteError": "site", "NetworkError": "network", "DiskError": "disk", "ToolError": "tool",
    "PluginError": "bug", "UsageError": "usage",
}


def _word(error: BaseException) -> str:
    """The word a failure line counts an error under: "network", "gone", …; else "error"."""
    return next((WORDS[kind.__name__] for kind in type(error).__mro__ if kind.__name__ in WORDS),
                "error")


def _shown(path: Any) -> str:
    """A path as short as it can be shown: relative to the current folder."""
    try:
        return os.path.relpath(path)
    except ValueError:
        # On Windows, a path on another drive has no relative form.
        return str(path)
