"""The flnd command line: flags in, one library call, events out as lines.

SPEC.md#commands-and-flags explains it."""

from __future__ import annotations

import argparse
import os
import sys
from collections.abc import Callable, Sequence
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    import flnd

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
    """Give each global flag left out its default."""
    for name, default in (("home", None), ("verbose", False), ("json", False)):
        if not hasattr(args, name):
            setattr(args, name, default)
    if not hasattr(args, "command"):
        args.command = None


def _report(error: Any, *, as_json: bool = False) -> None:
    """Print an error a person can act on: its message, then its hint; never a traceback."""
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

    --exec CMD runs CMD once for each saved file, after every post-download step, with
    no shell: {path}, {target} and {post} inside an argument become the file's values, which
    are also in $FLND_PATH, $FLND_TARGET and $FLND_POST.

    Args:
        args: the parsed flags.

    Returns:
        The run's exit code.
    """
    _lower_priority()
    command = _command(args)
    targets = _targets(args.targets)
    with _open(args) as fl:
        if not targets and not any(row.follow for row in fl.targets):
            if args.folder is not None:
                fl.get(when=args.when, limit=args.limit, dry=args.dry,
                       folder=args.folder, exec=command).wait()
            from flnd.ui import out

            out.line(("nothing to sync", "dim"))
            return OK
        run = fl.get(*targets, when=args.when, limit=args.limit, dry=args.dry,
                     folder=args.folder, exec=command)
        return _watch(fl, run, args)


def target_list(args: argparse.Namespace) -> int:
    """Work in the targets this home knows: followed or not, files, size, last run.

    On a terminal it is a list: f follows or unfollows the target under the cursor, r makes its
    failed files retryable, a gets a target, d forgets one. On a pipe it is a plain table.

    Args:
        args: the parsed flags.

    Returns:
        0.
    """
    from flnd.ui import lists

    with _open(args) as fl:
        if args.json:
            for row in fl.targets:
                _json(row)
            return OK

        def load() -> list[lists.TargetRow]:
            return [lists.TargetRow(str(row.target), row.follow, row.categories, row.files,
                                    row.size, _ago(row.at)) for row in fl.targets]

        def follow(row: lists.TargetRow) -> None:
            (fl.targets.unfollow if row.followed else fl.targets.follow)(row.name)

        def add(text: str) -> None:
            _watch(fl, fl.get(text), args)

        lists.targets(load, follow=follow, retry=lambda row: fl.targets.retry(row.name),
                      add=add, delete=lambda row: fl.targets.clean(row.name))
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
        if not args.json and not _sure(fl, args):
            _line(args, None, "kept: nothing was forgotten")
            return OK
        forgotten = fl.targets.clean(*args.targets, media=args.media)
    for row in forgotten:
        _line(args, row, f"{row.target}: forgotten" + (" with its files" if args.media else ""))
    return OK


def _sure(fl: flnd.Flnd, args: argparse.Namespace) -> bool:
    """Ask "Forget 2 targets and delete 1.2 GB?" of a terminal; a pipe isn't asked."""
    from flnd.ui import forms, out

    named = {str(each) for each in args.targets}
    size = sum(row.size for row in fl.targets if str(row.target) in named)
    title = f"Forget {out.count(len(named), 'target')}"
    if args.media:
        title += f" and delete {out.size(size)}" if size else " and delete their files"
    return forms.confirm(title + "?")


def auth_list(args: argparse.Namespace) -> int:
    """Work in the accounts of every plugin: origin, default, and whether each is usable.

    On a terminal it is a list: * makes the account under the cursor its plugin's default, a
    logs one in, d logs it out. On a pipe it is a plain table.

    Args:
        args: the parsed flags.

    Returns:
        0.
    """
    from flnd.ui import lists

    with _open(args) as fl:
        if args.json:
            for row in fl.auth:
                _json(row)
            return OK

        def load() -> list[lists.AccountRow]:
            last: dict[tuple[str, str], str] = {}
            for each in fl.targets:
                key = (each.target.plugin, each.account or "")
                last[key] = max(last.get(key, ""), each.at)
            return [lists.AccountRow(row.plugin, row.name, row.is_default, row.origin,
                                     _left(row.held_until, held=True), _left(row.expires),
                                     _ago(last.get((row.plugin, row.name), "")))
                    for row in fl.auth]

        def login(row: lists.AccountRow | None) -> None:
            code = row.plugin if row is not None else _pick_plugin(fl)
            method, path = _ask_how(args, code, fl)
            fl.auth.login(code, method=method, path=path)

        lists.accounts(load, default=lambda row: fl.auth.default(row.plugin, row.name),
                       add=login, logout=lambda row: fl.auth.logout(row.plugin, name=row.name))
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
    from flnd.ui import forms

    with _open(args) as fl:
        if not args.json and not forms.confirm(
                f"Log {args.account or 'the default account'} out of {args.plugin}?"):
            _line(args, None, "kept: nobody was logged out")
            return OK
        gone = fl.auth.logout(args.plugin, name=args.account)
    _line(args, gone, f"{gone.plugin}: {gone.name} logged out")
    return OK


