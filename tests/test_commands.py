"""The flnd command line: download, login and logout. No network, no browser."""

import io
import json
import os
import stat
import subprocess
import tempfile
import unittest
from contextlib import redirect_stdout
from datetime import date
from pathlib import Path
from unittest import mock

from flnd import cli as flnd_cli
from flnd.engine import Engine
from flnd.session import Options
from flnd.sources.base import Source
from flnd.target import Target
from shared import cookies, theme
from shared._theme import Screen

NETSCAPE = (
    "# Netscape HTTP Cookie File\n"
    ".instagram.com\tTRUE\t/\tTRUE\t1825861327\tsessionid\tins-session\n"
    "www.instagram.com\tFALSE\t/\tTRUE\t1825861327\tds_user_id\t42\n"
    ".x.com\tTRUE\t/\tTRUE\t1825861327\tauth_token\ttwt-token\n"
    ".example.com\tTRUE\t/\tFALSE\t0\tother\tvalue\n"
)


class Stdin(io.StringIO):
    def __init__(self, text="", tty=False):
        super().__init__(text)
        self.tty = tty
        self.buffer = io.BytesIO(text.encode())

    def isatty(self):
        return self.tty


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

    def saved(self, code):
        return cookies.load_from_file(Path(".flnd/cookies") / f"{code}.txt")


class DownloadTest(FlndCase):
    def setUp(self):
        super().setUp()
        self.runs = []

        def run(engine, targets, options):
            self.runs.append((engine, targets, options))
            return 3

        patcher = mock.patch.object(Engine, "run", run)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_targets_and_options_reach_the_engine(self):
        code, _, out = self.run_app("download", "ins:example_user", "https://x.com/example_handle", "--from", "reels,media",
                                    "--from", "tagged", "--since", "2026-01-01", "--until", "2026-06-30", "--unpack",
                                    "--zip", "--locale", "fr-FR", "--timezone", "Europe/Paris")
        self.assertEqual(code, 0)
        engine, targets, options = self.runs[0]
        self.assertEqual(targets, [Target("ins", "user", "example_user"), Target("twt", "user", "example_handle")])
        self.assertEqual(options, Options(categories=("reels", "media", "tagged"), since=date(2026, 1, 1),
                                          until=date(2026, 6, 30), unpack=True, zip=True))
        self.assertEqual((engine.home, engine.locale, engine.timezone), (Path(".flnd"), "fr-FR", "Europe/Paris"))
        storage = (Path(self.tmp.name) / ".flnd/downloads").resolve()
        self.assertEqual(out.splitlines(), [f"• Storage: {storage}", "✓ Complete. 3 files"])

    def test_defaults(self):
        self.assertEqual(self.run_app("download", "tik:a")[0], 0)
        self.assertEqual(self.runs[0][2], Options())
        self.assertIsNone(Options().categories)
        self.assertEqual(self.run_app("download", "tik:a", "ins:b", "--from", "all")[0], 0)
        self.assertEqual(self.runs[1][2].categories, ("all",))

    def test_targets_from_stdin(self):
        self.pipe("ins:a\n\n# a comment\ntwt:b\n")
        self.assertEqual(self.run_app("download")[0], 0)
        self.assertEqual([str(target) for target in self.runs[0][1]], ["ins:a", "twt:b"])

    def test_no_targets_is_a_usage_error_and_nothing_is_asked(self):
        self.keys()
        code, _, out = self.run_app("download")
        self.assertEqual(code, 2)
        self.assertIn("✗ the following arguments are required: targets", out)

    def test_bad_targets_are_usage_errors_before_anything_starts(self):
        for target, message in (("ytb:nasa", "✗ unknown source 'ytb'. Sources: tik, fbk, ins, twt"),
                                ("example_user", '✗ which source is "example_user"? Write it as tik:example_user'),
                                ("https://www.facebook.com/example.page/posts/1",
                                 "✗ fbk: single posts and videos aren't supported yet")):
            code, _, out = self.run_app("download", "ins:a", target)
            self.assertEqual(code, 2, target)
            self.assertIn(message, out)
        self.assertEqual(self.runs, [])
        self.assertFalse(Path(".flnd").exists())

    def test_bad_options(self):
        message = ("✗ no source here has 'reels': use all, or tik: videos, stories, playlists, reposts, liked, "
                   "favorites, profile; twt: posts, media, highlights, reposts, replies, profile")
        self.assertIn(message, self.run_app("download", "tik:a", "twt:b", "--from", "videos,reels")[2])
        code, _, out = self.run_app("download", "tik:a", "--since", "yesterday")
        self.assertEqual(code, 2)
        self.assertIn("✗ argument --since: invalid date: 'yesterday' (use YYYY-MM-DD)", out)
        self.assertIn("✗ --since is after --until", self.run_app("download", "tik:a", "--since", "2026-02-01",
                                                                  "--until", "2026-01-01")[2])
        for option in ("--limit", "--output", "--headless"):
            self.assertEqual(self.run_app("download", option, "5", "ins:a")[0], 2)
        self.assertEqual(self.runs, [])

    def test_help(self):
        code, stdout, _ = self.run_app("--help")
        self.assertEqual(code, 0)
        self.assertIn("Dead simple media downloader for TikTok, Facebook, Instagram, Twitter/X, LOIBUS, and Telegram.", stdout)
        self.assertIn("Sources: tik, fbk, ins, twt, loi", stdout)
        self.assertIn("https://www.facebook.com/profile.php?id=100012345678901", stdout)
        for command in ("download", "login", "logout", "install-browser"):
            self.assertIn(command, stdout)
        self.assertNotIn("auth", stdout)
        self.assertEqual(self.run_app("auth", "set-cookie")[0], 2)
        download_help = self.run_app("download", "--help")[1]
        for option in ("--from", "--since", "--until", "--[no-]unpack", "--[no-]zip", "--locale", "--timezone"):
            self.assertIn(option, download_help)
        self.assertIn("match where your connection comes from", download_help)
        self.assertIn("  --from CATEGORIES  ", download_help)
        self.assertIn("  --since DATE  ", download_help)
        self.assertNotIn("--categories", download_help)
        self.assertEqual(self.run_app("download", "tik:a", "--categories", "posts")[0], 2)

    def test_bare_flnd_prints_help_even_on_a_terminal(self):
        self.keys()
        code, stdout, _ = self.run_app()
        self.assertEqual(code, 0)
        self.assertIn("Usage:\n  flnd [options] [command]", stdout)


