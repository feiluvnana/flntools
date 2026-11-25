"""CLI tests for `flnh`: commands, exit codes, prompts, tracking, and confirmation."""

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
        self.assertEqual(cli.exit_code(errors.AuthError("auth")), 3)
        self.assertEqual(cli.exit_code(errors.SafetyError("os")), 4)
        self.assertEqual(cli.exit_code(errors.PermissionError("perm")), 4)
        self.assertEqual(cli.exit_code(errors.DiskError("disk")), 4)
        self.assertEqual(cli.exit_code(errors.Stopped("ctrl-c")), 130)

    def test_pass_auto_selects_vault_and_pass_with_flag_refuses(self) -> None:
        folder = self.root / "docs"
        folder.mkdir()
        (folder / "a.txt").write_text("hello", encoding="utf-8")

        code, _, err = self.run_cli("hide", str(folder), "--level", "flag", "--pass")
        self.assertEqual(code, 2)
        self.assertIn("--level flag", err)

        with mock.patch.object(forms, "text", side_effect=["pw123", "pw123"]):
            code, out, err = self.run_cli("hide", str(folder), "--pass", tty=True)
        self.assertEqual(code, 0, err)
        self.assertIn("[vault]", out)
        self.assertFalse(folder.exists())

        code, out, _ = self.run_cli("show", "--list")
        self.assertEqual(code, 0)
        self.assertIn("docs", out)
        self.assertIn("vault", out)

        with (
            mock.patch.object(forms, "select", side_effect=lambda _t, opts: opts[0].value),
            mock.patch.object(forms, "text", return_value="pw123"),
        ):
            code, out, err = self.run_cli("show", tty=True)
        self.assertEqual(code, 0, err)
        self.assertIn("[vault]", out)
        self.assertEqual((folder / "a.txt").read_text(encoding="utf-8"), "hello")

    def test_pass_stdin_keyfile_and_no_track(self) -> None:
        f1 = self.root / "f1.txt"
        f1.write_text("one", encoding="utf-8")
        code, _, err = self.run_cli("hide", str(f1), "-l", "3", "--pass-stdin", "--no-track", stdin_text="pw\n")
        self.assertEqual(code, 0, err)
        self.assertFalse(f1.exists())
        with state.Store() as store:
            self.assertEqual(store.all(), [])

        code, _, _ = self.run_cli("show", str(f1), "--pass-stdin", stdin_text="wrong\n")
        self.assertEqual(code, 3)

        code, _, err = self.run_cli("show", str(f1), "--pass-stdin", stdin_text="pw\n")
        self.assertEqual(code, 0, err)
        self.assertEqual(f1.read_text(encoding="utf-8"), "one")

        kf = self.root / "my.key"
        kf.write_bytes(b"secret-key-bytes")
        f2 = self.root / "f2"
        f2.mkdir()
        (f2 / "k.txt").write_text("two", encoding="utf-8")
        code, _, err = self.run_cli("hide", str(f2), "--keyfile", str(kf))
        self.assertEqual(code, 0, err)
        code, _, err = self.run_cli("show", str(f2), "--keyfile", str(kf))
        self.assertEqual(code, 0, err)
        self.assertEqual((f2 / "k.txt").read_text(encoding="utf-8"), "two")

    def test_volume_confirmation_off_tty_and_cancel(self) -> None:
        folder = self.root / "vol_dir"
        folder.mkdir()
        (folder / "x.txt").write_text("v", encoding="utf-8")

        code, _, err = self.run_cli("hide", str(folder), "--level", "volume", "--pass-stdin", stdin_text="pw\n")
        self.assertEqual(code, 2)
        self.assertIn("--yes", err)

        with mock.patch.object(forms, "confirm", return_value=False):
            code, out, _ = self.run_cli(
                "hide", str(folder), "--level", "4", "--pass-stdin", stdin_text="pw\n", tty=True
            )
        self.assertEqual(code, 0)
        self.assertIn("cancelled", out)
        self.assertTrue(folder.exists())


if __name__ == "__main__":
    unittest.main()
