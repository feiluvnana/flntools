"""The flnd command line, command by command: flags in, the library call it makes, lines out.

Every command (SPEC.md#commands-and-flags) runs here, on a pipe and (for the screens) on a
pyte terminal, with its exit code. `RunsCase` replaces the engine's `_Runner.run`, so a command's
flags can be checked against what reaches the engine without a site; `RunTest` runs a whole
`get` of an example plugin over a fake network. No network, no browser, no real site.
"""

from __future__ import annotations

import io
import json
import os
import re
import signal
import sys
import tempfile
import threading
import unittest
from contextlib import redirect_stderr, redirect_stdout
from dataclasses import replace
from datetime import date
from pathlib import Path
from unittest import mock

import pyte
from fakes import (
    CDN,
    kept_login,
    login_cookies,
    make_plugin,
    noise,
    sign_in,
)
from prompt_lines import select_from_lines
from test_library import example_source

from flnd import _library
from flnd import cli as flnd_cli
from flnd.engine import auth as _auth
from flnd.engine import run as _run
from flnd.engine import store as _store
from flnd.engine._engine import _Runner
from flnd.engine.crawl import Options, TargetReport
from flnd.engine.plugins import PLUGINS
from flnd.errors import (
    Blocked,
    Changed,
    DiskError,
    Error,
    LoginError,
    NetworkError,
    NotFound,
    PluginError,
    SiteError,
    Stopped,
    ToolError,
    UsageError,
)
from flnd.plugin import Target
from flnd.ui.log import ui
from flnkit import net as _net
from flnkit.ui import out

NETSCAPE = (
    "# Netscape HTTP Cookie File\n"
    ".instagram.com\tTRUE\t/\tTRUE\t1825861327\tsessionid\tins-session\n"
    "www.instagram.com\tFALSE\t/\tTRUE\t1825861327\tds_user_id\t42\n"
    ".x.com\tTRUE\t/\tTRUE\t1825861327\tauth_token\ttwt-token\n"
)


class Stdin(io.StringIO):
    """A standard input that is a terminal or a pipe, as a test needs."""

    def __init__(self, text="", tty=False):
        super().__init__(text)
        self.tty = tty

    def isatty(self):
        return self.tty


class Terminal(io.StringIO):
    """A stream that is a terminal, so a run draws its screen on it."""

    encoding = "utf-8"

    def isatty(self) -> bool:
        return True


class FlndCase(unittest.TestCase):
    """Runs in a temporary working directory, so the home is `./.flnd`, with stdin a terminal."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        previous = os.getcwd()
        os.chdir(self.tmp.name)
        self.addCleanup(os.chdir, previous)
        self.enterContext(mock.patch.dict(os.environ))
        os.environ.pop("FLND_HOME", None)
        self.dest = io.StringIO()
        self.enterContext(ui.scope(dest=self.dest, width=400, color=False))
        self.stdin = mock.patch("sys.stdin", Stdin(tty=True))
        self.stdin.start()
        self.addCleanup(lambda: self.stdin.stop())
        # What a terminal is asked is recorded, and answered with `self.sure`; a pipe isn't asked.
        self.asked, self.sure = [], True

        def confirm(title, *, default=True):
            if not sys.stdin.isatty():
                return default
            self.asked.append(title)
            return self.sure

        self.enterContext(mock.patch("flnkit.ui.forms.confirm", confirm))
        # The logins folder, outside the home, as `~/.config/flnd/logins` is.
        self.enterContext(mock.patch.object(_auth, "LOGINS", Path(self.tmp.name) / "logins"))

    def pipe(self, text):
        self.stdin.stop()
        self.stdin = mock.patch("sys.stdin", Stdin(text))
        self.stdin.start()

    def answer(self, *lines):
        """Answer the prompts with `lines`, on a terminal."""
        self.stdin.stop()
        self.stdin = mock.patch("sys.stdin", Stdin("".join(f"{line}\n" for line in lines),
                                                    tty=True))
        self.stdin.start()
        patch = select_from_lines(*lines)
        patch.start()
        self.addCleanup(patch.stop)

    def run_app(self, *argv):
        """Run `flnd <argv>`: the exit code, standard output, and standard error with flnd's log."""
        stdout = io.StringIO()
        if type(self.dest) is io.StringIO:
            self.dest.seek(0)
            self.dest.truncate()
        with redirect_stdout(stdout), redirect_stderr(self.dest):
            code = flnd_cli.main(list(argv))
        return code, stdout.getvalue(), self.dest.getvalue()

    def cookie_file(self, text, name="cookies.txt"):
        path = Path(self.tmp.name) / name
        path.write_text(text, encoding="utf-8")
        return path

    def fail_with(self, error, *flags):
        """Run `flnd status` with `Flnd.check` raising `error`: the code, stdout and stderr."""
        with mock.patch("flnd._library.Flnd.check", side_effect=error):
            return self.run_app("status", *flags)