class LoginFileTest(FlndCase):
    def test_a_full_export_keeps_only_the_source(self):
        path = self.cookie_file(NETSCAPE)
        code, _, out = self.run_app("login", "ins", "--method", "file", "--path", str(path))
        self.assertEqual(code, 0)
        self.assertEqual(out, "✓ ins: logged in from cookies.txt (2 cookies)\n")
        saved = self.saved("ins")
        self.assertEqual({cookie["name"]: cookie["domain"] for cookie in saved},
                         {"sessionid": ".instagram.com", "ds_user_id": ".www.instagram.com"})
        self.assertEqual(stat.S_IMODE(Path(".flnd/cookies/ins.txt").stat().st_mode), 0o600)

    def test_json_export_and_old_json_removed(self):
        Path(".flnd/cookies").mkdir(parents=True)
        Path(".flnd/cookies/tik.json").write_text("[]")
        path = self.cookie_file(json.dumps([{"domain": ".tiktok.com", "name": "sid_tt", "value": "v"}]), "c.json")
        self.assertEqual(self.run_app("login", "tik", "--method", "file", "--path", str(path))[0], 0)
        self.assertEqual(self.saved("tik")[0]["value"], "v")
        self.assertFalse(Path(".flnd/cookies/tik.json").exists())

    def test_a_missing_needs_cookie(self):
        path = self.cookie_file(NETSCAPE)
        code, _, out = self.run_app("login", "fbk", "--method", "file", "--path", str(path))
        self.assertEqual(code, 1)
        self.assertEqual(out, "✗ fbk: no c_user or xs for facebook.com in cookies.txt; log in there first\n")
        self.assertFalse(Path(".flnd/cookies/fbk.txt").exists())

    def test_a_broken_or_missing_file(self):
        self.assertEqual(self.run_app("login", "ins", "--method", "file", "--path", "missing.txt")[0], 1)
        broken = self.cookie_file("not\ta cookie\n")
        code, _, out = self.run_app("login", "ins", "--method", "file", "--path", str(broken))
        self.assertEqual(code, 1)
        self.assertIn("✗ ins: can't read", out)

    def test_the_path_is_asked_only_when_missing(self):
        path = self.cookie_file(NETSCAPE)
        self.keys(*"nope", "enter", *["backspace"] * 4, *str(path), "enter")
        self.assertEqual(self.run_app("login", "twt", "--method", "file")[0], 0)
        out = self.screen.out.getvalue()
        self.assertIn("┃ * no file at nope", out)
        self.assertIn("✓ twt: logged in from cookies.txt (1 cookies)", out)

    def test_without_a_terminal_a_missing_answer_is_a_usage_error(self):
        code, _, out = self.run_app("login", "ins")
        self.assertEqual(code, 2)
        self.assertIn("✗ give --method: browser, file", out)
        self.assertIn("✗ give --path with the cookie file", self.run_app("login", "ins", "--method", "file")[2])
        self.assertIn("✗ give --browser: chrome, firefox", self.run_app("login", "ins", "--method", "browser")[2])

    def test_unknown_method_and_source(self):
        code, _, out = self.run_app("login", "ins", "--method", "qr")
        self.assertEqual(code, 2)
        self.assertIn("✗ ins has no login method 'qr': use browser, file", out)
        self.assertEqual(self.run_app("login", "ytb")[0], 2)


