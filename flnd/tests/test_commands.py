"""The flnd command line, command by command: flags in, the library call it makes, lines out.

Most tests replace the engine's `_run` (`RunsCase`), so a command's flags can be checked against
what reaches the engine without a site; `OutputTest` runs a whole download of an example source
over a fake network to check the lines, the JSON and the exit codes a run ends with. No network,
no browser, no real site.
"""

import io
import json
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from dataclasses import replace
from datetime import date
from pathlib import Path
from unittest import mock

from prompt_lines import select_from_lines
from test_engine import CDN, make_source, noise
from test_library import example_source

from flnd import Blocked, Target, sources
from flnd import cli as flnd_cli
from flnd.engine import home as _home
from flnd.engine.core import Engine, TargetReport
from flnd.engine.events import Saved
from flnd.engine.options import Options
from flnd.kit import cookies as _cookies
from flnd.kit import net as _net
from flnd.source import Login, Source
from flnd.source import cookies as login_cookies
from flnd.source import target as route
from flnd.ui.log import ui

NETSCAPE = (
    "# Netscape HTTP Cookie File\n"
    ".instagram.com\tTRUE\t/\tTRUE\t1825861327\tsessionid\tins-session\n"
    "www.instagram.com\tFALSE\t/\tTRUE\t1825861327\tds_user_id\t42\n"
    ".x.com\tTRUE\t/\tTRUE\t1825861327\tauth_token\ttwt-token\n"
    ".example.com\tTRUE\t/\tFALSE\t0\tother\tvalue\n"
)


class Stdin(io.StringIO):
    """A standard input that is a terminal or a pipe, as a test needs."""

    def __init__(self, text="", tty=False):
        super().__init__(text)
        self.tty = tty

    def isatty(self):
        return self.tty


class _TTY(io.StringIO):
    """A stream that is a terminal, so a run draws its summary rows on it."""

    def isatty(self) -> bool:
        return True


class FlndCase(unittest.TestCase):
    """Runs in a temporary working directory, so the home is `./.flnd`, with stdin a terminal."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.previous_cwd = os.getcwd()
        os.chdir(self.tmp.name)
        self.addCleanup(os.chdir, self.previous_cwd)
        self.enterContext(mock.patch.dict(os.environ))
        os.environ.pop("FLND_HOME", None)
        self.dest = io.StringIO()
        self.enterContext(ui.scope(dest=self.dest, width=400, color=False))
        self.stdin = mock.patch("sys.stdin", Stdin(tty=True))
        self.stdin.start()
        self.addCleanup(lambda: self.stdin.stop())

    def pipe(self, text):
        self.stdin.stop()
        self.stdin = mock.patch("sys.stdin", Stdin(text))
        self.stdin.start()

    def run_app(self, *argv):
        """Run `flnd <argv>`: the exit code, standard output, and standard error with flnd's log."""
        stdout = io.StringIO()
        with redirect_stdout(stdout), redirect_stderr(self.dest):
            code = flnd_cli.main(list(argv))
        return code, stdout.getvalue(), self.dest.getvalue()

    def cookie_file(self, text, name="cookies.txt"):
        path = Path(self.tmp.name) / name
        path.write_text(text, encoding="utf-8")
        return path

    def saved(self, code):
        """The cookies the vault holds for a source; no plain cookie file is ever written."""
        self.assertFalse(Path(".flnd/cookies").exists())
        return _home.load(Path(".flnd"), code).list


class RunsCase(FlndCase):
    """`Engine._run` is replaced: each call goes in `runs`, with its first target's options."""

    def setUp(self):
        super().setUp()
        self.runs = []

        def run(engine, chosen, *, stop=None, tallies=(), browser_pool=None):
            targets = [target for target, _ in chosen]
            self.runs.append((engine, targets, replace(chosen[0][1], stop_at_saved=False)))
            return [TargetReport(target, (), 0) for target in targets]

        self.enterContext(mock.patch.object(Engine, "_run", run))


# ---------------------------------------------------------------------------------------------
# download and its flags
# ---------------------------------------------------------------------------------------------

