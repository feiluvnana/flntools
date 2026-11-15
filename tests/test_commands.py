"""The flnd command line: download (its options and saved settings), sync, login and logout. No network, no browser."""

import io
import json
import os
import stat
import tempfile
import unittest
from dataclasses import replace
from contextlib import redirect_stderr, redirect_stdout
from datetime import date
from pathlib import Path
from typing import Any
from unittest import mock

from test_engine import make_source

from flnd import Engine, Target, TargetReport
from flnd import cli as flnd_cli
from flnd import sources
from flnd.common import auth, paths
from flnd.options import Options
from flnd.source import Source
from shared import cookies, store, ui

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
        self._buffer = io.BytesIO(text.encode())

    @property
    def buffer(self):
        return self._buffer

    def isatty(self):
        return self.tty


class FlndCase(unittest.TestCase):
    """Runs in a temporary working directory with a fake, non-interactive stream."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.previous_cwd = os.getcwd()
        os.chdir(self.tmp.name)
        self.addCleanup(os.chdir, self.previous_cwd)
        self.dest = io.StringIO()
        scope = ui.scope(dest=self.dest, width=400, color=False)
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
        pass

    def run_app(self, *argv):
        stdout = io.StringIO()
        with redirect_stdout(stdout), redirect_stderr(self.dest):
            code = flnd_cli.app.run(list(argv))
        return code, stdout.getvalue(), self.dest.getvalue()

    def cookie_file(self, text, name="cookies.txt"):
        path = Path(self.tmp.name) / name
        path.write_text(text, encoding="utf-8")
        return path

    def saved(self, code):
        """The cookies the vault holds for a source; no plain cookie file is ever written."""
        self.assertFalse(Path(".flnd/cookies").exists())
        return auth.load(Path(".flnd"), code).raw


class RunsCase(FlndCase):
    """`Engine._run` is replaced: each call goes in `runs`, with its first target's options; 3 paths."""

    def setUp(self):
        super().setUp()
        self.runs = []

        def run(engine, chosen, *, stop=None, tallies=(), browser_pool=None):
            targets = [target for target, _ in chosen]
            self.runs.append((engine, targets, replace(chosen[0][1], stop_at_saved=False)))
            written = tuple(Path(f"{name}.jpg") for name in "abc")
            return [TargetReport(target, written if not number else (), 0)
                    for number, target in enumerate(targets)]

        patcher = mock.patch.object(Engine, "_run", run)
        patcher.start()
        self.addCleanup(patcher.stop)


