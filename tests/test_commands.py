"""The flnd command line: download (its options and saved settings), sync, login and logout. No network, no browser."""

import io
import json
import os
import stat
import tempfile
import unittest
from contextlib import redirect_stdout
from datetime import date
from pathlib import Path
from unittest import mock

from flnd import auth
from flnd import cli as flnd_cli
from flnd import sources
from flnd.compress import Choice
from flnd.engine import Engine
from flnd.session import Options
from flnd.sources.base import Source
from flnd.target import Target
from shared import cookies, kv, theme
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


class RunsCase(FlndCase):
    """Engine.run is replaced: each call is recorded in `runs`, and says 3 files were written."""

    def setUp(self):
        super().setUp()
        self.runs = []

        def run(engine, targets, options):
            self.runs.append((engine, targets, options))
            return 3

        patcher = mock.patch.object(Engine, "run", run)
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
        storage = (Path(self.tmp.name) / ".flnd/downloads").resolve()
        self.assertEqual(out.splitlines(), [f"• Storage: {storage}", "✓ Complete. 3 files"])

    def test_defaults(self):
        self.assertEqual(self.run_app("download", "tik:a")[0], 0)
        self.assertEqual(self.runs[0][2], Options())
        self.assertIsNone(Options().categories)
        self.assertEqual(self.run_app("download", "tik:a", "ins:b", "--from", "all")[0], 0)
        self.assertEqual(self.runs[1][2].categories, ("all",))

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
        for option in ("--limit", "--output", "--headless"):
            self.assertEqual(self.run_app("download", option, "5", "ins:a")[0], 2)
        self.assertEqual(self.runs, [])

    def test_removed_options_are_unknown(self):
        for args in (("--unpack",), ("--zip",), ("--locale", "fr-FR"), ("--timezone", "Europe/Paris"),
                     ("--since", "2026-01-01"), ("--until", "2026-01-01"), ("--compress-image", "small"),
                     ("--format-video", "h265")):
            code, _, out = self.run_app("download", "ins:a", *args)
            self.assertEqual(code, 2, args)
            self.assertIn("✗ unrecognized arguments", out)
        self.assertEqual(self.runs, [])

    def test_help(self):
        code, stdout, _ = self.run_app("--help")
        self.assertEqual(code, 0)
        self.assertIn("Dead simple media downloader for TikTok, Facebook, Instagram, Twitter/X, LOIBUS, and Telegram.", stdout)
        self.assertIn("Sources: tik, fbk, ins, twt, loi", stdout)
        self.assertIn("https://www.facebook.com/profile.php?id=100012345678901", stdout)
        for command in ("download", "sync", "login", "logout"):
            self.assertIn(command, stdout)
        self.assertNotIn("auth", stdout)
        self.assertNotIn("install-browser", stdout)
        self.assertNotIn("--compress", stdout)
        self.assertEqual(self.run_app("auth", "set-cookie")[0], 2)
        download_help = self.run_app("download", "--help")[1]
        for option in ("--from CATEGORIES", "--time SINCE,UNTIL", "--image FORMAT,LEVEL", "--video FORMAT,LEVEL",
                       "--audio FORMAT,LEVEL"):
            self.assertIn(f"  {option}  ", download_help)
        for gone in ("--since", "--until", "unpack", "--zip", "--locale", "--timezone", "--compress", "--format-"):
            self.assertNotIn(gone, download_help)
        # Help shows both ways to leave a side of --time open.
        self.assertIn("'*'", download_help)
        self.assertIn(",2026-06-30", download_help)
        self.assertNotIn("--categories", download_help)
        self.assertEqual(self.run_app("download", "tik:a", "--categories", "posts")[0], 2)

    def test_bare_flnd_prints_help_even_on_a_terminal(self):
        self.keys()
        code, stdout, _ = self.run_app()
        self.assertEqual(code, 0)
        self.assertIn("Usage:\n  flnd [options] [command]", stdout)


