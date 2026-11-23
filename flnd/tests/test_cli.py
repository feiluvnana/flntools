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
    login_cookies,
    make_plugin,
    noise,
    own_login,
    sign_in,
    vaulted,
)
from prompt_lines import select_from_lines
from test_library import example_source

from flnd import Blocked, Target
from flnd import cli as flnd_cli
from flnd.engine import run as _run
from flnd.engine import store as _store
from flnd.engine._engine import _Runner
from flnd.engine.crawl import Options, TargetReport
from flnd.engine.plugins import PLUGINS
from flnd.errors import (
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
from flnd.plugin import Plugin, Post, route
from flnd.ui import forms, out
from flnd.ui.log import ui
from flnkit import net as _net

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

        self.enterContext(mock.patch("flnd.ui.forms.confirm", confirm))
        ask = forms.ask

        def named(message, **options):
            # A login's name is taken as offered, as enter takes it; other questions are asked.
            return options["default"] if message == "Account name" else ask(message, **options)

        self.enterContext(mock.patch("flnd.ui.forms.ask", named))

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

        # With no targets and --folder, followed targets run with that folder.
        self.runs.clear()
        self.pipe("")
        code, _, err = self.run_app("get", "--folder=synced")
        self.assertEqual(code, 0, err)
        self.assertEqual(self.runs[0][2].folder, "synced")

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

    def test_targets_from_stdin_join_the_arguments(self):
        self.pipe("ins:a\n\n# a comment\ntwt:b\n")
        self.assertEqual(self.run_app("get", "tik:c")[0], 0)
        self.assertEqual([str(target) for target in self.runs[0][1]], ["tik:c", "ins:a", "twt:b"])

    def test_no_target_syncs_the_followed_ones_or_says_there_are_none(self):
        self.pipe("")
        code, out, err = self.run_app("get")
        self.assertEqual((code, self.runs, out), (0, [], "nothing to sync\n"), err)
        self.run_app("get", "ins:example_user|reels")
        self.run_app("get", "tik:example_creator")
        self.runs.clear()
        self.pipe("")
        self.assertEqual(self.run_app("get")[0], 0)
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
        import flnd

        with flnd.open() as fl:
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

    def test_json_lines(self):
        code, out, _ = self.run_app("--json", "get", "exs:example_user|reels")
        self.assertEqual(code, 0)
        rows = [json.loads(line) for line in out.splitlines()]
        self.assertEqual([row["post"] for row in rows if row["type"] == "Saved"], ["r1"])
        self.assertEqual((rows[-1]["type"], rows[-1]["saved"]), ("Report", 1))

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
        status = json.loads(self.run_app("--json", "status")[1])
        self.assertEqual((status["type"], status["status"]["saved"]), ("Status", 2))
        [failure] = status["status"]["failures"]
        self.assertEqual((failure["post"], failure["error"]["class"]), ("bad", "SiteError"))

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
        rows = [json.loads(line) for line in self.run_app("--json", "target",
                                                          "list")[1].splitlines()]
        self.assertIn({"text": "exs:example_user", "kind": "user"}, [row["target"] for row in rows])
        self.assertEqual(rows[0]["status"], "synced")
        code, out, _ = self.run_app("target", "clean", "exs:example_user", "--media")
        self.assertEqual((code, out), (0, "exs:example_user: forgotten with its files\n"))
        self.assertFalse(any(Path(".flnd/media").rglob("p1*")))
        code, _, err = self.run_app("target", "clean", "exs:nobody")
        self.assertEqual(code, 2)
        self.assertIn("not a known target", err)
        self.assertEqual(self.run_app("target", "clean")[0], 2)

    def test_a_terminal_is_asked_before_a_forget_and_a_pipe_is_not(self):
        self.run_app("get", "exs:example_user", "exs:p/9")
        self.sure = False
        code, text, _ = self.run_app("target", "clean", "exs:example_user", "--media")
        self.assertEqual((code, text), (0, "kept: nothing was forgotten\n"))
        self.assertRegex(self.asked[-1], r"^Forget 1 target and delete [\d.]+ B\?$")
        self.assertEqual(len(self.run_app("--json", "target", "list")[1].splitlines()), 2)
        self.sure = True
        self.assertEqual(self.run_app("target", "clean", "exs:example_user")[0], 0)
        self.assertEqual(self.asked[-1], "Forget 1 target?")
        self.pipe("")
        self.asked.clear()
        self.assertEqual(self.run_app("target", "clean", "exs:9")[0], 0)
        self.assertEqual((self.asked, self.run_app("target", "list")[1].split()[7:]),
                         ([], ["run"]))


class WiringTest(ExampleCase):
    """On a terminal each list gets its rows and keys from the library (`flnd.ui.lists`)."""

    def listed(self, script, *argv):
        """Run `flnd <argv>` with `lists.show` replaced by `script(title, load, keys, delete)`,
        which runs while the home is open, as the real list would."""
        from flnd.ui import lists

        def show(title, columns, load, *, keys=(), delete=None):
            script(title, load, {key.char: key for key in keys}, delete)

        with mock.patch.object(lists, "show", show):
            code, _, err = self.run_app(*argv)
        self.assertEqual(code, 0, err)

    def test_the_target_list_follows_retries_adds_and_forgets(self):
        self.run_app("get", "exs:example_user")

        def script(title, load, keys, delete):
            [row] = [each.key for each in load()]
            self.assertEqual((title, row.name, row.followed, row.files, row.status),
                             ("Targets", "exs:example_user", True, 3, "synced"))
            keys["f"].run(row)
            self.assertFalse(load()[0].key.followed)
            keys["f"].run(load()[0].key)
            self.assertTrue(load()[0].key.followed)
            keys["r"].run(row)
            with mock.patch("flnd.ui.forms.text", return_value="exs:p/9"):
                keys["a"].run(None)
            self.assertEqual(len(load()), 2)
            delete.run(row)
            self.assertEqual([each.key.name for each in load()], ["exs:9"])

        self.listed(script, "target", "list")

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
        rows = {
            row["target"]["text"]: (row["status"], row["failed"], tuple(row["pending"]))
            for row in (json.loads(line) for line in
                        self.run_app("--json", "target", "list")[1].splitlines())
        }
        self.assertEqual(rows, {
            "exs:brand_new": ("new", 0, ("posts", "reels")),
            "exs:cut_short": ("partial", 0, ("posts", "reels")),
            "exs:broken_file": ("failed", 1, ()),
            "exs:needs_retry": ("retry", 1, ("posts", "reels")),
        })

    def test_the_account_list_sets_the_default_and_logs_out(self):
        sign_in(".flnd", "ins", [{"name": "a", "value": "1", "domain": ".x.example"}], name="one")
        sign_in(".flnd", "ins", [{"name": "a", "value": "1", "domain": ".x.example"}], name="two")
        self.answer(str(list(PLUGINS["ins"].login.methods).index("file") + 1),
                    str(self.cookie_file(NETSCAPE)))

        def script(title, load, keys, delete):
            def defaults():
                return {each.key.name: each.key.default for each in load()}

            self.assertEqual((defaults(), load()[0].key.origin), ({"one": True, "two": False},
                                                                  "file"))
            keys["*"].run([each.key for each in load() if each.key.name == "two"][0])
            self.assertEqual(defaults(), {"one": False, "two": True})
            delete.run([each.key for each in load() if each.key.name == "two"][0])
            self.assertEqual(list(defaults()), ["one"])
            # `a` on a row logs in to its plugin, asking the way and the name (taken as offered:
            # the site gives none, so the next free `accountN`).
            keys["a"].run(load()[0].key)
            self.assertEqual([each.key.name for each in load()], ["one", "account1"])

        self.listed(script, "auth", "list")

    def test_the_plugin_list_installs_and_uninstalls(self):
        source = Path(self.tmp.name) / "exa.py"
        source.write_text(
            "from flnd.plugin import Plugin, Post, route\n\n\nclass Example(Plugin):\n"
            "    code, name, api, domains = 'exa', 'Example', 2, ('exa.example',)\n\n"
            "    @route('exa:{user}')\n    async def user(self, ctx, user):\n"
            "        yield Post(user)\n", encoding="utf-8")

        def script(title, load, keys, delete):
            self.assertIn("ins", [each.key.code for each in load() if each.key.builtin])
            with mock.patch("flnd.ui.forms.text", return_value=str(source)):
                keys["a"].run(None)
            [added] = [each.key for each in load() if not each.key.builtin]
            self.assertEqual((added.code, added.api), ("exa", 2))
            delete.run(added)
            self.assertEqual([each.key for each in load() if not each.key.builtin], [])

        with mock.patch.dict(os.environ, {"FLND_PLUGINS": str(Path(self.tmp.name) / "plugins")}):
            self.listed(script, "plugin", "list")


class FormsTest(ExampleCase):
    """What a terminal is asked, in forms (`flnd.ui.forms`): status's repair, config, the login."""

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

    def test_config_edits_adds_and_resets_a_setting_in_forms(self):
        def script(title, load, keys, delete):
            self.assertEqual(title, "Settings")
            with mock.patch("flnd.ui.forms.text", return_value="30d"):
                keys["e"].run("defaults.when")
            with mock.patch("flnd.ui.forms.text", side_effect=["defaults.height", "720"]):
                keys["a"].run(None)
            with mock.patch("flnd.ui.forms.text", return_value="true"):
                keys["e"].run("defaults.sidecars")
            rows = {row.key: row.cells for row in load()}
            self.assertEqual(rows["defaults.when"], ("defaults.when", "30d", "-", "home"))
            self.assertEqual(rows["defaults.height"][1], "720")
            self.assertEqual(rows["defaults.sidecars"][1:], ("true", "false", "home"))
            delete.run("defaults.when")
            self.assertEqual({row.key: row.cells[3] for row in load()}["defaults.when"], "default")

        from flnd.ui import lists

        with mock.patch.object(lists, "show", lambda *a, **k: script(*a[:1], a[2], *[
                {key.char: key for key in k["keys"]}, k["delete"]])):
            self.assertEqual(self.run_app("config")[0], 0)
        self.assertIn("height = 720", Path(".flnd/config.toml").read_text(encoding="utf-8"))

    def test_the_login_form_words_each_way_and_asks_the_path_of_a_file(self):
        shown = []

        def select(title, options, **_):
            shown.append((title, [(option.label, option.note) for option in options]))
            return "file"

        with mock.patch("flnd.ui.forms.select", select), \
                mock.patch("flnd.ui.forms.text", return_value=str(self.cookie_file(NETSCAPE))):
            code, text, err = self.run_app("auth", "login", "twt")
        self.assertEqual(code, 0, err)
        title, options = shown[0]
        self.assertEqual(title, "Log in to Twitter/X")
        self.assertIn(("From a cookie file", "a cookies.txt you exported"), options)
        self.assertIn(("From your Chrome", "through the extension"), options)
        self.assertRegex(text, r"^twt: logged in as ")


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
        self.pipe("")
        self.assertEqual(self.run_app("get")[0], 1)
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

    def test_help_lists_the_commands_and_every_plugin_from_the_manifest(self):
        code, text, _ = self.run_app("--help")
        self.assertEqual(code, 0)
        self.assertIn("flnd 0.2.0 - download whole accounts from the web", text)
        for verb in ("get ", "target list|clean", "auth list|login|logout",
                     "plugin list|install|uninstall", "extension install|uninstall", "status",
                     "config"):
            self.assertIn(f"  {verb}", text)
        self.assertIn("tik fbk ins thr twt loi tlg mht", text)
        self.assertEqual(self.run_app()[1], text)

    def test_get_help_has_a_row_per_plugin_and_its_flags(self):
        code, text, _ = self.run_app("get", "--help")
        self.assertEqual(code, 0)
        flat = " ".join(text.split())
        self.assertIn("tlg:<@username, chat title or id>, tlg:@<chat>, a link on t.me", flat)
        self.assertIn("|posts,reels,tagged", flat)
        for flag in ("--when", "--limit", "--dry", "--again", "--home", "--json"):
            self.assertIn(flag, text)
        self.assertNotIn("Args:", text)

    def test_a_group_lists_its_commands(self):
        for group, names in (("target", ("list", "clean")), ("auth", ("list", "login", "logout")),
                             ("plugin", ("list", "install", "uninstall")),
                             ("extension", ("install", "uninstall"))):
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
        self.assertEqual(self.run_app("auth", "list")[1].split(),
                         ["plugin", "name", "default", "origin", "status", "saved"])
        code, text, _ = self.run_app("config")
        self.assertEqual(code, 0)
        self.assertEqual(text.splitlines()[0].split(), ["key", "value", "default", "from"])
        self.assertRegex(text, r"defaults\.when +- +- +default")
        rows = [json.loads(line) for line in self.run_app("--json", "config")[1].splitlines()]
        self.assertEqual(rows[0]["key"], "defaults.dedupe")
        self.assertFalse((Path(".flnd") / _store.DB).exists())

    def test_plugin_list_shows_each_plugin_and_why_one_is_disabled(self):
        code, text, _ = self.run_app("plugin", "list")
        self.assertEqual(code, 0)
        self.assertRegex(text, r"ins +Instagram +built-in +2 ")
        rows = [json.loads(line) for line in self.run_app("--json", "plugin", "list")[1]
                .splitlines()]
        self.assertEqual([row["code"] for row in rows],
                         ["tik", "fbk", "ins", "thr", "twt", "loi", "tlg", "mht", "web"])

    def test_plugin_install_and_uninstall(self):
        source = Path(self.tmp.name) / "exa.py"
        source.write_text(
            "from flnd.plugin import Plugin, Post, route\n\n\nclass Example(Plugin):\n"
            "    code, name, api, domains = 'exa', 'Example', 2, ('exa.example',)\n\n"
            "    @route('exa:{user}')\n    async def user(self, ctx, user):\n"
            "        yield Post(user)\n", encoding="utf-8")
        folder = Path(self.tmp.name) / "plugins"
        with mock.patch.dict(os.environ, {"FLND_PLUGINS": str(folder)}):
            code, text, err = self.run_app("plugin", "install", str(source))
            self.assertEqual((code, text), (0, "exa: installed (Example)\n"), err)
            self.assertRegex(self.run_app("plugin", "list")[1], r"exa +Example +installed +2 ")
            self.sure = False
            self.assertEqual(self.run_app("plugin", "uninstall", "exa")[1],
                             "kept: nothing was uninstalled\n")
            self.assertEqual(self.asked, ["Uninstall exa?"])
            self.sure = True
            self.assertEqual(self.run_app("plugin", "uninstall", "exa")[1], "exa: uninstalled\n")
            self.assertEqual(self.run_app("plugin", "uninstall", "ins")[0], 2)
            self.assertEqual(self.run_app("plugin", "install", "missing.py")[0], 2)


class AuthTest(FlndCase):
    def test_login_by_file_keeps_only_the_plugins_cookies(self):
        code, text, err = self.run_app("auth", "login", "ins", "file",
                                       str(self.cookie_file(NETSCAPE)))
        self.assertEqual(code, 0, err)
        self.assertRegex(text, r"^ins: logged in as \w+ \(file\)\n$")
        self.assertEqual({cookie["name"]: cookie["domain"]
                          for cookie in vaulted(Path(".flnd"), "ins")[0].list},
                         {"sessionid": ".instagram.com", "ds_user_id": ".www.instagram.com"})
        # A path alone means a file; nothing but the two stores is written.
        self.assertEqual(self.run_app("auth", "login", "twt", str(self.cookie_file(NETSCAPE)))[0],
                         0)
        self.assertEqual(sorted(path.name for path in Path(".flnd").rglob("*") if path.is_file()
                                and path.suffix == ".db"), ["flnd.db", "secret.db"])

    def test_list_default_and_logout(self):
        import time

        sign_in(".flnd", "ins", [{"name": "sessionid", "value": "secret-val",
                                  "domain": ".instagram.com", "expires": time.time() - 10}],
                name="one")
        sign_in(".flnd", "ins", [{"name": "a", "value": "1", "domain": ".x.example"}], name="two")
        code, text, _ = self.run_app("auth", "list")
        self.assertEqual(code, 0)
        self.assertRegex(text, r"ins +one +. default +file +ok")
        self.assertRegex(text, r"ins +two +file +ok")
        rows = [json.loads(line) for line in self.run_app("--json", "auth", "list")[1]
                .splitlines()]
        self.assertEqual([row["name"] for row in rows], ["one", "two"])
        self.assertNotIn("secret-val", text)
        self.assertEqual(self.run_app("auth", "logout", "ins", "two")[1],
                         "ins: two logged out\n")
        self.assertEqual(self.run_app("auth", "logout", "ins")[1], "ins: one logged out\n")
        self.assertEqual(vaulted(".flnd", "ins")[0].list, [])
        for argv in (("ins",), ("ytb",), ("ins", "nobody")):
            with self.subTest(argv=argv):
                self.assertEqual(self.run_app("auth", "logout", *argv)[0], 2)

    def test_a_logout_asks_a_terminal_and_a_declined_one_keeps_the_account(self):
        sign_in(".flnd", "ins", [{"name": "a", "value": "1", "domain": ".x.example"}], name="one")
        self.sure = False
        self.assertEqual(self.run_app("auth", "logout", "ins")[1], "kept: nobody was logged out\n")
        self.assertEqual(self.asked, ["Log the default account out of ins?"])
        self.assertIn("one", self.run_app("auth", "list")[1])
        self.pipe("")
        self.assertEqual(self.run_app("auth", "logout", "ins")[1], "ins: one logged out\n")

    def test_a_missing_cookie_exits_3_and_a_wrong_way_or_plugin_exits_2(self):
        code, _, err = self.run_app("auth", "login", "fbk", "file",
                                    str(self.cookie_file(NETSCAPE)))
        self.assertEqual(code, 3)
        self.assertIn("fbk: no c_user or xs for facebook.com in cookies.txt", err)
        self.assertNotIn("Traceback", err)
        self.assertEqual(self.run_app("auth", "login", "ins", "netscape", "x")[0], 2)
        self.assertEqual(self.run_app("auth", "login", "ytb", "chrome")[0], 2)

    def test_the_way_is_asked_on_a_terminal_and_never_on_a_pipe_or_with_json(self):
        path = self.cookie_file(NETSCAPE)
        ways = list(PLUGINS["twt"].login.methods)
        self.answer(str(ways.index("file") + 1), str(path))
        code, text, err = self.run_app("auth", "login", "twt")
        self.assertEqual(code, 0, err)
        self.assertRegex(text, r"^twt: logged in as \w+ \(file\)\n$")
        self.pipe("")
        code, _, err = self.run_app("auth", "login", "ins")
        self.assertEqual(code, 2)
        self.assertIn("flnd auth login ins <method>", err)
        self.answer("1")
        self.assertEqual(self.run_app("auth", "login", "ins", "--json")[0], 2)

    def test_a_plugin_with_its_own_login_offers_its_own_ways(self):
        calls = []

        def start(self, ctx, method):
            calls.append(method)
            ctx.secrets["session"] = "s"

        class Own(Plugin):
            code, name, domains, api = "own", "Own", ("own.example",), 2
            login = own_login(start, methods=("qr", "phone"))

            @route("own:{user}", single=True)
            async def user(self, ctx, user):
                yield Post(user)

        self.answer("2")
        with mock.patch.dict(PLUGINS, {"own": Own}), \
                mock.patch.dict("flnd.plugins._manifest.PLUGINS", {"own": {
                    "name": "Own", "domains": ("own.example",), "forms": (), "categories": (),
                    "logins": ("qr", "phone")}}):
            code, _, err = self.run_app("auth", "login", "own")
        self.assertEqual(code, 0, err)
        self.assertEqual(calls, ["phone"])


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