class DownloadTest(RunsCase):
    def test_targets_and_flags_reach_the_engine(self):
        code, _, out = self.run_app("download", "ins:example_user", "https://x.com/example_handle",
                                    "--only", "reels,media", "--only", "tagged",
                                    "--when", "2026-01..2026-06", "--limit", "5")
        self.assertEqual(code, 0, out)
        engine, targets, options = self.runs[0]
        self.assertEqual(targets, [Target("ins:example_user", kind="user"),
                                   Target("twt:example_handle", kind="user")])
        self.assertEqual(options, Options(categories=("reels", "media", "tagged"), limit=5,
                                          since=date(2026, 1, 1), until=date(2026, 6, 30)))
        self.assertEqual(engine.home, Path(".flnd"))

    def test_no_only_takes_every_category(self):
        self.assertEqual(self.run_app("download", "tik:a", "ins:b")[0], 0)
        self.assertIsNone(self.runs[0][2].categories)

    def test_only_with_a_single_post_is_a_usage_error(self):
        message = ("--only applies to profiles and chats; ins:C1abcDEFghi is one post, "
                   "downloaded whole: give it without --only")
        for targets in (("https://www.instagram.com/p/C1abcDEFghi/",),
                        ("ins:example_user", "https://www.instagram.com/p/C1abcDEFghi/")):
            with self.subTest(targets=targets):
                code, _, out = self.run_app("download", *targets, "--only", "tagged")
                self.assertEqual(code, 2)
                self.assertIn(message, out)
        self.assertEqual(self.runs, [])
        self.assertEqual(self.run_app("download", "https://www.instagram.com/p/C1abcDEFghi/")[0], 0)

    def test_targets_from_stdin(self):
        self.pipe("ins:a\n\n# a comment\ntwt:b\n")
        self.assertEqual(self.run_app("download")[0], 0)
        self.assertEqual([str(target) for target in self.runs[0][1]], ["ins:a", "twt:b"])

    def test_no_targets_is_a_usage_error(self):
        code, _, out = self.run_app("download")
        self.assertEqual(code, 2)
        self.assertIn("x give at least one target, or pipe targets on stdin", out)

    def test_bad_targets_are_usage_errors_before_anything_starts(self):
        for target, message in (("ytb:nasa", "unknown source 'ytb'. Sources: tik, fbk, ins, twt"),
                                ("example_user",
                                 'which source is "example_user"? Write it as tik:example_user'),
                                ("https://www.facebook.com/example.page/posts/1",
                                 "fbk: this kind of link isn't supported yet")):
            code, _, out = self.run_app("download", "ins:a", target)
            self.assertEqual(code, 2, target)
            self.assertIn(message, out)
            self.assertNotIn("Traceback", out)
        self.assertEqual(self.runs, [])
        self.assertFalse((Path(".flnd") / _home.MEDIA).exists())

    def test_bad_flags(self):
        message = ("no source here has 'reels': use all, or tik: videos, stories, playlists, "
                   "reposts, liked, favorites, profile; twt: posts, media, highlights, reposts, "
                   "replies, profile")
        self.assertIn(message, self.run_app("download", "tik:a", "twt:b", "--only", "videos,reels")[2])
        for argv in (("--limit", "0"), ("--limit", "five"), ("--when", "yesterday"),
                     ("--from", "posts"), ("--time", "2026-01-01,"), ("--sync",)):
            with self.subTest(argv=argv):
                code, _, out = self.run_app("download", "ins:a", *argv)
                self.assertEqual(code, 2)
                self.assertNotIn("Traceback", out)
        self.assertEqual(self.runs, [])

    def test_a_broken_exec_is_a_usage_error_before_the_run(self):
        for command, message in (("no-such-program-here {path}", "no such program"),
                                 ("echo 'unclosed", "No closing quotation"), ("", "needs a command")):
            with self.subTest(command=command):
                code, _, out = self.run_app("download", "ins:a", "--exec", command)
                self.assertEqual(code, 2)
                self.assertIn(message, out)
                self.assertNotIn("Traceback", out)
        self.assertEqual(self.runs, [])


    def test_global_flags_go_before_or_after_the_command(self):
        self.assertEqual(self.run_app("--home", "one", "download", "ins:a")[0], 0)
        self.assertEqual(self.run_app("download", "ins:a", "--home", "two")[0], 0)
        self.assertEqual([run[0].home for run in self.runs], [Path("one"), Path("two")])

    def test_the_home_comes_from_flnd_home_when_not_given(self):
        os.environ["FLND_HOME"] = "from-env"
        self.assertEqual(self.run_app("download", "ins:a")[0], 0)
        self.assertEqual(self.runs[0][0].home, Path("from-env"))

    def test_download_writes_run_journal_to_log_folder(self):
        import json

        code, out, _ = self.run_app("download", "ins:a")
        self.assertEqual(code, 0)
        log_dir = Path(".flnd") / _home.LOGS
        self.assertTrue(log_dir.is_dir())
        timestamped = list(log_dir.glob("*_download.jsonl"))
        self.assertEqual(len(timestamped), 1)
        lines = [json.loads(line) for line in timestamped[0].read_text(encoding="utf-8").splitlines()
                 if line.strip()]
        types = {line["type"] for line in lines}
        self.assertIn("Start", types)
        self.assertIn("Report", types)
        self.assertFalse((log_dir / "run.log").exists())
        self.assertIn("done:", out)

    def test_download_v_logs_events_and_debug_in_realtime(self):
        def run_with_events(engine, chosen, *, stop=None, tallies=(), browser_pool=None):
            ui.debug("fetching post list for {target}", target="ins:a")
            engine.emit(Saved(Target("ins:a"), "posts", "1", Path("media/ins/a/posts/1.jpg"), None, 100))
            return [TargetReport(Target("ins:a"), (), 1)]

        with mock.patch.object(Engine, "_run", run_with_events):
            code, out, err = self.run_app("download", "ins:a", "-v")
            self.assertEqual(code, 0)
            log_dir = Path(".flnd") / _home.LOGS
            [run_log] = list(log_dir.glob("*_download.jsonl"))
            import json

            lines = [json.loads(line) for line in run_log.read_text(encoding="utf-8").splitlines()]
            texts = json.dumps(lines)
            self.assertIn("Saved", texts)
            self.assertIn("1 file saved", out)

    def test_download_progress_enabled_keeps_saved_off_stdout(self):
        def run_with_events(engine, chosen, *, stop=None, tallies=(), browser_pool=None):
            engine.emit(Saved(Target("ins:a"), "posts", "1", Path("media/ins/a/posts/1.jpg"), None, 100))
            return [TargetReport(Target("ins:a"), (), 1)]

        with mock.patch.object(Engine, "_run", run_with_events):
            with mock.patch.object(ui, "_progress_enabled", return_value=True):
                code, out, err = self.run_app("download", "ins:a")
                self.assertEqual(code, 0)
                self.assertNotIn("saved media", out)
                log_dir = Path(".flnd") / _home.LOGS
                [run_log] = list(log_dir.glob("*_download.jsonl"))
                import json

                lines = [json.loads(line) for line in run_log.read_text(encoding="utf-8").splitlines()]
                self.assertTrue(any(line.get("type") == "Saved" for line in lines))


