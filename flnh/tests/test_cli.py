"""CLI tests for `flnh`: commands, exit codes, tracking, picker, and confirmation."""

from __future__ import annotations

import io
import os
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

from flnh import cli, errors, state
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

    def test_volume_confirmation_off_tty_and_cancel(self) -> None:
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


if __name__ == "__main__":
    unittest.main()