class DownloadTest(RunsCase):
    def test_targets_and_options_reach_the_engine(self):
        code, _, out = self.run_app("download", "ins:example_user", "https://x.com/example_handle", "--from", "reels,media",
                                    "--from", "tagged", "--time", "2026-01-01,2026-06-30")
        self.assertEqual(code, 0)
        engine, targets, options = self.runs[0]
        self.assertEqual(targets, [Target("ins", "user", "example_user"), Target("twt", "user", "example_handle")])
        self.assertEqual(options, Options(categories=("reels", "media", "tagged"), since=date(2026, 1, 1),
                                          until=date(2026, 6, 30)))
        self.assertEqual(engine.home, Path(".flnd"))
        storage = (Path(self.tmp.name) / ".flnd" / paths.MEDIA).resolve()
        self.assertEqual(out.splitlines(),
                         [f"* Storage: {storage}", "* not synced; add --sync to keep it up to date", "+ Complete. 3 files"])

    def test_defaults(self):
        code, _, out = self.run_app("download", "tik:a")
        self.assertEqual(code, 2)
        self.assertIn("--from is required", out)
        self.assertEqual(self.run_app("download", "tik:a", "ins:b", "--from", "all")[0], 0)
        self.assertEqual(self.runs[0][2].categories, ("all",))
        self.assertFalse(Options().again)
        self.assertIsNone(Options().categories)
        self.assertEqual(self.run_app("download", "tik:a", "--from", "all", "--again")[0], 0)
        self.assertEqual(self.runs[1][2], Options(categories=("all",), again=True))

    def test_from_with_a_single_post_is_a_usage_error(self):
        message = ("✗ --from applies to profiles and chats; ins:C1abcDEFghi is one post, downloaded whole: "
                   "give it without --from")
        for targets in (("https://www.instagram.com/p/C1abcDEFghi/",),
                        ("ins:example_user", "https://www.instagram.com/p/C1abcDEFghi/")):
            with self.subTest(targets=targets):
                code, _, out = self.run_app("download", *targets, "--from", "tagged")
                self.assertEqual(code, 2)
                self.assertIn(message, out)
        for target in ("https://x.com/example_handle/status/1790000000000000000",
                       "https://www.tiktok.com/@example_creator/video/7301234567890123456"):
            with self.subTest(target=target):
                self.assertEqual(self.run_app("download", target, "--from", "posts,videos")[0], 2)
        self.assertEqual(self.runs, [])
        # Without --from the same link downloads whole; a loi post has categories and takes --from.
        self.assertEqual(self.run_app("download", "https://www.instagram.com/p/C1abcDEFghi/")[0], 0)
        self.assertEqual(self.run_app("download", "loi:5930", "--from", "photo")[0], 0)

    def test_targets_from_stdin(self):
        self.pipe("ins:a\n\n# a comment\ntwt:b\n")
        self.assertEqual(self.run_app("download", "--from", "all")[0], 0)
        self.assertEqual([str(target) for target in self.runs[0][1]], ["ins:a", "twt:b"])

    def test_no_targets_is_a_usage_error_and_nothing_is_asked(self):
        self.keys()
        code, _, out = self.run_app("download")
        self.assertEqual(code, 2)
        self.assertIn("give at least one target", out)

    def test_bad_targets_are_usage_errors_before_anything_starts(self):
        for target, message in (("ytb:nasa", "unknown source 'ytb'. Sources: tik, fbk, ins, twt"),
                                ("example_user", 'which source is "example_user"? Write it as tik:example_user'),
                                ("https://www.facebook.com/example.page/posts/1",
                                 "fbk: single posts and videos aren't supported yet")):
            code, _, out = self.run_app("download", "ins:a", target)
            self.assertEqual(code, 2, target)
            self.assertIn(message, out)
        self.assertEqual(self.runs, [])
        self.assertFalse((Path(".flnd") / paths.MEDIA).exists())
        self.assertFalse((Path(".flnd") / paths.METADATA).exists())

    def test_bad_options(self):
        message = ("no source here has 'reels': use all, or tik: videos, stories, playlists, reposts, liked, "
                   "favorites, profile; twt: posts, media, highlights, reposts, replies, profile")
        self.assertIn(message, self.run_app("download", "tik:a", "twt:b", "--from", "videos,reels")[2])
        for option in ("--output", "--headless"):
            self.assertEqual(self.run_app("download", option, "5", "ins:a")[0], 2)
        self.assertEqual(self.runs, [])

    def test_removed_options_are_unknown(self):
        for args in (("--unpack",), ("--zip",), ("--locale", "fr-FR"), ("--timezone", "Europe/Paris"),
                     ("--since", "2026-01-01"), ("--until", "2026-01-01"), ("--compress-image", "small"),
                     ("--format-video", "h265")):
            code, _, out = self.run_app("download", "ins:a", *args)
            self.assertEqual(code, 2, args)
            self.assertIn("Unknown option:", out)
        self.assertEqual(self.runs, [])

    def test_help(self):
        code, stdout, _ = self.run_app("--help")
        self.assertEqual(code, 0)
        self.assertIn("Dead simple media downloader for TikTok, Facebook, Instagram, Twitter/X, LOIBUS, Telegram, "
                      "and mimihentai.", stdout)
        self.assertIn("Sources: tik, fbk, ins, twt, loi, tlg, mht", stdout)
        self.assertIn("https://www.facebook.com/profile.php?id=", stdout)
        for command in ("download", "sync", "auth"):
            self.assertIn(command, stdout)
        self.assertNotIn("  login ", stdout)
        self.assertNotIn("  logout ", stdout)
        self.assertNotIn("install-browser", stdout)
        self.assertNotIn("--compress", stdout)
        self.assertEqual(self.run_app("auth", "set-cookie")[0], 2)
        self.assertEqual(self.run_app("login")[0], 2)
        self.assertEqual(self.run_app("logout", "all")[0], 2)
        download_help = self.run_app("download", "--help")[1]
        for option in ("--from", "--time", "--sync", "--again"):
            self.assertIn(option, download_help)
        for gone in ("--since", "--until", "unpack", "--zip", "--locale", "--timezone", "--compress", "--format-"):
            self.assertNotIn(gone, download_help)
        self.assertIn("2026-01-01,2026-06-30", download_help)
        self.assertNotIn("--categories", download_help)
        self.assertEqual(self.run_app("download", "tik:a", "--categories", "posts")[0], 2)

    def test_bare_flnd_prints_help_even_on_a_terminal(self):
        self.keys()
        code, stdout, _ = self.run_app()
        self.assertEqual(code, 0)
        self.assertIn("Usage: flnd COMMAND", stdout)


