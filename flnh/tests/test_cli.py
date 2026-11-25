"""CLI tests for `flnh`: commands, exit codes, tracking, picker, and elevation."""

from __future__ import annotations

import ctypes
import io
import json
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

from flnh import cli, elevate, errors, state
from flnkit.ui import forms


class Stdin(io.StringIO):
    def __init__(self, text: str = "", *, tty: bool = False) -> None:
        super().__init__(text)
        self._tty = tty

    def isatty(self) -> bool:
        return self._tty


class CLITest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.home = self.root / "flnh_home"
        self.enterContext(mock.patch.dict(os.environ, {"FLNH_HOME": str(self.home), "NO_COLOR": "1"}))
        os.environ.pop(elevate.ELEVATED_ENV, None)

    def run_cli(self, *argv: str, stdin_text: str = "", tty: bool = False) -> tuple[int, str, str]:
        out_buf, err_buf = io.StringIO(), io.StringIO()
        with (
            redirect_stdout(out_buf),
            redirect_stderr(err_buf),
            mock.patch("sys.stdin", Stdin(stdin_text, tty=tty)),
        ):
            code = cli.main(list(argv))
        return code, out_buf.getvalue(), err_buf.getvalue()

    def test_help_and_version(self) -> None:
        code, out, _ = self.run_cli("-h")
        self.assertEqual(code, 0)
        self.assertIn("hide", out)
        self.assertIn("show", out)
        self.assertNotIn("_elevated", out)

        code, out, _ = self.run_cli("-V")
        self.assertEqual(code, 0)
        self.assertTrue(out.startswith("flnh "))

    def test_exit_codes(self) -> None:
        self.assertEqual(cli.exit_code(None), 0)
        self.assertEqual(cli.exit_code(errors.BusyError("busy")), 1)
        self.assertEqual(cli.exit_code(errors.UsageError("bad")), 2)
        self.assertEqual(cli.exit_code(errors.SafetyError("os")), 4)
        self.assertEqual(cli.exit_code(errors.PermissionError("perm")), 4)
        self.assertEqual(cli.exit_code(errors.DiskError("disk")), 4)
        self.assertEqual(cli.exit_code(errors.Stopped("ctrl-c")), 130)

    def test_mutually_exclusive_modes_refuse(self) -> None:
        folder = self.root / "docs"
        folder.mkdir()
        code, _, _ = self.run_cli("hide", str(folder), "--lock", "--volume")
        self.assertEqual(code, 2)

    def test_flag_and_lock_hide_list_and_picker_without_prompts(self) -> None:
        doc = self.root / "plain.txt"
        doc.write_text("flag-file", encoding="utf-8")
        folder = self.root / "plain_dir"
        folder.mkdir()
        (folder / "x.txt").write_text("lock-dir", encoding="utf-8")

        with mock.patch.object(
            forms, "text", side_effect=AssertionError("unexpected prompt")
        ):
            code, out, err = self.run_cli("hide", str(doc))
            self.assertEqual(code, 0, err)
            self.assertIn("[flag]", out)

            code, out, err = self.run_cli("hide", str(folder), "--lock")
            self.assertEqual(code, 0, err)
            self.assertIn("[lock]", out)

            code, out, _ = self.run_cli("show", "--list")
            self.assertEqual(code, 0)
            self.assertIn("plain.txt", out)
            self.assertIn("plain_dir", out)

            code, out, err = self.run_cli("show", str(doc))
            self.assertEqual(code, 0, err)
            self.assertIn("[flag]", out)
            self.assertEqual(doc.read_text(encoding="utf-8"), "flag-file")

            with mock.patch.object(forms, "select", side_effect=lambda _t, opts: opts[0].value):
                code, out, err = self.run_cli("show", tty=True)
            self.assertEqual(code, 0, err)
            self.assertIn("[lock]", out)
            self.assertEqual((folder / "x.txt").read_text(encoding="utf-8"), "lock-dir")

        with state.Store() as store:
            self.assertEqual(store.all(), [])

    def test_volume_confirmation_off_tty_cancel_and_refuse_without_tty(self) -> None:
        folder = self.root / "vol_dir"
        folder.mkdir()
        (folder / "x.txt").write_text("v", encoding="utf-8")

        code, _, err = self.run_cli("hide", str(folder), "--volume")
        self.assertEqual(code, 2)
        self.assertIn("--yes", err)

        with mock.patch.object(forms, "confirm", return_value=False):
            code, out, _ = self.run_cli("hide", str(folder), "--volume", tty=True)
        self.assertEqual(code, 0)
        self.assertIn("cancelled", out)
        self.assertTrue(folder.exists())

        with mock.patch.object(elevate, "is_admin", return_value=False):
            code, _, err = self.run_cli("hide", str(folder), "--volume", "--yes", tty=False)
        self.assertEqual(code, 4)
        self.assertIn("Administrator/root", err)

    def test_volume_hide_and_show_trigger_elevation_when_not_admin(self) -> None:
        folder = self.root / "vol_target"
        folder.mkdir()
        (folder / "x.txt").write_text("v", encoding="utf-8")
        bak = self.root / "vol.bak"
        bak.write_text("{}", encoding="utf-8")

        with (
            mock.patch.object(elevate, "is_admin", return_value=False),
            mock.patch.object(forms, "confirm", return_value=True),
            mock.patch.object(elevate, "run_elevated", return_value=0) as elevated,
        ):
            code, _, err = self.run_cli("hide", str(folder), "--volume", tty=True)
        self.assertEqual(code, 0, err)
        elevated.assert_called_once_with(
            ["hide", str(folder.resolve()), "--volume", "--yes"]
        )

        with state.Store() as store:
            store.add(
                state.Entry(
                    id="abc",
                    orig_path=state.norm_path(folder),
                    kind="dir",
                    level="volume",
                    hidden_path="disk@lba:100",
                    meta={"meta_lba": 100},
                    size_bytes=1,
                    hidden_at=1.0,
                )
            )
        with (
            mock.patch.object(elevate, "is_admin", return_value=False),
            mock.patch.object(elevate, "run_elevated", return_value=0) as elevated,
        ):
            code, _, err = self.run_cli("show", str(folder), "--backup", str(bak), tty=True)
        self.assertEqual(code, 0, err)
        elevated.assert_called_once_with(
            ["show", state.norm_path(folder), "--backup", str(bak.resolve())]
        )

    def test_elevated_worker_flag_captures_output_and_sets_home(self) -> None:
        out_f = self.root / "out.txt"
        err_f = self.root / "err.txt"
        req_f = self.root / "req.json"
        custom_home = self.root / "elevated_home"
        req_f.write_text(
            json.dumps({"home": str(custom_home), "out": str(out_f), "err": str(err_f)}),
            encoding="utf-8",
        )
        code = cli.main(["--_elevated", str(req_f), "show", "--list"])
        self.assertEqual(code, 0)
        self.assertEqual(os.environ.get("FLNH_HOME"), str(custom_home))
        self.assertEqual(os.environ.get(elevate.ELEVATED_ENV), "1")
        self.assertIn("no tracked hidden items", out_f.read_text(encoding="utf-8"))

    def test_uac_cancelled_and_unix_sudo(self) -> None:
        if sys.platform == "win32":
            fake_shell = mock.MagicMock()
            fake_shell.ShellExecuteExW.return_value = 0
            fake_k32 = mock.MagicMock()
            with (
                mock.patch.object(ctypes, "WinDLL", side_effect=[fake_shell, fake_k32]),
                mock.patch.object(ctypes, "get_last_error", return_value=elevate.ERROR_CANCELLED),
                self.assertRaises(errors.PermissionError) as caught,
            ):
                elevate.run_elevated(["hide", "X:\\a", "--volume", "--yes"])
            self.assertIn("cancelled", str(caught.exception))

        with (
            mock.patch.object(sys, "platform", "linux"),
            mock.patch("subprocess.run", return_value=mock.Mock(returncode=0)) as sub,
        ):
            rc = elevate.run_elevated(["hide", "/mnt/usb/a", "--volume", "--yes"])
        self.assertEqual(rc, 0)
        args = sub.call_args.args[0]
        self.assertEqual(args[0:2], ["sudo", "env"])
        self.assertIn(f"{elevate.ELEVATED_ENV}=1", args)

    def test_progress_formatting_and_elevated_prog_file(self) -> None:
        indet = cli.format_progress("shrinking", 0, 0, 0.0, fancy=False)
        self.assertEqual(indet, "  shrinking volume...")

        line = cli.format_progress("writing", 50 * 1024 * 1024, 100 * 1024 * 1024, 2.0, fancy=False)
        self.assertIn("writing tail", line)
        self.assertIn("50%", line)
        self.assertIn("/s", line)
        self.assertIn("eta 2.0s", line)

        prog_f = self.root / "prog.txt"
        old_env = os.environ.get(elevate.PROG_ENV)
        old_fancy = os.environ.get(elevate.PROG_FANCY_ENV)
        try:
            os.environ[elevate.PROG_ENV] = str(prog_f)
            os.environ[elevate.PROG_FANCY_ENV] = "0"
            with cli._Progress(min_interval=0.0) as prog:
                prog.update("writing", 512, 1024)
                written = prog_f.read_text(encoding="utf-8")
                self.assertIn("50%", written)
                self.assertIn("========", written)
            self.assertEqual(prog_f.read_text(encoding="utf-8"), "")
        finally:
            if old_env is None:
                os.environ.pop(elevate.PROG_ENV, None)
            else:
                os.environ[elevate.PROG_ENV] = old_env
            if old_fancy is None:
                os.environ.pop(elevate.PROG_FANCY_ENV, None)
            else:
                os.environ[elevate.PROG_FANCY_ENV] = old_fancy


if __name__ == "__main__":
    unittest.main()