class LoginBrowserTest(FlndCase):
    def setUp(self):
        super().setUp()
        self.reads = []
        self.store = {"x.com": [{"name": "auth_token", "value": "t", "domain": "x.com", "path": "/", "expires": -1,
                                 "secure": True, "httpOnly": True, "sameSite": "Lax"}],
                      "twitter.com": [{"name": "guest_id", "value": "g", "domain": ".twitter.com", "path": "/",
                                       "expires": -1, "secure": True, "httpOnly": False, "sameSite": "Lax"}]}

        def load_from_browser(domain, *, browser="chrome", profile=None):
            self.reads.append((domain, browser, profile))
            if browser == "edge":
                raise RuntimeError("can't read edge cookies on Windows")
            return self.store.get(domain, [])

        patcher = mock.patch("shared._cookies.load_from_browser", load_from_browser)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_every_domain_is_read(self):
        code, _, out = self.run_app("login", "twt", "--method", "browser", "--browser", "firefox", "--profile", "work")
        self.assertEqual(code, 0)
        self.assertEqual(out, "✓ twt: logged in from Firefox (2 cookies)\n")
        self.assertEqual(self.reads, [("x.com", "firefox", "work"), ("twitter.com", "firefox", "work")])
        self.assertEqual([cookie["domain"] for cookie in self.saved("twt")], [".x.com", ".twitter.com"])

    def test_a_missing_needs_cookie_and_an_unreadable_browser(self):
        self.store = {}
        code, _, out = self.run_app("login", "twt", "--method", "browser", "--browser", "chrome")
        self.assertEqual((code, out), (1, "✗ twt: no auth_token for x.com in Chrome; log in there first\n"))
        code, _, out = self.run_app("login", "twt", "--method", "browser", "--browser", "edge")
        self.assertEqual(code, 1)
        self.assertIn("✗ twt: can't read Edge's cookies: can't read edge cookies on Windows; try --method file", out)

    def test_method_and_browser_are_asked_when_missing(self):
        # Method: the first (browser); browser: the second (Firefox).
        self.keys("enter", "down", "enter")
        self.assertEqual(self.run_app("login", "twt")[0], 0)
        out = self.screen.out.getvalue()
        self.assertIn("✓ Log in to Twitter/X: From your browser", out)
        self.assertIn("✓ Read cookies from: Firefox", out)
        self.assertEqual(self.reads[0][1], "firefox")