class TimeTest(RunsCase):
    def test_either_side_may_be_open(self):
        for value, since, until in (("2026-01-01,", date(2026, 1, 1), None), ("*,2026-06-30", None, date(2026, 6, 30)),
                                    (",2026-06-30", None, date(2026, 6, 30)), ("2026-01-01,*", date(2026, 1, 1), None),
                                    ("2026-01-01,2026-06-30", date(2026, 1, 1), date(2026, 6, 30)),
                                    ("2026-03-01,2026-03-01", date(2026, 3, 1), date(2026, 3, 1))):
            with self.subTest(value=value):
                self.runs.clear()
                self.assertEqual(self.run_app("download", "tik:a", "--from", "all", "--time", value)[0], 0)
                options = self.runs[0][2]
                self.assertEqual((options.since, options.until), (since, until))

    def test_bad_values_are_usage_errors(self):
        message = ("--time takes <since>,<until>, such as 2026-01-01,2026-06-30; use * or leave a side empty "
                   "for no limit")
        for value in ("2026-01-01", "2026-13-01,", "2026-06-30,2026-01-01", "yesterday,", "2026-01-01,2026-02-01,"):
            with self.subTest(value=value):
                code, _, out = self.run_app("download", "tik:a", "--time", value)
                self.assertEqual(code, 2)
                self.assertIn(message, out)
        self.assertEqual(self.runs, [])
        self.assertFalse((Path(".flnd") / paths.TARGETS).exists())