class RunsCase(FlndCase):
    """`_Runner.run` is replaced: each call goes in `runs`, with its first target's options."""

    def setUp(self):
        super().setUp()
        self.runs = []

        async def run(runner, chosen, *, depth=0):
            targets = [target for target, _ in chosen]
            self.runs.append((runner.engine, targets, replace(chosen[0][1], stop_at_saved=False)))
            return [TargetReport(target, (), 0) for target in targets]

        self.enterContext(mock.patch.object(_Runner, "run", run))


class GetTest(RunsCase):
    def test_targets_and_flags_reach_the_engine(self):
        code, _, err = self.run_app("get", "ins:example_user|tagged,reels",
                                    "https://x.com/example_handle", "--when", "2026-01..2026-06",
                                    "--limit", "5")
        self.assertEqual(code, 0, err)
        engine, targets, options = self.runs[0]
        self.assertEqual(targets, [Target("ins:example_user", kind="user"),
                                   Target("twt:example_handle", kind="user")])
        self.assertEqual(options, Options(categories=("reels", "tagged"), limit=5,
                                          since=date(2026, 1, 1), until=date(2026, 6, 30)))
        self.assertEqual(engine.home, Path(".flnd"))

    def test_when_takes_periods_and_ranges(self):
        for value, since, until in (("2026", date(2026, 1, 1), date(2026, 12, 31)),
                                    ("2026-06-15", date(2026, 6, 15), date(2026, 6, 15)),
                                    ("2026-01..", date(2026, 1, 1), None)):
            with self.subTest(value=value):
                self.runs.clear()
                self.assertEqual(self.run_app("get", "tik:a", "--when", value)[0], 0)
                self.assertEqual((self.runs[0][2].since, self.runs[0][2].until), (since, until))

    def test_folder_flag_and_validation(self):
        code, _, err = self.run_app("get", "ins:example_user", "--folder=example_handle/tagged")
        self.assertEqual(code, 0, err)
        self.assertEqual(self.runs[0][2].folder, "example_handle/tagged")

        self.runs.clear()
        code, _, err = self.run_app("get", "twt:example_handle", "--folder", "{target}/{date}")
        self.assertEqual(code, 0, err)
        self.assertEqual(self.runs[0][2].folder, "{target}/{date}")

        # Invalid folder templates fail with exit code 2 before starting any run.
        self.runs.clear()
        for bad in ("..", "a/../b", "/abs", "\\back", "C:drive", "a\x00b", "{nope}",
                    "{bad", "bad}", "{}"):
            with self.subTest(bad=bad):
                code, _, err = self.run_app("get", "ins:example_user", f"--folder={bad}")
                self.assertEqual(code, 2, err)
                self.assertNotIn("Traceback", err)
        self.assertEqual(self.runs, [])

    def test_a_mistake_is_a_usage_error_before_anything_starts(self):
        for argv in (("ytb:nasa",), ("tik:7312345678901234567|videos",), ("ins:a|videos",),
                     ("ins:a", "--limit", "0"), ("ins:a", "--limit", "five"),
                     ("ins:a", "--when", "yesterday"), ("ins:a", "--nope"),
                     ("ins:a", "--exec", "no-such-program-here {path}"),
                     ("ins:a", "--exec", "echo 'unclosed")):
            with self.subTest(argv=argv):
                code, _, err = self.run_app("get", *argv)
                self.assertEqual(code, 2)
                self.assertNotIn("Traceback", err)
        self.assertEqual(self.runs, [])
        self.assertFalse((Path(".flnd") / _store.MEDIA).exists())

    def test_a_dash_reads_the_targets_from_stdin(self):
        self.pipe("ins:a\n\n# a comment\ntwt:b\n")
        self.assertEqual(self.run_app("get", "-")[0], 0)
        self.assertEqual([str(target) for target in self.runs[0][1]], ["ins:a", "twt:b"])
        # A dash may come with other targets, and stands where it is written.
        self.runs.clear()
        self.pipe("ins:a\n")
        self.assertEqual(self.run_app("get", "tik:c", "-", "twt:d")[0], 0)
        self.assertEqual([str(target) for target in self.runs[0][1]], ["tik:c", "ins:a", "twt:d"])

    def test_stdin_is_not_read_without_a_dash(self):
        class Blocked(Stdin):
            def __iter__(self):
                raise AssertionError("stdin was read")

            read = readline = readlines = __iter__

        self.stdin.stop()
        self.stdin = mock.patch("sys.stdin", Blocked("ins:a\n"))
        self.stdin.start()
        self.assertEqual(self.run_app("get", "tik:c")[0], 0)
        self.assertEqual([str(target) for target in self.runs[0][1]], ["tik:c"])

    def test_get_needs_a_target(self):
        for argv in ((), ("--limit", "3"), ("-",)):
            with self.subTest(argv=argv):
                self.pipe("\n# nothing\n")
                code, _, err = self.run_app("get", *argv)
                self.assertEqual(code, 2)
                self.assertIn("flnd get needs a target; flnd sync syncs the followed ones", err)
        self.assertEqual(self.runs, [])

    def test_sync_with_no_pattern_syncs_the_followed_ones_or_says_there_are_none(self):
        code, out, err = self.run_app("sync")
        self.assertEqual((code, self.runs, out), (0, [], "nothing to sync\n"), err)
        self.run_app("get", "ins:example_user|reels")
        self.run_app("get", "tik:example_creator")
        self.runs.clear()
        self.assertEqual(self.run_app("sync")[0], 0)
        self.assertEqual({str(t) for t in self.runs[0][1]},
                         {"ins:example_user", "tik:example_creator"})

    def test_global_flags_and_the_home(self):
        self.assertEqual(self.run_app("--home", "one", "get", "ins:a")[0], 0)
        self.assertEqual(self.run_app("get", "ins:a", "--home", "two")[0], 0)
        os.environ["FLND_HOME"] = "from-env"
        self.assertEqual(self.run_app("get", "ins:a")[0], 0)
        self.assertEqual([run[0].home for run in self.runs],
                         [Path("one"), Path("two"), Path("from-env")])

    def test_the_run_leaves_a_journal(self):
        code, out, _ = self.run_app("get", "ins:a")
        self.assertEqual(code, 0)
        [journal] = (Path(".flnd") / _store.LOGS).glob("*_get.jsonl")
        types = {json.loads(line)["type"] for line in journal.read_text("utf-8").splitlines()}
        self.assertLessEqual({"Start", "Report"}, types)
        self.assertIn("done in", out)

    def test_a_short_link_is_resolved_once(self):
        followed = []

        def follow(url):
            followed.append(url)
            return "https://www.tiktok.com/@example_creator/video/7301234567890123456"

        with mock.patch("flnkit.net.follow", follow):
            self.assertEqual(self.run_app("get", "https://www.tiktok.com/example_creator")[0], 0)
        self.assertEqual(followed, ["https://www.tiktok.com/example_creator"])
        self.assertEqual(self.runs[0][1], [Target("tik:post/7301234567890123456", kind="post")])


