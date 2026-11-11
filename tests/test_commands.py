"""
Tests for the flnd command line: download, auth set-cookie, prompt mode, and the cookie prompt.
"""

import io
import json
import os
import sys
import tempfile
import threading
import time
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

from flnd import cli as flnd_cli
from flnd.cookies import ask_for_cookies, parse_netscape_cookies
from shared import log, theme
from shared._theme import Screen

NETSCAPE = (
    "# Netscape HTTP Cookie File\n"
    ".instagram.com\tTRUE\t/\tTRUE\t1825861327\tsessionid\tins-session\n"
    ".instagram.com\tTRUE\t/\tTRUE\t1825861327\tds_user_id\t42\n"
    ".x.com\tTRUE\t/\tTRUE\t1825861327\tauth_token\ttwt-token\n"
    ".example.com\tTRUE\t/\tFALSE\t0\tother\tvalue\n"
)
JSON_COOKIES = json.dumps([
    {"domain": ".tiktok.com", "name": "sessionid", "value": "tik-session", "path": "/",
     "secure": True, "httpOnly": True, "expirationDate": 1825861327.5},
    {"domain": "www.facebook.com", "name": "c_user", "value": "7", "path": "/", "secure": False},
])


class Stdin(io.StringIO):
    def __init__(self, text="", tty=False):
        super().__init__(text)
        self.tty = tty
        self.buffer = io.BytesIO(text.encode())

    def isatty(self):
        return self.tty


class FakeSource:
    """Stands in for a real source, so no test opens a browser or the network."""

    calls = []
    fail_on = set()

    def download(self, target, download_dir, limit=None, headless=True):
        FakeSource.calls.append((str(target), download_dir, limit, headless))
        if target.value in FakeSource.fail_on:
            raise RuntimeError("blocked")
        return 2