class TimeTest(RunsCase):
    def test_either_side_may_be_open(self):
        for value, since, until in (("2026-01-01,", date(2026, 1, 1), None), ("*,2026-06-30", None, date(2026, 6, 30)),
                                    (",2026-06-30", None, date(2026, 6, 30)), ("2026-01-01,*", date(2026, 1, 1), None),
                                    ("2026-01-01,2026-06-30", date(2026, 1, 1), date(2026, 6, 30)),
                                    ("2026-03-01,2026-03-01", date(2026, 3, 1), date(2026, 3, 1))):
            with self.subTest(value=value):
                self.runs.clear()
                self.assertEqual(self.run_app("download", "tik:a", "--time", value)[0], 0)
                options = self.runs[0][2]
                self.assertEqual((options.since, options.until), (since, until))

    def test_bad_values_are_usage_errors(self):
        message = ("✗ --time takes <since>,<until>, such as 2026-01-01,2026-06-30; use * or leave a side empty "
                   "for no limit")
        for value in ("2026-01-01", "2026-13-01,", "2026-06-30,2026-01-01", "yesterday,", "2026-01-01,2026-02-01,"):
            with self.subTest(value=value):
                code, _, out = self.run_app("download", "tik:a", "--time", value)
                self.assertEqual(code, 2)
                self.assertIn(message, out)
        self.assertEqual(self.runs, [])
        self.assertFalse(Path(".flnd/targets.db").exists())


class MediaOptionsTest(RunsCase):
    def test_choices_reach_the_engine_and_are_saved_for_the_target(self):
        code, _, out = self.run_app("download", "ins:example_user", "--image", "best,webp", "--video", "mkv:h265,low",
                                    "--audio", "opus")
        self.assertEqual(code, 0, out)
        self.assertEqual(self.runs[0][2], Options(image=Choice("webp", "best"), video=Choice("mkv:h265", "low"),
                                                  audio=Choice("opus", "high")))
        with kv.open(".flnd/targets.db", table="targets") as targets:
            row = targets["ins:example_user"]
        self.assertEqual((row["kind"], row["image"], row["video"], row["audio"]),
                         ("user", "webp,best", "mkv:h265,low", "opus,high"))

    def test_saved_ones_are_used_again_changed_one_at_a_time_and_turned_off(self):
        self.run_app("download", "ins:example_user", "--image", "webp,mid", "--video", "h265")
        code, _, out = self.run_app("download", "ins:example_user")
        self.assertEqual(code, 0, out)
        self.assertEqual(self.runs[1][2], Options(image=Choice("webp", "mid"), video=Choice("h265", "high")))
        self.assertIn("• ins:example_user: --image webp,mid --video h265,high (saved)", out)
        self.run_app("download", "ins:example_user", "--image", "best")
        self.assertEqual(self.runs[2][2], Options(image=Choice(None, "best"), video=Choice("h265", "high")))
        self.run_app("download", "ins:example_user", "--image", "off", "--video", "OFF")
        self.assertEqual(self.runs[3][2], Options())
        self.run_app("download", "ins:example_user")
        self.assertEqual(self.runs[4][2], Options())

    def test_from_and_time_are_not_saved(self):
        self.run_app("download", "ins:example_user", "--from", "reels", "--time", "2026-01-01,")
        self.run_app("download", "ins:example_user")
        self.assertEqual(self.runs[1][2], Options())

    def test_bad_values_are_usage_errors_naming_what_each_part_takes(self):
        for args, message in ((("--image", "webp,huge"), "✗ --image webp,huge: the level is best, high, mid or low"),
                              (("--video", "webm:h265"), "✗ --video webm:h265: webm holds vp9, not h265"),
                              (("--image", "heic"), "✗ --image heic: give a format (jpg, webp, avif, png, gif, tif "
                                                    "or bmp), a level (best, high, mid or low), or both"),
                              (("--audio", "opus,mid,low"), "✗ --audio takes <format>,<level>")):
            code, _, out = self.run_app("download", "ins:a", *args)
            self.assertEqual(code, 2, args)
            self.assertIn(message, out)
        self.assertEqual(self.runs, [])
        self.assertFalse(Path(".flnd/targets.db").exists())