class WhenTest(RunsCase):
    def test_periods_and_ranges(self):
        for value, since, until in (("2026", date(2026, 1, 1), date(2026, 12, 31)),
                                    ("2026-02", date(2026, 2, 1), date(2026, 2, 28)),
                                    ("2026-06-15", date(2026, 6, 15), date(2026, 6, 15)),
                                    ("2026-01..2026-03", date(2026, 1, 1), date(2026, 3, 31)),
                                    ("2026-01..", date(2026, 1, 1), None)):
            with self.subTest(value=value):
                self.runs.clear()
                self.assertEqual(self.run_app("download", "tik:a", "--when", value)[0], 0)
                options = self.runs[0][2]
                self.assertEqual((options.since, options.until), (since, until))

    def test_bad_values_are_usage_errors(self):
        for value in ("2026-13", "2026-06..2026-01", "yesterday", "2026-01-01,2026-02-01"):
            with self.subTest(value=value):
                code, _, out = self.run_app("download", "tik:a", "--when", value)
                self.assertEqual(code, 2)
                self.assertIn("when takes a year (2026)", out)
        self.assertEqual(self.runs, [])


# ---------------------------------------------------------------------------------------------
# follow, sync, unfollow, ls
# ---------------------------------------------------------------------------------------------

class FollowTest(RunsCase):
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

        self.enterContext(mock.patch.object(Engine, "_plan", plan))

    def test_sync_takes_the_followed_targets_with_their_only(self):
        self.run_app("follow", "ins:example_user", "--only", "reels")
        self.run_app("follow", "twt:example_handle")
        self.run_app("download", "tik:example_creator")
        code, _, out = self.run_app("sync")
        self.assertEqual(code, 0, out)
        self.assertEqual(dict(self.synced[0]), {
            Target("ins:example_user", kind="user"): Options(categories=("reels",)),
            Target("twt:example_handle", kind="user"): Options()})
        self.run_app("sync", "twt:example_handle")
        self.assertEqual(self.synced[1], [(Target("twt:example_handle", kind="user"), Options())])

    def test_sync_v_logs_debug_lines_for_the_run_only(self):
        self.run_app("follow", "ins:example_user")
        self.assertEqual(ui.level, "info")
        self.assertEqual(self.run_app("sync", "-v")[0], 0)
        self.assertEqual(self.levels[-1], "debug")
        self.assertEqual(self.run_app("sync")[0], 0)
        self.assertEqual(self.levels[-1], "warn")
        self.assertEqual(ui.level, "info")

    def test_sync_takes_no_only_or_when(self):
        for argv in (("--only", "reels"), ("--when", "2026"), ("--limit", "1")):
            with self.subTest(argv=argv):
                self.assertEqual(self.run_app("sync", *argv)[0], 2)
        self.assertEqual(self.synced, [])

    def test_ls_and_unfollow(self):
        self.run_app("follow", "ins:example_user")
        self.run_app("download", "tik:example_creator")
        code, out, _ = self.run_app("ls")
        self.assertEqual(code, 0)
        lines = out.splitlines()
        self.assertEqual(lines[0].split(), ["target", "follow", "files", "last", "categories"])
        followed = {line.split()[0]: line.split()[1] for line in lines[1:]}
        self.assertEqual(followed, {"ins:example_user": "followed", "tik:example_creator": "-"})
        code, out, _ = self.run_app("unfollow", "ins:example_user")
        self.assertEqual((code, out), (0, "ins:example_user: unfollowed\n"))
        code, out, _ = self.run_app("ls", "ins:example_user", "--json")
        row = json.loads(out)
        self.assertEqual((row["target"]["value"], row["follow"]), ("example_user", False))
        code, _, out = self.run_app("unfollow", "ins:nobody")
        self.assertEqual(code, 2)
        self.assertIn("not a known target", out)
        self.assertEqual(self.run_app("unfollow")[0], 2)

    def test_a_short_link_is_resolved_once(self):
        followed = []

        def follow(url):
            followed.append(url)
            return "https://www.tiktok.com/@example_creator/video/7301234567890123456"

        with mock.patch("flnd.kit.net.follow", follow):
            self.assertEqual(self.run_app("sync", "https://vm.tiktok.com/ZMabc123/")[0], 0)
        self.assertEqual(followed, ["https://vm.tiktok.com/ZMabc123/"])
        self.assertEqual(self.synced[0][0][0], Target("tik:7301234567890123456", kind="video"))