class SyncCommandTest(RunsCase):
    def test_sync_matches_followed_targets_by_glob_patterns(self):
        code, out, err = self.run_app("sync")
        self.assertEqual((code, self.runs, out), (0, [], "nothing to sync\n"), err)

        self.run_app("get", "ins:example_user|reels")
        self.run_app("get", "ins:example_other")
        self.run_app("get", "tik:example_creator")
        self.run_app("get", "twt:a1")
        # Unfollow one target so we verify sync skips unfollowed targets.

        with _library.open() as fl:
            fl.targets.unfollow("ins:example_other")

        # No pattern syncs every followed target.
        self.runs.clear()
        code, _, err = self.run_app("sync")
        self.assertEqual(code, 0, err)
        self.assertEqual({str(t) for t in self.runs[0][1]},
                         {"ins:example_user", "tik:example_creator", "twt:a1"})

        # Glob `*` matches prefix/plugin (case-insensitive) and skips unfollowed targets.
        self.runs.clear()
        code, _, err = self.run_app("sync", "INS:*")
        self.assertEqual(code, 0, err)
        self.assertEqual([str(t) for t in self.runs[0][1]], ["ins:example_user"])
        self.assertTrue(list((Path(".flnd") / _store.LOGS).glob("*_sync.jsonl")))

        # Multiple patterns with `*`, `?`, `[seq]` combine without duplicates.
        self.runs.clear()
        code, _, err = self.run_app("sync", "*creator*", "twt:a[0-9]", "tik:*")
        self.assertEqual(code, 0, err)
        self.assertEqual({str(t) for t in self.runs[0][1]},
                         {"tik:example_creator", "twt:a1"})

        # Flags (`--when`, `--limit`, `--again`) reach the engine.
        self.runs.clear()
        code, _, err = self.run_app("sync", "ins:*", "--when", "2026-01..2026-06",
                                    "--limit", "5", "--again")
        self.assertEqual(code, 0, err)
        self.assertEqual([str(t) for t in self.runs[0][1]], ["ins:example_user"])
        self.assertEqual(self.runs[0][2], Options(
            categories=("reels",), limit=5, again=True,
            since=date(2026, 1, 1), until=date(2026, 6, 30),
        ))

        # No match prints `nothing to sync` and starts no run.
        self.runs.clear()
        code, out, err = self.run_app("sync", "nope:*")
        self.assertEqual((code, self.runs, out), (0, [], "nothing to sync\n"), err)

    def test_sync_usage_errors(self):
        for argv in (("",), ("   ",), ("ins:*", "--limit", "0"), ("ins:*", "--when", "yesterday")):
            with self.subTest(argv=argv):
                code, _, err = self.run_app("sync", *argv)
                self.assertEqual(code, 2, err)
                self.assertNotIn("Traceback", err)
        self.assertEqual(self.runs, [])


