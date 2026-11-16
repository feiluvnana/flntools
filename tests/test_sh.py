"""Tests for `flnd._tools`: running ffmpeg and 7zz, streaming their output, and faking them."""

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from flnd import _tools


class ShCase(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.folder = Path(folder.name)
        self.cache = self.folder / "cache" / "flnd" / "tools"
        env = mock.patch.dict(os.environ, {"XDG_CACHE_HOME": str(self.folder / "cache")})
        env.start()
        self.addCleanup(env.stop)
        found = mock.patch.dict(_tools._found, clear=True)
        found.start()
        self.addCleanup(found.stop)


class RunTest(ShCase):
    def setUp(self):
        super().setUp()
        self.python = _tools.Tool(Path(sys.executable), "python")

    def test_run_takes_arguments_and_captures_output(self):
        res = self.python.run("-c", "print('hello')")
        self.assertEqual((res.code, res.out.strip(), res.err), (0, "hello", ""))
        self.assertEqual(res.args, (sys.executable, "-c", "print('hello')"))

    def test_nonzero_raises_run_error_unless_check_is_false(self):
        code = "import sys; sys.stderr.write('boom'); sys.exit(3)"
        with self.assertRaises(_tools.RunError) as caught:
            self.python.run("-c", code)
        self.assertEqual((caught.exception.code, caught.exception.err), (3, "boom"))
        self.assertEqual(self.python.run("-c", code, check=False).code, 3)

    def test_run_missing_executable_raises_missing_error(self):
        gone = _tools.Tool(self.folder / "definitely_not_a_real_executable", "gone")
        with self.assertRaises(_tools.MissingError):
            gone.run()


class FindTest(ShCase):
    def test_find_on_path_is_cached_for_the_process(self):
        with mock.patch("flnd._tools.shutil.which", return_value=sys.executable) as which:
            tool = _tools.find("python")
            self.assertIs(_tools.find("python"), tool)
        self.assertEqual(which.call_count, 1)
        self.assertEqual(tool.path, Path(sys.executable))
        self.assertEqual(tool.run("-c", "print(42)").out.strip(), "42")

    def test_a_tool_not_on_path_is_missing_with_a_hint(self):
        with mock.patch("flnd._tools.shutil.which", return_value=None):
            with self.assertRaises(_tools.MissingError) as caught:
                _tools.ffprobe()
        self.assertEqual(caught.exception.hint, "install ffmpeg, or put it on PATH")

    def test_sevenzip_takes_7zz_then_7z(self):
        with _tools.fake({"7z": _tools.Result(0, "", "")}):
            self.assertEqual(_tools.sevenzip().name, "7z")
        with _tools.fake({"7zz": _tools.Result(0, "", ""), "7z": _tools.Result(0, "", "")}):
            self.assertEqual(_tools.sevenzip().name, "7zz")
        with _tools.fake({}), self.assertRaises(_tools.MissingError):
            _tools.sevenzip()


class StreamTest(ShCase):
    def setUp(self):
        super().setUp()
        self.python = _tools.Tool(Path(sys.executable), "python")

    def test_stream_yields_lines_as_they_come(self):
        code = "import sys\nfor n in range(3): print(n, flush=True)\nsys.stderr.write('done')"
        self.assertEqual(list(self.python.stream("-c", code)), ["0", "1", "2"])

    def test_a_nonzero_exit_raises_with_the_stderr_tail_after_the_lines(self):
        code = "import sys\nprint('out')\nsys.stderr.write('bad input\\n')\nsys.exit(4)"
        lines = []
        with self.assertRaises(_tools.RunError) as caught:
            for line in self.python.stream("-c", code):
                lines.append(line)
        self.assertEqual(lines, ["out"])
        self.assertEqual(caught.exception.code, 4)
        self.assertIn("bad input", caught.exception.err)

    def test_timeout_kills_the_tool(self):
        with self.assertRaises(_tools.RunError) as caught:
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

        with _tools.fake({"ffmpeg": ffmpeg, "7zz": _tools.Result(2, "", "wrong password")}):
            tool = _tools.find("ffmpeg")
            self.assertEqual(tool.path, Path("ffmpeg"))
            self.assertEqual(list(tool.stream("-i", "a.mp4")), ["frame=1", "frame=2"])
            self.assertEqual(_tools.Tool(Path("/usr/bin/ffmpeg"), "ffmpeg").run("-y").out,
                             "frame=1\nframe=2\n")
            with self.assertRaises(_tools.RunError) as caught:
                _tools.find("7zz").run("x")
            self.assertEqual(caught.exception.err, "wrong password")
            self.assertEqual(_tools.find("7zz").run("x", check=False).args, ("7zz", "x"))
            with self.assertRaises(_tools.MissingError):
                _tools.find("unrar")
        self.assertEqual(seen, [("ffmpeg", "-i", "a.mp4"), (str(Path("/usr/bin/ffmpeg")), "-y")])

    def test_a_failing_fake_stream_raises_after_its_lines(self):
        with _tools.fake({"ffmpeg": lambda args: (1, "a\n", "broken")}):
            lines = []
            with self.assertRaises(_tools.RunError):
                for line in _tools.find("ffmpeg").stream():
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

        tool = _tools.Tool(binary, "mytool")
        with _tools.fake({"mytool": mytool}):
            self.assertEqual(tool.version, "1.1.0")
            self.assertEqual(tool.version, "1.1.0")
            self.assertEqual(calls, [(str(binary), "--version")])
            self.assertTrue((self.cache / "versions.json").is_file())
            os.utime(binary, (1_000, 1_000))
            self.assertEqual(tool.version, "1.2.0")

    def test_ffmpeg_style_tools_get_one_dash(self):
        with _tools.fake({"ffmpeg": lambda args: (0, f"{args[1]} 6.1.1", "")}):
            self.assertEqual(_tools.find("ffmpeg").version, "6.1.1")
            self.assertEqual(_tools.find("ffmpeg").run("-version").out, "-version 6.1.1")


if __name__ == "__main__":
    unittest.main()
