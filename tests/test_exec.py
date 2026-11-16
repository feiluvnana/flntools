"""`--exec CMD`: a command per saved file, never through a shell.

The command really runs here: the current Python, with a tiny script that writes the arguments
and environment it got to a JSON file. A file name full of shell metacharacters must arrive as
one literal argument, and nothing in it may run.
"""

from __future__ import annotations

import io
import json
import shlex
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

import flnd
from flnd import cli
from flnd._events import Saved
from flnd.source import Target

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
        self.event = Saved(Target("ins:example_user", kind="user"), "posts", "9001",
                           self.tmp / NASTY, None, 3)

    def command(self, *args: str) -> str:
        return shlex.join([sys.executable, "-c", RECORD, str(self.out), *args])

    def got(self) -> dict:
        return json.loads(self.out.read_text(encoding="utf-8"))

    def test_a_file_name_with_shell_metacharacters_is_one_literal_argument(self):
        runner = cli._Exec(self.command("{path}", "post={post}", "{target}"))
        self.assertEqual(runner.run(self.event), 0)
        got = self.got()
        self.assertEqual(got["argv"], [str(self.tmp / NASTY), "post=9001", "ins:example_user"])
        for made in ("PWNED", "PWNED2", "PWNED3"):
            self.assertFalse((self.tmp / made).exists())
            self.assertFalse(Path(made).exists())

    def test_the_values_are_in_the_environment_too(self):
        cli._Exec(self.command()).run(self.event)
        env = {name: value for name, value in self.got()["env"].items() if name not in ("FLND_TEST_NO_PROFILES", "FLND_SOURCES")}
        self.assertEqual(env, {"FLND_PATH": str(self.tmp / NASTY),
                               "FLND_TARGET": "ins:example_user",
                               "FLND_POST": "9001"})

    def test_no_shell_is_ever_started(self):
        runner = cli._Exec(self.command("{path}"))
        with mock.patch("subprocess.run", wraps=subprocess.run) as run:
            runner.run(self.event)
        [call] = run.call_args_list
        self.assertIsInstance(call.args[0], list)
        self.assertIs(call.kwargs["shell"], False)
        self.assertNotIn("sh", Path(call.args[0][0]).name.split("."))

    def test_a_failing_command_is_reported_and_the_run_goes_on(self):
        runner = cli._Exec(shlex.join([sys.executable, "-c", "raise SystemExit(3)"]))
        err = io.StringIO()
        with redirect_stderr(err):
            self.assertEqual(runner.run(self.event), 3)
        self.assertIn("exited 3", err.getvalue())

    def test_json_keeps_the_commands_output_off_standard_output(self):
        runner = cli._Exec(shlex.join([sys.executable, "-c", "print('noise')"]), quiet=True)
        with mock.patch("subprocess.run") as run:
            run.return_value.returncode = 0
            runner.run(self.event)
        self.assertIs(run.call_args.kwargs["stdout"], sys.stderr)


class WatchTest(unittest.TestCase):
    """`_watch` runs the command for each `Saved` event, and for nothing else."""

    def test_each_saved_file_runs_the_command_once_in_order(self):
        events = [Saved(Target("ins:example_user", kind="user"), "posts", str(n), Path(f"{n}.jpg"),
                        None, 1) for n in (1, 2)]
        events.insert(1, flnd.Skipped(Target("ins:example_user", kind="user"), "posts", "0"))
        run = mock.MagicMock()
        run.__iter__.return_value = iter(events)
        run.report = flnd.Report(saved=2)
        runner = mock.Mock()
        args = mock.Mock(json=True, ui="plain", verbose=False)
        with redirect_stdout(io.StringIO()):
            self.assertEqual(cli._watch(mock.Mock(ui=None), run, args, runner=runner), 0)
        self.assertEqual([call.args[0].post for call in runner.run.call_args_list], ["1", "2"])


class JsonErrorTest(unittest.TestCase):
    def test_an_error_that_ends_a_command_is_a_json_line_too(self):
        out = io.StringIO()
        error = flnd.UsageError("nothing to do", hint="give a target")
        with mock.patch("flnd._library.Flnd.check", side_effect=error), redirect_stdout(out), \
                redirect_stderr(io.StringIO()):
            code = cli.main(["--json", "check"])
        self.assertEqual(code, 2)
        self.assertEqual(json.loads(out.getvalue()), {
            "type": "Error",
            "error": {"class": "UsageError", "message": "nothing to do", "hint": "give a target"}})


if __name__ == "__main__":
    unittest.main()