class ExampleCase(FlndCase):
    """An example plugin, `exs`, on a fake network: the CDN answers from a fake transport."""

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
        from flnkit import clock as _time

        self.enterContext(_time.use(_time.Fake()))
        self.listing = {key: list(ids) for key, ids in type(self).listing.items()}
        self.enterContext(mock.patch.dict(PLUGINS, {"exs": example_source(self.listing)}))


class RunTest(ExampleCase):
    """A real `get`, and the commands that read what it left."""

    def test_the_summary_and_a_second_get_that_skips(self):
        code, out, err = self.run_app("get", "exs:example_user")
        self.assertEqual(code, 0, err)
        self.assertRegex(out.splitlines()[-1], r"^  v done in .+ - 1 target - 3 saved \(.+\) - "
                                               r"0 already saved - 0 failed$")
        code, out, _ = self.run_app("get", "exs:example_user")
        self.assertRegex(out.splitlines()[-1], r" - 0 saved - 3 already saved - 0 failed$")

    def test_dry_plans_and_writes_nothing(self):
        code, out, _ = self.run_app("get", "exs:example_user", "--dry")
        self.assertEqual(code, 0)
        self.assertEqual(len([line for line in out.splitlines() if line.startswith("new ")]), 3)
        self.assertEqual(out.splitlines()[-1], "planned 3 files; nothing was saved")
        self.assertFalse((Path(".flnd") / _store.MEDIA).exists())

    def test_the_run_draws_its_screen_on_a_pyte_terminal(self):
        self.dest = Terminal()
        # The screen reads the live terminal's size, not `COLUMNS`: this one is 120 by 40.
        with mock.patch("os.get_terminal_size", return_value=os.terminal_size((120, 40))):
            code, _, err = self.run_app("get", "exs:example_user")
        self.assertEqual(code, 0)
        term = pyte.Screen(120, 40)
        pyte.Stream(term).feed(err.replace("\n", "\r\n"))
        rows = [row.rstrip() for row in term.display]
        self.assertTrue(any("flnd get · 1 target" in row for row in rows), rows)
        self.assertTrue(any(re.search(r"✓ exs:example_user +posts reels +3 +0 +0 ", row)
                            for row in rows), rows)

    def test_a_failed_file_exits_1_and_status_lists_it(self):
        self.listing[("example_user", "posts")] = ["p1", "bad"]
        code, out, err = self.run_app("get", "exs:example_user")
        self.assertEqual(code, 1, err)
        self.assertRegex(out, r"^  x done in .+ - 2 saved \(.+\) - 0 already saved - 1 failed\n$")
        self.assertEqual(err.splitlines()[-1], "  details  flnd status")
        code, out, _ = self.run_app("status")
        self.assertEqual(code, 1)
        lines = out.splitlines()
        self.assertRegex(lines[0], r"^last run: get exs:example_user, .+: 2 saved, 1 failed$")
        self.assertIn("  exs:example_user  1 failure (site): ", lines[1])
        self.assertEqual(lines[-1], "disk: 2 files: history and disk agree")

    def test_the_target_commands(self):
        self.run_app("get", "exs:example_user")
        self.run_app("get", "exs:p/9")
        code, out, _ = self.run_app("target", "list")
        self.assertEqual(code, 0)
        lines = out.splitlines()
        self.assertEqual(lines[0].split(),
                         ["target", "followed", "categories", "status", "files", "size",
                          "last", "run"])
        followed = {line.split()[0]: "followed" in line for line in lines[1:]}
        self.assertEqual(followed, {"exs:example_user": True, "exs:9": False})
        self.assertIn("synced", lines[1])
        code, out, _ = self.run_app("target", "clean", "exs:example_user", "--media")
        self.assertEqual((code, out), (0, "exs:example_user: forgotten with its files\n"))
        self.assertFalse(any(Path(".flnd/media").rglob("p1*")))
        code, _, err = self.run_app("target", "clean", "exs:nobody")
        self.assertEqual(code, 2)
        self.assertIn("not a known target", err)
        self.assertEqual(self.run_app("target", "clean")[0], 2)

    def test_follow_unfollow_and_retry_name_their_targets(self):
        self.run_app("get", "exs:example_user")
        self.assertEqual(self.run_app("target", "unfollow", "exs:example_user")[:2],
                         (0, "exs:example_user: unfollowed\n"))
        self.assertIn("-", self.run_app("target", "list")[1].splitlines()[1].split()[1])
        self.assertEqual(self.run_app("target", "follow", "exs:example_user")[:2],
                         (0, "exs:example_user: followed\n"))
        self.assertIn("followed", self.run_app("target", "list")[1].splitlines()[1])
        self.assertEqual(self.run_app("target", "retry", "exs:example_user")[:2],
                         (0, "exs:example_user: failed files will be retried\n"))
        for verb in ("follow", "unfollow", "retry"):
            with self.subTest(verb):
                self.assertEqual(self.run_app("target", verb)[0], 2)

    def test_target_list_reports_new_partial_failed_and_retry_statuses(self):
        from flnd.plugin import Target

        with _store.open(".flnd") as db:
            db.follow(Target("exs:brand_new", kind="user"), True)
            partial = Target("exs:cut_short", kind="user")
            db.record(partial, follow=True)
            db.progress(partial, cursors={"posts": "c1"}, complete=[])
            failed = Target("exs:broken_file", kind="user")
            db.record(failed, follow=True)
            db.progress(failed, cursors={}, complete=["posts", "reels"])
            db.fail(run=None, item="exs:p1", target="exs:broken_file",
                    error=RuntimeError("boom"), post="p1", category="posts")
            retry = Target("exs:needs_retry", kind="user")
            db.record(retry, follow=True)
            for _ in range(_store.RETRIES + 1):
                db.fail(run=None, item="exs:p2", target="exs:needs_retry",
                        error=RuntimeError("boom"), post="p2", category="posts")
        rows = {line.split()[0]: line for line in
                self.run_app("target", "list")[1].splitlines()[1:]}
        for name, state in (("brand_new", "new"), ("cut_short", "partial"),
                            ("broken_file", "1 failed"), ("needs_retry", "1 retry")):
            with self.subTest(name):
                self.assertIn(state, rows[f"exs:{name}"])

    def test_a_terminal_is_asked_before_a_forget_and_a_pipe_is_not(self):
        self.run_app("get", "exs:example_user", "exs:p/9")
        self.sure = False
        code, text, _ = self.run_app("target", "clean", "exs:example_user", "--media")
        self.assertEqual((code, text), (0, "kept: nothing was forgotten\n"))
        self.assertRegex(self.asked[-1], r"^Forget 1 target and delete [\d.]+ B\?$")
        self.assertEqual(len(self.run_app("target", "list")[1].splitlines()), 3)
        self.sure = True
        self.assertEqual(self.run_app("target", "clean", "exs:example_user")[0], 0)
        self.assertEqual(self.asked[-1], "Forget 1 target?")
        self.pipe("")
        self.asked.clear()
        self.assertEqual(self.run_app("target", "clean", "exs:9")[0], 0)
        self.assertEqual((self.asked, self.run_app("target", "list")[1].split()[7:]),
                         ([], ["run"]))