# ---------------------------------------------------------------------------------------------
# A whole run: its lines, its JSON and its exit code
# ---------------------------------------------------------------------------------------------

class OutputTest(FlndCase):
    """A real download of an example source; the CDN answers from a fake transport."""

    listing = {("example_user", "posts"): ["p1", "p2"], ("example_user", "reels"): ["r1"]}

    def setUp(self):
        super().setUp()
        jpeg = noise("JPEG")

        def serve(request):
            if str(request.url).startswith(f"{CDN}/bad"):
                return _net.reply(503)
            return _net.reply(200, content=jpeg)

        # A lambda, so a test that replaces `self.serve` is answered by its own.
        self.serve = serve
        self.enterContext(_net.fake(lambda request: self.serve(request)))
        from flnd.kit import clock as _time

        self.enterContext(_time.use(_time.Fake()))
        self.listing = {key: list(ids) for key, ids in type(self).listing.items()}
        self.enterContext(mock.patch.dict(sources.SOURCES, {"exs": example_source(self.listing)}))

    def test_plain_lines_and_the_summary(self):
        code, out, err = self.run_app("download", "exs:example_user")
        self.assertEqual(code, 0, err)
        lines = out.splitlines()
        saved = [line for line in lines if line.startswith("saved ")]
        self.assertEqual(saved, [])
        self.assertRegex(lines[-1], r"^done: 3 files saved \(.+\)$")
        log_dir = Path(".flnd") / _home.LOGS
        import json

        saved_rows = [
            line for path in log_dir.glob("*_download.jsonl")
            for line in (json.loads(raw) for raw in path.read_text(encoding="utf-8").splitlines())
            if line.get("type") == "Saved"
        ]
        self.assertEqual(len(saved_rows), 3)
        self.assertNotIn("Storage:", err)
        code, out, _ = self.run_app("download", "exs:example_user")
        self.assertEqual(out.splitlines()[-1], "done: 0 files saved, 3 already saved")

    def test_json_lines(self):
        code, out, _ = self.run_app("--json", "download", "exs:example_user", "--only", "reels")
        self.assertEqual(code, 0)
        rows = [json.loads(line) for line in out.splitlines()]
        self.assertEqual({row["type"] for row in rows[:-1]}, {"Saved", "Progress"})
        saved = [row for row in rows if row["type"] == "Saved"]
        self.assertEqual([row["post"] for row in saved], ["r1"])
        self.assertEqual(rows[-1]["type"], "Report")
        self.assertEqual(rows[-1]["saved"], 1)

    def test_dry_plans_and_writes_nothing(self):
        code, out, _ = self.run_app("download", "exs:example_user", "--dry")
        self.assertEqual(code, 0)
        planned = [line for line in out.splitlines() if line.startswith("new ")]
        self.assertEqual(len(planned), 3)
        self.assertEqual(out.splitlines()[-1], "planned 3 files; nothing was downloaded")
        self.assertFalse((Path(".flnd") / _home.MEDIA).exists())

    def test_a_failed_file_exits_1_without_its_line(self):
        self.listing[("example_user", "posts")] = ["p1", "bad"]
        code, out, err = self.run_app("download", "exs:example_user")
        self.assertEqual(code, 1, err)
        self.assertNotIn("failed", out)
        self.assertIn("x 1 file failed: 1 site (retry: flnd retry). Details: flnd status", err)

    def test_status_shows_the_failure_and_retry_brings_it_back(self):
        self.listing[("example_user", "posts")] = ["p1", "bad"]
        self.run_app("download", "exs:example_user")
        code, out, _ = self.run_app("status")
        self.assertEqual(code, 0)
        lines = out.splitlines()
        self.assertRegex(lines[0], r"^last run: download exs:example_user, .+: 2 saved, 1 failed$")
        self.assertIn("  exs:example_user bad (site): ", lines[1])
        self.assertEqual(lines[-1], "x 1 file failed: 1 site (retry: flnd retry)")
        code, out, _ = self.run_app("--json", "status")
        status = json.loads(out)
        self.assertEqual((status["type"], status["saved"]), ("Status", 2))
        [failure] = status["failures"]
        self.assertEqual((failure["target"], failure["post"], failure["error"]["class"]),
                         ("exs:example_user", "bad", "SiteError"))
        # The site answers now: a retry lists the post again and saves the one file.
        self.serve = lambda request: _net.reply(200, content=noise("JPEG"))
        code, out, err = self.run_app("retry")
        self.assertEqual(code, 0, err)
        last = out.splitlines()[-1]
        self.assertEqual(last, "done: 1 file saved ({}), 1 brought back by a retry".format(
            last.split("(")[1].split(")")[0]))
        self.assertRegex(self.run_app("status")[1], r"^last run: retry exs:example_user, .+: 1 saved, 0 failed\n$")

    def test_the_download_view_draws_and_ends_with_the_summary(self):
        code, out, err = self.run_app("download", "exs:example_user")
        self.assertEqual(code, 0, err)
        self.assertRegex(out.splitlines()[-1], r"^done: 3 files saved")

    def test_the_run_draws_summary_rows_on_a_terminal(self):
        tty = _TTY()
        with ui.scope(dest=tty, color=False, width=400):
            code, _, _ = self.run_app("download", "exs:example_user")
        self.assertEqual(code, 0)
        rows = tty.getvalue()
        self.assertIn("exs:example_user posts", rows)
        self.assertIn("exs:example_user reels", rows)
        self.assertIn("2 saved", rows)
        self.assertIn("1 saved", rows)
        self.assertIn("queued", rows)
        self.assertIn("downloading", rows)
        self.assertNotIn("media/", rows)