def plugin_list(args: argparse.Namespace) -> int:
    """Work in the plugins: built in, installed from a file or folder, and why one is disabled.

    On a terminal it is a list: a installs a plugin from a path, d uninstalls an installed one.
    On a pipe it is a plain table. Plugins load from $FLND_PLUGINS, else ~/.config/flnd/plugins
    (APPDATA's flnd/plugins on Windows); never from a home.

    Args:
        args: the parsed flags.

    Returns:
        0.
    """
    from flnd.ui import lists

    with _open(args) as fl:
        if args.json:
            for row in fl.plugins:
                _json(row)
            return OK

        def load() -> list[lists.PluginRow]:
            used: dict[str, int] = {}
            for each in fl.targets:
                used[each.target.plugin] = used.get(each.target.plugin, 0) + 1
            return [lists.PluginRow(row.code, row.name, row.origin == "built-in", row.api,
                                    tuple(row.domains), used.get(row.code, 0), row.disabled)
                    for row in fl.plugins if row.origin != "script"]

        lists.plugins(load, install=fl.plugins.install,
                      uninstall=lambda row: fl.plugins.uninstall(row.code))
        from flnd.ui import out

        for row in fl.plugins:
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
    from flnd.ui import forms

    with _open(args) as fl:
        if not args.json and not forms.confirm(f"Uninstall {args.code}?"):
            _line(args, None, "kept: nothing was uninstalled")
            return OK
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
    from flnd.ui import forms, out

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
    rows = len(checked.missing) + len(checked.resized)
    if rows and not checked.fixed and forms.confirm(f"Repair {out.count(rows, 'row')}?",
                                                    default=False):
        with _open(args) as fl:
            checked = fl.check(fix=True)
        _disk(checked)
        well = (found is None or not found.failures) and not accounts and checked.ok
    return OK if well else FAILED


def config(args: argparse.Namespace) -> int:
    """Work in every setting of config.toml: its value, its default and where it comes from.

    On a terminal it is a list: e edits the value under the cursor in a form, a sets another
    key, d resets one to its default. On a pipe it is a plain table.

    Args:
        args: the parsed flags.

    Returns:
        0.
    """
    from flnd.ui import forms, lists

    with _open(args) as fl:
        if args.json:
            from flnd.ui import out

            for key in fl.config:
                out.json({"type": "Setting", "key": key, "value": fl.config[key],
                          "default": fl.config.default(key), "from": fl.config.where(key)})
            return OK

        def load() -> list[lists.Row]:
            return [lists.Row(key, (key, _show(fl.config[key]), _show(fl.config.default(key)),
                                    fl.config.where(key))) for key in fl.config]

        def edit(key: str | None) -> None:
            if key is None:
                return
            typed = forms.text(f"{key} = ", default=_show(fl.config[key]))
            if typed is not None:
                fl.config.set(key, _typed(typed, fl.config.default(key)))

        def add(_: str | None) -> None:
            key = forms.text("Setting (defaults.when, plugins.ins.pace)")
            if key:
                edit(key.strip())

        columns = (lists.Column("key"), lists.Column("value"), lists.Column("default"),
                   lists.Column("from"))
        lists.show("Settings", columns, load,
                   keys=(lists.Key("e", "edit", edit, leave=True),
                         lists.Key("a", "add", add, leave=True)),
                   delete=lists.Key("d", "reset", lambda key: fl.config.unset(key)))
    return OK


def _typed(text: str, like: Any) -> Any:
    """What a form's text means for a setting that defaults to `like`: a bool, a number, a
    list or table written as JSON, else the text itself."""
    import json

    text = text.strip()
    if isinstance(like, bool):
        return text.lower() in ("true", "yes", "on", "1")
    if isinstance(like, (int, float, list, dict)) or text[:1] in "[{":
        try:
            return json.loads(text)
        except ValueError:
            return text
    if like is None and text.lower() in ("true", "false"):
        return text.lower() == "true"
    return "" if text == "-" else text


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
    elif found.fixed:
        out.line((line, "dim"), ("repaired " + ", ".join(problems), "bold good"),
                 ("; the history now matches the disk", "dim"))
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
    """argparse, with its help and its errors printed by `flnd.ui.out`."""

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
    """The flags every command takes, before or after its name."""
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
    """Add `--again` to a command that gets."""
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
    """A row per built-in plugin for a command's help: the targets it reads, its categories."""
    from flnd.plugins import _manifest

    rows: list[tuple[str, list[Any]]] = [("plugins", [("the targets each reads, then its "
                                                      "categories", "dim")])]
    for code, entry in _manifest.PLUGINS.items():
        forms: Any = entry["forms"]
        domains: Any = entry["domains"]
        categories: Any = entry["categories"]
        own = [form for form in forms if not form.startswith("http")]
        on = [f"a link on {', '.join(domains)}"] if domains else []
        cats: list[Any] = [("\n|", "dim"), ",".join(categories)] if categories else []
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
    """The targets on the command line, then the lines of a piped standard input."""
    found = list(given)
    if not sys.stdin.isatty():
        lines = [line.strip() for line in sys.stdin]
        found += [line for line in lines if line and not line.startswith("#")]
    return found


