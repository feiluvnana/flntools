"""`--exec CMD` and `[defaults] exec`: a command per saved file, never through a shell.

The command really runs here: the current Python, with a tiny script that writes the arguments
and environment it got to a JSON file. A file name full of shell metacharacters must arrive as
one literal argument, and nothing in it may run.
"""

from __future__ import annotations

import io
import json
import os
import shlex
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

import flnd
from flnd import cli
from flnd.engine import save
from flnd.errors import Stopped
from flnd.plugin import Target

# Writes what it was given to the file named by its first argument.
RECORD = ("import json, os, sys; json.dump({'argv': sys.argv[2:], 'env': {k: v for k, v in "
          "os.environ.items() if k.startswith('FLND_')}}, open(sys.argv[1], 'w'))")
# A name a site could give a file: quotes, a command substitution, a separator, a glob, a
# variable, a newline, and a placeholder of its own that must not be filled in again.
NASTY = "a b'; touch PWNED; $(touch PWNED2) `touch PWNED3` \"q\" * $HOME {post}\n.jpg"


class ExecTest(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.tmp = Path(folder.name)
        self.out = self.tmp / "got.json"
        self.target = Target("ins:example_user", kind="user")
        warn = mock.patch.object(save.ui, "warn")
        self.warn = warn.start()
        self.addCleanup(warn.stop)

    def run_exec(self, *argv: str, path: Path | None = None, timeout: float = 30,
                 stop: threading.Event | None = None) -> None:
        save._exec(list(argv), path or self.tmp / NASTY, self.target, "9001", timeout, stop)

    def python(self, code: str, *args: str) -> list[str]:
        return [sys.executable, "-c", code, *args]

    def said(self) -> str:
        return str(self.warn.call_args_list)

    def got(self) -> dict:
        return json.loads(self.out.read_text(encoding="utf-8"))

    def test_a_file_name_with_shell_metacharacters_is_one_literal_argument(self):
        self.run_exec(*self.python(RECORD, str(self.out), "{path}", "post={post}", "{target}"))
        self.assertEqual(self.got()["argv"],
                         [str(self.tmp / NASTY), "post=9001", "ins:example_user"])
        for made in ("PWNED", "PWNED2", "PWNED3"):
            self.assertFalse((self.tmp / made).exists())
            self.assertFalse(Path(made).exists())

    def test_the_values_are_in_the_environment_too(self):
        self.run_exec(*self.python(RECORD, str(self.out)))
        test_only = ("FLND_TEST_NO_PROFILES", "FLND_PLUGINS")
        env = {name: value for name, value in self.got()["env"].items() if name not in test_only}
        self.assertEqual(env, {"FLND_PATH": str(self.tmp / NASTY),
                               "FLND_TARGET": "ins:example_user", "FLND_POST": "9001"})

    def test_no_shell_is_started_and_stdout_is_not_kept(self):
        with mock.patch("subprocess.Popen", wraps=subprocess.Popen) as popen:
            self.run_exec(*self.python("print('x' * 100)"))
        [call] = popen.call_args_list
        self.assertIsInstance(call.args[0], list)
        self.assertNotIn("shell", call.kwargs)
        self.assertIs(call.kwargs["stdout"], subprocess.DEVNULL)

    def test_fifty_megabytes_on_stdout_are_dropped(self):
        code = ("import sys\nchunk = b'x' * (1 << 20)\n"
                "for _ in range(50): sys.stdout.buffer.write(chunk)")
        self.run_exec(*self.python(code))
        self.warn.assert_not_called()

    def test_a_failing_command_warns_with_the_last_line_of_a_long_stderr(self):
        code = ("import sys\nfor n in range(60000): sys.stderr.write('noise %d\\n' % n)\n"
                "sys.stderr.write('the last line\\n'); sys.exit(3)")
        self.run_exec(*self.python(code))
        self.assertIn("exit {code}", self.said())
        self.assertIn("code=3", self.said())
        self.assertIn("said=': the last line'", self.said())

    def test_the_kept_stderr_is_four_kib(self):
        code = "import sys; sys.stderr.write('a' * 50000 + '\\nlast'); sys.exit(1)"
        with mock.patch.object(save, "EXEC_TAIL", 64):
            self.run_exec(*self.python(code))
        self.assertIn("said=': last'", self.said())
        self.assertEqual(save.EXEC_TAIL, 4096)

    @unittest.skipIf(os.name == "nt", "the group's pid check is POSIX")
    def test_a_timeout_kills_the_whole_group_and_warns(self):
        code = ("import subprocess, sys, time\n"
                "child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])\n"
                "open(sys.argv[1], 'w').write(str(child.pid))\ntime.sleep(60)")
        started = time.monotonic()
        self.run_exec(*self.python(code, str(self.out)), timeout=0.8)
        self.assertLess(time.monotonic() - started, 2)
        self.assertIn("killed after", self.said())
        pid = int(self.out.read_text())
        for _ in range(20):
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                break
            time.sleep(0.05)
        else:
            self.fail("the child of the program is still running")

    def test_a_stop_kills_the_program(self):
        stop = threading.Event()
        threading.Timer(0.3, stop.set).start()
        started = time.monotonic()
        with self.assertRaises(Stopped):
            self.run_exec(*self.python("import time; time.sleep(60)"), stop=stop)
        self.assertLess(time.monotonic() - started, 2)

    def test_a_program_that_cannot_start_warns(self):
        self.run_exec(str(self.tmp / "missing"))
        self.assertIn("exec {program}", self.said())


class CommandTest(unittest.TestCase):
    def test_the_command_is_split_once_and_a_mistake_is_a_usage_error(self):
        args = mock.Mock(exec=shlex.join([sys.executable, "-c", "pass", "{path}"]))
        self.assertEqual(cli._command(args), [sys.executable, "-c", "pass", "{path}"])
        self.assertIsNone(cli._command(mock.Mock(exec=None)))
        for text in ("", "echo 'open", "definitely_not_a_program_x {path}"):
            with self.subTest(text), self.assertRaises(flnd.UsageError):
                cli._command(mock.Mock(exec=text))

    def test_get_hands_the_command_to_the_run(self):
        fl = mock.MagicMock()
        fl.targets = []
        args = mock.Mock(exec=shlex.join([sys.executable, "-c", "pass"]),
                         targets=["ins:example_user"], when=None, limit=None, dry=False,
                         folder=None)
        with mock.patch.object(cli, "_open", return_value=fl), \
                mock.patch.object(cli, "_targets", return_value=["ins:example_user"]), \
                mock.patch.object(cli, "_watch", return_value=0):
            cli.get(args)
        fl.__enter__.return_value.get.assert_called_once()
        self.assertEqual(fl.__enter__.return_value.get.call_args.kwargs["exec"],
                         [sys.executable, "-c", "pass"])


class JsonErrorTest(unittest.TestCase):
    def test_an_error_that_ends_a_command_is_a_json_line_too(self):
        out = io.StringIO()
        error = flnd.UsageError("nothing to do", hint="give a target")
        with mock.patch("flnd._library.Flnd.check", side_effect=error), redirect_stdout(out), \
                redirect_stderr(io.StringIO()):
            code = cli.main(["--json", "status"])
        self.assertEqual(code, 2)
        self.assertEqual(json.loads(out.getvalue()), {
            "type": "Error",
            "error": {"class": "UsageError", "message": "nothing to do", "hint": "give a target"}})


if __name__ == "__main__":
    unittest.main()
