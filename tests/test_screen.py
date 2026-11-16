"""The full-screen view (`flnd._screen`): its frames, its keys, and the terminal it gives back.

Frames are drawn on a Rich console recording to a string, never a real terminal. The keys are
read through a pipe that says it is a terminal, with a fake `termios` that records each mode
set, so a test can see the mode put back after `q`, Ctrl-C and a crash.
"""

import io
import os
import pathlib
import time
import unittest
from unittest import mock

from rich.console import Console

import flnd
from flnd import _prompt
from flnd._events import Host, Listing, Pause, Stats, System, Transfer, Transfers
from flnd._screen import Keys, Screen
from flnd.source import Target

TARGET = Target("ins:example_user", kind="user")


def stats(targets=3, files=2, **fields):
    """A `Stats` with `targets` listing rows and `files` transfers in flight."""
    states = ("listing", "finishing", "done", "caught up", "failed")
    listing = tuple(Listing(Target(f"ins:example_user{n}", kind="user"), "posts", pages=n,
                            posts=10 * n, found=20, saved=n, state=states[n % len(states)])
                    for n in range(targets))
    flows = tuple(Transfer(f"example_user/2026-03-01_{n}.mp4", "cdn.example.com", done=512,
                           total=2048, speed=1024.0, resumed=n == 0) for n in range(files))
    return Stats(elapsed=5.0, saved=4, bytes=8192, speed=4096.0, listing=listing,
                 transfers=Transfers(running=files, queued=7, resuming=1, files=flows,
                                     hosts=(Host("cdn.example.com", 8, files, 2, 32),)),
                 pauses=(Pause("ins", "www.example.com", 180.0, "Retry-After"),),
                 system=System(tabs=2, cap=6, browser=400 << 20, ram=150 << 20, cpu=12.0,
                               ffmpeg=1, pillow=2, disk=50 << 30), **fields)


def console(width=80, height=24):
    return Console(file=io.StringIO(), width=width, height=height, color_system=None)


class FrameTest(unittest.TestCase):
    def test_an_80_by_24_frame_shows_every_panel(self):
        screen = Screen("download ins:example_user", console=console())
        screen.show(flnd.Saved(TARGET, "posts", "1", pathlib.Path("a.jpg"), None, 2048))
        screen.log.write("! www.example.com: rate limited; waiting 3m\n")
        screen.show(stats())
        text = screen.text()
        lines = text.splitlines()
        self.assertEqual(len(lines), 24)
        self.assertTrue(all(len(line) <= 80 for line in lines))
        for part in ("flnd", "download ins:example_user", "1 saved", "Targets", "Downloads",
                     "System", "tabs 2/6", "ffmpeg 1", "pillow 2", "free", "Log",
                     "www.example.com", "Retry-After", "q", "stop", "pause"):
            self.assertIn(part, text)
        # Busy rows first: the target listing comes before the one done.
        self.assertLess(text.index("example_user0 posts"), text.index("example_user2 posts"))
        self.assertIn("7 queued", text)
        self.assertIn("cdn.example.com 2/8 +2", text)

    def test_a_wide_frame_adds_pages_and_hosts(self):
        screen = Screen("sync", console=console(140, 40))
        screen.show(stats())
        text = screen.text()
        self.assertIn("2 pages", text)
        self.assertRegex(text, r"example_user/2026-03-01_1\.mp4 .* cdn\.example\.com")

    def test_a_thousand_rows_fit_and_say_how_many_more(self):
        screen = Screen("sync", console=console())
        screen.show(stats(targets=1000, files=1000))
        lines = screen.text().splitlines()
        self.assertEqual(len(lines), 24)
        self.assertTrue(any("more (" in line for line in lines))

    def test_before_the_first_sample_the_rows_come_from_progress(self):
        screen = Screen("download", console=console())
        screen.show(flnd.Progress(TARGET, "reels", found=3, saved=1))
        text = screen.text()
        self.assertIn("ins:example_user reels", text)
        self.assertIn("listing", text)

    def test_the_stop_stages_and_a_pause_show_in_the_footer(self):
        screen = Screen("download", console=console())
        screen.show(stats(suspended=True))
        self.assertIn("paused", screen.text().splitlines()[-1])
        screen.show(stats(stopping="soon"))
        self.assertIn("Ctrl-C again to stop now", screen.text().splitlines()[-1])
        screen.show(stats(stopping="now"))
        self.assertIn("stopping now", screen.text().splitlines()[-1])
        self.assertIn("Ctrl-C again to quit now", screen.text(width=120).splitlines()[-1])

    def test_text_from_a_site_cannot_move_the_cursor(self):
        screen = Screen("download", console=console())
        screen.show(stats(files=0, targets=0))
        screen.log.write("! \x1b[2Jexample_user: \x1b]0;title\x07odd name\n")
        self.assertNotIn("\x1b", screen.text())
        self.assertIn("example_user: odd name", screen.text())

    def test_a_drawing_bug_shows_a_line_instead_of_raising(self):
        out = console()
        screen = Screen("download", console=out)
        with mock.patch.object(screen, "frame", side_effect=ValueError("broken")):
            out.print(screen)
        self.assertIn("can't draw the screen", out.file.getvalue())

    def test_failures_and_a_plan_are_printed_after_the_screen(self):
        screen = Screen("download", console=console())
        screen.show(flnd.Failed(TARGET, flnd.NotFound("gone", hint="nothing to do"), post="9"))
        screen.show(flnd.Planned(TARGET, "9", pathlib.Path("a.jpg"), "new"))
        out, err = io.StringIO(), io.StringIO()
        with mock.patch("sys.stdout", out), mock.patch("sys.stderr", err):
            screen.close()
        self.assertEqual(out.getvalue(), "new a.jpg\n")
        self.assertEqual(err.getvalue(),
                         "failed ins:example_user 9: gone\n  nothing to do\n")


