"""The flnd command line: flags in, one library call, events out as lines on the terminal.

    flnd get ins:example_user/reels --when 2026
    flnd target list
    flnd auth login ins chrome
    flnd status

Who calls it: `flnd` (the console script) and `python -m flnd`, through `main`. What it calls:
the library alone (`flnd.open`, its `Run` and events), so anything the command line does, a
script can do too; `flnd.plugins._manifest` for the built-in plugins `--help` lists; and
`flnd.ui` for every line it prints and every question it asks.

How a command runs (`main`):
1. argparse reads the flags. A mistake prints an error and exits 2; `--help` prints the help
   made from each command's docstring, drawn in the theme by `flnd.ui.out.help`: `flnd -h`
   fits in 24 lines, and `flnd get -h` adds a row per plugin.
2. The command function opens the home (`--home`, else `$FLND_HOME`, else `./.flnd`) and makes
   one library call with the flags.
3. A run's own display is the screen the library's events draw on a terminal (`flnd.ui.screen`);
   a dry run prints its listing, `--json` prints one JSON object per line. With `--exec CMD`,
   each `Saved` event also runs CMD, without a shell (`_Exec`). The end of a run prints a short
   summary, and its failures decide the exit code.
4. Whatever ends the command becomes an exit code (`exit_code`): 0 ok, 1 some files or targets
   failed, 2 usage, 3 a login is needed, 4 flnd itself stopped (a full disk, a missing tool, a
   broken plugin, a bug), 130 Ctrl-C. A `flnd.Error` prints its message and its hint, never a
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
    ("PluginError", BROKE),
    ("Error", FAILED),
)
# The most failed targets `status` lists before "and N more".
SHOWN = 10
# An account whose login ends within this many days is listed by `status` as expiring.
SOON_DAYS = 7


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
        out.json({"type": "Error", "error": error})


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

    import flnd
    from flnd.ui import log, out

    trace = "".join(traceback.format_exception(error))
    first = (str(error).strip().splitlines() or [""])[0]
    line = f"flnd hit a bug: {type(error).__name__}" + (f": {first}" if first else "")
    if not args.verbose:
        path = log.crash(args.home or os.environ.get("FLND_HOME") or ".flnd", trace)
        if path is not None:
            out.error(flnd.Error(line, hint=f"details in {path}"))
            return BROKE
    out.plain(trace, err=True)
    out.error(flnd.Error(line))
    return BROKE


# ---------------------------------------------------------------------------------------------
# The commands: each docstring is the command's help
# ---------------------------------------------------------------------------------------------

def get(args: argparse.Namespace) -> int:
    """Get what the targets have, and follow them, so `flnd get` alone keeps them up to date.

    A target that isn't a single post is added to the followed ones; the categories it names
    (ins:example_user/reels) are kept with it. With no target, from the arguments or standard
    input (one per line; blank lines and lines starting with # are skipped), every followed
    target is synced.

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
        if not targets and not any(row.follow for row in fl.targets):
            from flnd.ui import out

            out.line(("nothing to sync", "dim"))
            return OK
        run = fl.get(*targets, when=args.when, limit=args.limit, dry=args.dry)
        return _watch(fl, run, args, runner=runner)


def target_list(args: argparse.Namespace) -> int:
    """List the targets this home knows: followed or not, files, size, last run.

    Args:
        args: the parsed flags.

    Returns:
        0.
    """
    with _open(args) as fl:
        rows = list(fl.targets)
    if args.json:
        for row in rows:
            _json(row)
        return OK
    from flnd.ui import out

    table = []
    for row in rows:
        held = (out.count(row.seen, "target") + " found" if row.query
                else f"{out.count(row.files, 'file')}, {out.size(row.size)}")
        table.append((str(row.target), "followed" if row.follow else "-", held,
                      row.at[:10] or "never", " ".join(row.categories)))
    if not table:
        out.line(("no targets yet", "dim"))
        return OK
    _table(("target", "follow", "files", "last", "categories"), table)
    return OK


def target_clean(args: argparse.Namespace) -> int:
    """Forget targets: their rows, files rows, failures, cursors, heads and gaps.

    --media also deletes their recorded files, sidecars, links, extracted folders and the
    folders left empty. Files under media/ that nothing recorded are never deleted.

    Args:
        args: the parsed flags.

    Returns:
        0.
    """
    with _open(args) as fl:
        forgotten = fl.targets.clean(*args.targets, media=args.media)
    for row in forgotten:
        _line(args, row, f"{row.target}: forgotten" + (" with its files" if args.media else ""))
    return OK


def auth_list(args: argparse.Namespace) -> int:
    """List every account of every plugin: its origin, default, and whether it is usable.

    Args:
        args: the parsed flags.

    Returns:
        0.
    """
    with _open(args) as fl:
        rows = list(fl.auth)
    if args.json:
        for row in rows:
            _json(row)
        return OK
    from flnd.ui import out

    if not rows:
        out.line(("no accounts yet", "dim"))
        return OK
    _table(("plugin", "name", "default", "origin", "status", "since"),
           [(row.plugin, row.name, "default" if row.is_default else "-", row.origin,
             _state(row), row.at[:10]) for row in rows])
    return OK


def auth_login(args: argparse.Namespace) -> int:
    """Log an account of a plugin in, and save the login encrypted in the home.

    METHOD is a browser to read the site's cookies from (chrome, firefox, or safari on macOS),
    browser (a window of flnd's Chromium), file (a cookies.txt or JSON export, with its PATH), or
    the plugin's own way (tlg: qr, phone). Left out in a terminal, it is asked.

    Args:
        args: the parsed flags.

    Returns:
        0.

    Raises:
        UsageError: METHOD is left out where nobody can be asked (no terminal, or --json).
    """
    method, path = args.method, args.path
    if method is not None and path is None and method not in _methods(args.plugin):
        # `flnd auth login ins cookies.txt`: a path alone means a file.
        method, path = "file", method
    if method is None and path is None:
        method, path = _ask_how(args)
    with _open(args) as fl:
        done = fl.auth.login(args.plugin, method=method, path=path)
    _line(args, done, f"{done.plugin}: logged in as {done.name} ({done.origin})")
    return OK


def auth_logout(args: argparse.Namespace) -> int:
    """Log an account out: its saved login and its row. The default one when none is named.

    Args:
        args: the parsed flags.

    Returns:
        0.
    """
    with _open(args) as fl:
        gone = fl.auth.logout(args.plugin, name=args.account)
    _line(args, gone, f"{gone.plugin}: {gone.name} logged out")
    return OK


def plugin_list(args: argparse.Namespace) -> int:
    """List the plugins: built in, installed from a file or folder, and why one is disabled.

    Plugins load from $FLND_PLUGINS, else ~/.config/flnd/plugins (APPDATA's flnd/plugins on
    Windows); never from a home.

    Args:
        args: the parsed flags.

    Returns:
        0.
    """
    with _open(args) as fl:
        rows = list(fl.plugins)
        counts = {row.code: sum(1 for each in fl.targets if each.target.plugin == row.code)
                  for row in rows}
    if args.json:
        for row in rows:
            _json(row)
        return OK
    _table(("code", "name", "kind", "api", "domains", "targets"),
           [(row.code, row.name, row.disabled and "disabled" or row.origin, str(row.api),
             " ".join(row.domains), str(counts[row.code])) for row in rows])
    from flnd.ui import out

    for row in rows:
        if row.disabled:
            out.line((f"{row.code} disabled: ", "bold bad"), (str(row.disabled), "bad"))
    return OK


def plugin_install(args: argparse.Namespace) -> int:
    """Install a plugin from a .py file or a package folder, replacing one with its code.

    Args:
        args: the parsed flags.

    Returns:
        0.
    """
    with _open(args) as fl:
        done = fl.plugins.install(args.path)
    _line(args, done, f"{done.code}: installed ({done.name})")
    return OK


def plugin_uninstall(args: argparse.Namespace) -> int:
    """Uninstall a plugin. Its accounts and targets stay.

    Args:
        args: the parsed flags.

    Returns:
        0.
    """
    with _open(args) as fl:
        gone = fl.plugins.uninstall(args.code)
    _line(args, gone, f"{gone.code}: uninstalled")
    return OK


def extension_install(args: argparse.Namespace) -> int:
    """Connect Chrome to flnd.

    It copies flnd's extension to ~/.config/flnd/extension/unpacked, writes its host for every
    Chromium browser found, prints the three steps that load it in Chrome, and waits up to 10
    minutes for it to connect. The popup acts on this home.

    Args:
        args: the parsed flags.

    Returns:
        0.
    """
    with _open(args) as fl:
        fl.extension.install()
    return OK


def extension_uninstall(args: argparse.Namespace) -> int:
    """Remove flnd's Chrome extension, its token and its host.

    Args:
        args: the parsed flags.

    Returns:
        0.
    """
    from flnd.ui import out

    with _open(args) as fl:
        for path in fl.extension.uninstall():
            out.line(("removed ", "dim"), str(path))
    return OK


def status(args: argparse.Namespace) -> int:
    """Show the last run, its failures by target, the accounts that need care, and the disk.

    A failure a later run saved is left out. A failed file is retried by itself at the start of
    its target's next 3 gets; `flnd target list`'s r asks for it again after that. With --json,
    one JSON object: the run's command and times, what it saved, and its failures.

    Args:
        args: the parsed flags.

    Returns:
        0 when all is well, else 1.
    """
    from flnd.ui import out

    with _open(args) as fl:
        found = fl.status
        accounts = [row for row in fl.auth if _state(row) != "ok"]
        checked = fl.check()
    well = (found is None or not found.failures) and not accounts and checked.ok
    if args.json:
        out.json({"type": "Status", "status": found, "accounts": accounts,
                  "disk": checked})
        return OK if well else FAILED
    if found is None:
        out.line(("no run yet", "dim"))
    else:
        _last(found)
    for row in accounts:
        out.line(("  account ", "dim"), (f"{row.plugin} {row.name}", "bold focus"), ": ",
                 (_state(row), "warn"))
    _disk(checked)
    return OK if well else FAILED


def config(args: argparse.Namespace) -> int:
    """Show every setting of config.toml: its value, its default and where it comes from.

    Args:
        args: the parsed flags.

    Returns:
        0.
    """
    with _open(args) as fl:
        rows = [(key, fl.config[key], fl.config.default(key), fl.config.where(key))
                for key in fl.config]
    if args.json:
        from flnd.ui import out

        for key, value, default, where in rows:
            out.json({"type": "Setting", "key": key, "value": value, "default": default,
                      "from": where})
        return OK
    _table(("key", "value", "default", "from"),
           [(key, _show(value), _show(default), where) for key, value, default, where in rows])
    return OK


def host(args: argparse.Namespace) -> int:
    """Serve flnd's Chrome extension on standard input and output; Chrome starts it."""
    with _open(args) as fl:
        return fl.extension.host(args.origin)


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
    else:
        out.line((line, "dim"), (", ".join(problems), "bold bad"))


def _state(account: Any) -> str:
    """What an account's status line says: ok, held, expired, or expiring soon."""
    import datetime

    now = datetime.datetime.now(datetime.UTC)
    if account.status == "held" and account.held_until:
        left = datetime.datetime.fromisoformat(account.held_until) - now
        return f"held {max(1, round(left.total_seconds() / 60))} min"
    if account.status == "expired":
        return "expired"
    if account.expires:
        left = datetime.datetime.fromisoformat(account.expires) - now
        if left.days < SOON_DAYS:
            return "expired" if left.total_seconds() <= 0 else f"expires in {left.days + 1} days"
    return "ok"


# ---------------------------------------------------------------------------------------------
# The parser
# ---------------------------------------------------------------------------------------------

class _Parser(argparse.ArgumentParser):
    """argparse, with its help and its errors printed by `flnd.ui.out`.

    `tail` holds the rows the help ends with: the plugins, for a command that takes targets.
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
    """The whole command line: the global flags, then one sub-parser per command or group.

    Each command's help is its function's docstring: its first clause in the list of commands,
    the paragraphs before `Args:` on its own `--help`.

    Returns:
        The parser; `parse_args` sets `command` to the command's function.
    """
    import flnd

    top = _Parser(prog="flnd", description="download whole accounts from the web",
                  parents=[_globals()])
    top.add_argument("-V", "--version", action=_Version, version=f"flnd {flnd.__version__}")
    top.tail = [("plugins", [" ".join(_plugins())]),
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
                     help="ins:example_user, ins:example_user/reels,posts, a link, or "
                          "mht?words; read from stdin too; none syncs every followed target")
    _when(sub)
    sub.add_argument("--limit", type=int, metavar="N",
                     help="at most N posts per target, or N targets per search")
    sub.add_argument("--dry", action="store_true",
                     help="list what would be saved, and save and write nothing")
    _again(sub)
    _exec(sub)
    sub.tail = _forms()

    targets = group("target", "List the targets flnd knows, or forget some")
    command(targets, "list", target_list)
    sub = command(targets, "clean", target_clean)
    sub.add_argument("targets", nargs="+", metavar="target", help="the targets to forget")
    sub.add_argument("--media", action="store_true",
                     help="also delete their files, sidecars, links and extracted folders")

    accounts = group("auth", "List the accounts, log one in or out")
    command(accounts, "list", auth_list)
    sub = command(accounts, "login", auth_login)
    sub.add_argument("plugin", help="the plugin's code (ins)")
    sub.add_argument("method", nargs="?",
                     help="a browser, browser, file, or the plugin's own way (qr, phone)")
    sub.add_argument("path", nargs="?", help="the cookie file's path, for file")
    sub = command(accounts, "logout", auth_logout)
    sub.add_argument("plugin", help="the plugin's code (ins)")
    sub.add_argument("account", nargs="?", help="the account's name; the default when left out")

    plugins = group("plugin", "List the plugins, install or uninstall one")
    command(plugins, "list", plugin_list)
    sub = command(plugins, "install", plugin_install)
    sub.add_argument("path", help="a plugin's .py file or package folder")
    sub = command(plugins, "uninstall", plugin_uninstall)
    sub.add_argument("code", help="the plugin's code")

    chrome = group("extension", "Connect Chrome to flnd, or remove it")
    command(chrome, "install", extension_install)
    command(chrome, "uninstall", extension_uninstall)

    command(verbs, "status", status)
    command(verbs, "config", config)

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


def _when(sub: argparse.ArgumentParser) -> None:
    # Left out, it is None, and the library reads `[defaults] when` from config.toml.
    sub.add_argument("--when", metavar="WHEN",
                     help="posts from 2026, 2026-06, 2026-06-15, 2026-01..2026-03, or 30d; "
                          "\"\" for every day")


def _again(sub: argparse.ArgumentParser) -> None:
    """Add `--again` to a command that gets.

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


def _plugins() -> list[str]:
    """The code of every built-in plugin, read from the manifest, so no plugin is imported."""
    from flnd.plugins import _manifest

    return list(_manifest.PLUGINS)


def _methods(code: str) -> tuple[str, ...]:
    """The login methods of a built-in plugin, from the manifest; none for any other code."""
    from flnd.plugins import _manifest

    entry: Any = _manifest.PLUGINS.get(code)
    return tuple(entry["logins"]) if entry is not None else ()


def _forms() -> list[tuple[str, list[Any]]]:
    """A row per built-in plugin for a command's help: the targets it reads, its categories.

    Returns:
        The rows: the plugin's code, then its forms and categories as styled parts.
    """
    from flnd.plugins import _manifest

    rows: list[tuple[str, list[Any]]] = [("plugins", [("the targets each reads, then its "
                                                      "categories", "dim")])]
    for code, entry in _manifest.PLUGINS.items():
        forms: Any = entry["forms"]
        domains: Any = entry["domains"]
        categories: Any = entry["categories"]
        own = [form for form in forms if not form.startswith("http")]
        on = [f"a link on {', '.join(domains)}"] if domains else []
        cats: list[Any] = [("\n/", "dim"), ",".join(categories)] if categories else []
        rows.append((code, [", ".join([*own, *on]), *cats]))
    return rows


# ---------------------------------------------------------------------------------------------
# Helpers of the commands
# ---------------------------------------------------------------------------------------------

def _open(args: argparse.Namespace) -> flnd.Flnd:
    """The library over `--home`; the caller uses it in a `with`."""
    import flnd

    return flnd.open(args.home, again=True if getattr(args, "again", False) else None)


def _targets(given: Sequence[str]) -> list[str]:
    """The targets on the command line, then the lines of a piped standard input.

    Args:
        given: the targets typed after the command.

    Returns:
        The targets, in order; none when neither holds one.
    """
    found = list(given)
    if not sys.stdin.isatty():
        lines = [line.strip() for line in sys.stdin]
        found += [line for line in lines if line and not line.startswith("#")]
    return found


def _ask_how(args: argparse.Namespace) -> tuple[str, str | None]:
    """Ask how to log in to `args.plugin`: one of its methods, and a path for a file.

    Args:
        args: the parsed flags, with `plugin` set.

    Returns:
        The method, and the cookie file's path when the method is `file`.

    Raises:
        UsageError: an unknown plugin, `--json`, or no terminal to ask in.
    """
    import flnd
    from flnd.ui import forms

    ways = _methods(args.plugin)
    if not ways:
        # Not built in: a plugin's ways are read from its class, which this imports.
        with _open(args) as fl:
            found = next((row for row in fl.plugins if row.code == args.plugin), None)
        if found is None:
            raise flnd.UsageError(f"unknown plugin {args.plugin!r}",
                                  hint="flnd plugin list shows the plugins")
        ways = tuple(found.logins)
    if args.json or not forms.interactive():
        raise flnd.UsageError(f"{args.plugin}: how to log in?",
                              hint=f"flnd auth login {args.plugin} <method>, a method of "
                                   f"{', '.join(ways)}")
    picked = forms.choose(f"Log in to {args.plugin} with", list(ways))
    if picked == "file":
        return picked, forms.ask("Path of the cookie file").strip()
    return picked, None


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
    out.line((head, "bold focus"), (colon, "dim"), tail)


def _json(value: Any) -> None:
    """Print one value (an event, a row, or a plain dict) as one line of JSON on standard output.

    Args:
        value: an event, record, or dict to encode.
    """
    from flnd.ui import out

    out.json(value)


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

def _watch(fl: flnd.Flnd, run: flnd.Run, args: argparse.Namespace, *,
           runner: _Exec | None = None) -> int:
    """Read a run's events into the chosen view, print its end, and return its exit code.

    flnd's own log lines are held to warnings for the run, or opened to debug lines with `-v`.
    The run's events go to `<home>/log/<stamp>_get.jsonl` (see src/docs/storage). On a
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
    from contextlib import ExitStack, nullcontext

    import flnd
    from flnd.ui.log import stamped, ui
    from flnd.ui.screen import Screen

    name = "get"
    journal_path = stamped(fl.home, name, ".jsonl")
    view: _View = _Json() if args.json else _JournalView()
    with ExitStack() as stack:
        try:
            journal = stack.enter_context(ui.file(journal_path))
        except OSError:
            journal = None
        view.journal = journal
        stack.enter_context(view)
        stack.enter_context(ui.scope(level="debug" if args.verbose else "warn"))
        if journal is not None:
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
            report: the run's `flnd.Report`.
            state: its last snapshot (`run.state`).
        """
        from flnd.ui import out, screen

        if report.planned:
            out.line(("planned ", "bold focus"), (out.count(report.planned, "file"),
                                                 "bold focus"), ("; nothing was saved", "dim"))
            return
        screen.summary(report, state)


class _JournalView(_View):
    """Terminal output from run events; the machine record goes to the `.jsonl` journal.

    The run's own display is the screen (`flnd.ui.screen`), drawn from the run's snapshot, so
    a view prints no line per file or failure: the screen counts them, and `end` sums them up.
    """

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
        _json(report)


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