class SyncCommandTest(RunsCase):
    def setUp(self):
        super().setUp()
        self.synced = []
        self.levels = []
        real_plan = Engine._plan

        def plan(engine, targets):
            self.levels.append(ui.level)
            chosen = real_plan(engine, targets)
            self.synced.append(chosen)
            return chosen

        patcher = mock.patch.object(Engine, "_plan", plan)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_from_and_time_are_not_saved(self):
        self.run_app("download", "ins:example_user", "--from", "reels", "--time", "2026-01-01,")
        self.run_app("download", "ins:example_user", "--from", "all")
        self.assertEqual(self.runs[1][2], Options(categories=("all",)))

    def test_every_recorded_target_with_its_saved_settings(self):
        self.run_app("download", "ins:example_user", "--from", "reels", "--sync")
        self.run_app("download", "twt:example_handle", "--from", "all", "--sync")
        code, _, out = self.run_app("sync")
        self.assertEqual(code, 0, out)
        self.assertEqual(dict(self.synced[0]), {Target("ins", "user", "example_user"): Options(),
                                                Target("twt", "user", "example_handle"): Options()})
        self.run_app("sync", "twt:example_handle")
        self.assertEqual(self.synced[1], [(Target("twt", "user", "example_handle"), Options())])

    def test_sync_v_shows_every_line_and_changes_no_setting(self):
        self.run_app("download", "ins:example_user", "--from", "all", "--sync")
        self.assertEqual(ui.level, "info")
        self.assertEqual(self.run_app("sync", "-v")[0], 0)
        self.assertEqual(self.levels[-1], "debug")
        self.assertEqual(ui.level, "info")
        self.assertEqual(dict(self.synced[0]),
                         {Target("ins", "user", "example_user"): Options()})

    def test_sync_takes_no_options(self):
        self.run_app("download", "ins:example_user", "--from", "all", "--sync")
        for args in (("--only", "info"), ("--from", "reels"), ("--time", "2026-01-01,"), ("--shrink", "on"),
                     ("--compress-video", "small"), ("--since", "2026-01-01")):
            code, _, out = self.run_app("sync", *args)
            self.assertEqual(code, 2, args)
            self.assertIn("Unknown option:", out)
        self.assertEqual(self.synced, [])

    def test_target_ls_sync_on_off_and_del(self):
        self.run_app("download", "ins:example_user", "--from", "all")
        code, _, out = self.run_app("target", "ls")
        self.assertEqual(code, 0)
        self.assertIn("ins:example_user", out)
        self.assertIn("off", out)
        self.assertEqual(self.run_app("target", "ls", "extra")[0], 2)
        code, _, out = self.run_app("target", "sync-on", "ins:example_user")
        self.assertEqual(code, 0)
        self.assertIn("+ ins:example_user: sync on", out)
        code, _, out = self.run_app("target", "sync-off", "ins:example_user")
        self.assertEqual(code, 0)
        self.assertIn("+ ins:example_user: sync off", out)
        code, _, out = self.run_app("target", "del", "ins:example_user")
        self.assertEqual(code, 0)
        self.assertIn("+ ins:example_user: removed", out)
        code, _, out = self.run_app("target", "sync-on", "ins:unknown")
        self.assertEqual(code, 2)
        self.assertIn("not a known target", out)

    def test_per_source_from(self):
        code, _, _ = self.run_app(
            "download", "mht:70982", "ins:example_user",
            "--from", "mht:cover", "--from", "ins:reels,posts",
        )
        self.assertEqual(code, 0)
        code, _, out = self.run_app(
            "download", "ins:example_user", "--from", "nope",
        )
        self.assertEqual(code, 2)
        self.assertIn("no source here has 'nope'", out)

    def test_a_short_link_is_resolved_once(self):
        # Fix 11: cli.sync and sync.plan each resolved the targets, so a short link made two HEAD requests.
        followed = []

        def follow(url):
            followed.append(url)
            return "https://www.tiktok.com/@example_creator/video/7301234567890123456"

        with mock.patch("flnd.common.net.follow", follow):
            self.assertEqual(self.run_app("sync", "https://vm.tiktok.com/ZMabc123/")[0], 0)
        self.assertEqual(followed, ["https://vm.tiktok.com/ZMabc123/"])
        self.assertEqual(self.synced[0][0][0], Target("tik", "video", "7301234567890123456"))


class ExitStatusTest(FlndCase):
    """Fix 4 and 5: download and sync exit 1 when a target failed, so a script or cron job can tell."""

    def setUp(self):
        super().setUp()

        def connect(self, site):
            raise RuntimeError("the saved login no longer works")

        def run(site):
            if site.target.value == "bad":
                raise RuntimeError("blocked")

        patcher = mock.patch.dict(sources.SOURCES, {"aaa": make_source("aaa", run),
                                                    "bbb": make_source("bbb", run, connect=connect)})
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_download_exits_1_when_a_target_failed(self):
        code, _, out = self.run_app("download", "aaa:bad", "aaa:good", "--from", "all")
        self.assertEqual(code, 1, out)
        self.assertIn("x aaa:bad: blocked", out)
        self.assertEqual(self.run_app("download", "aaa:good", "--from", "all")[0], 0)

    def test_download_exits_1_when_a_source_cant_start(self):
        code, _, out = self.run_app("download", "bbb:good", "--from", "all")
        self.assertEqual(code, 1, out)
        self.assertNotIn("✓ bbb:good", out)

    def test_sync_exits_1_when_a_target_failed(self):
        self.run_app("download", "aaa:bad", "aaa:good", "--from", "all", "--sync")
        code, _, out = self.run_app("sync")
        self.assertEqual(code, 1, out)
        self.assertEqual(self.run_app("sync", "aaa:good")[0], 0)