class KeysTest(unittest.TestCase):
    def test_q_stops_and_p_suspends_then_resumes(self):
        run = mock.Mock()
        screen = Screen("download", run=run, console=console(), keys=False)
        screen.key("p")
        screen.key("p")
        screen.key("x")
        screen.key("q")
        self.assertEqual([call[0] for call in run.method_calls], ["suspend", "resume", "stop"])
        self.assertIn("stopping", screen.text().splitlines()[-1])


class _Terminal:
    """A fake `termios`: a mode per descriptor, and every mode set, in order."""

    ICANON, ECHO, VMIN, VTIME, TCSANOW, TCSADRAIN = 2, 8, 6, 5, 0, 1

    def __init__(self):
        self.mode = [1, 2, 3, 0xFF, 9600, 9600, [0] * 20]
        self.set = []

    def tcgetattr(self, fd):
        return [*self.mode[:6], list(self.mode[6])]

    def tcsetattr(self, fd, when, mode):
        self.set.append((when, mode))
        self.mode = mode


class _Tty(io.StringIO):
    """Standard input as a terminal: a pipe whose read end says it is one."""

    def __init__(self):
        super().__init__()
        self.read_end, self.write_end = os.pipe()

    def isatty(self):
        return True

    def fileno(self):
        return self.read_end

    def shut(self):
        for fd in (self.read_end, self.write_end):
            os.close(fd)