class HomeLockTest(ExampleCase):
    """A second command on a home another run is using stops at once, with exit status 2."""

    def test_get_on_a_locked_home_exits_2_and_follow_still_works(self):
        from flnkit import files

        with files.lock(Path(".flnd") / ".lock") as free:
            self.assertTrue(free)
            code, _, err = self.run_app("get", "exs:example_user")
            self.assertEqual(code, 2)
            self.assertIn("another flnd run is using .flnd", err)
            self.assertIn("wait for it to finish, or use another --home", err)
            self.assertEqual(self.run_app("target", "follow", "exs:example_user")[0], 0)
        self.assertEqual(self.run_app("get", "exs:example_user")[0], 0)


class FormsTest(ExampleCase):
    """What a terminal is asked, in forms (`flnd.ui.forms`): status's repair, the login."""

    def test_status_offers_to_repair_the_rows_a_terminal_can_and_a_pipe_cannot(self):
        import time

        self.run_app("get", "exs:example_user")
        next(Path(".flnd/media").rglob("*p1*")).unlink()
        aged = mock.patch("flnd.engine.store.time.time",
                          return_value=time.time() + 2 * _store.SETTLING)
        with aged:
            self.sure = False
            code, text, _ = self.run_app("status")
            self.assertEqual((code, self.asked), (1, ["Repair 1 row?"]))
            self.assertIn("disk: 3 files: 1 missing", text)
            self.pipe("")
            self.assertEqual(self.run_app("status")[0], 1)
            self.assertEqual(self.asked, ["Repair 1 row?"])
            self.stdin.stop()
            self.stdin = mock.patch("sys.stdin", Stdin(tty=True))
            self.stdin.start()
            self.sure = True
            code, text, _ = self.run_app("status")
            self.assertEqual(code, 0)
            self.assertEqual(text.splitlines()[-1], "disk: 3 files: repaired 1 missing; "
                             "the history now matches the disk")
            self.assertEqual(self.run_app("status")[0], 0)

    def test_status_fix_repairs_without_asking(self):
        import time

        self.run_app("get", "exs:example_user")
        next(Path(".flnd/media").rglob("*p1*")).unlink()
        aged = mock.patch("flnd.engine.store.time.time",
                          return_value=time.time() + 2 * _store.SETTLING)
        with aged:
            self.sure = False
            code, text, _ = self.run_app("status", "--fix")
            self.assertEqual((code, self.asked), (0, []))
            self.assertEqual(text.splitlines()[-1], "disk: 3 files: repaired 1 missing; "
                             "the history now matches the disk")
            self.assertEqual(self.run_app("status")[0], 0)

    def test_a_cookie_site_asks_its_file_without_a_menu(self):
        def select(title, options, **_):
            raise AssertionError(f"no menu for a cookie site: {title}")

        with mock.patch("flnkit.ui.forms.select", select), \
                mock.patch("flnkit.ui.forms.text", return_value=str(self.cookie_file(NETSCAPE))):
            code, text, err = self.run_app("auth", "login", "twt")
        self.assertEqual(code, 0, err)
        self.assertRegex(text, r"^twt: logged in \(ok\)")