class LoginCase(FlndCase):
    """`flnd auth login` in a fake terminal: mocks `prompt.choose` and `prompt.ask`."""

    def login(self, source, method=None, *, browser=None, paths=()):
        choices = [source]
        if method is not None:
            choices.append(method)
        if browser is not None:
            choices.append(browser)
        choose_iter = iter(choices)
        ask_iter = iter(paths)
        self.seen_choices: list[tuple[str, Any]] = []

        def fake_choose(message, options, **kwargs):
            self.seen_choices.append((message, options))
            return next(choose_iter)

        def fake_ask(message, *, validate=None, **kwargs):
            while True:
                value = next(ask_iter)
                if validate is None:
                    return value
                err = validate(value)
                if err is True or err is None:
                    return value
                ui.warn(str(err))

        with (
            mock.patch("shared.prompt.choose", fake_choose),
            mock.patch("shared.prompt.ask", fake_ask),
            mock.patch.object(type(ui.raw), "is_terminal", property(lambda _: True)),
        ):
            code, _, out = self.run_app("auth", "login")
        return code, out

    def from_file(self, source, path):
        return self.login(source, "file", paths=(str(path),))


class LoginFileTest(LoginCase):
    def test_a_full_export_keeps_only_the_source(self):
        code, out = self.from_file("ins", self.cookie_file(NETSCAPE))
        self.assertEqual(code, 0)
        self.assertTrue(out.splitlines()[-1].endswith("ins: logged in from cookies.txt (2 cookies)"), out)
        saved = self.saved("ins")
        self.assertEqual({cookie["name"]: cookie["domain"] for cookie in saved},
                         {"sessionid": ".instagram.com", "ds_user_id": ".www.instagram.com"})
        # Sealed in the vault, whose key is as private as the plain cookie files were; nothing else is written.
        self.assertEqual(sorted(path.name for path in Path(".flnd").rglob("*") if path.is_file()), ["secret.key", "secrets.db"])
        if os.name != "nt":
            self.assertEqual(stat.S_IMODE((Path(".flnd") / paths.KEY).stat().st_mode), 0o600)

    def test_json_export_and_old_json_removed(self):
        Path(".flnd/cookies").mkdir(parents=True)
        Path(".flnd/cookies/tik.json").write_text("[]")
        path = self.cookie_file(json.dumps([{"domain": ".tiktok.com", "name": "sid_tt", "value": "v"}]), "c.json")
        self.assertEqual(self.from_file("tik", path)[0], 0)
        self.assertEqual(self.saved("tik")[0]["value"], "v")
        self.assertFalse(Path(".flnd/cookies/tik.json").exists())

    def test_a_missing_needs_cookie(self):
        code, out = self.from_file("fbk", self.cookie_file(NETSCAPE))
        self.assertEqual(code, 1)
        self.assertTrue(out.splitlines()[-1].endswith("fbk: no c_user or xs for facebook.com in cookies.txt; log in there first"), out)
        self.assertNotIn("fbk", Engine(home=Path(".flnd")).logins)

    def test_a_broken_file(self):
        code, out = self.from_file("ins", self.cookie_file("not\ta cookie\n"))
        self.assertEqual(code, 1)
        self.assertIn("ins: can't read", out)

    def test_a_missing_path_is_asked_again(self):
        path = self.cookie_file(NETSCAPE)
        code, out = self.login("twt", "file", paths=("nope", str(path)))
        self.assertEqual(code, 0)
        self.assertIn("no file at nope", out)
        self.assertIn("twt: logged in from cookies.txt (1 cookie)", out)

    def test_logged_in_sources_are_marked(self):
        self.from_file("ins", self.cookie_file(NETSCAPE))
        self.dest.truncate(0)
        self.dest.seek(0)
        self.from_file("twt", self.cookie_file(NETSCAPE))
        labels = list(dict(self.seen_choices[0][1]).keys())
        self.assertIn("Instagram (ins) · logged in", labels)
        self.assertIn("TikTok (tik)", labels)
        self.assertNotIn("TikTok (tik) · logged in", labels)

    def test_an_api_key_alone_is_marked_apart_from_a_login(self):
        with auth.secrets(Path(".flnd"), "tlg") as secrets:
            secrets.update(api_id="1", api_hash="h")
        self.from_file("twt", self.cookie_file(NETSCAPE))
        labels = list(dict(self.seen_choices[0][1]).keys())
        self.assertIn("Telegram (tlg) · api key saved", labels)
        self.assertNotIn("Telegram (tlg) · logged in", labels)
        self.dest.truncate(0)
        self.dest.seek(0)
        with auth.secrets(Path(".flnd"), "tlg") as secrets:
            secrets["session"] = "s"
        self.from_file("twt", self.cookie_file(NETSCAPE))
        labels = list(dict(self.seen_choices[0][1]).keys())
        self.assertIn("Telegram (tlg) · logged in", labels)
        self.assertNotIn("Telegram (tlg) · api key saved", labels)