class ExitStatusTest(FlndCase):
    """A target that fails exits 1, so a script or cron job can tell; a login needed exits 3."""

    def setUp(self):
        super().setUp()

        def connect(self, site):
            raise RuntimeError("the saved login no longer works")

        def run(site):
            if site.target.value == "bad":
                raise Blocked("blocked")

        self.enterContext(mock.patch.dict(sources.SOURCES, {
            "aaa": make_source("aaa", run), "bbb": make_source("bbb", run, connect=connect),
            "ccc": make_source("ccc", login=login_cookies("sid", required=True))}))

    def test_download_exits_1_when_a_target_failed(self):
        code, out, err = self.run_app("download", "aaa:bad", "aaa:good")
        self.assertEqual(code, 1, err)
        self.assertNotIn("failed", out)
        self.assertIn("x 1 target failed: 1 blocked (retry: flnd retry)", err)
        self.assertEqual(self.run_app("download", "aaa:good")[0], 0)

    def test_download_exits_1_when_a_source_cant_start(self):
        self.assertEqual(self.run_app("download", "bbb:good")[0], 1)

    def test_sync_exits_1_when_a_target_failed(self):
        self.run_app("follow", "aaa:bad", "aaa:good")
        self.assertEqual(self.run_app("sync")[0], 1)
        self.assertEqual(self.run_app("sync", "aaa:good")[0], 0)

    def test_a_login_the_source_needs_exits_3_with_the_command_to_run(self):
        code, out, err = self.run_app("download", "ccc:example_user")
        self.assertEqual(code, 3)
        self.assertNotIn("failed", out)
        self.assertIn("x 1 target failed: 1 login (retry: flnd retry)", err)
        self.assertIn("run flnd login ccc", err)