class SyncCommandTest(RunsCase):
    def setUp(self):
        super().setUp()
        self.synced = []
        patcher = mock.patch("flnd.sync.run", lambda engine, chosen: self.synced.append(chosen) or [])
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_every_recorded_target_with_its_saved_settings(self):
        self.run_app("download", "ins:example_user", "--image", "low", "--from", "reels")
        self.run_app("download", "twt:example_handle")
        code, _, out = self.run_app("sync")
        self.assertEqual(code, 0, out)
        self.assertEqual(dict(self.synced[0]), {Target("ins", "user", "example_user"): Options(image=Choice(None, "low")),
                                                Target("twt", "user", "example_handle"): Options()})
        self.run_app("sync", "twt:example_handle")
        self.assertEqual(self.synced[1], [(Target("twt", "user", "example_handle"), Options())])

    def test_sync_takes_no_options(self):
        self.run_app("download", "ins:example_user")
        for args in (("--only", "info"), ("--from", "reels"), ("--time", "2026-01-01,"), ("--image", "mid"),
                     ("--compress-video", "small"), ("--since", "2026-01-01")):
            code, _, out = self.run_app("sync", *args)
            self.assertEqual(code, 2, args)
            self.assertIn("✗ unrecognized arguments", out)
        self.assertEqual(self.synced, [])

    def test_a_short_link_is_resolved_once(self):
        # Fix 11: cli.sync and sync.plan each resolved the targets, so a short link made two HEAD requests.
        followed = []

        def follow(url):
            followed.append(url)
            return "https://www.tiktok.com/@example_creator/video/7301234567890123456"

        with mock.patch("flnd.net.follow", follow):
            self.assertEqual(self.run_app("sync", "https://vm.tiktok.com/ZMabc123/")[0], 0)
        self.assertEqual(followed, ["https://vm.tiktok.com/ZMabc123/"])
        self.assertEqual(self.synced[0][0][0], Target("tik", "video", "7301234567890123456"))


class ExitStatusTest(FlndCase):
    """Fix 4 and 5: download and sync exit 1 when a target failed, so a script or cron job can tell."""

    def setUp(self):
        super().setUp()

        def run(self, session):
            if session.target.value == "bad":
                raise RuntimeError("blocked")

        def connect(self, session):
            raise RuntimeError("the saved login no longer works")

        def resolve(self, text):
            return Target(self.code, "user", text)

        common = {"name": "Test", "examples": (), "categories": ("posts",),
                  "resolve": resolve, "run": run}
        patcher = mock.patch.dict(sources.SOURCES, {
            "aaa": type("Aaa", (Source,), {"code": "aaa", "domains": ("aaa.example",), **common}),
            "bbb": type("Bbb", (Source,), {"code": "bbb", "domains": ("bbb.example",), "connect": connect, **common})})
        patcher.start()
        self.addCleanup(patcher.stop)
        # No person's pause between targets: these sources make no request.
        pauses = mock.patch("flnd.engine._sleep", lambda seconds: None)
        pauses.start()
        self.addCleanup(pauses.stop)

    def test_download_exits_1_when_a_target_failed(self):
        code, _, out = self.run_app("download", "aaa:bad", "aaa:good")
        self.assertEqual(code, 1, out)
        self.assertIn("✗ aaa:bad: blocked", out)
        self.assertEqual(self.run_app("download", "aaa:good")[0], 0)

    def test_download_exits_1_when_a_source_cant_start(self):
        code, _, out = self.run_app("download", "bbb:good")
        self.assertEqual(code, 1, out)
        self.assertNotIn("✓ bbb:good", out)

    def test_sync_exits_1_when_a_target_failed(self):
        self.run_app("download", "aaa:bad", "aaa:good")
        code, _, out = self.run_app("sync")
        self.assertEqual(code, 1, out)
        self.assertEqual(self.run_app("sync", "aaa:good")[0], 0)