@unittest.skipIf(os.name == "nt", "termios is POSIX")
class RestoreTest(unittest.TestCase):
    def setUp(self):
        self.terminal, self.tty = _Terminal(), _Tty()
        self.addCleanup(self.tty.shut)
        self.original = self.terminal.tcgetattr(0)
        self.run = mock.Mock()
        self.screen = Screen("download", run=self.run, console=console())
        self.screen._keys = Keys(self.screen.key, stream=self.tty, terminal=self.terminal)

    def assert_restored(self):
        self.assertEqual(self.terminal.mode, self.original)
        self.assertEqual(self.terminal.set[-1][0], _Terminal.TCSADRAIN)
        self.assertFalse(self.screen._live.is_started)
        self.assertEqual(_prompt._covers, [])

    def test_cbreak_while_shown_and_restored_after_q(self):
        with self.screen:
            cbreak = self.terminal.mode
            self.assertEqual(cbreak[3] & (_Terminal.ICANON | _Terminal.ECHO), 0)
            self.assertEqual((cbreak[6][_Terminal.VMIN], cbreak[6][_Terminal.VTIME]), (1, 0))
            os.write(self.tty.write_end, b"q")
            deadline = time.monotonic() + 2
            while not self.run.stop.called and time.monotonic() < deadline:
                time.sleep(0.01)
        self.run.stop.assert_called_once()
        self.assert_restored()

    def test_restored_after_ctrl_c(self):
        with self.assertRaises(KeyboardInterrupt), self.screen:
            raise KeyboardInterrupt
        self.assert_restored()

    def test_restored_after_a_crash(self):
        with self.assertRaises(RuntimeError), self.screen:
            raise RuntimeError("a bug")
        self.assert_restored()

    def test_a_question_gives_the_terminal_back_and_takes_it_again(self):
        with self.screen:
            with _prompt._paused():
                self.assertEqual(self.terminal.mode, self.original)
                self.assertFalse(self.screen._live.is_started)
            self.assertTrue(self.screen._live.is_started)
            self.assertNotEqual(self.terminal.mode, self.original)
        self.assert_restored()

    def test_a_hard_exit_puts_the_mode_and_the_screen_back_without_leaving_the_with(self):
        from flnd import _run

        terminal = io.StringIO()
        terminal.isatty = lambda: True
        self.screen.console = Console(file=terminal, width=80, height=24, force_terminal=True)
        with self.screen:
            self.assertNotEqual(self.terminal.mode, self.original)
            self.assertEqual(_run._RESTORERS, [self.screen.restore_now])
            # What a third Ctrl-C runs before `os._exit`.
            for undo in reversed(_run._RESTORERS):
                undo()
            self.assertEqual(self.terminal.mode, self.original)
            self.assertIn("\x1b[?25h\x1b[?1049l", terminal.getvalue())
        self.assertEqual(_run._RESTORERS, [])
        self.assert_restored()

    def test_off_a_terminal_keys_change_nothing(self):
        keys = Keys(print, stream=io.StringIO(""), terminal=self.terminal)
        keys.start()
        keys.stop()
        self.assertEqual(self.terminal.set, [])


class ViewTest(unittest.TestCase):
    """Which view a run takes: `--ui`, else the home's `[defaults] ui`, else full on a terminal."""

    def view(self, *, ui=None, configured=None, terminal=True, json=False):
        from flnd import cli

        args = mock.Mock(ui="plain" if json else ui, json=json)
        with mock.patch("sys.stderr.isatty", return_value=terminal):
            return cli._view(mock.Mock(ui=configured), args)

    def test_full_is_the_default_on_a_terminal(self):
        self.assertEqual(self.view(), "full")

    def test_plain_off_a_terminal_even_when_asked_for_full(self):
        self.assertEqual(self.view(terminal=False), "plain")
        self.assertEqual(self.view(ui="full", terminal=False), "plain")
        self.assertEqual(self.view(configured="full", terminal=False), "plain")

    def test_the_flag_wins_over_the_config_file(self):
        self.assertEqual(self.view(configured="inline"), "inline")
        self.assertEqual(self.view(ui="plain", configured="inline"), "plain")

    def test_json_is_plain(self):
        self.assertEqual(self.view(json=True), "plain")

    def test_the_config_file_takes_ui(self):
        import tempfile

        from flnd.options import config

        with tempfile.TemporaryDirectory() as folder:
            home = pathlib.Path(folder)
            (home / "config.toml").write_text('[defaults]\nui = "inline"\n')
            self.assertEqual(config(home).ui, "inline")
            (home / "config.toml").write_text('[defaults]\nui = "fancy"\n')
            with self.assertRaises(flnd.UsageError):
                config(home)


if __name__ == "__main__":
    unittest.main()