class LoginQuestionsTest(LoginCase):
    def test_without_a_terminal_it_exits_2(self):
        with mock.patch.object(type(ui.raw), "is_terminal", property(lambda _: False)):
            code, _, out = self.run_app("auth", "login")
        self.assertEqual(code, 2)
        self.assertIn("flnd auth login asks its questions in a terminal", out)
        self.assertFalse(Path(".flnd").exists())

    def test_it_takes_no_arguments(self):
        for argv in (["ins"], ["--method", "file"], ["--path", "c.txt"], ["--browser", "chrome"], ["--profile", "x"]):
            with self.subTest(argv=argv):
                self.assertEqual(self.run_app("auth", "login", *argv)[0], 2)


class LoginBrowserTest(LoginCase):
    def setUp(self):
        super().setUp()
        self.reads = []
        self.store = {"x.com": [{"name": "auth_token", "value": "t", "domain": "x.com", "path": "/", "expires": -1,
                                 "secure": True, "httpOnly": True, "sameSite": "Lax"}],
                      "twitter.com": [{"name": "guest_id", "value": "g", "domain": ".twitter.com", "path": "/",
                                       "expires": -1, "secure": True, "httpOnly": False, "sameSite": "Lax"}]}

        # The whole profile comes back, another site's cookie included; flnd keeps the source's own.
        def from_browser(browser="chrome", *, profile=None):
            self.reads.append((browser, profile))
            if browser == "edge":
                raise RuntimeError("can't read edge cookies on Windows")
            other = {"name": "auth_token", "value": "o", "domain": "example.com", "path": "/"}
            return cookies.Jar([*(cookie for jar in self.store.values() for cookie in jar), other])

        patcher = mock.patch("shared.cookies.load", from_browser)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_source_method_and_browser_are_asked_in_order(self):
        code, out = self.login("twt", "browser", browser="firefox")
        self.assertEqual(code, 0)
        self.assertIn("twt: logged in from Firefox (2 cookies)", out)
        # One read of the browser for both of the source's domains.
        self.assertEqual(self.reads, [("firefox", None)])
        self.assertEqual([cookie["domain"] for cookie in self.saved("twt")], [".x.com", ".twitter.com"])

    def test_a_missing_needs_cookie_and_an_unreadable_browser(self):
        self.store = {}
        code, out = self.login("twt", "browser", browser="chrome")
        self.assertEqual(code, 1)
        self.assertTrue(out.splitlines()[-1].endswith("twt: no auth_token for x.com in Chrome; log in there first"), out)
        code, out = self.login("twt", "browser", browser="edge")
        self.assertEqual(code, 1)
        self.assertIn("twt: can't read Edge's cookies: can't read edge cookies on Windows; try a cookie file instead",
                      out)


class OwnLoginTest(LoginCase):
    def test_a_source_with_its_own_login_and_one_method_is_not_asked_the_method(self):
        calls = []

        class Own(Source):
            code, name, domains = "own", "Own", ("own.example",)
            login_methods = ("qr",)

            def login(self, login, method):
                calls.append((method, type(login).__name__))

        with mock.patch.dict(flnd_cli.SOURCES, {"own": Own}):
            code, out = self.login("own")
        self.assertEqual(code, 0)
        self.assertEqual(calls, [("qr", "LoginSite")])
        self.assertNotIn("Log in to Own:", out)