# ---------------------------------------------------------------------------------------------
# Help
# ---------------------------------------------------------------------------------------------

class HelpTest(FlndCase):
    def test_help_lists_the_commands_and_every_source_from_the_manifest(self):
        code, out, _ = self.run_app("--help")
        self.assertEqual(code, 0)
        self.assertIn("flnd 0.1.0 - download whole accounts from the web", out)
        for verb in ("download", "follow", "sync", "unfollow", "ls", "status", "retry", "check",
                     "login", "logout", "sources"):
            self.assertIn(f"  {verb} ", out)
        self.assertIn("tik fbk ins twt loi tlg mht", out)

    def test_download_help_has_a_row_per_source(self):
        code, out, _ = self.run_app("download", "--help")
        self.assertEqual(code, 0)
        # Placeholders show as written, then the categories.
        self.assertIn("tlg:<@username, chat title or id>, a link on t.me", out)
        self.assertIn("fbk:<user>, a link on facebook.com, fb.com, fb.watch", out)
        self.assertIn("--only posts reels tagged", " ".join(out.split()))

    def test_command_help_is_its_docstring_with_its_flags(self):
        code, out, _ = self.run_app("download", "--help")
        self.assertEqual(code, 0)
        self.assertIn("Download what the targets have now, once.", out)
        for flag in ("--only", "--when", "--limit", "--dry", "--again", "--exec",
                     "--home", "--json"):
            self.assertIn(flag, out)
        for gone in ("--from", "--time", "--sync", "Args:"):
            self.assertNotIn(gone, out)

    def test_help_imports_no_source_engine_or_network(self):
        import subprocess

        code = ("import sys\nfrom flnd.cli import main\nmain(['--help'])\n"
                "print(sorted(m for m in sys.modules if m.startswith(('flnd.engine.core', 'flnd.sources.ins', 'curl_cffi'))))")
        out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True,
                             check=True).stdout.splitlines()[-1]
        self.assertEqual(out, "[]")

    def test_bare_flnd_prints_help_and_version(self):
        code, out, _ = self.run_app()
        self.assertEqual(code, 0)
        self.assertIn("flnd", out)
        code, out, _ = self.run_app("--version")
        self.assertEqual((code, out), (0, "flnd 0.1.0\n"))

    def test_an_unknown_command_or_flag_is_a_usage_error(self):
        for argv in (("target", "ls"), ("auth", "login"), ("download", "--nope")):
            with self.subTest(argv=argv):
                code, _, err = self.run_app(*argv)
                self.assertEqual(code, 2)
                self.assertIn("flnd", err)

    def test_sources_lists_each_source(self):
        code, out, _ = self.run_app("sources")
        self.assertEqual(code, 0)
        self.assertIn("ins  Instagram", out)
        self.assertIn("  categories: posts, reels", out)
        code, out, _ = self.run_app("sources", "--json")
        rows = [json.loads(line) for line in out.splitlines()]
        self.assertEqual([row["code"] for row in rows], ["tik", "fbk", "ins", "twt", "loi", "tlg", "mht"])

    def test_status_and_retry_on_a_home_with_no_run(self):
        self.assertEqual(self.run_app("status")[:2], (0, "no run yet\n"))
        code, out, _ = self.run_app("--json", "status")
        self.assertEqual((code, json.loads(out)), (0, {"type": "Status"}))
        code, out, _ = self.run_app("retry")
        self.assertEqual((code, out), (0, "done: 0 files saved\n"))


