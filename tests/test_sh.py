"""Tests for `shared.sh`: subprocess execution and checksum-verified external tools."""

import hashlib
import io
import os
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest import mock

import httpx

import shared
from shared import sh


class RunTest(unittest.TestCase):
    def test_run_captures_output_and_raises_run_error_on_nonzero(self):
        res = sh.run([sys.executable, "-c", "print('hello')"])
        self.assertEqual(res.returncode, 0)
        self.assertEqual(res.stdout.strip(), "hello")

        with self.assertRaises(sh.RunError) as caught:
            sh.run([sys.executable, "-c", "import sys; sys.stderr.write('boom'); sys.exit(3)"])
        self.assertEqual(caught.exception.returncode, 3)
        self.assertIn("boom", caught.exception.stderr)

    def test_run_missing_executable_raises_missing_error(self):
        with self.assertRaises(sh.MissingError):
            sh.run(["definitely_not_a_real_executable_xyz_123"])


class ToolTest(unittest.TestCase):
    def test_tool_on_path_uses_shutil_which(self):
        with mock.patch("shared._sh.shutil.which", return_value=sys.executable):
            tool = sh.tool("python")
        self.assertEqual(tool.path, Path(sys.executable))
        res = tool.run("-c", "print(42)")
        self.assertEqual(res.stdout.strip(), "42")

    def test_tool_without_spec_raises_missing_error(self):
        with mock.patch("shared._sh.shutil.which", return_value=None):
            with self.assertRaises(sh.MissingError):
                sh.tool("no_such_tool_xyz")

    def test_tool_downloads_verifies_checksum_and_caches(self):
        buf = io.BytesIO()
        exe_name = "mytool.exe" if sys.platform == "win32" else "mytool"
        payload = b"fake-binary-bytes"
        with zipfile.ZipFile(buf, "w") as zf:
            zf.writestr(f"bin/{exe_name}", payload)
        archive_bytes = buf.getvalue()
        digest = hashlib.sha256(archive_bytes).hexdigest()

        plat = sh._platform_key()
        spec = sh.Spec(url="https://tools.example/mytool.zip", sha256=digest, bin=f"bin/{exe_name}")
        calls = []

        def fake_download(url, dest, **kwargs):
            calls.append(url)
            Path(dest).write_bytes(archive_bytes)
            return Path(dest)

        with tempfile.TemporaryDirectory() as tmp:
            cache_dir = Path(tmp) / "tools"
            with (
                mock.patch("shared._sh.shutil.which", return_value=None),
                mock.patch("shared._sh.http.download", side_effect=fake_download),
            ):
                tool = sh.tool("mytool", platforms={plat: spec}, dest=cache_dir)
                self.assertTrue(tool.path.is_file())
                self.assertEqual(tool.path.read_bytes(), payload)
                # Second call uses cache without downloading again.
                tool2 = sh.tool("mytool", platforms={plat: spec}, dest=cache_dir)
                self.assertEqual(tool2.path, tool.path)
            self.assertEqual(len(calls), 1)

    def test_tool_checksum_mismatch_raises_unsafe_error(self):
        plat = sh._platform_key()
        spec = sh.Spec(url="https://tools.example/bad.bin", sha256="0" * 64)

        def fake_download(url, dest, **kwargs):
            Path(dest).write_bytes(b"tampered")
            return Path(dest)

        with tempfile.TemporaryDirectory() as tmp:
            with (
                mock.patch("shared._sh.shutil.which", return_value=None),
                mock.patch("shared._sh.http.download", side_effect=fake_download),
                self.assertRaises(sh.UnsafeError),
            ):
                sh.tool("badtool", platforms={plat: spec}, dest=Path(tmp) / "tools")


if __name__ == "__main__":
    unittest.main()