class LogoutTest(FlndCase):
    def log_in(self, *codes):
        for code in codes:
            auth.save(Path(".flnd"), code, cookies.Jar([{"name": "a", "value": "1", "domain": ".x.example"}]))

    def test_named_sources(self):
        self.log_in("ins", "twt")
        with auth.secrets(Path(".flnd"), "ins") as secrets:
            secrets["token"] = "t"
        code, _, out = self.run_app("auth", "logout", "ins")
        self.assertEqual((code, out), (0, "+ ins: logged out\n"))
        self.assertEqual(auth.load(Path(".flnd"), "ins"), cookies.Jar())
        self.assertTrue(auth.logged_in(Path(".flnd"), "twt"))
        with auth.secrets(Path(".flnd"), "ins") as secrets:
            self.assertEqual(len(secrets), 0)
        self.dest.truncate(0)
        self.dest.seek(0)
        code, _, out = self.run_app("auth", "logout", "ins", "twt", "tik")
        self.assertEqual(code, 0)
        self.assertIn("ins: not logged in", out)
        self.assertIn("twt: logged out", out)
        self.assertIn("tik: not logged in", out)

    def test_all(self):
        self.log_in("ins", "tik")
        with auth.secrets(Path(".flnd"), "tlg") as secrets:
            secrets["session"] = "s"
        code, _, out = self.run_app("auth", "logout", "all")
        self.assertEqual(code, 0)
        for name in ("tik: logged out", "ins: logged out", "tlg: logged out"):
            self.assertIn(name, out)
        self.assertFalse(Path(".flnd/cookies").exists())
        self.assertFalse(any(auth.logged_in(Path(".flnd"), code) for code in sources.SOURCES))

    def test_an_api_key_alone_is_removed_but_is_not_a_login(self):
        with auth.secrets(Path(".flnd"), "tlg") as secrets:
            secrets.update(api_id="1", api_hash="h")
        self.assertIn("no source is logged in", self.run_app("auth", "logout")[2])
        self.dest.truncate(0)
        self.dest.seek(0)
        code, _, out = self.run_app("auth", "logout", "tlg")
        self.assertEqual(code, 0)
        self.assertIn("tlg: logged out", out)
        self.assertFalse(auth.api_key_saved(Path(".flnd"), "tlg"))

    def test_bare_is_a_usage_error_listing_the_logged_in_sources(self):
        code, _, out = self.run_app("auth", "logout")
        self.assertEqual(code, 2)
        self.assertIn("✗ give the sources to log out of, or all; no source is logged in", out)
        self.log_in("twt", "ins")
        self.assertIn("✗ give the sources to log out of, or all; logged in: ins, twt",
                      self.run_app("auth", "logout")[2])

    def test_an_unknown_source_and_the_old_flag(self):
        self.assertEqual(self.run_app("auth", "logout", "ytb")[0], 2)
        self.assertEqual(self.run_app("auth", "logout", "--all")[0], 2)


class AuthLsTest(FlndCase):
    def test_auth_ls_shows_state_origin_and_expiring_cookie_and_takes_nothing(self):
        import time

        jar = cookies.Jar([{"name": "sessionid", "value": "secret-val", "domain": ".instagram.com",
                            "expires": time.time() - 10}])
        auth.store(sources.SOURCES["ins"](), jar, home=Path(".flnd"), origin="cookies.txt")
        with auth.secrets(Path(".flnd"), "tlg") as secrets:
            secrets["api_id"] = "1"
        code, _, out = self.run_app("auth", "ls")
        self.assertEqual(code, 0)
        for token in ("Instagram", "logged in", "cookies.txt", "sessionid expired", "Telegram", "api key only",
                      "TikTok", "not logged in"):
            self.assertIn(token, out)
        self.assertNotIn("secret-val", out)
        self.assertEqual(self.run_app("auth", "ls", "extra")[0], 2)


class ThinCliTest(unittest.TestCase):
    def test_the_browser_list_is_the_engines(self):
        # The CLI asks from the engine's table, so it has no copy to drift from.
        self.assertEqual(Engine().browsers, auth.BROWSERS)
        self.assertFalse(hasattr(flnd_cli, "BROWSERS"))

    def test_the_cookie_logins_are_the_sources_without_their_own(self):
        self.assertEqual(Engine().cookie_logins, ("tik", "fbk", "ins", "twt", "loi", "mht"))


if __name__ == "__main__":
    unittest.main()
