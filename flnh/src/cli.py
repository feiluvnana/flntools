"""The flnh command line: `hide` and `show` (`SPEC.md#cli-and-exit-codes`)."""

from __future__ import annotations

import argparse
import sys
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

from flnh import __version__, errors, os_hide, state, vault, volume
from flnkit import clock, text
from flnkit.ui import forms, out

OK, FAILED, USAGE, AUTH, BROKE, STOPPED = 0, 1, 2, 3, 4, 130
CODES = (
    ("Stopped", STOPPED),
    ("UsageError", USAGE),
    ("AuthError", AUTH),
    ("DiskError", BROKE),
    ("PermissionError", BROKE),
    ("SafetyError", BROKE),
    ("Error", FAILED),
)


def main(argv: Sequence[str] | None = None) -> int:
    """Run one flnh command and return its exit code."""
    out.on_error(None)
    try:
        return _main(argv)
    finally:
        out.close()


def _main(argv: Sequence[str] | None) -> int:
    try:
        args = parser().parse_args(argv)
    except SystemExit as stop:
        return stop.code if isinstance(stop.code, int) else USAGE
    if not getattr(args, "command", None):
        parser().print_help()
        return OK
    try:
        return int(args.command(args))
    except KeyboardInterrupt:
        out.line(("stopped", "warn"), err=True)
        return STOPPED
    except Exception as error:
        if isinstance(error, errors.Error):
            out.error(error)
            return exit_code(error)
        out.error(errors.Error(f"flnh hit a bug: {type(error).__name__}: {error}"))
        return BROKE


def exit_code(error: BaseException | None) -> int:
    """Map an exception to its CLI exit code."""
    if error is None:
        return OK
    for name, code in CODES:
        if isinstance(error, getattr(errors, name)):
            return code
    return BROKE


def _read_password(*, confirm_new: bool) -> bytes:
    """Read a password from piped stdin or masked terminal prompt."""
    if not forms.interactive():
        line = sys.stdin.readline().rstrip("\r\n")
        if not line:
            raise errors.UsageError("password required (pipe via stdin or run in a terminal)")
        return line.encode("utf-8")
    pw = forms.text("Password", secret=True)
    if pw is None:
        raise errors.Stopped("cancelled")
    if not pw:
        raise errors.UsageError("password cannot be empty")
    if confirm_new:
        again = forms.text("Confirm password", secret=True)
        if again is None:
            raise errors.Stopped("cancelled")
        if pw != again:
            raise errors.UsageError("passwords do not match")
    return pw.encode("utf-8")


def _hide_cmd(args: argparse.Namespace) -> int:
    """Hide a folder or file (`flag` by default, or `--lock`, `--vault`, `--volume`)."""
    level: str = args.level
    target = Path(args.path)
    if not target.exists() and not target.is_symlink():
        raise errors.UsageError(f"path not found: {target}")

    if level == "volume" and not args.yes and not forms.interactive():
        raise errors.UsageError(
            "--volume requires confirmation",
            hint="run in a terminal or pass --yes",
        )

    secret = _read_password(confirm_new=True) if level in ("vault", "volume") else b""

    if level == "volume":
        size_est = vault.tree_size(target)
        out.line(
            ("volume ", "bold focus"),
            f"{target} ({text.size(size_est)}) will be encrypted and moved to a hidden tail",
        )
        if not args.yes and not forms.confirm("Shrink partition and hide volume?", default=False):
            out.line(("cancelled: nothing changed", "dim"))
            return OK

    item_id = vault.item_id(target.name)
    if level in ("flag", "lock"):
        res = os_hide.hide(target, level=level)
    elif level == "vault":
        res = vault.hide(target, secret)
    else:
        backup_path = state.backups_dir() / f"{item_id}-{int(clock.wall())}.flnh"
        res = volume.hide(target, secret, backup_path=backup_path)

    with state.Store() as store:
        store.add(
            state.Entry(
                id=item_id,
                orig_path=state.norm_path(target),
                kind=res["kind"],
                level=level,
                hidden_path=res["hidden_path"],
                meta=res["meta"],
                size_bytes=res["size_bytes"],
                hidden_at=clock.wall(),
            )
        )
    out.line(("hidden ", "bold good"), f"{target} ", (f"[{level}]", "dim"), mark="ok")
    return OK


def _print_table(entries: list[state.Entry]) -> None:
    if not entries:
        out.line(("no tracked hidden items", "dim"))
        return
    rows = [
        [(entry.orig_path, "bold focus"), entry.level, entry.kind, text.size(entry.size_bytes)]
        for entry in entries
    ]
    out.table(rows, ("path", "level", "kind", "size"))