class ExitStatusTest(FlndCase):
    """A target that fails exits 1, so a script or cron job can tell; a login needed exits 3."""

    def setUp(self):
        super().setUp()

        def connect(self, ctx):
            raise RuntimeError("the saved login no longer works")

        def run(ctx):
            if ctx.target.value == "bad":
                raise Blocked("blocked")

        self.enterContext(mock.patch.dict(PLUGINS, {
            "aaa": make_plugin("aaa", run), "bbb": make_plugin("bbb", run, connect=connect),
            "ccc": make_plugin("ccc", login=login_cookies("sid", required=True))}))

    def test_a_failed_target_exits_1_in_a_get_and_in_a_sync(self):
        code, out, err = self.run_app("get", "aaa:bad", "aaa:good")
        self.assertEqual(code, 1, err)
        self.assertRegex(out, r" - 2 targets - 0 saved - 0 already saved - 1 failed\n$")
        self.assertIn("  x aaa:bad\n    blocked\n", err)
        self.assertEqual(self.run_app("get", "aaa:good")[0], 0)
        self.assertEqual(self.run_app("sync")[0], 1)
        self.assertEqual(self.run_app("get", "bbb:good")[0], 1)

    def test_a_login_the_plugin_needs_exits_3_with_the_command_to_run(self):
        code, out, err = self.run_app("get", "ccc:example_user")
        self.assertEqual(code, 3)
        self.assertRegex(out, r" - 1 failed\n$")
        self.assertIn("  hint  run flnd auth login ccc", err)


class ErrorsTest(FlndCase):
    def test_each_class_has_its_exit_code(self):
        cases = [(UsageError("bad flag"), 2), (LoginError("not logged in"), 3),
                 (SiteError("refused"), 1), (Blocked("wait"), 1), (NotFound("gone"), 1),
                 (Changed("new shape"), 1), (NetworkError("reset"), 1), (DiskError("full"), 4),
                 (ToolError("no ffmpeg"), 4), (PluginError("bug"), 4), (Error("other"), 1),
                 (Stopped("stopped"), 130), (RuntimeError("bug"), 4)]
        for error, code in cases:
            with self.subTest(type(error).__name__):
                self.assertEqual(self.fail_with(error)[0], code)
                self.assertEqual(flnd_cli.exit_code(error), code)
        self.assertEqual(flnd_cli.exit_code(None), 0)
        # A bare Ctrl-C ends the command with 130 too, though only `Stopped` has that code.
        self.assertEqual(self.fail_with(KeyboardInterrupt())[0], 130)

    def test_a_persons_mistake_is_one_line_and_a_hint_never_a_traceback(self):
        for error in (UsageError("give a target", hint="flnd get ins:example_user"),
                      LoginError("ins: not logged in", hint="run flnd auth login ins"),
                      DiskError("media: No space left on device", hint="free some space"),
                      ToolError("ffmpeg: executable not found", hint="install ffmpeg")):
            with self.subTest(type(error).__name__):
                _, stdout, err = self.fail_with(error)
                self.assertEqual(stdout, "")
                self.assertEqual(err.splitlines(), [f"x {error}", f"  hint  {error.hint}"])
                self.assertFalse((Path(".flnd") / _store.LOGS).exists())

    def test_a_flag_mistake_exits_2_as_a_usage_error(self):
        code, _, err = self.run_app("get", "--nope")
        self.assertEqual(code, 2)
        self.assertEqual(err.splitlines(), ["x flnd: unrecognized arguments: --nope",
                                            "  hint  flnd -h lists what it takes"])
        for argv in (("download",), ("target", "ls"), ("auth", "login"), ("sync")):
            with self.subTest(argv=argv):
                self.assertEqual(self.run_app(*argv)[0], 2)

    def test_a_bug_writes_a_crash_file_and_exits_4(self):
        code, _, err = self.fail_with(RuntimeError("an example bug"), "--home", "elsewhere")
        self.assertEqual(code, 4)
        [crash] = (Path("elsewhere") / _store.LOGS).glob("crash-*.txt")
        self.assertIn("Traceback", crash.read_text(encoding="utf-8"))
        self.assertEqual(err.splitlines(), ["x flnd hit a bug: RuntimeError: an example bug",
                                            f"  hint  details in {crash}"])

    def test_with_verbose_a_bug_prints_its_traceback_instead(self):
        code, _, err = self.fail_with(RuntimeError("an example bug"), "-v")
        self.assertEqual(code, 4)
        self.assertIn("RuntimeError: an example bug", err)
        self.assertFalse((Path(".flnd") / _store.LOGS).exists())