class FlndCase(unittest.TestCase):
    """Runs in a temporary working directory with a fake, non-interactive screen."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.previous_cwd = os.getcwd()
        os.chdir(self.tmp.name)
        self.addCleanup(os.chdir, self.previous_cwd)
        self.screen = Screen(out=io.StringIO(), inp=io.StringIO(""), width=400, tty=False, interactive=False)
        scope = theme.scope(screen=self.screen, color="none", unicode=True)
        scope.__enter__()
        self.addCleanup(scope.__exit__, None, None, None)
        self.stdin = mock.patch("sys.stdin", Stdin(tty=True))
        self.stdin.start()
        self.addCleanup(lambda: self.stdin.stop())
        FakeSource.calls, FakeSource.fail_on = [], set()
        get_source = mock.patch.object(flnd_cli, "get_source", lambda code: FakeSource())
        get_source.start()
        self.addCleanup(get_source.stop)

    def pipe(self, text):
        self.stdin.stop()
        self.stdin = mock.patch("sys.stdin", Stdin(text))
        self.stdin.start()

    def keys(self, *names):
        self.screen._interactive = True
        self.screen.readkey = iter(names).__next__

    def run_app(self, *argv):
        stdout = io.StringIO()
        with redirect_stdout(stdout):
            code = flnd_cli.app.run(list(argv))
        return code, stdout.getvalue(), self.screen.out.getvalue()

    def cookie_file(self, text, name="cookies.txt"):
        path = Path(self.tmp.name) / name
        path.write_text(text, encoding="utf-8")
        return path


class DownloadTest(FlndCase):
    def test_targets_from_argv(self):
        code, _, out = self.run_app("download", "ins:example_user", "tik:@example_creator")
        self.assertEqual(code, 0)
        storage = (Path(self.tmp.name) / ".flnd/downloads").resolve()
        # Different sources run at the same time, so their calls may come in either order.
        self.assertCountEqual(FakeSource.calls, [
            ("ins:example_user", storage, None, False),
            ("tik:example_creator", storage, None, False),
        ])
        self.assertEqual(out.splitlines(), [
            f"• Storage: {storage}",
            "✓ download",
            "✓ ins:example_user",
            "✓ tik:example_creator",
            "✓ Complete. 4 files",
        ])

    def test_targets_from_stdin(self):
        self.pipe("ins:a\n\n# a comment\ntwt:b\n")
        self.assertEqual(self.run_app("download")[0], 0)
        self.assertCountEqual([call[0] for call in FakeSource.calls], ["ins:a", "twt:b"])

    def test_no_targets_is_a_usage_error(self):
        code, _, out = self.run_app("download")
        self.assertEqual(code, 2)
        self.assertIn("✗ the following arguments are required: targets", out)

    def test_removed_options_are_usage_errors(self):
        for option in ("--limit", "--output", "--headless"):
            self.assertEqual(self.run_app("download", option, "5", "ins:a")[0], 2)
        self.assertEqual(FakeSource.calls, [])

    def test_bad_target_exits_1_before_any_row(self):
        code, _, out = self.run_app("download", "ins:a", "ytb:nasa")
        self.assertEqual(code, 1)
        self.assertEqual(out, "✗ unknown source 'ytb'. Sources: tik, fbk, ins, twt\n")
        self.assertEqual(FakeSource.calls, [])

    def test_failed_target_ends_its_row_and_the_run_goes_on(self):
        FakeSource.fail_on = {"a"}
        code, _, out = self.run_app("download", "ins:a", "ins:b")
        self.assertEqual(code, 0)
        self.assertEqual(len(FakeSource.calls), 2)
        lines = out.splitlines()
        self.assertIn("✗ ins:a: blocked", lines)
        self.assertIn("✗ ins:a", lines)
        self.assertIn("✓ ins:b", lines)
        self.assertEqual(lines[-1], "✓ Complete. 2 files")

    def test_targets_run_in_parallel_one_per_source(self):
        lock = threading.Lock()
        running = {"ins": 0, "twt": 0, "tik": 0}
        peaks = {"ins": 0, "twt": 0, "tik": 0, "all": 0}

        def download(target, download_dir, limit=None, headless=True):
            with lock:
                running[target.source] += 1
                peaks[target.source] = max(peaks[target.source], running[target.source])
                peaks["all"] = max(peaks["all"], sum(running.values()))
            time.sleep(0.1)
            with lock:
                running[target.source] -= 1
            return 1

        with mock.patch.object(FakeSource, "download", staticmethod(download)):
            code, _, out = self.run_app("download", "ins:a", "ins:b", "twt:c", "ins:d", "tik:e")
        self.assertEqual(code, 0)
        self.assertEqual((peaks["ins"], peaks["twt"], peaks["tik"]), (1, 1, 1))
        self.assertGreater(peaks["all"], 1)
        self.assertEqual(out.splitlines()[-1], "✓ Complete. 5 files")

    def test_help_and_unknown_command(self):
        code, stdout, _ = self.run_app("--help")
        self.assertEqual(code, 0)
        self.assertIn("Dead simple media downloader for TikTok, Facebook, Instagram, and Twitter/X.", stdout)
        self.assertIn("Sources: tik, fbk, ins, twt", stdout)
        self.assertIn("cat list.txt | flnd download", stdout)
        self.assertEqual(self.run_app("no-such")[0], 2)

    def test_no_arguments_without_a_terminal_prints_help(self):
        self.pipe("")
        code, stdout, _ = self.run_app()
        self.assertEqual(code, 0)
        self.assertIn("Usage:\n  flnd [options] [command]", stdout)


class SetCookieTest(FlndCase):
    def saved(self, code):
        return parse_netscape_cookies(Path(".flnd/cookies") / f"{code}.txt")

    def test_path_with_several_sources_and_other_sites(self):
        path = self.cookie_file(NETSCAPE)
        code, _, out = self.run_app("auth", "set-cookie", "--path", str(path))
        self.assertEqual(code, 0)
        self.assertEqual(out.splitlines(), [
            "• skipped 1 cookies for other sites",
            "✓ ins: 2 cookies saved to .flnd/cookies/ins.txt",
            "✓ twt: 1 cookies saved to .flnd/cookies/twt.txt",
        ])
        self.assertEqual({c["name"]: c["value"] for c in self.saved("ins")}, {"sessionid": "ins-session", "ds_user_id": "42"})
        self.assertEqual(self.saved("twt")[0]["expires"], 1825861327)

    def test_content_as_json(self):
        code, _, out = self.run_app("auth", "set-cookie", "--content", JSON_COOKIES)
        self.assertEqual(code, 0)
        tik = self.saved("tik")[0]
        self.assertEqual((tik["domain"], tik["name"], tik["value"], tik["expires"], tik["secure"]),
                         (".tiktok.com", "sessionid", "tik-session", 1825861327, True))
        fbk = self.saved("fbk")[0]
        self.assertEqual((fbk["domain"], fbk["expires"], fbk["secure"]), ("www.facebook.com", 0, False))
        written = (Path(".flnd/cookies") / "fbk.txt").read_text(encoding="utf-8").splitlines()
        self.assertEqual(written[1].split("\t"), ["www.facebook.com", "FALSE", "/", "FALSE", "0", "c_user", "7"])

    def test_content_from_stdin(self):
        self.pipe(NETSCAPE)
        self.assertEqual(self.run_app("auth", "set-cookie")[0], 0)
        self.assertEqual(self.saved("twt")[0]["value"], "twt-token")

    def test_path_does_not_read_stdin(self):
        path = self.cookie_file(NETSCAPE)
        self.pipe("")
        sys.stdin.read = lambda *args: self.fail("read stdin although --path was given")
        self.assertEqual(self.run_app("auth", "set-cookie", "--path", str(path))[0], 0)

    def test_path_and_content_together(self):
        code, _, out = self.run_app("auth", "set-cookie", "--path", "x.txt", "--content", NETSCAPE)
        self.assertEqual(code, 2)
        self.assertIn("✗ give --path or --content, not both", out)

    def test_no_input(self):
        code, _, out = self.run_app("auth", "set-cookie")
        self.assertEqual(code, 2)
        self.assertIn("✗ give --path, --content, or pipe the cookie text in", out)

    def test_no_matching_domain_writes_nothing(self):
        path = self.cookie_file(".example.com\tTRUE\t/\tFALSE\t0\tother\tvalue\n")
        code, _, out = self.run_app("auth", "set-cookie", "--path", str(path))
        self.assertEqual(code, 1)
        self.assertEqual(out, "✗ no cookies for any source (tik, fbk, ins, twt)\n")
        self.assertFalse(Path(".flnd").exists())

    def test_no_cookies_and_missing_file(self):
        self.assertEqual(self.run_app("auth", "set-cookie", "--content", "# empty\n")[0], 1)
        self.assertIn("✗ no cookies found", self.screen.out.getvalue())
        self.assertEqual(self.run_app("auth", "set-cookie", "--path", "missing.txt")[0], 1)

    def test_old_json_is_removed(self):
        folder = Path(".flnd/cookies")
        folder.mkdir(parents=True)
        (folder / "ins.json").write_text("[]", encoding="utf-8")
        (folder / "twt.json").write_text("[]", encoding="utf-8")
        self.assertEqual(self.run_app("auth", "set-cookie", "--content", NETSCAPE)[0], 0)
        self.assertFalse((folder / "ins.json").exists())
        self.assertFalse((folder / "twt.json").exists())
        self.assertTrue((folder / "ins.txt").exists())


class PromptModeTest(FlndCase):
    def test_one_target(self):
        # Menu: Download; Source: Instagram (third); value; Add another? No.
        self.keys("enter", "down", "down", "enter", *"someone", "enter", "enter")
        code, _, out = self.run_app()
        self.assertEqual(code, 0)
        self.assertEqual([call[0] for call in FakeSource.calls], ["ins:someone"])
        self.assertIn("✓ What do you want to do?: Download each target", out)
        self.assertIn("✓ Source: Instagram", out)
        self.assertIn("✓ Username or id: someone", out)
        self.assertIn("✓ Add another target? No", out)
        self.assertIn("• flnd download ins:someone\n", out)

    def test_two_targets_and_empty_value(self):
        self.keys("enter", "enter", "enter", *"a", "enter", "y", "down", "down", "down", "enter", *"b", "enter", "n")
        self.assertEqual(self.run_app()[0], 0)
        # Each source runs in its own worker, so the two calls can come in either order.
        self.assertCountEqual([call[0] for call in FakeSource.calls], ["tik:a", "twt:b"])
        out = self.screen.out.getvalue()
        self.assertIn("✗ enter a username or id", out)
        self.assertIn("• flnd download tik:a twt:b\n", out)

    def test_set_cookie(self):
        path = self.cookie_file(NETSCAPE)
        self.keys("down", "enter", "enter", *str(path), "enter")
        self.assertEqual(self.run_app()[0], 0)
        out = self.screen.out.getvalue()
        self.assertIn(f"• flnd auth set-cookie --path {path}\n", out)
        self.assertIn("✓ ins: 2 cookies saved to .flnd/cookies/ins.txt", out)

    def test_cookie_path_is_checked(self):
        empty = self.cookie_file(".example.com\tTRUE\t/\tFALSE\t0\tother\tvalue\n", "other.txt")
        good = self.cookie_file(NETSCAPE)
        keys = ["down", "enter", "enter", *"nope", "enter"]
        keys += ["backspace"] * 4 + [*str(empty), "enter"]
        keys += ["backspace"] * len(str(empty)) + [*str(good), "enter"]
        self.keys(*keys)
        self.assertEqual(self.run_app()[0], 0)
        out = self.screen.out.getvalue()
        self.assertIn("✗ no file at nope", out)
        self.assertIn("✗ no cookies for any source (tik, fbk, ins, twt)", out)


class AskForCookiesTest(FlndCase):
    def test_not_interactive(self):
        self.assertIsNone(ask_for_cookies("ins"))
        self.assertEqual(self.screen.out.getvalue().splitlines(), [
            "✗ No cookies for Instagram",
            "• Put a Netscape cookies.txt at .flnd/cookies/ins.txt, or run: flnd auth set-cookie --path FILE",
        ])

    def test_interactive_stores_the_file(self):
        path = self.cookie_file(NETSCAPE)
        self.keys("enter", *str(path), "enter")
        self.assertEqual(ask_for_cookies("twt"), Path(".flnd/cookies/twt.txt"))
        out = self.screen.out.getvalue()
        self.assertIn("✓ No cookies for Twitter/X. Set a cookie file now? Yes", out)
        self.assertIn("✓ twt: 1 cookies saved to .flnd/cookies/twt.txt", out)

    def test_interactive_file_without_this_source(self):
        path = self.cookie_file(NETSCAPE)
        self.keys("enter", *str(path), "enter")
        self.assertIsNone(ask_for_cookies("fbk"))
        self.assertIn("! That file has no cookies for Facebook", self.screen.out.getvalue())
        self.assertTrue(Path(".flnd/cookies/ins.txt").exists())

    def test_interactive_declined(self):
        self.keys("n")
        self.assertIsNone(ask_for_cookies("ins"))
        self.assertFalse(Path(".flnd").exists())

    def test_asked_inside_a_download_block(self):
        path = self.cookie_file(NETSCAPE)
        self.keys("enter", *str(path), "enter")
        with log.multi("download") as jobs:
            row = jobs.add("ins:a")
            self.assertEqual(ask_for_cookies("ins"), Path(".flnd/cookies/ins.txt"))
            row.finish()
        self.assertTrue(self.screen.out.getvalue().endswith("✓ download\n✓ ins:a\n"))


if __name__ == "__main__":
    unittest.main()
