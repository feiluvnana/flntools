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

from shared import sh


class ShCase(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.folder = Path(folder.name)
        self.cache = self.folder / "cache" / "flntools" / "tools"
        env = mock.patch.dict(os.environ, {"XDG_CACHE_HOME": str(self.folder / "cache")})
        env.start()
        self.addCleanup(env.stop)
        found = mock.patch.dict(sh._found, clear=True)
        found.start()
        self.addCleanup(found.stop)


class RunTest(ShCase):
    def test_run_takes_arguments_and_captures_output(self):
        res = sh.run(sys.executable, "-c", "print('hello')")
        self.assertEqual((res.code, res.out.strip(), res.err), (0, "hello", ""))
        self.assertEqual(res.args, (sys.executable, "-c", "print('hello')"))

    def test_nonzero_raises_run_error_unless_check_is_false(self):
        code = "import sys; sys.stderr.write('boom'); sys.exit(3)"
        with self.assertRaises(sh.RunError) as caught:
            sh.run(sys.executable, "-c", code)
        self.assertEqual((caught.exception.code, caught.exception.err), (3, "boom"))
        self.assertEqual(sh.run(sys.executable, "-c", code, check=False).code, 3)

    def test_run_missing_executable_raises_missing_error(self):
        with self.assertRaises(sh.MissingError):
            sh.run("definitely_not_a_real_executable_xyz_123")


class FindTest(ShCase):
    def test_find_on_path_is_cached_for_the_process(self):
        with mock.patch("shared._sh.shutil.which", return_value=sys.executable) as which:
            tool = sh.find("python")
            self.assertIs(sh.find("python"), tool)
        self.assertEqual(which.call_count, 1)
        self.assertEqual(tool.path, Path(sys.executable))
        self.assertEqual(tool.run("-c", "print(42)").out.strip(), "42")

    def test_find_without_spec_raises_missing_error(self):
        with mock.patch("shared._sh.shutil.which", return_value=None):
            with self.assertRaises(sh.MissingError):
                sh.find("no_such_tool_xyz")

    def test_find_downloads_verifies_checksum_and_caches(self):
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

        with (
            mock.patch("shared._sh.shutil.which", return_value=None),
            mock.patch("shared._sh.http.download", side_effect=fake_download),
        ):
            tool = sh.find("mytool", specs={plat: spec})
            self.assertTrue(tool.path.is_file())
            self.assertTrue(tool.path.is_relative_to(self.cache))
            self.assertEqual(tool.path.read_bytes(), payload)
            self.assertIs(sh.find("mytool", specs={plat: spec}), tool)
            sh._found.clear()
            # A new process finds the cached binary without downloading again.
            self.assertEqual(sh.find("mytool", specs={plat: spec}).path, tool.path)
        self.assertEqual(len(calls), 1)

    def test_find_checksum_mismatch_raises_unsafe_error(self):
        plat = sh._platform_key()
        spec = sh.Spec(url="https://tools.example/bad.bin", sha256="0" * 64)

        def fake_download(url, dest, **kwargs):
            Path(dest).write_bytes(b"tampered")
            return Path(dest)

        with (
            mock.patch("shared._sh.shutil.which", return_value=None),
            mock.patch("shared._sh.http.download", side_effect=fake_download),
            self.assertRaises(sh.UnsafeError),
        ):
            sh.find("badtool", specs={plat: spec})



class StreamTest(ShCase):
    def setUp(self):
        super().setUp()
        self.python = sh.Tool(Path(sys.executable), "python")

    def test_stream_yields_lines_as_they_come(self):
        code = "import sys\nfor n in range(3): print(n, flush=True)\nsys.stderr.write('done')"
        self.assertEqual(list(self.python.stream("-c", code)), ["0", "1", "2"])

    def test_a_nonzero_exit_raises_with_the_stderr_tail_after_the_lines(self):
        code = "import sys\nprint('out')\nsys.stderr.write('bad input\\n')\nsys.exit(4)"
        lines = []
        with self.assertRaises(sh.RunError) as caught:
            for line in self.python.stream("-c", code):
                lines.append(line)
        self.assertEqual(lines, ["out"])
        self.assertEqual(caught.exception.code, 4)
        self.assertIn("bad input", caught.exception.err)

    def test_timeout_kills_the_tool(self):
        with self.assertRaises(sh.RunError) as caught:
            list(self.python.stream("-c", "import time; time.sleep(30)", timeout=0.2))
        self.assertEqual(caught.exception.code, -1)

    def test_leaving_early_stops_the_tool(self):
        code = "import itertools\nfor n in itertools.count(): print(n, flush=True)"
        lines = self.python.stream("-c", code)
        self.assertEqual(next(lines), "0")
        lines.close()


class FakeTest(ShCase):
    def test_fake_answers_run_find_and_stream_without_a_subprocess(self):
        seen = []

        def ffmpeg(args):
            seen.append(args)
            return 0, "frame=1\nframe=2\n", ""

        with sh.fake({"ffmpeg": ffmpeg, "7zz": sh.Result(2, "", "wrong password")}):
            tool = sh.find("ffmpeg")
            self.assertEqual(tool.path, Path("ffmpeg"))
            self.assertEqual(list(tool.stream("-i", "a.mp4")), ["frame=1", "frame=2"])
            self.assertEqual(sh.run("/usr/bin/ffmpeg", "-y").out, "frame=1\nframe=2\n")
            with self.assertRaises(sh.RunError) as caught:
                sh.find("7zz").run("x")
            self.assertEqual(caught.exception.err, "wrong password")
            self.assertEqual(sh.run("7zz", "x", check=False).args, ("7zz", "x"))
            with self.assertRaises(sh.MissingError):
                sh.find("unrar")
            with self.assertRaises(sh.MissingError):
                sh.run("git", "status")
        self.assertEqual(seen, [("ffmpeg", "-i", "a.mp4"), ("/usr/bin/ffmpeg", "-y")])

    def test_a_failing_fake_stream_raises_after_its_lines(self):
        with sh.fake({"ffmpeg": lambda args: (1, "a\n", "broken")}):
            lines = []
            with self.assertRaises(sh.RunError):
                for line in sh.find("ffmpeg").stream():
                    lines.append(line)
        self.assertEqual(lines, ["a"])


class VersionTest(ShCase):
    def test_version_is_parsed_once_and_cached_by_path_and_mtime(self):
        binary = self.folder / "mytool"
        binary.write_text("x")
        calls = []

        def mytool(args):
            calls.append(args)
            return 0, f"mytool version 1.{len(calls)}.0 (example.com build)", ""

        tool = sh.Tool(binary, "mytool")
        with sh.fake({"mytool": mytool}):
            self.assertEqual(tool.version, "1.1.0")
            self.assertEqual(tool.version, "1.1.0")
            self.assertEqual(calls, [(str(binary), "--version")])
            self.assertTrue((self.cache / "versions.json").is_file())
            os.utime(binary, (1_000, 1_000))
            self.assertEqual(tool.version, "1.2.0")

    def test_ffmpeg_style_tools_get_one_dash(self):
        with sh.fake({"ffmpeg": lambda args: (0, f"{args[1]} 6.1.1", "")}):
            self.assertEqual(sh.find("ffmpeg").version, "6.1.1")
            self.assertEqual(sh.find("ffmpeg").run("-version").out, "-version 6.1.1")


if __name__ == "__main__":
    unittest.main()