class LoginCase(FlndCase):
    """`flnd login` in a fake terminal: `login` picks the source by its place in the list, then types `keys`."""

    def setUp(self):
        super().setUp()
        # No real browser profile is ever looked at; a test that wants profiles sets them.
        self.profiles = []
        patcher = mock.patch("shared._cookies.profiles", lambda browser="chrome": self.profiles)
        patcher.start()
        self.addCleanup(patcher.stop)

    def login(self, source, *keys):
        self.keys(*["down"] * list(flnd_cli.SOURCES).index(source), "enter", *keys)
        code, _, out = self.run_app("login")
        return code, out

    def from_file(self, source, path):
        # The method's second choice is "From a cookie file".
        return self.login(source, "down", "enter", *str(path), "enter")


class LoginFileTest(LoginCase):
    def test_a_full_export_keeps_only_the_source(self):
        code, out = self.from_file("ins", self.cookie_file(NETSCAPE))
        self.assertEqual(code, 0)
        self.assertEqual(out.splitlines()[-1], "✓ ins: logged in from cookies.txt (2 cookies)")
        saved = self.saved("ins")
        self.assertEqual({cookie["name"]: cookie["domain"] for cookie in saved},
                         {"sessionid": ".instagram.com", "ds_user_id": ".www.instagram.com"})
        self.assertEqual(stat.S_IMODE(Path(".flnd/cookies/ins.txt").stat().st_mode), 0o600)

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
        self.assertEqual(out.splitlines()[-1], "✗ fbk: no c_user or xs for facebook.com in cookies.txt; log in there first")
        self.assertFalse(Path(".flnd/cookies/fbk.txt").exists())

    def test_a_broken_file(self):
        code, out = self.from_file("ins", self.cookie_file("not\ta cookie\n"))
        self.assertEqual(code, 1)
        self.assertIn("✗ ins: can't read", out)

    def test_a_missing_path_is_asked_again(self):
        path = self.cookie_file(NETSCAPE)
        code, out = self.login("twt", "down", "enter", *"nope", "enter", *["backspace"] * 4, *str(path), "enter")
        self.assertEqual(code, 0)
        self.assertIn("┃ * no file at nope", out)
        self.assertIn("✓ twt: logged in from cookies.txt (1 cookies)", out)

    def test_logged_in_sources_are_marked(self):
        self.from_file("ins", self.cookie_file(NETSCAPE))
        self.screen.out.truncate(0)
        self.from_file("twt", self.cookie_file(NETSCAPE))
        out = self.screen.out.getvalue()
        self.assertIn("Instagram (ins) · logged in", out)
        self.assertIn("TikTok (tik)", out)
        self.assertNotIn("TikTok (tik) · logged in", out)
        self.assertIn("✓ Log in to: Twitter/X (twt)", out)


class LoginQuestionsTest(LoginCase):
    def test_without_a_terminal_it_exits_2(self):
        code, _, out = self.run_app("login")
        self.assertEqual(code, 2)
        self.assertIn("✗ flnd login asks its questions in a terminal", out)
        self.assertFalse(Path(".flnd").exists())

    def test_it_takes_no_arguments(self):
        for argv in (["ins"], ["--method", "file"], ["--path", "c.txt"], ["--browser", "chrome"], ["--profile", "x"]):
            with self.subTest(argv=argv):
                self.assertEqual(self.run_app("login", *argv)[0], 2)