class OwnLoginTest(FlndCase):
    def test_a_source_with_its_own_login_and_one_method_is_not_asked(self):
        calls = []

        class Own(Source):
            code, name, domains = "own", "Own", ("own.example",)
            login_methods = ("qr",)

            def resolve(self, text):
                return Target("own", "chat", text)

            def run(self, session):
                pass

            def login(self, session, method):
                calls.append((method, session.target.kind))

        with mock.patch.dict(flnd_cli.SOURCES, {"own": Own}):
            self.assertEqual(flnd_cli.login_command("own"), 0)
        self.assertEqual(calls, [("qr", "account")])


class LogoutTest(FlndCase):
    def log_in(self, *codes):
        for code in codes:
            cookies.to_file([{"name": "a", "value": "1", "domain": ".x.example"}], Path(".flnd/cookies") / f"{code}.txt")

    def test_one_source(self):
        self.log_in("ins", "twt")
        from flnd import vault
        with vault.open(Path(".flnd"), "ins") as secrets:
            secrets["token"] = "t"
        code, _, out = self.run_app("logout", "ins")
        self.assertEqual((code, out), (0, "✓ ins: logged out\n"))
        self.assertFalse(Path(".flnd/cookies/ins.txt").exists())
        self.assertTrue(Path(".flnd/cookies/twt.txt").exists())
        with vault.open(Path(".flnd"), "ins") as secrets:
            self.assertEqual(len(secrets), 0)
        self.assertEqual(self.run_app("logout", "ins")[2].splitlines()[-1], "• ins: was not logged in")

    def test_all(self):
        self.log_in("ins", "tik")
        code, _, out = self.run_app("logout", "--all")
        self.assertEqual((code, out), (0, "✓ tik: logged out\n✓ ins: logged out\n"))
        self.assertEqual(list(Path(".flnd/cookies").iterdir()), [])

    def test_a_source_or_all(self):
        self.assertIn("✗ give a source, or --all", self.run_app("logout")[2])
        self.assertIn("✗ give a source or --all, not both", self.run_app("logout", "ins", "--all")[2])


class InstallBrowserTest(FlndCase):
    """Playwright's installer is mocked: nothing is downloaded."""

    def install(self, returncode=0, error=None):
        calls = []

        def run(argv, **options):
            calls.append((argv, options))
            if error is not None:
                raise error
            return subprocess.CompletedProcess(argv, returncode)

        with mock.patch.object(flnd_cli.subprocess, "run", run):
            code, _, out = self.run_app("install-browser")
        return code, out, calls

    def test_runs_playwrights_own_installer_for_chromium(self):
        code, out, calls = self.install()
        self.assertEqual((code, out), (0, "✓ Chromium is installed\n"))
        (argv, options), = calls
        self.assertTrue(Path(argv[0]).name.startswith("node"))
        self.assertEqual(Path(argv[1]).name, "cli.js")
        self.assertEqual(argv[2:], ["install", "chromium"])
        self.assertEqual(options["env"]["PW_LANG_NAME"], "python")
        # Into the user cache: the installer's output folder is left to Playwright.
        self.assertNotIn("PLAYWRIGHT_BROWSERS_PATH", {key: value for key, value in options["env"].items()
                                                      if key not in os.environ})

    def test_a_failed_install(self):
        code, out, _ = self.install(returncode=1)
        self.assertEqual((code, out), (1, "✗ Chromium was not installed: Playwright's installer exited with 1\n"))
        code, out, _ = self.install(error=OSError("Permission denied"))
        self.assertEqual(code, 1)
        self.assertIn("✗ Chromium was not installed: Permission denied", out)

    def test_help(self):
        self.assertIn("Chromium", self.run_app("install-browser", "--help")[1])


if __name__ == "__main__":
    unittest.main()