def _show_cmd(args: argparse.Namespace) -> int:
    """Restore a hidden folder or file, or list/pick tracked hidden items."""
    with state.Store() as store:
        if args.list:
            _print_table(store.all())
            return OK
        if args.path is None:
            entries = store.all()
            if not entries:
                out.line(("no tracked hidden items", "dim"))
                return OK
            if not forms.interactive():
                _print_table(entries)
                return OK
            options = [
                forms.Option(
                    entry.orig_path,
                    entry,
                    f"{entry.level} · {text.size(entry.size_bytes)}",
                )
                for entry in entries
            ]
            picked = forms.select("Choose a hidden item to restore", options)
            if picked is None:
                return OK
            target = Path(picked.orig_path)
            entry: state.Entry | None = picked
        else:
            target = Path(args.path)
            entry = store.find(target)

        backup_path = Path(args.backup) if args.backup else None
        detected_level = ""

        def _get_pw() -> str:
            return _read_password(confirm_new=False).decode("utf-8")

        if backup_path is not None or (entry is not None and entry.level == "volume"):
            secret = _read_password(confirm_new=False)
            volume.show(target, secret, entry=entry, backup_path=backup_path)
            detected_level = "volume"
        elif entry is not None and entry.level in ("flag", "lock"):
            _, detected_level = os_hide.show(target, entry=entry, get_password=_get_pw)
        elif vault.locate(target, entry) is not None:
            secret = _read_password(confirm_new=False)
            vault.show(target, secret, entry=entry)
            detected_level = "vault"
        elif os_hide.detect(target, entry) is not None:
            _, detected_level = os_hide.show(target, entry=entry, get_password=_get_pw)
        else:
            raise errors.UsageError(
                f"no hidden item found for: {target}",
                hint="run flnh show --list to see tracked items or pass --backup for a volume",
            )

        store.remove(target)
        out.line(
            ("restored ", "bold good"),
            f"{target} ",
            (f"[{detected_level}]", "dim"),
            mark="ok",
        )
        return OK


class _Parser(argparse.ArgumentParser):
    def print_help(self, file: Any = None) -> None:
        out.help(self)

    def error(self, message: str) -> Any:
        out.error(
            errors.UsageError(
                f"{self.prog}: {message}",
                hint=f"{self.prog} -h lists what it takes",
            )
        )
        self.exit(USAGE)


class _Version(argparse.Action):
    def __init__(
        self,
        option_strings: Sequence[str],
        version: str,
        dest: str = "version",
        help: str = "print flnh's version",
    ) -> None:
        super().__init__(option_strings, dest, nargs=0, default=argparse.SUPPRESS, help=help)
        self.version = version

    def __call__(self, parser: argparse.ArgumentParser, *args: Any) -> None:
        out.plain(self.version)
        parser.exit()


def parser() -> argparse.ArgumentParser:
    """Build the CLI parser for `flnh` (`hide` and `show` only)."""
    top = _Parser(prog="flnh", description="make folders and files vanish in plain sight")
    top.add_argument("-V", "--version", action=_Version, version=f"flnh {__version__}")
    verbs = top.add_subparsers(title="commands", metavar="<command>")

    def cmd(name: str, fn: Callable[[argparse.Namespace], int], desc: str) -> _Parser:
        sub = verbs.add_parser(name, help=desc, description=desc)
        sub.set_defaults(command=fn)
        return sub

    hide_p = cmd("hide", _hide_cmd, "Make a folder or file vanish")
    hide_p.add_argument("path", help="folder or file to hide")
    mode = hide_p.add_mutually_exclusive_group()
    mode.add_argument(
        "--lock",
        action="store_const",
        dest="level",
        const="lock",
        help="vanish and deny opening or listing (ACL / chmod)",
    )
    mode.add_argument(
        "--vault",
        action="store_const",
        dest="level",
        const="vault",
        help="vanish into a password-encrypted vault",
    )
    mode.add_argument(
        "--volume",
        action="store_const",
        dest="level",
        const="volume",
        help="encrypted vault + hide size from drive space",
    )
    hide_p.set_defaults(level="flag")
    hide_p.add_argument(
        "-y",
        "--yes",
        action="store_true",
        help="confirm --volume shrink without prompting",
    )

    show_p = cmd("show", _show_cmd, "Restore a hidden folder or file")
    show_p.add_argument("path", nargs="?", help="hidden folder or file to restore")
    show_p.add_argument(
        "--list",
        action="store_true",
        help="list tracked hidden items without restoring",
    )
    show_p.add_argument(
        "--backup",
        metavar="FILE",
        help="recover a --volume using a saved partition backup",
    )
    return top