class HelpTest(FlndCase):
    class Pipe(io.StringIO):
        encoding = "utf-8"

    class Ascii(Terminal):
        encoding = "cp1252"

    def test_flnd_h_fits_in_24_lines_of_80_columns(self):
        self.enterContext(mock.patch.dict(os.environ, {"COLUMNS": "80"}))
        for kind in (self.Pipe, Terminal, self.Ascii):
            with self.subTest(kind.__name__):
                stdout = kind()
                with mock.patch("sys.stdout", stdout), redirect_stderr(io.StringIO()):
                    self.assertEqual(flnd_cli.main(["-h"]), 0)
                lines = out.clean(stdout.getvalue()).splitlines()
                self.assertLessEqual(len(lines), 24, "\n".join(lines))
                self.assertTrue(all(len(line) <= 80 for line in lines), "\n".join(lines))

    def test_help_lists_the_commands_and_every_plugin(self):
        code, text, _ = self.run_app("--help")
        self.assertEqual(code, 0)
        self.assertIn("flnd 0.2.0 - download whole accounts from the web", text)
        for verb in ("get ", "target list|follow|unfollow|retry|clean", "auth list|login|logout",
                     "status", "config"):
            self.assertIn(f"  {verb}", text)
        self.assertIn("tik fbk ins thr twt loi mht", text)
        self.assertEqual(self.run_app()[1], text)

    def test_get_help_has_a_row_per_plugin_and_its_flags(self):
        code, text, _ = self.run_app("get", "--help")
        self.assertEqual(code, 0)
        flat = " ".join(text.split())
        self.assertIn("|posts,reels,tagged", flat)
        for flag in ("--when", "--limit", "--dry", "--again", "--home"):
            self.assertIn(flag, text)
        self.assertNotIn("--json", text)
        self.assertEqual(self.run_app("--json", "status")[0], 2)
        self.assertNotIn("Args:", text)

    def test_a_group_lists_its_commands(self):
        for group, names in (("target", ("list", "follow", "unfollow", "retry", "clean")),
                             ("auth", ("list", "login", "logout"))):
            with self.subTest(group):
                code, text, _ = self.run_app(group)
                self.assertEqual(code, 0)
                for name in names:
                    self.assertIn(f"  {name}", text)

    def test_help_imports_no_plugin_engine_or_network(self):
        import subprocess

        code = ("import sys\nfrom flnd.cli import main\nmain(['--help'])\n"
                "print(sorted(m for m in sys.modules if m.startswith(("
                "'flnd.engine._engine', 'flnd.plugins.ins', 'curl_cffi'))))")
        stdout = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True,
                                check=True).stdout.splitlines()[-1]
        self.assertEqual(stdout, "[]")

    def test_the_version(self):
        self.assertEqual(self.run_app("--version")[:2], (0, "flnd 0.2.0\n"))


class ListsTest(FlndCase):
    def test_status_config_and_the_lists_of_a_home_with_nothing_in_it(self):
        self.assertEqual(self.run_app("status")[:2], (0, "no run yet\ndisk: 0 files: "
                                                         "history and disk agree\n"))
        self.assertEqual(self.run_app("target", "list")[1].split(),
                         ["target", "followed", "categories", "status", "files", "size",
                          "last", "run"])
        self.assertEqual(self.run_app("auth", "list")[1], "no logins\n")
        code, text, _ = self.run_app("config")
        self.assertEqual(code, 0)
        self.assertEqual(text.splitlines()[0].split(), ["key", "value", "default", "from"])
        self.assertRegex(text, r"defaults\.when +- +- +default")
        self.assertRegex(text.splitlines()[-1], r"^edit .*config\.toml for this home, or .*config\.toml "
                                                r"for every home$")
        self.assertEqual(text.splitlines()[1].split()[0], "defaults.when")
        self.assertFalse((Path(".flnd") / _store.DB).exists())