# ---------------------------------------------------------------------------------------------
# Logins
# ---------------------------------------------------------------------------------------------

class LoginFileTest(FlndCase):
    def test_a_full_export_keeps_only_the_source(self):
        code, out, err = self.run_app("login", "ins", str(self.cookie_file(NETSCAPE)))
        self.assertEqual(code, 0, err)
        self.assertEqual(out, "ins: logged in (cookies.txt)\n")
        saved = self.saved("ins")
        self.assertEqual({cookie["name"]: cookie["domain"] for cookie in saved},
                         {"sessionid": ".instagram.com", "ds_user_id": ".www.instagram.com"})
        # Stored in the vault; nothing else is written.
        self.assertEqual(sorted(path.name for path in Path(".flnd").rglob("*") if path.is_file()),
                         ["flnd.db", "secret.db"])

    def test_a_json_export(self):
        path = self.cookie_file(json.dumps([{"domain": ".tiktok.com", "name": "sid_tt",
                                             "value": "v"}]), "c.json")
        self.assertEqual(self.run_app("login", "tik", str(path))[0], 0)
        self.assertEqual(self.saved("tik")[0]["value"], "v")

    def test_a_missing_needs_cookie_exits_3_without_a_traceback(self):
        code, _, out = self.run_app("login", "fbk", str(self.cookie_file(NETSCAPE)))
        self.assertEqual(code, 3)
        self.assertNotIn("Traceback", out)
        self.assertIn("fbk: no c_user or xs for facebook.com in cookies.txt; log in there first",
                      out)

    def test_a_wrong_way_or_source(self):
        code, _, out = self.run_app("login", "ins", "netscape")
        self.assertEqual(code, 2)
        self.assertIn("ins logs in by a browser", out)
        self.assertEqual(self.run_app("login", "ytb", "chrome")[0], 2)


