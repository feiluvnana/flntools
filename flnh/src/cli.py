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
LEVEL_MAP = {
    "1": "flag",
    "flag": "flag",
    "2": "lock",
    "lock": "lock",
    "3": "vault",
    "vault": "vault",
    "4": "volume",
    "volume": "volume",
}


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


def _read_secret_bytes(args: argparse.Namespace, *, confirm_new: bool) -> tuple[bytes, bool]:
    """Read secret bytes from `--keyfile`, `--pass-stdin`, or masked terminal prompt."""
    keyfile = getattr(args, "keyfile", None)
    if keyfile:
        kf = Path(keyfile)
        if not kf.is_file():
            raise errors.UsageError(f"keyfile not found: {kf}")
        data = kf.read_bytes()
        if not data:
            raise errors.UsageError(f"keyfile is empty: {kf}")
        return data, True
    if getattr(args, "pass_stdin", False):
        line = sys.stdin.readline().rstrip("\r\n")
        if not line:
            raise errors.UsageError("empty password from standard input")
        return line.encode("utf-8"), False
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
    return pw.encode("utf-8"), False


def _resolve_level(args: argparse.Namespace) -> str:
    raw = getattr(args, "level", None)
    has_secret = bool(args.use_pass or args.pass_stdin or args.keyfile)
    if raw is None:
        return "vault" if has_secret else "flag"
    return LEVEL_MAP[raw]


def _hide_cmd(args: argparse.Namespace) -> int:
    """Hide a folder or file at the chosen level (`flag`, `lock`, `vault`, or `volume`)."""
    level = _resolve_level(args)
    target = Path(args.path)
    has_secret = bool(args.use_pass or args.pass_stdin or args.keyfile)
    if level == "flag" and has_secret:
        raise errors.UsageError(
            "--pass and --keyfile are not used with --level flag",
            hint="omit --level (defaults to vault with --pass) or use --level lock|vault|volume",
        )
    if level == "lock" and args.keyfile:
        raise errors.UsageError(
            "--keyfile requires --level vault or volume",
            hint="use --pass with --level lock",
        )
    if not target.exists() and not target.is_symlink():
        raise errors.UsageError(f"path not found: {target}")

    track = not args.no_track
    password: str | None = None
    secret = b""
    is_keyfile = False
    if level in ("vault", "volume"):
        secret, is_keyfile = _read_secret_bytes(args, confirm_new=True)
    elif level == "lock" and has_secret:
        pw_bytes, _ = _read_secret_bytes(args, confirm_new=True)
        password = pw_bytes.decode("utf-8")

    if level == "volume":
        size_est = vault.tree_size(target)
        out.line(
            ("volume ", "bold focus"),
            f"{target} ({text.size(size_est)}) will be encrypted and moved to a hidden tail",
        )
        if not args.yes:
            if not forms.interactive():
                raise errors.UsageError(
                    "level 4 (volume) requires confirmation",
                    hint="run in a terminal or pass --yes",
                )
            if not forms.confirm("Shrink partition and hide volume?", default=False):
                out.line(("cancelled: nothing changed", "dim"))
                return OK

    item_id = vault.item_id(target.name)
    if level in ("flag", "lock"):
        res = os_hide.hide(target, level=level, password=password, track=track)
    elif level == "vault":
        res = vault.hide(target, secret, is_keyfile=is_keyfile)
    else:
        backup_path = None
        if not args.no_backup:
            backup_path = state.backups_dir() / f"{item_id}-{int(clock.wall())}.flnh"
        res = volume.hide(target, secret, is_keyfile=is_keyfile, backup_path=backup_path)

    if track:
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
        if backup_path is not None or (entry is not None and entry.level == "volume"):
            secret, _ = _read_secret_bytes(args, confirm_new=False)
            volume.show(target, secret, entry=entry, backup_path=backup_path)
            detected_level = "volume"
        elif vault.locate(target, entry) is not None:
            secret, _ = _read_secret_bytes(args, confirm_new=False)
            vault.show(target, secret, entry=entry)
            detected_level = "vault"
        elif os_hide.detect(target, entry) is not None:
            def _get_pw() -> str:
                sec, _ = _read_secret_bytes(args, confirm_new=False)
                return sec.decode("utf-8")

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
    hide_p.add_argument(
        "-l",
        "--level",
        choices=list(LEVEL_MAP),
        default=None,
        help="hiding level: flag (1), lock (2), vault (3), or volume (4)",
    )
    hide_p.add_argument(
        "--pass",
        dest="use_pass",
        action="store_true",
        help="prompt for a password (defaults level to vault)",
    )
    hide_p.add_argument(
        "--pass-stdin",
        action="store_true",
        help="read password from standard input",
    )
    hide_p.add_argument("--keyfile", metavar="FILE", help="encrypt with a keyfile")
    hide_p.add_argument(
        "--no-track",
        action="store_true",
        help="do not record this item in local state",
    )
    hide_p.add_argument(
        "--no-backup",
        action="store_true",
        help="skip saving a level 4 partition backup file",
    )
    hide_p.add_argument(
        "-y",
        "--yes",
        action="store_true",
        help="confirm level 4 volume shrink without prompting",
    )

    show_p = cmd("show", _show_cmd, "Restore a hidden folder or file")
    show_p.add_argument("path", nargs="?", help="hidden folder or file to restore")
    show_p.add_argument(
        "--list",
        action="store_true",
        help="list tracked hidden items without restoring",
    )
    show_p.add_argument(
        "--pass-stdin",
        action="store_true",
        help="read password from standard input",
    )
    show_p.add_argument("--keyfile", metavar="FILE", help="unlock with a keyfile")
    show_p.add_argument(
        "--backup",
        metavar="FILE",
        help="recover level 4 volume using a saved partition backup",
    )
    show_p.add_argument(
        "-y",
        "--yes",
        action="store_true",
        help="confirm non-interactively",
    )
    return top