def _ask_how(args: argparse.Namespace, code: str | None = None,
             fl: flnd.Flnd | None = None) -> tuple[str, str | None]:
    """Ask how to log in to a plugin: one of its methods, and a path for a file."""
    from contextlib import nullcontext

    import flnd
    from flnd.ui import forms

    code = code or args.plugin
    ways = _methods(code)
    if not ways:
        # Not built in: a plugin's ways are read from its class, which this imports.
        with (nullcontext(fl) if fl is not None else _open(args)) as home:
            found = next((row for row in home.plugins if row.code == code), None)
        if found is None:
            raise flnd.UsageError(f"unknown plugin {code!r}",
                                  hint="flnd plugin list shows the plugins")
        ways = tuple(found.logins)
    if args.json or not forms.interactive():
        raise flnd.UsageError(f"{code}: how to log in?",
                              hint=f"flnd auth login {code} <method>, a method of "
                                   f"{', '.join(ways)}")
    name = _names().get(code, code)
    picked = forms.select(f"Log in to {name}", [_way(way) for way in ways])
    if picked is None:
        raise flnd.UsageError(f"{code}: no way chosen to log in")
    path = None
    if picked == "file":
        path = (forms.text("Cookie file") or "").strip()
    return picked, path


def _way(way: str) -> Any:
    """One way to log in as the login form words it."""
    from flnd.ui import forms

    words = {"browser": ("In a browser window", "flnd opens Chromium; log in as usual"),
             "chrome": ("From your Chrome", "through the extension"),
             "file": ("From a cookie file", "a cookies.txt you exported"),
             "qr": ("With a QR code", "scan it in the app"),
             "phone": ("With your phone number", "a code comes by message")}
    label, note = words.get(way, (f"From your {way.title()}", "reads its cookies"))
    return forms.Option(label, way, note)


def _names() -> dict[str, str]:
    """The name of every built-in plugin, by code, from the manifest."""
    from flnd.plugins import _manifest

    return {code: str(entry["name"]) for code, entry in _manifest.PLUGINS.items()}


def _pick_plugin(fl: flnd.Flnd) -> str:
    """Ask which plugin to log in to, of those that have a login."""
    from flnd.ui import forms

    logins = {row.code: row.name for row in fl.plugins if row.logins}
    picked = forms.select("Log in to", {f"{name} ({code})": code for code, name in logins.items()})
    if picked is None:
        import flnd

        raise flnd.UsageError("no plugin chosen to log in to")
    return str(picked)


def _ago(at: str) -> float | None:
    """Seconds since an ISO 8601 time; None for none."""
    import datetime

    if not at:
        return None
    then = datetime.datetime.fromisoformat(at)
    return (datetime.datetime.now(datetime.UTC) - then).total_seconds()


def _left(at: str | None, *, held: bool = False) -> float | None:
    """Seconds until an ISO 8601 time (0 or less: it has passed); None for none."""
    ago = _ago(at or "")
    if ago is None or (held and ago >= 0):
        return None
    return -ago


def _line(args: argparse.Namespace, value: Any, text: str) -> None:
    """Print one result: as JSON with `--json`, else as styled `text`."""
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
    """Print one value (an event, a row, or a plain dict) as one line of JSON on standard output."""
    from flnd.ui import out

    out.json(value)


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

def _watch(fl: flnd.Flnd, run: flnd.Run, args: argparse.Namespace) -> int:
    """Read a run's events into the chosen view, print its end, and return its exit code."""
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
            report = run.report
            # The warnings still folded print above the screen, never after the summary.
            ui.drain()
        view.end(report, run.state)
        if journal is not None:
            journal.write(report)
    if any(_login(failed.error) for failed in report.failed):
        return LOGIN
    return FAILED if report.failed else OK


def _command(args: argparse.Namespace) -> list[str] | None:
    """`--exec`'s command split once like a POSIX shell; None without `--exec`."""
    import shlex
    import shutil

    import flnd

    text = args.exec
    if text is None:
        return None
    lexer = shlex.shlex(text, posix=True)
    lexer.whitespace_split = True
    if os.name == "nt":
        lexer.escape = ""
    try:
        argv = list(lexer)
    except ValueError as error:
        raise flnd.UsageError(f"--exec: {error}: {text!r}") from None
    if not argv:
        raise flnd.UsageError("--exec needs a command", hint="--exec 'cp {path} /backup/'")
    if shutil.which(argv[0]) is None:
        raise flnd.UsageError(f"--exec: {argv[0]}: no such program",
                              hint="give a program on PATH, or its full path")
    return argv


def _login(error: BaseException) -> bool:
    """Whether `error` is a `flnd.LoginError`: a login the target needs, exit code 3."""
    import flnd

    return isinstance(error, flnd.LoginError)


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
    """A path as short as it can be shown: relative to the current folder."""
    try:
        return os.path.relpath(path)
    except ValueError:
        # On Windows, a path on another drive has no relative form.
        return str(path)