class LoginQuestionsTest(FlndCase):
    def answer(self, *lines):
        """Answer the prompts with `lines`, on a terminal."""
        self.stdin.stop()
        self.stdin = mock.patch("sys.stdin", Stdin("".join(f"{line}\n" for line in lines),
                                                    tty=True))
        self.stdin.start()
        self.qpatch = select_from_lines(*lines)
        self.qpatch.start()
        self.addCleanup(self.qpatch.stop)

    def test_the_way_is_asked_with_numbered_choices(self):
        path = self.cookie_file(NETSCAPE)
        choices = [b for b in _cookies.BROWSERS if b != "safari" or sys.platform == "darwin"]
        self.answer(str(len(choices) + 1), str(path))
        code, out, err = self.run_app("login", "twt")
        self.assertEqual(code, 0, err)
        self.assertEqual(out, "twt: logged in (cookies.txt)\n")

    def test_a_wrong_answer_is_asked_again(self):
        self.answer("9", "nope", "2")
        with mock.patch("flnd.engine.home.from_browser", return_value=_cookies.Jar(
                [{"name": "auth_token", "value": "t", "domain": ".x.com", "path": "/"}])) as read:
            code, out, err = self.run_app("login", "twt")
        self.assertEqual(code, 0, err)
        self.assertEqual(read.call_args.kwargs["browser"], _cookies.BROWSERS[1])

    def test_without_a_terminal_or_with_json_nothing_is_asked(self):
        self.pipe("")
        code, _, out = self.run_app("login", "ins")
        self.assertEqual(code, 2)
        self.assertIn("name it: flnd login ins <how>", out)
        self.answer("1")
        self.assertEqual(self.run_app("login", "ins", "--json")[0], 2)
        self.assertFalse(Path(".flnd").exists())

    def test_a_source_with_its_own_login_offers_its_own_ways(self):
        calls = []

        class OwnLogin(Login):
            methods = ("qr", "phone")

            def start(self, site, method):
                calls.append((method, type(site).__name__))

        class Own(Source):
            code, name, domains, api = "own", "Own", ("own.example",), 1
            login = OwnLogin()

            @route("own:{user}")
            def user(self, site, user): ...

        self.answer("2")
        with mock.patch.dict(sources.SOURCES, {"own": Own}), \
                mock.patch.dict("flnd.sources._manifest.SOURCES", {"own": {
                    "name": "Own", "domains": ("own.example",), "forms": (), "categories": (),
                    "logins": ("qr", "phone")}}):
            code, _, err = self.run_app("login", "own")
        self.assertEqual(code, 0, err)
        self.assertEqual(calls, [("phone", "LoginSite")])


class LoginsTest(FlndCase):
    def test_login_with_no_source_lists_every_login(self):
        import time

        jar = _cookies.Jar([{"name": "sessionid", "value": "secret-val", "domain": ".instagram.com",
                            "expires": time.time() - 10}])
        _home.store(sources.SOURCES["ins"](), jar, home=Path(".flnd"), origin="cookies.txt")
        with _home.secrets(Path(".flnd"), "tlg") as secrets:
            secrets["api_id"] = "1"
        code, out, _ = self.run_app("login")
        self.assertEqual(code, 0)
        for token in ("Instagram", "logged in", "cookies.txt", "sessionid expired", "Telegram",
                      "api key only", "TikTok", "not logged in"):
            self.assertIn(token, out)
        self.assertNotIn("secret-val", out)
        code, out, _ = self.run_app("login", "--json")
        rows = {row["source"]: row for row in map(json.loads, out.splitlines())}
        self.assertEqual(rows["ins"]["state"], "logged in")
        self.assertNotIn("secret-val", out)


class LogoutTest(FlndCase):
    def test_logout(self):
        _home.save(Path(".flnd"), "ins", _cookies.Jar([{"name": "a", "value": "1",
                                                     "domain": ".x.example"}]))
        code, out, _ = self.run_app("logout", "ins")
        self.assertEqual((code, out), (0, "ins: logged out\n"))
        self.assertEqual(_home.load(Path(".flnd"), "ins"), _cookies.Jar())
        self.assertEqual(self.run_app("logout", "ins")[1], "ins: was not logged in\n")

    def test_logout_takes_one_known_source(self):
        self.assertEqual(self.run_app("logout")[0], 2)
        self.assertEqual(self.run_app("logout", "ytb")[0], 2)
        self.assertEqual(self.run_app("logout", "ins", "twt")[0], 2)


if __name__ == "__main__":
    unittest.main()