class AuthTest(FlndCase):
    def test_login_by_file_keeps_only_the_plugins_cookies(self):
        code, text, err = self.run_app("auth", "login", "ins", str(self.cookie_file(NETSCAPE)))
        self.assertEqual(code, 0, err)
        self.assertRegex(text, r"^ins: logged in \(ok\)\n$")
        self.assertEqual({cookie["name"]: cookie["domain"]
                          for cookie in kept_login("ins")[0].list},
                         {"sessionid": ".instagram.com", "ds_user_id": ".www.instagram.com"})
        self.assertEqual(self.run_app("auth", "login", "twt", str(self.cookie_file(NETSCAPE)))[0],
                         0)
        # The logins are in the logins folder, never in the home.
        self.assertEqual(sorted(path.name for path in _auth.folder().iterdir()),
                         ["ins.txt", "twt.txt"])
        self.assertEqual([path.name for path in Path(".flnd").rglob("*.txt")], [])

    def test_list_and_logout(self):
        import time

        sign_in("ins", [{"name": "sessionid", "value": "secret-val",
                         "domain": ".instagram.com", "expires": time.time() - 10}])
        sign_in("twt", session="s")
        code, text, _ = self.run_app("auth", "list")
        self.assertEqual(code, 0)
        self.assertRegex(text, r"ins +expired +\S*ins\.txt")
        self.assertRegex(text, r"twt +ok +\S*twt\.json")
        self.assertNotIn("secret-val", text)
        self.assertEqual(self.run_app("auth", "logout", "ins")[1], "ins: logged out\n")
        self.assertEqual(kept_login("ins")[0].list, [])
        self.assertEqual([row.plugin for row in _flnd_auth_rows()], ["twt"])
        for argv in (("ins",), ("ytb",)):
            with self.subTest(argv=argv):
                self.assertEqual(self.run_app("auth", "logout", *argv)[0], 2)

    def test_an_empty_list_says_so(self):
        self.assertEqual(self.run_app("auth", "list")[1], "no logins\n")

    def test_a_logout_asks_a_terminal_and_a_declined_one_keeps_the_login(self):
        sign_in("ins", [{"name": "a", "value": "1", "domain": ".x.example"}])
        self.sure = False
        self.assertEqual(self.run_app("auth", "logout", "ins")[1], "kept: nobody was logged out\n")
        self.assertEqual(self.asked, ["Log out of ins?"])
        self.assertIn("ins", self.run_app("auth", "list")[1])
        self.pipe("")
        self.assertEqual(self.run_app("auth", "logout", "ins")[1], "ins: logged out\n")

    def test_a_missing_cookie_exits_3_and_a_wrong_way_or_plugin_exits_2(self):
        code, _, err = self.run_app("auth", "login", "fbk", str(self.cookie_file(NETSCAPE)))
        self.assertEqual(code, 3)
        self.assertIn("fbk: no login cookie (c_user, xs) in cookies.txt", err)
        self.assertIn("export the cookies from a browser where you are logged in", err)
        self.assertNotIn("Traceback", err)
        self.assertEqual(self.run_app("auth", "login", "ins", "no-such-file.txt")[0], 2)
        self.assertEqual(self.run_app("auth", "login", "ytb", "chrome")[0], 2)

    def test_an_expired_file_exits_3(self):
        old = NETSCAPE.replace("1825861327", "1000000000")
        code, _, err = self.run_app("auth", "login", "ins", str(self.cookie_file(old)))
        self.assertEqual(code, 3)
        self.assertIn("ins: the login in cookies.txt has expired", err)
        self.assertIn("export a fresh cookie file", err)

    def test_the_file_is_asked_on_a_terminal_and_never_on_a_pipe(self):
        path = self.cookie_file(NETSCAPE)
        self.answer(str(path))
        code, text, err = self.run_app("auth", "login", "twt")
        self.assertEqual(code, 0, err)
        self.assertRegex(text, r"^twt: logged in \(ok\)\n$")
        self.pipe("")
        code, _, err = self.run_app("auth", "login", "ins")
        self.assertEqual(code, 2)
        self.assertIn("flnd auth login ins <file>", err)


def _flnd_auth_rows():
    """The logins `flnd auth list` shows, read through the library over `./.flnd`."""

    with _library.open(".flnd") as fl:
        return list(fl.auth)


class StoppingTest(unittest.TestCase):
    def test_two_stage_ctrl_c_sets_soon_then_now(self):
        previous = signal.getsignal(signal.SIGINT)
        with _run.stopping() as stop:
            self.assertFalse(stop.soon.is_set())
            signal.raise_signal(signal.SIGINT)
            self.assertTrue(stop.soon.is_set())
            self.assertFalse(stop.now.is_set())
            signal.raise_signal(signal.SIGINT)
            self.assertTrue(stop.now.is_set())
        self.assertIs(signal.getsignal(signal.SIGINT), previous)

    def test_stopping_in_worker_thread_is_safe(self):
        results = []

        def worker():
            with _run.stopping() as stop:
                results.append((stop.soon.is_set(), stop.now.is_set()))

        thread = threading.Thread(target=worker)
        thread.start()
        thread.join()
        self.assertEqual(results, [(False, False)])


class PriorityTest(unittest.TestCase):
    def test_lower_priority_posix_and_windows(self):
        from types import SimpleNamespace

        from flnd import cli

        calls = []

        def fake_nice(inc):
            calls.append(inc)
            return 0 if inc == 0 else inc

        for plat in ("darwin", "linux"):
            calls.clear()
            with mock.patch.object(cli.sys, "platform", plat), \
                    mock.patch.object(cli.os, "nice", fake_nice, create=True):
                cli._lower_priority()
            self.assertEqual(calls, [0, 5])

        set_calls = []
        fake_kernel = SimpleNamespace(
            GetCurrentProcess=lambda: 1,
            GetPriorityClass=lambda h: 0x20,
            SetPriorityClass=lambda h, level: set_calls.append(level),
        )
        with mock.patch.object(cli.sys, "platform", "win32"), \
                mock.patch("ctypes.windll", SimpleNamespace(kernel32=fake_kernel), create=True):
            cli._lower_priority()
        self.assertEqual(set_calls, [0x4000])


if __name__ == "__main__":
    unittest.main()
