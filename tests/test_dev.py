"""scripts/dev.py, the task runner, and the Makefile that is only a shortcut to it.

subprocess.run is faked for the tasks, so no uv command or build runs; the unknown-task test
runs the real script in a fresh interpreter.
"""

import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from test_packaging import ROOT, load_dev

dev = load_dev()


def finished(command: list[str], *, stdout: str = "", returncode: int = 0) -> subprocess.CompletedProcess[str]:
    """What a fake subprocess.run returns for `command`."""
    return subprocess.CompletedProcess(command, returncode, stdout=stdout, stderr="")


class TasksTest(unittest.TestCase):
    def test_unknown_task_lists_the_tasks_and_exits_2(self):
        for argv in (["nothing"], []):
            result = subprocess.run([sys.executable, str(ROOT / "scripts" / "dev.py"), *argv],
                                    capture_output=True, text=True, timeout=60)
            self.assertEqual(result.returncode, 2, argv)
            self.assertIn("tasks: setup test lint type check api flnd dist try", result.stderr)

    def test_python_option_and_passthrough(self):
        with mock.patch.object(dev.subprocess, "run", side_effect=lambda command, **kwargs: finished(command)) as run:
            self.assertEqual(dev.main(["flnd", "--python", "3.11", "download", "ins:example_user"]), 0)
        self.assertEqual(run.call_args.args[0], ["uv", "run", "--python", "3.11", "flnd", "download", "ins:example_user"])

    def test_python_from_the_environment(self):
        with mock.patch.object(dev.subprocess, "run", side_effect=lambda command, **kwargs: finished(command)) as run, \
                mock.patch.dict(dev.os.environ, {"PYTHON": "3.12"}):
            dev.main(["setup"])
        self.assertEqual(run.call_args.args[0], ["uv", "sync", "--python", "3.12", "--extra", "archive"])

    def test_check_stops_at_the_first_failure_with_its_exit_code(self):
        # lint passes, type fails with 3, so test never runs and make sees pyright's own code.
        def run(command, **kwargs):
            if "pyright" in command:
                raise subprocess.CalledProcessError(3, command)
            return finished(command)

        with mock.patch.object(dev.subprocess, "run", side_effect=run) as fake:
            self.assertEqual(dev.main(["check"]), 3)
        self.assertEqual([call.args[0][4:] for call in fake.call_args_list],
                         [["python", "-m", "compileall", "-q", "flnd", "shared"], ["pyright"]])

    def test_api_writes_the_snapshot_from_inside_the_environment(self):
        # The snapshot imports shared and flnd, so it can't run in this --no-project interpreter.
        with mock.patch.object(dev.subprocess, "run", side_effect=lambda command, **kwargs: finished(command)) as run, \
                mock.patch.dict(dev.os.environ, {"PYTHON": ""}):
            self.assertEqual(dev.main(["api"]), 0)
        self.assertEqual(run.call_args.args[0], ["uv", "run", "--python", "3.14", "python", "-c", dev.WRITE_API])
        self.assertIn("dev.write_api()", dev.WRITE_API)

    def test_try_runs_the_dry_run_inside_the_environment_and_passes_its_arguments_through(self):
        # The listing needs flnd's code, which this --no-project interpreter lacks, so it runs through uv.
        with mock.patch.object(dev.subprocess, "run", side_effect=lambda command, **kwargs: finished(command)) as run, \
                mock.patch.dict(dev.os.environ, {"PYTHON": ""}):
            self.assertEqual(dev.main(["try", "ins:example_user", "--from", "reels", "--time", "2026-01-01,"]), 0)
        self.assertEqual(run.call_args.args[0], ["uv", "run", "--python", "3.14", "python", "-m", "flnd.dry",
                                                 "ins:example_user", "--from", "reels", "--time", "2026-01-01,"])
        self.assertEqual(run.call_args.kwargs["cwd"], dev.ROOT)
        self.assertIsNone(run.call_args.kwargs.get("shell"))

    def test_try_exits_with_the_failing_commands_code(self):
        with mock.patch.object(dev.subprocess, "run", side_effect=subprocess.CalledProcessError(1, ["uv"])):
            self.assertEqual(dev.main(["try", "ins:example_user"]), 1)

    def test_dist_writes_ffmpeg_source_in_utf8(self):
        def run(command, **kwargs):
            if command[-1].startswith("import imageio_ffmpeg"):
                return finished(command, stdout="/bin/ffmpeg\n")
            return finished(command, stdout="ffmpeg version 7.1 © the FFmpeg developers\n")

        with tempfile.TemporaryDirectory() as root, mock.patch.object(dev, "ROOT", Path(root)), \
                mock.patch.object(dev.subprocess, "run", side_effect=run) as fake, \
                mock.patch.dict(dev.os.environ, {"PYTHON": ""}):
            licenses = Path(root) / "dist" / "flnd" / "licenses"
            licenses.mkdir(parents=True)  # what the real bundler.build leaves behind
            self.assertEqual(dev.main(["dist"]), 0)
            text = (licenses / "ffmpeg-source.txt").read_text(encoding="utf-8")
        self.assertTrue(text.startswith("ffmpeg-source\n"))
        self.assertIn("ffmpeg version 7.1 © the FFmpeg developers", text)
        commands = [call.args[0] for call in fake.call_args_list]
        self.assertEqual(commands[0], ["uv", "sync", "--python", "3.14", "--group", "dist"])
        self.assertIn("bundler.build('flnd/__main__.py', name='flnd', data=('licenses',), dest='dist')", commands[1][-1])
        self.assertEqual(commands[-1], ["/bin/ffmpeg", "-hide_banner", "-version"])
        # Every command is a list of arguments, never a shell string.
        self.assertTrue(all(call.kwargs.get("shell") is None for call in fake.call_args_list))


class MakefileTest(unittest.TestCase):
    """The Makefile holds no logic and no shell syntax, so it runs the same under sh and cmd.exe."""

    makefile = (ROOT / "Makefile").read_text(encoding="utf-8")

    def test_every_target_is_a_task(self):
        targets = re.search(r"^TASKS := (.+)$", self.makefile, re.MULTILINE)
        assert targets is not None
        self.assertEqual(targets.group(1).split(), list(dev.TASKS))

    def test_no_shell_syntax(self):
        for banned in ("ifeq", "'", '"', ".venv"):
            self.assertNotIn(banned, self.makefile)


if __name__ == "__main__":
    unittest.main()
