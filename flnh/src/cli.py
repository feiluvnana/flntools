"""The flnh command line: `hide` and `show` (`SPEC.md#cli-and-exit-codes`)."""

from __future__ import annotations

import argparse
import builtins
import contextlib
import json
import os
import sys
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

from flnh import __version__, elevate, errors, os_hide, pack, state, volume
from flnkit import clock, text
from flnkit.ui import forms, out, theme

OK, FAILED, USAGE, BROKE, STOPPED = 0, 1, 2, 4, 130
_STAGE_LABELS = {
    "shrinking": "shrinking volume",
    "writing": "writing tail",
    "verifying": "verifying tail",
    "cleaning": "cleaning source",
    "restoring": "restoring data",
    "expanding": "expanding volume",
}
CODES = (
    ("Stopped", STOPPED),
    ("UsageError", USAGE),
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
    raw = list(sys.argv[1:] if argv is None else argv)
    elevated_spec: str | None = None
    if len(raw) >= 2 and raw[0] == "--_elevated":
        elevated_spec, raw = raw[1], raw[2:]
    try:
        args = parser().parse_args(raw)
    except SystemExit as stop:
        return stop.code if isinstance(stop.code, int) else USAGE
    if not getattr(args, "command", None):
        parser().print_help()
        return OK
    if elevated_spec:
        spec = json.loads(Path(elevated_spec).read_text(encoding="utf-8"))
        os.environ[elevate.ELEVATED_ENV] = "1"
        os.environ["FLNH_HOME"] = str(spec["home"])
        os.environ["NO_COLOR"] = "1"
        if spec.get("prog"):
            os.environ[elevate.PROG_ENV] = str(spec["prog"])
        if spec.get("prog_fancy") is not None:
            os.environ[elevate.PROG_FANCY_ENV] = str(spec["prog_fancy"])
        with (
            open(spec["out"], "w", encoding="utf-8") as out_f,
            open(spec["err"], "w", encoding="utf-8") as err_f,
            contextlib.redirect_stdout(out_f),
            contextlib.redirect_stderr(err_f),
        ):
            try:
                return _run_command(args)
            finally:
                out.close()
    try:
        return _run_command(args)
    finally:
        elevate.give_back(state.home())


def _run_command(args: argparse.Namespace) -> int:
    try:
        return int(args.command(args))
    except KeyboardInterrupt as stop:
        detail = f": {stop}" if str(stop) else ""
        out.line(("stopped", "warn"), detail, err=True)
        return STOPPED
    except Exception as error:
        if isinstance(error, errors.Error):
            out.error(error)
            return exit_code(error)
        if isinstance(error, builtins.PermissionError):
            out.error(errors.PermissionError(
                f"permission denied: {error.filename or error}",
                hint="check the owner and mode of the path, or run as its owner",
            ))
            return BROKE
        if isinstance(error, OSError):
            disk_error = errors.disk(error)
            out.error(disk_error)
            return exit_code(disk_error)
        out.error(errors.Error(f"flnh hit a bug: {type(error).__name__}: {error}"))
        return BROKE


def format_progress(
    stage: str, done: int, total: int, elapsed: float, *, fancy: bool = True
) -> str:
    """Format one human-readable volume progress bar line."""
    label = _STAGE_LABELS.get(stage, stage)
    if total <= 0:
        return f"  {label}..."
    clamped = min(done, total)
    pct = min(100, (clamped * 100) // total)
    filled = round(16 * clamped / total)
    bar = theme.glyph("full", fancy) * filled + theme.glyph("empty", fancy) * (16 - filled)
    extra = ""
    if elapsed >= 0.25 and clamped > 0:
        rate = clamped / elapsed
        extra = f" · {text.size(rate)}/s"
        if clamped < total and rate > 0:
            extra += f" · eta {text.duration((total - clamped) / rate)}"
    return f"  {label} {bar} {pct:3d}% ({text.size(clamped)} / {text.size(total)}){extra}"


class _Progress:
    """Throttled single-line progress reporter for TTY stderr or elevated UAC IPC."""

    def __init__(self, *, min_interval: float = 0.15) -> None:
        prog_env = os.environ.get(elevate.PROG_ENV, "")
        self._prog_file = Path(prog_env) if prog_env else None
        self._is_tty = theme.tty(sys.stderr)
        if self._prog_file is not None:
            self._fancy = os.environ.get(elevate.PROG_FANCY_ENV, "1") == "1"
        else:
            self._fancy = theme.unicode(sys.stderr)
        self._min_interval = min_interval
        self._stage = ""
        self._stage_start = 0.0
        self._last_emit = -min_interval
        self._shown = False

    def update(self, stage: str, done: int, total: int) -> None:
        now = clock.now()
        stage_changed = stage != self._stage
        finished = total > 0 and done >= total
        if stage_changed:
            self._stage = stage
            self._stage_start = now
        elif not finished and now - self._last_emit < self._min_interval:
            return
        elapsed = now - self._stage_start
        line = format_progress(stage, done, total, elapsed, fancy=self._fancy)
        self._last_emit = now
        if self._prog_file is not None:
            try:
                self._prog_file.write_text(line, encoding="utf-8")
            except OSError:
                pass
        elif self._is_tty:
            sys.stderr.write(f"\r\x1b[K{line}")
            sys.stderr.flush()
            self._shown = True

    def close(self) -> None:
        if self._prog_file is not None:
            try:
                self._prog_file.write_text("", encoding="utf-8")
            except OSError:
                pass
        elif self._shown:
            sys.stderr.write("\r\x1b[K")
            sys.stderr.flush()
            self._shown = False

    def __enter__(self) -> _Progress:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()


def exit_code(error: BaseException | None) -> int:
    """Map an exception to its CLI exit code."""
    if error is None:
        return OK
    for name, code in CODES:
        if isinstance(error, getattr(errors, name)):
            return code
    return BROKE


def _hide_cmd(args: argparse.Namespace) -> int:
    """Hide a folder or file (`flag` by default, or `--lock`, `--volume`)."""
    level: str = args.level
    target = Path(args.path)
    if not target.exists() and not target.is_symlink():
        raise errors.UsageError(f"path not found: {target}")

    if level == "volume":
        if target.is_symlink():
            # Checked here too: the elevated run gets a full path and must get this same one.
            raise errors.UsageError(
                f"--volume does not hide a symlink: {target}",
                hint="hide the folder or file it points to, or use flnh hide / --lock",
            )
        size_est = pack.tree_size(target)
        out.line(
            ("volume ", "bold focus"),
            f"{target} ({text.size(size_est)}) will be moved behind the partition boundary",
        )
        if not args.yes:
            if not forms.interactive():
                raise errors.UsageError(
                    "--volume requires confirmation",
                    hint="run in a terminal or pass --yes",
                )
            if not forms.confirm("Shrink partition and hide volume?", default=False):
                out.line(("cancelled: nothing changed", "dim"))
                return OK
        if not elevate.is_admin():
            if elevate.can_elevate():
                full = target.parent.resolve() / target.name
                return elevate.run_elevated(["hide", str(full), "--volume", "--yes"])
            raise elevate.permission_error("--volume")

    # The whole path, not the name: two `docs` folders must not share one tracked row.
    item_id = pack.item_id(state.norm_path(target))
    if level in ("flag", "lock"):
        res = os_hide.hide(target, level=level)
    else:
        backup_path = state.backups_dir() / f"{item_id}-{int(clock.wall())}.flnh"
        with _Progress() as prog:
            res = volume.hide(target, backup_path=backup_path, on_progress=prog.update)

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
    if res.get("cleanup_error"):
        raise errors.DiskError(
            f"hidden, but some original files could not be deleted: {res['cleanup_error']}",
            hint=f"delete what is left of {target} by hand; flnh show {target} still restores it",
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
            if not elevate.is_admin():
                if elevate.can_elevate():
                    sub_argv = ["show", state.norm_path(target)]
                    if backup_path is not None:
                        sub_argv += ["--backup", str(backup_path.resolve())]
                    store.close()
                    return elevate.run_elevated(sub_argv)
                raise elevate.permission_error("--volume restore")
            try:
                with _Progress() as prog:
                    volume.show(
                        target, entry=entry, backup_path=backup_path, on_progress=prog.update
                    )
            except volume.ExpandError:
                # The data is back: forget the item, then report the volume left small.
                store.remove(target)
                raise
            detected_level = "volume"
        elif entry is not None and entry.level in ("flag", "lock"):
            _, detected_level = os_hide.show(target, entry=entry)
        elif os_hide.detect(target, entry) is not None:
            _, detected_level = os_hide.show(target, entry=entry)
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
        "--volume",
        action="store_const",
        dest="level",
        const="volume",
        help="hide behind partition boundary so used space matches visible files",
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