class LoginBrowserTest(LoginCase):
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

    def test_source_method_browser_and_profile_are_asked_in_order(self):
        self.profiles = ["Default", "work"]
        # Source: Twitter/X; method: the first (browser); browser: the second (Firefox); profile: the second.
        code, out = self.login("twt", "enter", "down", "enter", "down", "enter")
        self.assertEqual(code, 0)
        answers = ["✓ Log in to: Twitter/X (twt)", "✓ Log in to Twitter/X: From your browser",
                   "✓ Read cookies from: Firefox", "✓ Profile: work", "✓ twt: logged in from Firefox (2 cookies)"]
        lines = out.splitlines()
        self.assertEqual([line for line in lines if line in answers], answers)
        self.assertEqual(self.reads, [("x.com", "firefox", "work"), ("twitter.com", "firefox", "work")])
        self.assertEqual([cookie["domain"] for cookie in self.saved("twt")], [".x.com", ".twitter.com"])

    def test_the_default_profile_is_preselected_and_one_profile_is_not_asked(self):
        self.profiles = ["Default", "work"]
        self.assertEqual(self.login("twt", "enter", "enter", "enter")[0], 0)
        self.assertEqual(self.reads[0], ("x.com", "chrome", "Default"))
        self.reads.clear()
        self.profiles = ["Default"]
        code, out = self.login("twt", "enter", "enter")
        self.assertEqual(code, 0)
        self.assertEqual(self.reads[0], ("x.com", "chrome", "Default"))
        self.assertEqual(out.count("✓ Profile:"), 1)

    def test_a_missing_needs_cookie_and_an_unreadable_browser(self):
        self.store = {}
        code, out = self.login("twt", "enter", "enter")
        self.assertEqual((code, out.splitlines()[-1]), (1, "✗ twt: no auth_token for x.com in Chrome; log in there first"))
        code, out = self.login("twt", "enter", "down", "down", "enter")
        self.assertEqual(code, 1)
        self.assertIn("✗ twt: can't read Edge's cookies: can't read edge cookies on Windows; try a cookie file instead",
                      out)


class OwnLoginTest(LoginCase):
    def test_a_source_with_its_own_login_and_one_method_is_not_asked_the_method(self):
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
            code, out = self.login("own")
        self.assertEqual(code, 0)
        self.assertEqual(calls, [("qr", "account")])
        self.assertNotIn("Log in to Own:", out)


class LogoutTest(FlndCase):
    def log_in(self, *codes):
        for code in codes:
            cookies.to_file([{"name": "a", "value": "1", "domain": ".x.example"}], Path(".flnd/cookies") / f"{code}.txt")

    def test_named_sources(self):
        self.log_in("ins", "twt")
        with auth.secrets(Path(".flnd"), "ins") as secrets:
            secrets["token"] = "t"
        code, _, out = self.run_app("logout", "ins")
        self.assertEqual((code, out), (0, "✓ ins: logged out\n"))
        self.assertFalse(Path(".flnd/cookies/ins.txt").exists())
        self.assertTrue(Path(".flnd/cookies/twt.txt").exists())
        with auth.secrets(Path(".flnd"), "ins") as secrets:
            self.assertEqual(len(secrets), 0)
        self.screen.out.truncate(0)
        self.screen.out.seek(0)
        code, _, out = self.run_app("logout", "ins", "twt", "tik")
        self.assertEqual((code, out), (0, "• ins: not logged in\n✓ twt: logged out\n• tik: not logged in\n"))

    def test_all(self):
        self.log_in("ins", "tik")
        with auth.secrets(Path(".flnd"), "tlg") as secrets:
            secrets["session"] = "s"
        code, _, out = self.run_app("logout", "all")
        self.assertEqual((code, out), (0, "✓ tik: logged out\n✓ ins: logged out\n✓ tlg: logged out\n"))
        self.assertEqual(list(Path(".flnd/cookies").iterdir()), [])
        self.assertFalse(any(auth.logged_in(Path(".flnd"), code) for code in sources.SOURCES))

    def test_bare_is_a_usage_error_listing_the_logged_in_sources(self):
        code, _, out = self.run_app("logout")
        self.assertEqual(code, 2)
        self.assertIn("✗ give the sources to log out of, or all; no source is logged in", out)
        self.log_in("twt", "ins")
        self.assertIn("✗ give the sources to log out of, or all; logged in: ins, twt", self.run_app("logout")[2])

    def test_an_unknown_source_and_the_old_flag(self):
        self.assertEqual(self.run_app("logout", "ytb")[0], 2)
        self.assertEqual(self.run_app("logout", "--all")[0], 2)


if __name__ == "__main__":
    unittest.main()
