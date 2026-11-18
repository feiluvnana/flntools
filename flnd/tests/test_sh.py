"""`flnkit.files`: running ffmpeg and 7zz, and faking them."""

import sys
import unittest
from pathlib import Path
from unittest import mock

from flnkit import files as _tools


class ShCase(unittest.TestCase):
    def setUp(self):
        self.enterContext(mock.patch.dict(_tools._found, clear=True))


class RunTest(ShCase):
    def test_run_captures_output_and_raises_on_a_failure_unless_unchecked(self):
        python = _tools.Tool(Path(sys.executable), "python")
        res = python.run("-c", "print('hello')")
        self.assertEqual((res.code, res.out.strip(), res.err), (0, "hello", ""))
        self.assertEqual(res.args, (sys.executable, "-c", "print('hello')"))
        code = "import sys; sys.stderr.write('boom'); sys.exit(3)"
        with self.assertRaises(_tools.RunError) as caught:
            python.run("-c", code)
        self.assertEqual((caught.exception.code, caught.exception.err), (3, "boom"))
        self.assertEqual(python.run("-c", code, check=False).code, 3)
        with self.assertRaises(_tools.MissingError):
            _tools.Tool(Path(sys.executable).parent / "definitely_not_a_real_executable",
                        "gone").run()

    def test_find_on_path_is_cached_and_a_missing_tool_has_a_hint(self):
        with mock.patch("flnkit.files.shutil.which", return_value=sys.executable) as which:
            tool = _tools.find("python")
            self.assertIs(_tools.find("python"), tool)
        self.assertEqual((which.call_count, tool.path), (1, Path(sys.executable)))
        self.assertEqual(tool.run("-c", "print(42)").out.strip(), "42")
        with mock.patch("flnkit.files.shutil.which", return_value=None), \
                self.assertRaises(_tools.MissingError) as caught:
            _tools.ffprobe()
        self.assertEqual(caught.exception.hint, "install ffmpeg, or put it on PATH")

    def test_sevenzip_takes_7zz_then_7z(self):
        ok = _tools.Result(0, "", "")
        with _tools.fake({"7z": ok}):
            self.assertEqual(_tools.sevenzip().name, "7z")
        with _tools.fake({"7zz": ok, "7z": ok}):
            self.assertEqual(_tools.sevenzip().name, "7zz")
        with _tools.fake({}), self.assertRaises(_tools.MissingError):
            _tools.sevenzip()


class FakeTest(ShCase):
    def test_fake_answers_run_and_find_without_a_subprocess(self):
        seen = []

        def ffmpeg(args):
            seen.append(args)
            return 0, "frame=1\nframe=2\n", ""

        with _tools.fake({"ffmpeg": ffmpeg, "7zz": _tools.Result(2, "", "wrong password")}):
            tool = _tools.find("ffmpeg")
            self.assertEqual(tool.path, Path("ffmpeg"))
            self.assertEqual(tool.run("-i", "a.mp4").out, "frame=1\nframe=2\n")
            self.assertEqual(_tools.Tool(Path("/usr/bin/ffmpeg"), "ffmpeg").run("-y").out,
                             "frame=1\nframe=2\n")
            with self.assertRaises(_tools.RunError) as caught:
                _tools.find("7zz").run("x")
            self.assertEqual(caught.exception.err, "wrong password")
            self.assertEqual(_tools.find("7zz").run("x", check=False).args, ("7zz", "x"))
            with self.assertRaises(_tools.MissingError):
                _tools.find("unrar")
        self.assertEqual(seen, [("ffmpeg", "-i", "a.mp4"), (str(Path("/usr/bin/ffmpeg")), "-y")])


if __name__ == "__main__":
    unittest.main()